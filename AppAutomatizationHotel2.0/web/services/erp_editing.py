"""
Corrections the accountant makes to the ERP (Bordero) from the Pendientes inbox.

Every operation runs as ONE transaction that:
  1. changes the ERP row (edit, add by hand, or soft delete),
  2. writes the change to ErpTransactionChange (old value -> new value, who, when, why, from where),
  3. crosses again the voucher being worked on, plus any other voucher linked to the same row,
     and moves each case to "Corregido en ERP" when it now matches, or back to Pendiente when it does not.
"""

from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time

from ..models import (
    AuditResolutionLog,
    CardType,
    CrossMatchResult,
    ErpTransaction,
    ErpTransactionChange,
    ReconciliationStatus,
)
from .erp_corrections import CORRECTABLE_FIELDS
from .voucher_recross import NOT_LINKED_RESULTS, fmt_clp, recross_voucher

FIELD_LABELS = {
    "amount": "Monto",
    "authorization_code": "Código de autorización",
    "transaction_date": "Fecha",
    "payment_code": "Medio de pago",
    "document_number": "Documento",
    "transaction_time": "Hora",
    "invoice_number": "Factura",
    "account_code": "Cuenta",
    "reservation_number": "Reserva",
    "room_number": "Habitación",
    "room_type": "Tipo de habitación",
    "guest_name": "Huésped",
    "cashier_username": "Cajero",
}
# Optional text fields of the Bordero row: max length; an empty value clears them
OPTIONAL_TEXT_FIELDS = {
    "invoice_number": 50,
    "account_code": 50,
    "reservation_number": 50,
    "room_number": 20,
    "room_type": 20,
    "guest_name": 150,
    "cashier_username": 50,
}
CLEARABLE_FIELDS = (*OPTIONAL_TEXT_FIELDS, "transaction_time")
RESULT_LABELS = {
    "MATCHED": "Cuadrado",
    "AMOUNT_MISMATCH": "Diferencia de monto",
    "CARD_TYPE_MISMATCH": "Medio de pago distinto",
    "NOT_FOUND_IN_ERP": "No está en el ERP",
    "OUT_OF_PERIOD": "Fuera de período",
}
# Fallback payment codes when the database has no rows of a currency yet: code -> card type code
DEFAULT_PAYMENT_CODES = {
    "CLP": {"RCO": None, "V$": "VI", "MC$": "MC", "AME$": "AX", "DIN$": "DI"},
    "USD": {"VUS$": "VI", "MCUS$": "MC", "AUS$": "AX", "DUS$": "DI"},
}
OPEN_STATUS_CODES = ("PENDING", "REJECTED")
FIXED_STATUS_CODE = "RESOLVED_ERP_FIXED"


class CorrectionError(ValueError):
    pass


# ------------------------------------------------------------------------------
# Payment codes
# ------------------------------------------------------------------------------

def payment_code_choices(currency: str) -> List[Tuple[str, str, Optional[CardType]]]:
    """(code, label, card type) of every payment code seen in the Bordero of the currency, most used first."""
    card_types = {c.id: c for c in CardType.objects.all()}
    card_by_code = {c.code: c for c in card_types.values()}
    seen: Dict[str, Optional[CardType]] = {}
    rows = (
        ErpTransaction.objects.filter(source_currency=currency).exclude(payment_code__isnull=True).exclude(payment_code="")
        .values("payment_code", "card_type_id").annotate(n=Count("id")).order_by("-n")
    )
    for row in rows:
        seen.setdefault(row["payment_code"], card_types.get(row["card_type_id"]))
    for code, card_code in DEFAULT_PAYMENT_CODES.get(currency, {}).items():
        seen.setdefault(code, card_by_code.get(card_code))

    def _label(code, card):
        if code == "RCO":
            return "RCO · Red Compra (débito / prepago)"
        return f"{code} · {card.name}" if card else code

    return [(code, _label(code, card), card) for code, card in seen.items()]


def _card_type_for(currency: str, payment_code: str) -> Tuple[bool, Optional[CardType]]:
    for code, _, card in payment_code_choices(currency):
        if code == payment_code:
            return True, card
    return False, None


def default_payment_code(match: CrossMatchResult) -> str:
    """Payment code a hand-added row of this voucher should carry."""
    currency = match.process.process_currency
    tbk = match.transbank_transaction
    if currency == "CLP" and tbk and tbk.payment_modality in ("DEBIT", "PREPAID"):
        return "RCO"
    for code, _, card in payment_code_choices(currency):
        if code != "RCO" and card and card.id == match.card_type_id:
            return code
    return ""


# ------------------------------------------------------------------------------
# Parsing the form values
# ------------------------------------------------------------------------------

def _parse_amount(raw: Any) -> Decimal:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        raise CorrectionError("Indica el monto.")
    if "," in text:  # 115.204,50 -> 115204.50
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise CorrectionError(f"El monto “{raw}” no es un número válido.")


def _parse_value(field_name: str, raw: Any, currency: str):
    if field_name == "amount":
        return _parse_amount(raw)
    if field_name == "transaction_date":
        if isinstance(raw, date):
            return raw
        parsed = parse_date(str(raw or "").strip())
        if parsed is None:
            raise CorrectionError("Indica una fecha válida.")
        return parsed
    if field_name == "transaction_time":
        if isinstance(raw, time):
            return raw
        text = str(raw or "").strip()
        if not text:
            return None
        parsed = parse_time(text)
        if parsed is None:
            raise CorrectionError("Indica una hora válida (HH:MM).")
        return parsed
    text = str(raw or "").strip()
    if field_name in OPTIONAL_TEXT_FIELDS:
        if field_name in ("cashier_username", "guest_name"):
            text = " ".join(text.upper().split())  # the Bordero writes users and designations in capitals
        return text[:OPTIONAL_TEXT_FIELDS[field_name]] or None
    if field_name == "authorization_code":
        return text.upper()
    if field_name == "document_number":
        if not text:
            raise CorrectionError("El documento no puede quedar vacío.")
        return text[:50]
    if field_name == "payment_code":
        known, _ = _card_type_for(currency, text.upper())
        if not known:
            raise CorrectionError(f"El medio de pago “{text}” no existe en el Borderó.")
        return text.upper()
    raise CorrectionError(f"El campo {field_name} no se puede corregir.")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value)


def display_value(field_name: str, value: Any, currency: str) -> str:
    if value in (None, ""):
        return "—"
    if field_name == "amount":
        return fmt_clp(value)  # Bordero amounts are pesos in both flows (USD rows carry the CLP charge)
    if field_name == "transaction_date":
        parsed = value if isinstance(value, date) else parse_date(str(value))
        return parsed.strftime("%d/%m/%Y") if parsed else str(value)
    if field_name == "transaction_time":
        parsed = value if isinstance(value, time) else parse_time(str(value))
        return parsed.strftime("%H:%M") if parsed else str(value)
    return str(value)


# ------------------------------------------------------------------------------
# Re-cross and case status
# ------------------------------------------------------------------------------

def _affected_matches(row: ErpTransaction, match: Optional[CrossMatchResult]) -> List[CrossMatchResult]:
    """The case being worked on first, then any other case that was crossed with this row."""
    others = list(
        CrossMatchResult.objects.filter(erp_transaction_id=row.pk)
        .exclude(pk=match.pk if match else None)
        .exclude(system_result__in=NOT_LINKED_RESULTS)
        .select_related("process", "transbank_transaction", "card_type", "management_status")
    )
    return ([match] if match else []) + others


def _settle_case(match: CrossMatchResult, user, summary: str, previous_result: Optional[str] = None) -> Dict[str, Any]:
    """Crosses the voucher again and moves the case to Corregido en ERP / Pendiente, with its log."""
    outcome = recross_voucher(match)
    if previous_result:
        outcome["previous_result"] = previous_result  # result before a step-2 correction already moved it
    previous_status = match.management_status
    matched = outcome["system_result"] == "MATCHED"
    new_status = ReconciliationStatus.objects.get(code=FIXED_STATUS_CODE if matched else "PENDING")
    match.management_status = new_status
    match.resolved_by = user if matched else None
    match.resolution_date = timezone.now() if matched else None
    match.save()

    comment = (
        f"{summary}. Cruce: {RESULT_LABELS.get(outcome['previous_result'], outcome['previous_result'])} → "
        f"{RESULT_LABELS.get(outcome['system_result'], outcome['system_result'])}"
    )
    if not matched and outcome["system_result"] != "NOT_FOUND_IN_ERP":
        comment += f". Aún hay diferencia de {outcome['remaining']}"
    AuditResolutionLog.objects.create(
        cross_match_result=match, user=user, previous_status=previous_status, new_status=new_status, comment=comment,
    )
    return {"match": match, "matched": matched, **outcome}


def _settle_all(
    row: ErpTransaction, match: Optional[CrossMatchResult], user, summary: str, previous_result: Optional[str] = None
) -> Dict[str, Any]:
    results = [
        _settle_case(m, user, summary, previous_result if m is match else None) for m in _affected_matches(row, match)
    ]
    return results[0] if results else {"match": None, "matched": False}


def _record(row, user, field_name, old, new, source, match, reason):
    return ErpTransactionChange(
        erp_transaction=row, field_name=field_name, old_value=_as_text(old), new_value=_as_text(new),
        user=user, source=source, cross_match_result=match, reason=(reason or "").strip() or None,
    )


# ------------------------------------------------------------------------------
# Operations
# ------------------------------------------------------------------------------

@transaction.atomic
def edit_row(
    row: ErpTransaction,
    values: Dict[str, Any],
    user,
    match: Optional[CrossMatchResult] = None,
    source: str = ErpTransactionChange.SOURCE_MANUAL,
    reason: str = "",
    previous_result: Optional[str] = None,
) -> Dict[str, Any]:
    """Opción B "la fila existe" (and Opción A "Aceptar"): changes the given fields of a Bordero row."""
    if row.is_deleted:
        raise CorrectionError("Esta fila del ERP está eliminada; no se puede corregir.")
    currency = row.source_currency or (match.process.process_currency if match else "CLP")
    changes = []
    for field_name, raw in values.items():
        if field_name not in CORRECTABLE_FIELDS:
            raise CorrectionError(f"El campo {field_name} no se puede corregir.")
        if raw is None:
            continue
        if isinstance(raw, str) and not raw.strip() and field_name not in CLEARABLE_FIELDS:
            continue  # blank required field: keep the current value
        new_value = _parse_value(field_name, raw, currency)
        old_value = getattr(row, field_name)
        if _as_text(old_value) == _as_text(new_value):
            continue
        changes.append((field_name, old_value, new_value))
    if not changes:
        raise CorrectionError("No cambiaste ningún dato de la fila.")

    history = []
    changed_fields = {f for f, _, _ in changes}
    for field_name, old_value, new_value in changes:
        setattr(row, field_name, new_value)
        if field_name == "document_number" and row.account_code == old_value and "account_code" not in changed_fields:
            row.account_code = new_value  # in the CLP Bordero the account is the document
        if field_name == "payment_code":
            _, row.card_type = _card_type_for(currency, new_value)
        history.append(_record(row, user, field_name, old_value, new_value, source, match, reason))
    row.save()
    ErpTransactionChange.objects.bulk_create(history)

    summary = "Corrección en ERP" if source == ErpTransactionChange.SOURCE_MANUAL else "Sugerencia aceptada"
    summary += ": " + "; ".join(
        f"{FIELD_LABELS[f]} {display_value(f, o, currency)} → {display_value(f, n, currency)}" for f, o, n in changes
    )
    return _settle_all(row, match, user, summary, previous_result)


@transaction.atomic
def add_row(match: CrossMatchResult, values: Dict[str, Any], user, reason: str = "") -> Dict[str, Any]:
    """Opción B "la fila no existe": adds a Bordero row by hand, prefilled from the Transbank voucher."""
    currency = match.process.process_currency
    # The Bordero shows payments as negative values (-15572): accept it as written or as a positive amount
    amount = abs(_parse_value("amount", values.get("amount"), currency))
    if amount == 0:
        raise CorrectionError("El valor de la fila no puede ser cero.")
    code = _parse_value("authorization_code", values.get("authorization_code"), currency)
    if not code:
        raise CorrectionError("Indica el Documento (código de autorización del voucher).")
    # Bordero "Cuenta" is what the uploads store as the row's document number; it is optional
    raw_account = str(values.get("account_code") or values.get("document_number") or "").strip()
    document = _parse_value("document_number", raw_account, currency) if raw_account else ""
    tx_date = _parse_value("transaction_date", values.get("transaction_date"), currency)
    payment_code = _parse_value("payment_code", values.get("payment_code"), currency)
    _, card_type = _card_type_for(currency, payment_code)
    optional = {f: _parse_value(f, values.get(f), currency) for f in CLEARABLE_FIELDS}
    if not optional["room_number"] and match.room_number and match.room_number != "—":
        optional["room_number"] = match.room_number

    row = ErpTransaction.objects.create(
        process=match.process,
        card_type=card_type,
        account_code=document or None,
        document_number=document,
        invoice_number=optional["invoice_number"] or "",
        payment_code=payment_code,
        authorization_code=code,
        transaction_date=tx_date,
        transaction_time=optional["transaction_time"],
        cashier_username=optional["cashier_username"],
        guest_name=optional["guest_name"],
        reservation_number=optional["reservation_number"],
        room_type=optional["room_type"],
        room_number=optional["room_number"],
        amount=amount,
        source_currency=currency,
        origin=ErpTransaction.ORIGIN_MANUAL,
        source_key=None,
    )
    ErpTransactionChange.objects.create(
        erp_transaction=row, field_name=ErpTransactionChange.FIELD_CREATED, old_value="",
        new_value=f"Cta {document or '—'} · Doc {code} · {display_value('amount', amount, currency)} · {tx_date:%d/%m/%Y} · {payment_code}",
        user=user, source=ErpTransactionChange.SOURCE_MANUAL, cross_match_result=match, reason=(reason or "").strip() or None,
    )
    summary = f"Fila agregada a mano en ERP: Cuenta {document or '—'}, Documento {code}, {display_value('amount', amount, currency)}"
    return _settle_all(row, match, user, summary)


@transaction.atomic
def soft_delete_row(row: ErpTransaction, user, reason: str, match: Optional[CrossMatchResult] = None) -> Dict[str, Any]:
    """Opción B "la fila sobra": logical delete with a mandatory reason. Nothing is removed from the database."""
    reason = (reason or "").strip()
    if not reason:
        raise CorrectionError("Indica por qué se elimina la fila.")
    if row.is_deleted:
        raise CorrectionError("Esta fila ya estaba eliminada.")
    row.is_deleted = True
    row.deleted_at = timezone.now()
    row.deleted_by = user
    row.reconciled = False
    row.save(update_fields=["is_deleted", "deleted_at", "deleted_by", "reconciled"])
    ErpTransactionChange.objects.create(
        erp_transaction=row, field_name=ErpTransactionChange.FIELD_DELETED, old_value="activa", new_value="eliminada",
        user=user, source=ErpTransactionChange.SOURCE_MANUAL, cross_match_result=match, reason=reason,
    )
    currency = row.source_currency or "CLP"
    summary = f"Fila eliminada del ERP (Doc {row.document_number}, {display_value('amount', row.amount, currency)}): {reason}"
    return _settle_all(row, match, user, summary)


def candidate_rows(match: CrossMatchResult, limit: int = 4) -> List[ErpTransaction]:
    """
    ERP rows the accountant may want to fix for this case: the row it was crossed with, the row the
    suggestion points to, then rows of the same voucher code. Only active rows.
    """
    from .column_detector import normalize_auth_code
    from .voucher_recross import _rows_with_code

    currency = match.process.process_currency
    seen: Dict[int, ErpTransaction] = {}
    if match.erp_transaction_id:
        linked = ErpTransaction.objects.filter(pk=match.erp_transaction_id).select_related("card_type").first()
        if linked:
            seen[linked.pk] = linked
    proposed = (getattr(match, "proposed_change", None) or {}).get("erp_id")
    if proposed and proposed not in seen:
        row = ErpTransaction.objects.filter(pk=proposed).select_related("card_type").first()
        if row:
            seen[row.pk] = row
    for row in _rows_with_code(currency, normalize_auth_code(match.authorization_code or "")):
        if len(seen) >= limit:
            break
        seen.setdefault(row.pk, row)
    return list(seen.values())[:limit]
