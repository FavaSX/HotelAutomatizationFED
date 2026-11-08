"""
Corrections to Bordero (ERP) rows that must survive a new upload of the same Excel.

Every Excel row gets a fingerprint (`source_key`) computed from its ORIGINAL values. When the accountant
corrects or soft-deletes a row, the fingerprint stays the same, so on the next upload:
  - the row is recognized instead of being inserted again with the typo, and
  - `apply_saved_corrections` puts the corrected values (or drops the deleted row) BEFORE the cross-match,
    so the voucher stays matched instead of falling back to a mismatch.
"""

import hashlib
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..models import ErpTransaction
from .column_detector import safe_date, safe_time

# Fields the accountant may correct; their DB value replaces the Excel value on a new upload
CORRECTABLE_FIELDS = ("amount", "authorization_code", "transaction_date", "payment_code", "document_number")


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
    rows = (
        ErpTransaction.all_objects.filter(source_currency=source_currency, source_key__in=keys)
        .filter(changes__isnull=False)
        .distinct()
    )
    return {row.source_key: row for row in rows}


def apply_saved_corrections(
    erp_rows: List[Dict[str, Any]],
    source_currency: str,
    doc_field: str = "document_number",
) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Stamps every preview row with its `source_key` and applies the corrections saved in the database:
    corrected fields take their DB value and soft-deleted rows are left out of the cross-match.
    Returns (rows to cross-match, corrected count, deleted count). Rows keep their original order.
    """
    for e_data in erp_rows:
        e_data["source_key"] = source_key_for_raw(e_data, source_currency, doc_field)

    corrected = _corrected_rows_by_key(source_currency, (r["source_key"] for r in erp_rows))
    if not corrected:
        return erp_rows, 0, 0

    kept: List[Dict[str, Any]] = []
    corrected_count = deleted_count = 0
    for e_data in erp_rows:
        db_row = corrected.get(e_data["source_key"])
        if db_row is None:
            kept.append(e_data)
            continue
        if db_row.is_deleted:
            deleted_count += 1
            continue
        _overlay_db_values(e_data, db_row, doc_field)
        e_data["is_corrected"] = True
        corrected_count += 1
        kept.append(e_data)
    return kept, corrected_count, deleted_count


def _overlay_db_values(e_data: Dict[str, Any], db_row: ErpTransaction, doc_field: str) -> None:
    """Writes the DB values of the correctable fields onto a preview row, in the preview's own formats."""
    e_data["amount"] = float(db_row.amount)
    e_data["authorization_code"] = db_row.authorization_code or ""
    e_data["transaction_date"] = db_row.transaction_date.strftime("%d/%m/%Y") if db_row.transaction_date else None
    e_data["payment_code"] = db_row.payment_code or ""
    e_data[doc_field] = db_row.document_number
    if "is_reversal" in e_data:
        e_data["is_reversal"] = db_row.amount < 0


def overlay_on_instances(
    instances: List[ErpTransaction],
    source_currency: str,
) -> Tuple[List[ErpTransaction], int, int]:
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
    corrected = _corrected_rows_by_key(source_currency, (i.source_key for i in instances))
    if not corrected:
        return instances, 0, 0

    kept: List[ErpTransaction] = []
    corrected_count = deleted_count = 0
    for inst in instances:
        db_row: Optional[ErpTransaction] = corrected.get(inst.source_key)
        if db_row is None:
            kept.append(inst)
            continue
        if db_row.is_deleted:
            deleted_count += 1
            continue
        for field_name in CORRECTABLE_FIELDS:
            setattr(inst, field_name, getattr(db_row, field_name))
        inst.is_corrected = True
        corrected_count += 1
        kept.append(inst)
    return kept, corrected_count, deleted_count
