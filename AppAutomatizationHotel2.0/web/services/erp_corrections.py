"""
Corrections to Bordero (ERP) rows that must survive a new upload of the same Excel.

Every Excel row gets a fingerprint (`source_key`) computed from its ORIGINAL values. When the accountant
corrects or soft-deletes a row, the fingerprint stays the same, so on the next upload:
  - the row is recognized instead of being inserted again with the typo, and
  - `apply_saved_corrections` puts the corrected values (or drops the deleted row) BEFORE the cross-match,
    so the voucher stays matched instead of falling back to a mismatch.
Rows the accountant added by hand have no fingerprint. They join the cross-match of a new upload when one
of its vouchers carries their code, and a new Excel row that looks like one of them is flagged as a
possible duplicate for the accountant to decide.
"""

import hashlib
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..models import ErpTransaction
from .column_detector import normalize_auth_code, safe_date, safe_time

# Fields the accountant may correct; their DB value replaces the Excel value on a new upload.
# Guest name and room number stay excluded to preserve Pendientes/Conciliación privacy.
CORRECTABLE_FIELDS = (
    "amount", "authorization_code", "transaction_date", "payment_code", "document_number",
    "transaction_time", "invoice_number", "account_code", "reservation_number",
    "room_type", "cashier_username",
)
MANUAL_SOURCE_LABEL = "Agregada a mano"


def erp_source_key(
    source_currency: str,
    document_number: Any,
    invoice_number: Any,
    authorization_code: Any,
    amount: Any,
    transaction_date: Any,
    transaction_time: Any,
) -> str:
    """
    Fingerprint of a Bordero row: the same natural key the uploads already deduplicated by
    (document + invoice + authorization + amount + date + time), scoped by currency.
    Accepts raw Excel/session values or model values; both normalize to the same key.
    Migration 0006 inlines this algorithm for the backfill: keep both in sync.
    """
    amount_dec = round(Decimal(str(amount or 0)), 2)
    date_val = safe_date(transaction_date) if transaction_date not in (None, "") else None
    time_val = safe_time(transaction_time) if transaction_time not in (None, "") else None
    parts = [
        source_currency or "",
        str(document_number or ""),
        str(invoice_number or ""),
        str(authorization_code or ""),
        f"{amount_dec:.2f}",
        date_val.isoformat() if date_val else "",
        time_val.strftime("%H:%M:%S") if time_val else "",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def source_key_for_raw(e_data: Dict[str, Any], source_currency: str, doc_field: str = "document_number") -> str:
    """Fingerprint of a session/preview ERP row; reuses the one already stamped on it when present."""
    if e_data.get("source_key"):
        return e_data["source_key"]
    return erp_source_key(
        source_currency,
        e_data.get(doc_field) or e_data.get("account_code") or "",
        e_data.get("invoice_number"),
        e_data.get("authorization_code"),
        e_data.get("amount"),
        e_data.get("transaction_date"),
        e_data.get("transaction_time"),
    )


def _corrected_rows_by_key(source_currency: str, keys: Iterable[str]) -> Dict[str, ErpTransaction]:
    """Rows with these fingerprints that were corrected or soft-deleted at least once."""
    keys = [k for k in set(keys) if k]
    if not keys:
        return {}
    result: Dict[str, ErpTransaction] = {}
    for start in range(0, len(keys), 900):
        chunk = keys[start : start + 900]
        rows = (
            ErpTransaction.all_objects.filter(source_currency=source_currency, source_key__in=chunk)
            .filter(changes__isnull=False)
            .distinct()
        )
        for row in rows:
            result[row.source_key] = row
    return result


def _manual_rows_for_codes(source_currency: str, voucher_codes: Optional[Set[str]]) -> List[ErpTransaction]:
    """Active hand-added rows whose authorization code belongs to one of the uploaded vouchers."""
    clean_codes = {c for c in (voucher_codes or set()) if c}
    if not clean_codes:
        return []
    from django.db.models import Q
    q = Q()
    for c in clean_codes:
        q |= Q(authorization_code__icontains=c)
    rows = (
        ErpTransaction.objects.filter(source_currency=source_currency, origin=ErpTransaction.ORIGIN_MANUAL)
        .filter(q)
        .select_related("card_type")
    )
    return [r for r in rows if normalize_auth_code(r.authorization_code or "") in clean_codes]


def _same_sale(a_code: Any, a_amount: Any, a_date: Any, row: ErpTransaction) -> bool:
    return (
        normalize_auth_code(a_code or "") == normalize_auth_code(row.authorization_code or "")
        and abs(Decimal(str(a_amount or 0)) - row.amount) < 1
        and safe_date(a_date) == row.transaction_date
    )


def _possible_duplicates(
    source_currency: str, candidates: List[Tuple[str, Any, Any, Any, str]]
) -> List[Dict[str, Any]]:
    """
    candidates = (source_key, code, amount, date, excel_label) of the uploaded rows.
    A NEW Excel row (fingerprint not in the DB) that matches a hand-added row is a possible duplicate.
    """
    if not candidates:
        return []
    cand_dates = {d for _, _, _, raw_d, _ in candidates if (d := safe_date(raw_d)) is not None}
    manual_qs = ErpTransaction.objects.filter(source_currency=source_currency, origin=ErpTransaction.ORIGIN_MANUAL)
    if cand_dates:
        manual_qs = manual_qs.filter(transaction_date__in=cand_dates)
    manual = list(manual_qs)
    if not manual:
        return []
    all_cand_keys = [c[0] for c in candidates if c[0]]
    known: set = set()
    for start in range(0, len(all_cand_keys), 900):
        known.update(
            ErpTransaction.all_objects.filter(source_key__in=all_cand_keys[start : start + 900])
            .values_list("source_key", flat=True)
        )
    duplicates = []
    for key, code, amount, tx_date, excel_label in candidates:
        if key in known:
            continue
        for row in manual:
            if _same_sale(code, amount, tx_date, row):
                duplicates.append({
                    "manual_id": row.id,
                    "authorization_code": row.authorization_code or "",
                    "amount": float(row.amount),
                    "transaction_date": row.transaction_date.strftime("%d/%m/%Y") if row.transaction_date else "",
                    "excel_row": excel_label,
                })
                break
    return duplicates


# ------------------------------------------------------------------------------
# CLP: preview rows are dictionaries
# ------------------------------------------------------------------------------

def _clp_raw_from_model(row: ErpTransaction) -> Dict[str, Any]:
    """A hand-added CLP row in the same shape as a parsed Bordero row."""
    card_code = row.card_type.code if row.card_type else ""
    is_redcompra = row.payment_code == "RCO" or not card_code
    return {
        "excel_row": f"Manual #{row.id}",
        "source_file": MANUAL_SOURCE_LABEL,
        "card_code": card_code,
        "payment_group": "REDCOMPRA" if is_redcompra else "CREDIT",
        "payment_code": row.payment_code or "",
        "payment_description": "Red Compra" if is_redcompra else f"{row.card_type.name} $",
        "room_number": "",
        "room_type": row.room_type or "",
        "reservation_number": row.reservation_number or "",
        "account_code": row.account_code or row.document_number,
        "invoice_number": row.invoice_number or "",
        "authorization_code": row.authorization_code or "",
        "transaction_date": row.transaction_date.strftime("%d/%m/%Y") if row.transaction_date else None,
        "transaction_time": row.transaction_time.strftime("%H:%M:%S") if row.transaction_time else None,
        "cashier_username": row.cashier_username or "",
        "guest_name": "",
        "amount": float(row.amount),
        "is_reversal": row.amount < 0,
        "reconciled": False,
        "manual_id": row.id,
        "is_manual": True,
    }


def apply_saved_corrections(
    erp_rows: List[Dict[str, Any]],
    source_currency: str,
    doc_field: str = "document_number",
    voucher_codes: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Stamps every preview row with its `source_key`, applies the corrections saved in the database
    (corrected fields take their DB value, soft-deleted rows leave the cross-match), and appends the
    hand-added rows of the uploaded vouchers. Returns (rows to cross-match, report for the preview).
    """
    for e_data in erp_rows:
        e_data["source_key"] = source_key_for_raw(e_data, source_currency, doc_field)

    report = {
        "corrected": 0,
        "deleted": 0,
        "manual": 0,
        "possible_duplicates": _possible_duplicates(source_currency, [
            (r["source_key"], r.get("authorization_code"), r.get("amount"), r.get("transaction_date"),
             f"{r.get('source_file') or 'Borderó'} fila {r.get('excel_row') or '—'}")
            for r in erp_rows
        ]),
    }
    corrected = _corrected_rows_by_key(source_currency, (r["source_key"] for r in erp_rows))

    kept: List[Dict[str, Any]] = []
    for e_data in erp_rows:
        db_row = corrected.get(e_data["source_key"])
        if db_row is None:
            kept.append(e_data)
            continue
        if db_row.is_deleted:
            report["deleted"] += 1
            continue
        _overlay_db_values(e_data, db_row, doc_field)
        e_data["is_corrected"] = True
        report["corrected"] += 1
        kept.append(e_data)

    for row in _manual_rows_for_codes(source_currency, voucher_codes):
        kept.append(_clp_raw_from_model(row))
        report["manual"] += 1
    return kept, report


def _overlay_db_values(e_data: Dict[str, Any], db_row: ErpTransaction, doc_field: str) -> None:
    """Writes the DB values of the correctable fields onto a preview row, in the preview's own formats."""
    e_data["amount"] = float(db_row.amount)
    e_data["authorization_code"] = db_row.authorization_code or ""
    e_data["transaction_date"] = db_row.transaction_date.strftime("%d/%m/%Y") if db_row.transaction_date else None
    e_data["transaction_time"] = db_row.transaction_time.strftime("%H:%M:%S") if db_row.transaction_time else None
    e_data["payment_code"] = db_row.payment_code or ""
    e_data["account_code"] = db_row.account_code or db_row.document_number
    e_data["invoice_number"] = db_row.invoice_number or ""
    e_data["reservation_number"] = db_row.reservation_number or ""
    e_data["room_type"] = db_row.room_type or ""
    e_data["cashier_username"] = db_row.cashier_username or ""
    e_data[doc_field] = (db_row.account_code or db_row.document_number) if doc_field == "account_code" else db_row.document_number
    if "is_reversal" in e_data:
        e_data["is_reversal"] = db_row.amount < 0
    if "payment_group" in e_data:
        # A corrected payment code can move the row between Red Compra and credit
        e_data["payment_group"] = "REDCOMPRA" if db_row.payment_code == "RCO" else "CREDIT"
        if db_row.payment_code == "RCO":
            e_data["card_code"] = ""
        elif db_row.card_type_id:
            e_data["card_code"] = db_row.card_type.code


# ------------------------------------------------------------------------------
# USD: preview rows are unsaved ErpTransaction instances
# ------------------------------------------------------------------------------

def overlay_on_instances(
    instances: List[ErpTransaction],
    source_currency: str,
    voucher_codes: Optional[Set[str]] = None,
) -> Tuple[List[ErpTransaction], Dict[str, Any]]:
    """Same as `apply_saved_corrections`, for the unsaved ErpTransaction instances of the USD flow."""
    for inst in instances:
        inst.source_key = erp_source_key(
            source_currency,
            inst.document_number or inst.account_code or "",
            inst.invoice_number,
            inst.authorization_code,
            inst.amount,
            inst.transaction_date,
            inst.transaction_time,
        )
    report = {
        "corrected": 0,
        "deleted": 0,
        "manual": 0,
        "possible_duplicates": _possible_duplicates(source_currency, [
            (i.source_key, i.authorization_code, i.amount, i.transaction_date,
             f"{getattr(i, 'source_file', '') or 'ERP'} · Doc {i.document_number}")
            for i in instances
        ]),
    }
    corrected = _corrected_rows_by_key(source_currency, (i.source_key for i in instances))

    kept: List[ErpTransaction] = []
    for inst in instances:
        db_row: Optional[ErpTransaction] = corrected.get(inst.source_key)
        if db_row is None:
            kept.append(inst)
            continue
        if db_row.is_deleted:
            report["deleted"] += 1
            continue
        for field_name in CORRECTABLE_FIELDS:
            setattr(inst, field_name, getattr(db_row, field_name))
        inst.is_corrected = True
        report["corrected"] += 1
        kept.append(inst)

    for row in _manual_rows_for_codes(source_currency, voucher_codes):
        row.source_file = MANUAL_SOURCE_LABEL
        kept.append(row)
        report["manual"] += 1
    return kept, report
