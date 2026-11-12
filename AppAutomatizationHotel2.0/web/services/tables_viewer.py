"""
Read-only visualization data preparation helpers for the 6 table panels:
  - In-memory preview source tables (Borderó ERP, Cartola Bancaria, Posición por Clientes, Transbank Detallado)
  - Saved ReconciliationProcess source querysets and labels across deduplicated re-uploads
  - Read-only Audit Trail formatting (ErpTransactionChange & AuditResolutionLog)
Never exposes guest_name or room_number in rendered payloads.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.utils import timezone

from ..models import (
    BankDepositValidation,
    BankStatementMovement,
    CardOperatorTransaction,
    CardType,
    CrossMatchResult,
    ErpTransaction,
    ErpTransactionChange,
    ReconciliationProcess,
    ReconciliationStatus,
    TransbankTransaction,
)
from .column_detector import safe_date


MODALITY_LABELS: Dict[str, str] = {
    "CREDIT": "Crédito",
    "DEBIT": "Débito",
    "PREPAID": "Prepago",
    "CLP_CREDIT": "Crédito",
    "CLP_DEBIT": "Débito",
    "CLP_PREPAID": "Prepago",
    "USD_GENERAL": "Dólares (General)",
    "INTERNATIONAL_USD": "Internacional USD",
}

RESULT_META: Dict[str, Tuple[str, str]] = {
    "MATCHED": ("Conciliado", "status-success"),
    "AMOUNT_MISMATCH": ("Dif. de monto", "status-warning"),
    "CARD_TYPE_MISMATCH": ("Medio distinto", "status-warning"),
    "NOT_FOUND_IN_ERP": ("No está en ERP", "status-danger"),
    "OUT_OF_PERIOD": ("Fuera de período", "status-neutral"),
}

ERP_CHANGE_FIELD_LABELS: Dict[str, str] = {
    "authorization_code": "Cód. Autorización",
    "amount": "Monto",
    "account_code": "Cuenta",
    "document_number": "Documento",
    "reservation_number": "N° Reserva",
    "room_type": "Tipo",
    "invoice_number": "Factura",
    "transaction_date": "Fecha",
    "cashier_username": "Cajero",
    "payment_code": "Cód. Pago",
    ErpTransactionChange.FIELD_CREATED: "Fila agregada",
    ErpTransactionChange.FIELD_DELETED: "Fila eliminada",
}


def result_label_and_tone(code: str) -> Tuple[str, str]:
    """Returns (human label, status-chip CSS class) for a CrossMatchResult.system_result."""
    return RESULT_META.get(code or "", ((code or "—").replace("_", " ").title(), "status-neutral"))


def parse_iso_date(raw: Optional[str]) -> Optional[date]:
    """Parses a YYYY-MM-DD GET query parameter safely, returning None when invalid/empty."""
    val = (raw or "").strip()
    if not val:
        return None
    try:
        return datetime.strptime(val, "%Y-%m-%d").date()
    except ValueError:
        return None


def _format_erp_field_value(field_name: str, raw_val: Any) -> str:
    """Formats an ERP field value consistently across tables, audit log and history modal."""
    if raw_val is None:
        return "—"
    val = str(raw_val).strip()
    if not val:
        return "—"
    if field_name == "amount":
        try:
            num = Decimal(val)
            s = f"{round(abs(num)):,}".replace(",", ".")
            return f"{'-' if num < 0 else ''}${s}"
        except (InvalidOperation, ValueError):
            return val
    if field_name == "transaction_date":
        parsed = safe_date(val)
        return parsed.strftime("%d/%m/%Y") if parsed else val
    return val


def _build_erp_history_entry(
    *,
    key: str,
    currency: str,
    process_id: Optional[int],
    account: str,
    document: str,
    reservation_number: str = "—",
    room_type: str = "—",
    room_number: Optional[str] = None,
    guest_name: Optional[str] = None,
    auth_code: str,
    invoice_number: str,
    payment_code: str,
    tx_date: str,
    cashier: str,
    amount_display: str,
    raw_changes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Builds a modal-ready before/after comparison and change timeline for one modified ERP row."""
    first_old: Dict[str, str] = {}
    latest_new: Dict[str, str] = {}
    timeline: List[Dict[str, Any]] = []

    for ch in raw_changes:
        fname = ch["field_name"]
        old_raw = "" if ch.get("old_value") is None else str(ch.get("old_value"))
        new_raw = "" if ch.get("new_value") is None else str(ch.get("new_value"))
        if fname not in (ErpTransactionChange.FIELD_CREATED, ErpTransactionChange.FIELD_DELETED):
            if fname not in first_old:
                first_old[fname] = old_raw
            latest_new[fname] = new_raw
        timeline.append({
            "field_code": fname,
            "field_label": ERP_CHANGE_FIELD_LABELS.get(fname, fname),
            "before": _format_erp_field_value(fname, old_raw),
            "after": _format_erp_field_value(fname, new_raw),
            "created_at": ch.get("created_at") or "—",
            "username": ch.get("username") or "—",
            "source": ch.get("source") or "",
            "source_label": ch.get("source_label") or "—",
            "reason": (ch.get("reason") or "").strip() or "—",
        })

    diff_fields = [
        {
            "field_code": fname,
            "field_label": ERP_CHANGE_FIELD_LABELS.get(fname, fname),
            "before": _format_erp_field_value(fname, first_old[fname]),
            "after": _format_erp_field_value(fname, latest_new[fname]),
        }
        for fname in first_old
    ]

    summary = {
        "account": account or "—",
        "document": document or "—",
        "reservation_number": reservation_number or "—",
        "room_type": room_type or "—",
        "auth_code": auth_code or "—",
        "invoice_number": invoice_number or "—",
        "payment_code": payment_code or "—",
        "tx_date": tx_date or "—",
        "cashier": cashier or "—",
        "amount": amount_display or "—",
    }
    if room_number is not None:
        summary["room_number"] = room_number or "—"
    if guest_name is not None:
        summary["guest_name"] = guest_name or "—"
    before_summary = dict(summary)
    field_to_summary_key = {
        "account_code": "account",
        "document_number": "document",
        "reservation_number": "reservation_number",
        "room_type": "room_type",
        "authorization_code": "auth_code",
        "invoice_number": "invoice_number",
        "payment_code": "payment_code",
        "transaction_date": "tx_date",
        "cashier_username": "cashier",
        "amount": "amount",
    }
    if room_number is not None:
        field_to_summary_key["room_number"] = "room_number"
    if guest_name is not None:
        field_to_summary_key["guest_name"] = "guest_name"
    for fname, old_raw in first_old.items():
        skey = field_to_summary_key.get(fname)
        if skey:
            before_summary[skey] = _format_erp_field_value(fname, old_raw)
    if (
        currency == "CLP"
        and "document_number" in first_old
        and "account_code" not in first_old
        and summary["account"] == summary["document"]
    ):
        before_summary["account"] = before_summary["document"]

    timeline.reverse()
    ident = auth_code if auth_code and auth_code != "—" else (document if document and document != "—" else key)
    return {
        "key": key,
        "title": f"Fila Borderó ERP · {ident}",
        "subtitle": f"Cierre #{process_id} · {currency}" if process_id else f"Vista previa en memoria · {currency}",
        "currency": currency,
        "process_id": process_id,
        "before_summary": before_summary,
        "current_summary": summary,
        "diff_fields": diff_fields,
        "changes": timeline,
        "changes_count": len(timeline),
    }


def _change_models_to_raw_list(changes: List[ErpTransactionChange]) -> List[Dict[str, Any]]:
    raw_list = []
    for c in changes:
        raw_list.append({
            "field_name": c.field_name,
            "old_value": c.old_value,
            "new_value": c.new_value,
            "created_at": timezone.localtime(c.created_at).strftime("%d/%m/%Y %H:%M") if c.created_at else "—",
            "username": (c.user.get_full_name() or c.user.username) if c.user_id else "—",
            "source": c.source,
            "source_label": c.get_source_display(),
            "reason": c.reason or "",
        })
    return raw_list


def _fetch_erp_history_map_for_rows(
    erp_rows_by_id: Dict[int, ErpTransaction],
    *,
    include_guest_and_room: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Loads ErpTransactionChange history in one query and builds modal entries keyed by str(erp_id)."""
    if not erp_rows_by_id:
        return {}
    grouped: Dict[int, List[ErpTransactionChange]] = {}
    for ch in (
        ErpTransactionChange.objects.filter(erp_transaction_id__in=erp_rows_by_id.keys())
        .exclude(field_name=ErpTransactionChange.FIELD_CREATED)
        .select_related("user")
        .order_by("created_at", "id")
    ):
        grouped.setdefault(ch.erp_transaction_id, []).append(ch)

    history_map: Dict[str, Dict[str, Any]] = {}
    for erp_id, ch_list in grouped.items():
        row = erp_rows_by_id[erp_id]
        currency = (row.source_currency or (row.process.process_currency if row.process_id else "") or "CLP").upper()
        extra_kwargs = (
            {
                "room_number": (row.room_number or "").strip() or "—",
                "guest_name": (row.guest_name or "").strip() or "—",
            }
            if include_guest_and_room
            else {}
        )
        history_map[str(erp_id)] = _build_erp_history_entry(
            key=str(erp_id),
            currency=currency,
            process_id=row.process_id,
            account=(row.account_code or row.document_number or "").strip() or "—",
            document=(row.document_number or row.authorization_code or "").strip() or "—",
            reservation_number=(row.reservation_number or "").strip() or "—",
            room_type=(row.room_type or "").strip() or "—",
            **extra_kwargs,
            auth_code=(row.authorization_code or "").strip() or "—",
            invoice_number=(row.invoice_number or "").strip() or "—",
            payment_code=(row.payment_code or "").strip() or "—",
            tx_date=row.transaction_date.strftime("%d/%m/%Y") if row.transaction_date else "—",
            cashier=(row.cashier_username or "").strip() or "—",
            amount_display=_format_erp_field_value("amount", row.amount),
            raw_changes=_change_models_to_raw_list(ch_list),
        )
    return history_map


def _build_preview_erp_status_maps(
    preview: Dict[str, Any], currency: str
) -> Tuple[Dict[int, Tuple[str, str]], set]:
    """
    Maps each preview erp_transactions index -> (status_code, status_label) by inspecting
    matched, discrepancy, and out-of-period voucher buckets.
    """
    status_by_idx: Dict[int, Tuple[str, str]] = {}
    matched_auths = set()
    diff_auths = set()

    def _register_bucket(bucket_key: str, status_code: str, status_label: str) -> None:
        for v in preview.get(bucket_key, []):
            auth = (v.get("erp_authorization_code") or v.get("authorization_code") or "").strip()
            if auth:
                (matched_auths if status_code == "matched" else diff_auths).add(auth)
            indices = list(v.get("erp_indices") or [])
            if v.get("erp_index") is not None and v["erp_index"] not in indices:
                indices.append(v["erp_index"])
            for idx in indices:
                # Matched takes precedence if an index somehow appears more than once
                if idx not in status_by_idx or status_code == "matched":
                    status_by_idx[idx] = (status_code, status_label)

    _register_bucket("matched_vouchers", "matched", "Conciliado")
    _register_bucket("discrepancy_vouchers", "diff", "Con diferencia")
    _register_bucket("out_of_period_vouchers", "diff", "Fuera de período")

    if currency == "USD":
        return status_by_idx, matched_auths | diff_auths
    return status_by_idx, set()


def _sanitize_preview_erp_row(
    idx: int,
    raw: Dict[str, Any],
    currency: str,
    status_by_idx: Dict[int, Tuple[str, str]],
    usd_linked_auths: set,
    history_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Builds a read-only Borderó ERP row without exposing guest_name or room_number."""
    account = (raw.get("account_code") or raw.get("document_number") or "").strip()
    auth = (raw.get("authorization_code") or "").strip()
    if currency == "CLP":
        document = auth or (raw.get("document_number") or "").strip()
    else:
        # In USD Borderó, Col. J ('Documento') holds the authorization/document code
        document = auth or (raw.get("document_number") or "").strip()

    if idx in status_by_idx:
        match_status, match_label = status_by_idx[idx]
    elif raw.get("reconciled") and (currency != "USD" or auth in usd_linked_auths or not usd_linked_auths):
        match_status, match_label = ("matched", "Conciliado")
    elif raw.get("reconciled"):
        match_status, match_label = ("diff", "Vinculado")
    else:
        match_status, match_label = ("unlinked", "Solo en ERP (sin TBK)")

    if raw.get("manual_id"):
        origin_label = "Agregada a mano"
    elif raw.get("is_auxiliary"):
        origin_label = "Borderó adicional"
    else:
        origin_label = "Excel (Borderó)"

    return {
        "index": idx + 1,
        "account_code": account or "—",
        "document_number": document or "—",
        "reservation_number": (raw.get("reservation_number") or "").strip() or "—",
        "room_type": (raw.get("room_type") or "").strip() or "—",
        "authorization_code": auth or "—",
        "invoice_number": (raw.get("invoice_number") or "").strip() or "—",
        "payment_code": (raw.get("payment_code") or "").strip() or "—",
        "card_code": (raw.get("card_code") or "").strip() or "—",
        "transaction_date": raw.get("transaction_date") or "—",
        "transaction_time": raw.get("transaction_time") or "",
        "cashier_username": (raw.get("cashier_username") or "").strip() or "—",
        "amount": float(raw.get("amount") or 0),
        "source_currency": (raw.get("source_currency") or currency or "").upper() or "—",
        "origin_label": origin_label,
        "is_corrected": bool(raw.get("is_corrected")),
        "is_modified": bool(history_entry),
        "modifications_count": history_entry["changes_count"] if history_entry else 0,
        "history": history_entry,
        "match_status": match_status,
        "match_label": match_label,
    }


def _serialize_preview_bank_row(idx: int, raw: Dict[str, Any]) -> Dict[str, Any]:
    is_matched = bool(raw.get("is_matched_target"))
    is_tbk = bool(raw.get("is_transbank_deposit"))
    if is_matched:
        status_code, status_label = ("matched", "Abono cuadrado")
    elif is_tbk:
        status_code, status_label = ("tbk", "Movimiento TBK")
    else:
        status_code, status_label = ("other", "Otro movimiento")

    return {
        "index": idx + 1,
        "movement_date": raw.get("movement_date") or "—",
        "description": raw.get("description") or "—",
        "branch_or_channel": raw.get("branch_or_channel") or "—",
        "document_number": raw.get("document_number") or "—",
        "charge_amount": float(raw.get("charge_amount") or 0),
        "deposit_amount": float(raw.get("deposit_amount") or 0),
        "balance_amount": float(raw.get("balance_amount") or 0),
        "status_code": status_code,
        "status_label": status_label,
    }


def _serialize_preview_operator_row(idx: int, raw: Dict[str, Any]) -> Dict[str, Any]:
    auth = (raw.get("authorization_code") or "").strip()
    is_used = bool(auth)
    return {
        "index": idx + 1,
        "card_code": (raw.get("card_code") or "").strip() or "—",
        "document_number": (raw.get("document_number") or "").strip() or "—",
        "sequence_number": (raw.get("sequence_number") or "").strip() or "—",
        "authorization_code": auth or "—",
        "foreign_currency_amount": float(raw.get("foreign_currency_amount") or 0),
        "balance_amount": float(raw.get("balance_amount") or 0),
        "corrected_balance_amount": float(raw.get("corrected_balance_amount") or 0),
        "status_code": "used" if is_used else "unused",
        "status_label": "Usado en cruce" if is_used else "Sin usar en cruce",
    }


def _serialize_preview_tbk_row(idx: int, raw: Dict[str, Any], currency: str) -> Dict[str, Any]:
    modality = raw.get("payment_modality") or ""
    gross = float(raw.get("original_sale_amount") or 0)
    valid_amt = float(raw.get("valid_amount") if raw.get("valid_amount") is not None else gross)
    return {
        "index": idx + 1,
        "authorization_code": raw.get("authorization_code") or "—",
        "payment_modality": modality,
        "modality_label": MODALITY_LABELS.get(modality, modality or "—"),
        "card_code": raw.get("card_code") or "—",
        "sale_date": raw.get("sale_date") or "—",
        "masked_card_number": raw.get("masked_card_number") or "—",
        "merchant_code": raw.get("merchant_code") or "—",
        "merchant_location_name": raw.get("merchant_location_name") or "—",
        "receipt_number": raw.get("receipt_number") or "—",
        "transaction_identifier": raw.get("transaction_identifier") or "—",
        "installment_type": raw.get("installment_type") or "—",
        "installment_number": raw.get("installment_number") or "—",
        "original_sale_amount": gross,
        "valid_amount": valid_amt,
        "transbank_commission": float(raw.get("transbank_commission") or 0),
        "commission_vat_tax": float(raw.get("commission_vat_tax") or 0),
        "net_deposit_amount": float(raw.get("net_deposit_amount") or 0),
        "reconciled": bool(raw.get("reconciled")),
        "currency": currency,
    }


def _preview_erp_history_for_rows(
    raw_rows: List[Dict[str, Any]],
    pending_corrections: List[Dict[str, Any]],
    currency: str,
) -> Dict[int, Dict[str, Any]]:
    """Builds history modal payloads for preview ERP rows (inherited DB corrections + in-memory edits)."""
    source_keys = {
        raw["source_key"] for raw in raw_rows if raw.get("is_corrected") and raw.get("source_key")
    }
    manual_ids = {raw["manual_id"] for raw in raw_rows if raw.get("manual_id")}

    db_changes_by_key: Dict[str, List[ErpTransactionChange]] = {}
    db_changes_by_id: Dict[int, List[ErpTransactionChange]] = {}
    if source_keys or manual_ids:
        q_filter = Q()
        if source_keys:
            q_filter |= Q(erp_transaction__source_currency=currency, erp_transaction__source_key__in=source_keys)
        if manual_ids:
            q_filter |= Q(erp_transaction_id__in=manual_ids)
        for ch in (
            ErpTransactionChange.objects.filter(q_filter)
            .exclude(field_name=ErpTransactionChange.FIELD_CREATED)
            .select_related("user", "erp_transaction")
            .order_by("created_at", "id")
        ):
            db_changes_by_id.setdefault(ch.erp_transaction_id, []).append(ch)
            if ch.erp_transaction.source_key:
                db_changes_by_key.setdefault(ch.erp_transaction.source_key, []).append(ch)

    pending_by_idx: Dict[int, List[Dict[str, Any]]] = {}
    for entry in pending_corrections:
        if not entry.get("in_memory"):
            continue
        idx = entry.get("erp_index")
        if idx is None:
            continue
        pending_by_idx.setdefault(idx, []).append({
            "field_name": entry.get("field") or "",
            "old_value": entry.get("old"),
            "new_value": entry.get("new"),
            "created_at": "En esta previsualización",
            "username": "Auditor (sin guardar aún)",
            "source": ErpTransactionChange.SOURCE_SUGGESTION,
            "source_label": "Sugerencia aplicada (vista previa)",
            "reason": "Cambio aplicado en memoria; se guardará en BD al confirmar la conciliación.",
        })

    result: Dict[int, Dict[str, Any]] = {}
    for idx, raw in enumerate(raw_rows):
        combined: List[Dict[str, Any]] = []
        if raw.get("manual_id") and raw["manual_id"] in db_changes_by_id:
            combined.extend(_change_models_to_raw_list(db_changes_by_id[raw["manual_id"]]))
        elif raw.get("is_corrected") and raw.get("source_key") and raw["source_key"] in db_changes_by_key:
            combined.extend(_change_models_to_raw_list(db_changes_by_key[raw["source_key"]]))
        if idx in pending_by_idx:
            combined.extend(pending_by_idx[idx])
        if not combined:
            continue
        account = (raw.get("account_code") or raw.get("document_number") or "").strip() or "—"
        auth = (raw.get("authorization_code") or "").strip() or "—"
        document = (auth if auth != "—" else (raw.get("document_number") or "").strip()) or "—"
        result[idx] = _build_erp_history_entry(
            key=f"preview-{idx}",
            currency=currency,
            process_id=None,
            account=account,
            document=document,
            reservation_number=(raw.get("reservation_number") or "").strip() or "—",
            room_type=(raw.get("room_type") or "").strip() or "—",
            auth_code=auth,
            invoice_number=(raw.get("invoice_number") or "").strip() or "—",
            payment_code=(raw.get("payment_code") or "").strip() or "—",
            tx_date=_format_erp_field_value("transaction_date", raw.get("transaction_date")),
            cashier=(raw.get("cashier_username") or "").strip() or "—",
            amount_display=_format_erp_field_value("amount", raw.get("amount")),
            raw_changes=combined,
        )
    return result


def build_preview_source_tables_context(preview: Dict[str, Any], currency: str) -> Dict[str, Any]:
    """
    Builds a sanitized, strictly read-only payload for the preview source-table viewer
    (Borderó ERP, Cartola Bancaria, Posición por Clientes in USD, Transbank Detallado).
    """
    status_by_idx, usd_linked_auths = _build_preview_erp_status_maps(preview, currency)
    raw_erp_rows = preview.get("erp_transactions", [])
    history_by_idx = _preview_erp_history_for_rows(
        raw_erp_rows, preview.get("pending_corrections", []), currency
    )
    erp_rows = [
        _sanitize_preview_erp_row(
            idx, row, currency, status_by_idx, usd_linked_auths, history_by_idx.get(idx)
        )
        for idx, row in enumerate(raw_erp_rows)
    ]
    bank_rows = [
        _serialize_preview_bank_row(idx, row)
        for idx, row in enumerate(preview.get("bank_movements", []))
    ]
    operator_rows = (
        [
            _serialize_preview_operator_row(idx, row)
            for idx, row in enumerate(preview.get("card_operator_transactions", []))
        ]
        if currency == "USD"
        else []
    )
    tbk_rows = [
        _serialize_preview_tbk_row(idx, row, currency)
        for idx, row in enumerate(preview.get("transbank_transactions", []))
    ]

    counts = {
        "erp_total": len(erp_rows),
        "erp_matched": sum(1 for r in erp_rows if r["match_status"] == "matched"),
        "erp_diff": sum(1 for r in erp_rows if r["match_status"] == "diff"),
        "erp_unlinked": sum(1 for r in erp_rows if r["match_status"] == "unlinked"),
        "erp_modified": sum(1 for r in erp_rows if r["is_modified"]),
        "bank_total": len(bank_rows),
        "bank_matched": sum(1 for r in bank_rows if r["status_code"] == "matched"),
        "bank_tbk": sum(1 for r in bank_rows if r["status_code"] in ("matched", "tbk")),
        "operator_total": len(operator_rows),
        "operator_used": sum(1 for r in operator_rows if r["status_code"] == "used"),
        "operator_unused": sum(1 for r in operator_rows if r["status_code"] == "unused"),
        "tbk_total": len(tbk_rows),
    }
    return {
        "currency": currency,
        "counts": counts,
        "payload": {
            "currency": currency,
            "erp": erp_rows,
            "bank": bank_rows,
            "operator": operator_rows,
            "tbk": tbk_rows,
        },
    }


def process_erp_queryset(process: ReconciliationProcess):
    """
    Returns active ErpTransaction rows belonging to or cross-matched in `process`.
    Handles deduplicated re-uploads where existing rows retain their original process FK.
    """
    cm_erp_ids = CrossMatchResult.objects.filter(
        process=process, erp_transaction__isnull=False
    ).values("erp_transaction_id")
    return (
        ErpTransaction.objects.filter(Q(process=process) | Q(id__in=cm_erp_ids))
        .select_related("card_type", "process")
        .order_by("transaction_date", "transaction_time", "id")
    )


def all_erp_queryset():
    """Returns all active ErpTransaction rows consolidated across every closing."""
    return ErpTransaction.objects.select_related("card_type", "process").order_by(
        "-transaction_date", "-transaction_time", "-id"
    )


def process_bank_movements_queryset(process: ReconciliationProcess):
    """Returns BankStatementMovement rows created by or validated against `process`."""
    matched_ids = process.bank_validations.exclude(bank_movement__isnull=True).values("bank_movement_id")
    return (
        BankStatementMovement.objects.filter(Q(process=process) | Q(id__in=matched_ids))
        .select_related("process")
        .order_by("movement_date", "id")
    )


def all_bank_movements_queryset():
    """Returns all BankStatementMovement rows consolidated across every closing."""
    return BankStatementMovement.objects.select_related("process").order_by("-movement_date", "-id")


def process_operator_queryset(process: ReconciliationProcess):
    """Returns CardOperatorTransaction rows belonging to or linked in `process`."""
    cm_op_ids = CrossMatchResult.objects.filter(
        process=process, card_operator_transaction__isnull=False
    ).values("card_operator_transaction_id")
    return (
        CardOperatorTransaction.objects.filter(Q(process=process) | Q(id__in=cm_op_ids))
        .select_related("card_type", "process")
        .order_by("card_type__code", "document_number", "sequence_number", "id")
    )


def all_operator_queryset():
    """Returns all CardOperatorTransaction rows consolidated across every USD closing."""
    return CardOperatorTransaction.objects.select_related("card_type", "process").order_by(
        "-process__execution_date", "card_type__code", "document_number", "sequence_number", "id"
    )


def process_transbank_queryset(process: ReconciliationProcess):
    """Returns TransbankTransaction rows belonging to or linked in `process`."""
    cm_tbk_ids = CrossMatchResult.objects.filter(
        process=process, transbank_transaction__isnull=False
    ).values("transbank_transaction_id")
    return (
        TransbankTransaction.objects.filter(Q(process=process) | Q(id__in=cm_tbk_ids))
        .select_related("card_type", "process")
        .order_by("sale_date", "payment_modality", "id")
    )


def all_transbank_queryset():
    """Returns all TransbankTransaction rows consolidated across every closing."""
    return TransbankTransaction.objects.select_related("card_type", "process").order_by("-sale_date", "-id")


def all_matches_queryset():
    """Returns all CrossMatchResult rows consolidated across every closing."""
    return CrossMatchResult.objects.select_related(
        "process",
        "card_type",
        "erp_transaction",
        "transbank_transaction",
        "card_operator_transaction",
        "management_status",
        "resolved_by",
    ).order_by("-sale_date", "-process__execution_date", "-transbank_amount", "id")


def get_consultas_nav_counts() -> Dict[str, int]:
    """Returns global row counts for the top Consultas navigation switcher."""
    return {
        "erp": ErpTransaction.objects.count(),
        "transbank": TransbankTransaction.objects.count(),
        "bank": BankStatementMovement.objects.count(),
        "operadoras": CardOperatorTransaction.objects.count(),
        "cruces": CrossMatchResult.objects.count(),
        "cierres": ReconciliationProcess.objects.count(),
    }


def _paginate(queryset_or_list, page_num: Any, per_page: int = 30):
    return Paginator(queryset_or_list, per_page).get_page(page_num)


def _normalize_moneda(request) -> str:
    moneda = (request.GET.get("moneda") or "ALL").upper()
    return moneda if moneda in ("ALL", "USD", "CLP") else "ALL"


def build_cruce_view_data(
    request, process: Optional[ReconciliationProcess] = None, per_page: int = 30
) -> Dict[str, Any]:
    """Builds filtered, paginated CrossMatchResult table context for one closing or consolidated."""
    q = (request.GET.get("q") or "").strip()
    resultado = (request.GET.get("resultado") or "").strip()
    tarjeta = (request.GET.get("tarjeta") or "").strip()
    estado = (request.GET.get("estado") or "").strip()
    moneda = process.process_currency if process else _normalize_moneda(request)
    date_from = None if process else parse_iso_date(request.GET.get("desde"))
    date_to = None if process else parse_iso_date(request.GET.get("hasta"))

    if process:
        qs = (
            process.cross_match_results.select_related(
                "process",
                "card_type",
                "erp_transaction",
                "transbank_transaction",
                "card_operator_transaction",
                "management_status",
                "resolved_by",
            )
            .order_by("-transbank_amount", "id")
        )
    else:
        qs = all_matches_queryset()
        if moneda in ("USD", "CLP"):
            qs = qs.filter(process__process_currency=moneda)
        if date_from:
            qs = qs.filter(sale_date__gte=date_from)
        if date_to:
            qs = qs.filter(sale_date__lte=date_to)

    if resultado:
        qs = qs.filter(system_result=resultado)
    if tarjeta:
        qs = qs.filter(card_type__code=tarjeta)
    if estado:
        qs = qs.filter(management_status__code=estado)
    if q:
        qs = qs.filter(
            Q(authorization_code__icontains=q)
            | Q(account_code__icontains=q)
            | Q(document_number__icontains=q)
            | Q(cashier_username__icontains=q)
            | Q(erp_transaction__account_code__icontains=q)
            | Q(erp_transaction__document_number__icontains=q)
            | Q(erp_transaction__authorization_code__icontains=q)
            | Q(card_operator_transaction__document_number__icontains=q)
        )

    agg = qs.aggregate(
        total=Count("id"),
        matched=Count("id", filter=Q(system_result="MATCHED")),
    )
    total_count = agg["total"] or 0
    matched_count = agg["matched"] or 0

    page = _paginate(qs, request.GET.get("page"), per_page=per_page)
    linked_erp_by_id = {
        item.erp_transaction_id: item.erp_transaction
        for item in page.object_list
        if item.erp_transaction_id and item.erp_transaction is not None
    }
    erp_history_map = _fetch_erp_history_map_for_rows(linked_erp_by_id)
    for item in page.object_list:
        label, tone = result_label_and_tone(item.system_result)
        item.result_label = label
        item.result_tone = tone
        item.row_is_usd = (item.process.process_currency == "USD")
        key = str(item.erp_transaction_id) if item.erp_transaction_id else ""
        item.has_erp_modifications = bool(key and key in erp_history_map)
        item.erp_history_key = key if item.has_erp_modifications else ""
        item.erp_modifications_count = erp_history_map[key]["changes_count"] if item.has_erp_modifications else 0

    return {
        "page": page,
        "cruce_rows": page.object_list,
        "erp_history_map": erp_history_map,
        "kpis": {
            "total": total_count,
            "matched": matched_count,
            "discrepancies": total_count - matched_count,
            "match_rate": round(matched_count / total_count * 100, 1) if total_count else 0.0,
        },
        "result_choices": [
            ("MATCHED", "Conciliado"),
            ("AMOUNT_MISMATCH", "Dif. de monto"),
            ("CARD_TYPE_MISMATCH", "Medio distinto"),
            ("NOT_FOUND_IN_ERP", "No está en ERP"),
            ("OUT_OF_PERIOD", "Fuera de período"),
        ],
        "card_types": CardType.objects.order_by("name"),
        "status_choices": ReconciliationStatus.objects.order_by("id"),
        "filters": {
            "q": q,
            "resultado": resultado,
            "tarjeta": tarjeta,
            "estado": estado,
            "moneda": moneda,
            "desde": request.GET.get("desde", ""),
            "hasta": request.GET.get("hasta", ""),
        },
    }


def build_erp_view_data(
    request, process: Optional[ReconciliationProcess] = None, per_page: int = 30
) -> Dict[str, Any]:
    """Builds filtered, paginated Borderó ERP table context for one closing or consolidated."""
    q = (request.GET.get("q") or "").strip()
    cruce = (request.GET.get("cruce") or "").strip()
    origen = (request.GET.get("origen") or "").strip()
    tarjeta = (request.GET.get("tarjeta") or "").strip()
    moneda = process.process_currency if process else _normalize_moneda(request)
    date_from = None if process else parse_iso_date(request.GET.get("desde"))
    date_to = None if process else parse_iso_date(request.GET.get("hasta"))

    cm_scope = CrossMatchResult.objects.filter(process=process) if process else CrossMatchResult.objects.all()
    has_matched = Exists(cm_scope.filter(erp_transaction_id=OuterRef("pk"), system_result="MATCHED"))
    has_diff = Exists(cm_scope.filter(erp_transaction_id=OuterRef("pk")).exclude(system_result="MATCHED"))
    has_modifications = Exists(
        ErpTransactionChange.objects.filter(erp_transaction_id=OuterRef("pk")).exclude(
            field_name=ErpTransactionChange.FIELD_CREATED
        )
    )

    qs = (process_erp_queryset(process) if process else all_erp_queryset()).annotate(
        _has_matched=has_matched,
        _has_diff=has_diff,
        _has_modifications=has_modifications,
    )
    if not process:
        if moneda in ("USD", "CLP"):
            qs = qs.filter(
                Q(source_currency=moneda)
                | (Q(source_currency__isnull=True) | Q(source_currency="")) & Q(process__process_currency=moneda)
            )
        if date_from:
            qs = qs.filter(transaction_date__gte=date_from)
        if date_to:
            qs = qs.filter(transaction_date__lte=date_to)

    if cruce == "solo_erp":
        qs = qs.filter(reconciled=False, _has_matched=False, _has_diff=False)
    elif cruce == "conciliados":
        qs = qs.filter(Q(_has_matched=True) | Q(reconciled=True, _has_diff=False))
    elif cruce == "con_diferencia":
        qs = qs.filter(_has_matched=False, _has_diff=True)

    if origen == "MODIFIED":
        qs = qs.filter(_has_modifications=True)
    elif origen in (ErpTransaction.ORIGIN_EXCEL, ErpTransaction.ORIGIN_MANUAL):
        qs = qs.filter(origin=origen)
    if tarjeta:
        qs = qs.filter(card_type__code=tarjeta)
    if q:
        qs = qs.filter(
            Q(account_code__icontains=q)
            | Q(document_number__icontains=q)
            | Q(reservation_number__icontains=q)
            | Q(room_number__icontains=q)
            | Q(room_type__icontains=q)
            | Q(guest_name__icontains=q)
            | Q(authorization_code__icontains=q)
            | Q(invoice_number__icontains=q)
            | Q(cashier_username__icontains=q)
            | Q(payment_code__icontains=q)
        )

    agg = qs.aggregate(
        total=Count("id"),
        matched=Count("id", filter=Q(_has_matched=True) | Q(reconciled=True, _has_diff=False)),
        diff=Count("id", filter=Q(_has_matched=False, _has_diff=True)),
        solo_erp=Count("id", filter=Q(reconciled=False, _has_matched=False, _has_diff=False)),
        modified=Count("id", filter=Q(_has_modifications=True)),
        total_clp=Sum("amount"),
    )

    page = _paginate(qs, request.GET.get("page"), per_page=per_page)
    mod_rows_by_id = {row.id: row for row in page.object_list if row._has_modifications}
    erp_history_map = _fetch_erp_history_map_for_rows(mod_rows_by_id, include_guest_and_room=True)
    for row in page.object_list:
        row.display_account = (row.account_code or row.document_number or "").strip() or "—"
        row.display_document = (row.document_number or row.authorization_code or "").strip() or "—"
        row.source_cur = (row.source_currency or row.process.process_currency or "").upper()
        key = str(row.id)
        row.has_modifications = key in erp_history_map
        row.history_key = key if row.has_modifications else ""
        row.modifications_count = erp_history_map[key]["changes_count"] if row.has_modifications else 0
        if row._has_matched:
            row.link_label, row.link_tone = ("Conciliado", "status-success")
        elif row._has_diff:
            row.link_label, row.link_tone = ("Con diferencia", "status-warning")
        elif row.reconciled:
            row.link_label, row.link_tone = ("Conciliado", "status-success")
        else:
            row.link_label, row.link_tone = ("Solo en ERP (sin TBK)", "status-danger")

    return {
        "page": page,
        "erp_rows": page.object_list,
        "erp_history_map": erp_history_map,
        "kpis": {
            "total": agg["total"] or 0,
            "matched": agg["matched"] or 0,
            "diff": agg["diff"] or 0,
            "solo_erp": agg["solo_erp"] or 0,
            "modified": agg["modified"] or 0,
            "total_clp": agg["total_clp"] or Decimal("0"),
        },
        "card_types": CardType.objects.order_by("name"),
        "filters": {
            "q": q,
            "cruce": cruce,
            "origen": origen,
            "tarjeta": tarjeta,
            "moneda": moneda,
            "desde": request.GET.get("desde", ""),
            "hasta": request.GET.get("hasta", ""),
        },
    }


def build_bank_view_data(
    request, process: Optional[ReconciliationProcess] = None, per_page: int = 30
) -> Dict[str, Any]:
    """Builds filtered, paginated Cartola Bancaria table context for one closing or consolidated."""
    q = (request.GET.get("q") or "").strip()
    tipo = (request.GET.get("tipo") or "").strip()
    moneda = process.process_currency if process else _normalize_moneda(request)
    date_from = None if process else parse_iso_date(request.GET.get("desde"))
    date_to = None if process else parse_iso_date(request.GET.get("hasta"))

    if process:
        validations = list(process.bank_validations.select_related("bank_movement").order_by("payment_modality"))
        for v in validations:
            v.modality_label = MODALITY_LABELS.get(v.payment_modality, v.payment_modality)
        val_scope = process.bank_validations.all()
        base_qs = process_bank_movements_queryset(process)
    else:
        validations = []
        val_scope = BankDepositValidation.objects.all()
        base_qs = all_bank_movements_queryset()

    is_matched_sub = Exists(val_scope.filter(bank_movement_id=OuterRef("pk"), is_matched=True))
    is_validated_sub = Exists(val_scope.filter(bank_movement_id=OuterRef("pk")))
    qs = base_qs.annotate(_is_matched_val=is_matched_sub | is_validated_sub)

    if not process:
        if moneda in ("USD", "CLP"):
            qs = qs.filter(process__process_currency=moneda)
        if date_from:
            qs = qs.filter(movement_date__gte=date_from)
        if date_to:
            qs = qs.filter(movement_date__lte=date_to)

    if tipo == "cuadrados":
        qs = qs.filter(_is_matched_val=True)
    elif tipo == "tbk":
        qs = qs.filter(Q(is_transbank_deposit=True) | Q(_is_matched_val=True))
    if q:
        qs = qs.filter(
            Q(description__icontains=q)
            | Q(document_number__icontains=q)
            | Q(branch_or_channel__icontains=q)
        )

    agg = qs.aggregate(
        total=Count("id"),
        matched=Count("id", filter=Q(_is_matched_val=True)),
        tbk=Count("id", filter=Q(is_transbank_deposit=True) | Q(_is_matched_val=True)),
    )

    page = _paginate(qs, request.GET.get("page"), per_page=per_page)
    for row in page.object_list:
        row.row_is_usd = (row.process.process_currency == "USD")
        if row._is_matched_val:
            row.bank_tag_label, row.bank_tag_tone = ("Abono cuadrado", "status-success")
        elif row.is_transbank_deposit:
            row.bank_tag_label, row.bank_tag_tone = ("Movimiento TBK", "status-warning")
        else:
            row.bank_tag_label, row.bank_tag_tone = ("Otro movimiento", "status-neutral")

    return {
        "page": page,
        "bank_rows": page.object_list,
        "validations": validations,
        "kpis": {
            "total": agg["total"] or 0,
            "matched": agg["matched"] or 0,
            "tbk": agg["tbk"] or 0,
        },
        "filters": {
            "q": q,
            "tipo": tipo,
            "moneda": moneda,
            "desde": request.GET.get("desde", ""),
            "hasta": request.GET.get("hasta", ""),
        },
    }


def build_operator_view_data(
    request, process: Optional[ReconciliationProcess] = None, per_page: int = 30
) -> Dict[str, Any]:
    """Builds filtered, paginated Posición por Clientes (USD) context for one closing or consolidated."""
    q = (request.GET.get("q") or "").strip()
    tarjeta = (request.GET.get("tarjeta") or "").strip()
    cruce = (request.GET.get("cruce") or "").strip()
    date_from = None if process else parse_iso_date(request.GET.get("desde"))
    date_to = None if process else parse_iso_date(request.GET.get("hasta"))

    cm_scope = CrossMatchResult.objects.filter(process=process) if process else CrossMatchResult.objects.all()
    has_cm_link = Exists(cm_scope.filter(card_operator_transaction_id=OuterRef("pk")))
    base_qs = process_operator_queryset(process) if process else all_operator_queryset()
    qs = base_qs.annotate(_has_cm_link=has_cm_link)

    if not process:
        if date_from:
            qs = qs.filter(process__execution_date__date__gte=date_from)
        if date_to:
            qs = qs.filter(process__execution_date__date__lte=date_to)

    if tarjeta:
        qs = qs.filter(card_type__code=tarjeta)
    if cruce == "usados":
        qs = qs.filter(Q(_has_cm_link=True) | (~Q(authorization_code__isnull=True) & ~Q(authorization_code="")))
    elif cruce == "sin_usar":
        qs = qs.filter(_has_cm_link=False).filter(Q(authorization_code__isnull=True) | Q(authorization_code=""))
    if q:
        qs = qs.filter(
            Q(document_number__icontains=q)
            | Q(sequence_number__icontains=q)
            | Q(authorization_code__icontains=q)
        )

    agg = qs.aggregate(
        total=Count("id"),
        used=Count(
            "id",
            filter=Q(_has_cm_link=True) | (~Q(authorization_code__isnull=True) & ~Q(authorization_code="")),
        ),
        unused=Count(
            "id",
            filter=Q(_has_cm_link=False) & (Q(authorization_code__isnull=True) | Q(authorization_code="")),
        ),
        total_usd=Sum("foreign_currency_amount"),
    )

    page = _paginate(qs, request.GET.get("page"), per_page=per_page)
    for row in page.object_list:
        row.op_used = bool(row._has_cm_link or (row.authorization_code or "").strip())

    return {
        "page": page,
        "operator_rows": page.object_list,
        "kpis": {
            "total": agg["total"] or 0,
            "used": agg["used"] or 0,
            "unused": agg["unused"] or 0,
            "total_usd": agg["total_usd"] or Decimal("0"),
        },
        "card_types": CardType.objects.filter(code__in=("AX", "DI", "MC", "VI")).order_by("name"),
        "filters": {
            "q": q,
            "tarjeta": tarjeta,
            "cruce": cruce,
            "desde": request.GET.get("desde", ""),
            "hasta": request.GET.get("hasta", ""),
        },
    }


def build_transbank_view_data(
    request, process: Optional[ReconciliationProcess] = None, per_page: int = 30
) -> Dict[str, Any]:
    """Builds filtered, paginated Transbank Detallado table context for one closing or consolidated."""
    q = (request.GET.get("q") or "").strip()
    modalidad = (request.GET.get("modalidad") or "").strip()
    tarjeta = (request.GET.get("tarjeta") or "").strip()
    estado = (request.GET.get("estado") or "").strip()
    moneda = process.process_currency if process else _normalize_moneda(request)
    date_from = None if process else parse_iso_date(request.GET.get("desde"))
    date_to = None if process else parse_iso_date(request.GET.get("hasta"))

    cm_scope = CrossMatchResult.objects.filter(process=process) if process else CrossMatchResult.objects.all()
    qs = (process_transbank_queryset(process) if process else all_transbank_queryset()).annotate(
        _has_matched_cm=Exists(cm_scope.filter(transbank_transaction_id=OuterRef("pk"), system_result="MATCHED")),
        _has_resolved_cm=Exists(
            cm_scope.filter(
                transbank_transaction_id=OuterRef("pk"),
                management_status__code__startswith="RESOLVED_",
            )
        ),
    )
    if not process:
        if moneda in ("USD", "CLP"):
            qs = qs.filter(process__process_currency=moneda)
        if date_from:
            qs = qs.filter(sale_date__gte=date_from)
        if date_to:
            qs = qs.filter(sale_date__lte=date_to)

    is_matched_q = Q(reconciled=True) | Q(_has_matched_cm=True)
    is_resolved_q = Q(reconciled=False, _has_matched_cm=False, _has_resolved_cm=True)
    is_pending_q = Q(reconciled=False, _has_matched_cm=False, _has_resolved_cm=False)

    if modalidad:
        qs = qs.filter(payment_modality=modalidad)
    if tarjeta:
        qs = qs.filter(card_type__code=tarjeta)
    if estado == "conciliado":
        qs = qs.filter(is_matched_q)
    elif estado == "resuelto":
        qs = qs.filter(is_resolved_q)
    elif estado == "pendiente":
        qs = qs.filter(is_pending_q)
    if q:
        qs = qs.filter(
            Q(authorization_code__icontains=q)
            | Q(masked_card_number__icontains=q)
            | Q(merchant_location_name__icontains=q)
            | Q(receipt_number__icontains=q)
            | Q(transaction_identifier__icontains=q)
            | Q(merchant_code__icontains=q)
        )

    agg = qs.aggregate(
        total=Count("id"),
        conciliadas=Count("id", filter=is_matched_q),
        resueltas=Count("id", filter=is_resolved_q),
        pendientes=Count("id", filter=is_pending_q),
        net_clp=Sum("net_deposit_amount", filter=Q(process__process_currency="CLP")),
        net_usd=Sum("net_deposit_amount", filter=Q(process__process_currency="USD")),
    )

    page = _paginate(qs, request.GET.get("page"), per_page=per_page)
    resolved_ids = [
        row.id
        for row in page.object_list
        if not row.reconciled and not row._has_matched_cm and row._has_resolved_cm
    ]
    resolved_labels: Dict[int, str] = {}
    if resolved_ids:
        for cm in (
            cm_scope.filter(
                transbank_transaction_id__in=resolved_ids,
                management_status__code__startswith="RESOLVED_",
            )
            .select_related("management_status")
            .order_by("id")
        ):
            resolved_labels[cm.transbank_transaction_id] = cm.management_status.display_name

    for row in page.object_list:
        row.modality_label = MODALITY_LABELS.get(row.payment_modality, row.payment_modality)
        row.row_is_usd = (row.process.process_currency == "USD")
        if row.reconciled or row._has_matched_cm:
            row.tbk_status_label = "Conciliado"
            row.tbk_status_tone = "status-success"
            row.tbk_status_title = ""
        elif row._has_resolved_cm:
            row.tbk_status_label = "Resuelto"
            row.tbk_status_tone = "status-neutral"
            row.tbk_status_title = resolved_labels.get(row.id, "")
        else:
            row.tbk_status_label = "Pendiente"
            row.tbk_status_tone = "status-warning"
            row.tbk_status_title = ""

    return {
        "page": page,
        "tbk_rows": page.object_list,
        "kpis": {
            "total": agg["total"] or 0,
            "conciliadas": agg["conciliadas"] or 0,
            "resueltas": agg["resueltas"] or 0,
            "pendientes": agg["pendientes"] or 0,
            "net_clp": agg["net_clp"] or Decimal("0"),
            "net_usd": agg["net_usd"] or Decimal("0"),
        },
        "card_types": CardType.objects.order_by("name"),
        "filters": {
            "q": q,
            "modalidad": modalidad,
            "tarjeta": tarjeta,
            "estado": estado,
            "moneda": moneda,
            "desde": request.GET.get("desde", ""),
            "hasta": request.GET.get("hasta", ""),
        },
    }


def format_erp_change_row(change: ErpTransactionChange) -> Dict[str, Any]:
    """Formats an ErpTransactionChange row for read-only audit visualization without guest/room fields."""
    erp = change.erp_transaction
    process = (
        change.cross_match_result.process
        if change.cross_match_result_id and change.cross_match_result.process_id
        else (erp.process if erp else None)
    )
    currency = (erp.source_currency if erp else "") or (process.process_currency if process else "") or "—"

    account = ((erp.account_code or erp.document_number or "") if erp else "").strip() or "—"
    document = ((erp.authorization_code or erp.document_number or "") if erp else "").strip() or "—"

    return {
        "id": change.id,
        "created_at": change.created_at,
        "username": change.user.get_full_name() or change.user.username,
        "currency": currency,
        "process_id": process.id if process else None,
        "accounting_period": (process.accounting_period if process else "") or "",
        "field_code": change.field_name,
        "field_label": ERP_CHANGE_FIELD_LABELS.get(change.field_name, change.field_name),
        "old_value": _format_erp_field_value(change.field_name, change.old_value),
        "new_value": _format_erp_field_value(change.field_name, change.new_value),
        "source": change.source,
        "source_label": change.get_source_display(),
        "reason": (change.reason or "").strip() or "—",
        "account_code": account,
        "document_number": document,
        "auth_code": (
            (change.cross_match_result.authorization_code if change.cross_match_result_id else "")
            or (erp.authorization_code if erp else "")
            or "—"
        ),
        "match_id": change.cross_match_result_id,
    }
