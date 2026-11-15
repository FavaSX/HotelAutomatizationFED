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
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from django.db.models import Q

from ..models import CrossMatchResult, ErpTransaction, ErpTransactionChange
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
    amount_dec = round(amount, 2) if isinstance(amount, Decimal) else round(Decimal(str(amount or 0)), 2)
    date_val = (
        transaction_date
        if isinstance(transaction_date, date) and not isinstance(transaction_date, datetime)
        else (safe_date(transaction_date) if transaction_date not in (None, "") else None)
    )
    time_val = (
        transaction_time
        if isinstance(transaction_time, dt_time)
        else (safe_time(transaction_time) if transaction_time not in (None, "") else None)
    )
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
            .select_related("card_type", "process")
            .distinct()
        )
        for row in rows:
            result[row.source_key] = row
    return result


def _reconciled_rows_by_key(source_currency: str, keys: Iterable[str]) -> Dict[str, ErpTransaction]:
    """Active ERP rows already reconciled in DB for these fingerprints."""
    keys = [k for k in set(keys) if k]
    if not keys:
        return {}
    result: Dict[str, ErpTransaction] = {}
    for start in range(0, len(keys), 900):
        chunk = keys[start : start + 900]
        rows = (
            ErpTransaction.objects.filter(source_currency=source_currency, source_key__in=chunk, reconciled=True)
            .select_related("card_type", "process")
            .order_by("id")
        )
        for row in rows:
            result.setdefault(row.source_key, row)
    return result


def modified_excel_rows_by_current_key(
    source_currency: Optional[str] = None,
) -> Dict[str, ErpTransaction]:
    """
    Maps current-value natural keys -> active modified ORIGIN_EXCEL DB rows whose
    editable fields diverged from their immutable upload `source_key`.
    """
    qs = (
        ErpTransaction.objects.filter(origin=ErpTransaction.ORIGIN_EXCEL, changes__isnull=False)
        .select_related("card_type", "process")
        .order_by("id")
        .distinct()
    )
    if source_currency:
        qs = qs.filter(source_currency=source_currency)

    result: Dict[str, ErpTransaction] = {}
    for row in qs:
        curr_key = erp_source_key(
            row.source_currency,
            row.document_number or row.account_code or "",
            row.invoice_number,
            row.authorization_code,
            row.amount,
            row.transaction_date,
            row.transaction_time,
        )
        if curr_key and curr_key != row.source_key:
            result.setdefault(curr_key, row)
    return result


def _sale_key_for_match(m: CrossMatchResult) -> Optional[Tuple[str, str]]:
    tbk = m.transbank_transaction
    if not tbk:
        return None
    return (
        tbk.sale_date.isoformat() if tbk.sale_date else "",
        normalize_auth_code(tbk.authorization_code or ""),
    )


def _match_claims_secondary_row(
    m: CrossMatchResult,
    row_info: Dict[str, Any],
    row_norm: str,
) -> bool:
    """True when `m` (already MATCHED) owns `row_info` as a secondary split/reversal ERP row."""
    if m.erp_transaction_id == row_info["id"] or not m.erp_transaction or not m.transbank_transaction:
        return False
    if m.erp_transaction.source_currency != row_info["source_currency"]:
        return False
    m_norm = normalize_auth_code(m.authorization_code or m.erp_transaction.authorization_code or "")
    if not m_norm or m_norm != row_norm:
        return False

    m_acc = (m.account_code or "").strip()
    m_obs = (m.system_observation or "").lower()
    m_tokens = {tok.strip() for tok in m_acc.split("/") if tok.strip()}
    row_acc = (row_info["account_code"] or "").strip()
    row_doc = (row_info["document_number"] or "").strip()
    row_keys = {k for k in (row_acc, row_doc) if k}
    m_primary_acc = (m.erp_transaction.account_code or m.erp_transaction.document_number or "").strip()
    same_process = row_info["process_id"] in (m.process_id, m.erp_transaction.process_id)

    # USD split-invoice matches store `"acc1 / acc2"` in `account_code` (up to 50 chars).
    if len(m_tokens) > 1 and (row_keys & m_tokens):
        return True
    if "voucher dividido en" in m_obs:
        return same_process or bool(m_primary_acc and m_primary_acc in row_keys)

    # CLP split-folio / reversal matches group reconciled rows sharing account_code under one primary FK.
    if "cuadrado sumando" in m_obs and m_primary_acc and m_primary_acc in row_keys:
        return True
    return False


def active_claimed_sales_by_erp_id(erp_ids: Iterable[int]) -> Dict[int, Set[Tuple[str, str]]]:
    """
    Returns {erp_id: {(tbk_sale_date_iso, norm_tbk_auth), ...}} for active matched
    CrossMatchResult records claiming those ERP rows (both primary FK and split/reversal siblings).
    """
    clean_ids = sorted({eid for eid in erp_ids if eid})
    if not clean_ids:
        return {}

    matches = (
        CrossMatchResult.objects.filter(system_result="MATCHED", erp_transaction_id__in=clean_ids)
        .select_related("transbank_transaction")
    )
    claimed: Dict[int, Set[Tuple[str, str]]] = {}
    for m in matches:
        if not m.erp_transaction_id:
            continue
        sale_key = _sale_key_for_match(m)
        if sale_key is not None:
            claimed.setdefault(m.erp_transaction_id, set()).add(sale_key)

    remaining_ids = [eid for eid in clean_ids if eid not in claimed]
    if remaining_ids:
        secondary_rows = list(
            ErpTransaction.objects.filter(id__in=remaining_ids, reconciled=True).values(
                "id", "source_currency", "authorization_code", "account_code", "document_number", "process_id"
            )
        )
        rows_with_norm: List[Tuple[Dict[str, Any], str]] = []
        currencies: Set[str] = set()
        needed_norms: List[str] = []
        for row_info in secondary_rows:
            norm = normalize_auth_code(row_info["authorization_code"] or "")
            if norm:
                rows_with_norm.append((row_info, norm))
                currencies.add(row_info["source_currency"])
                if norm not in needed_norms:
                    needed_norms.append(norm)

        if rows_with_norm and needed_norms:
            split_matches_by_key: Dict[Tuple[str, str], List[CrossMatchResult]] = {}
            for start in range(0, len(needed_norms), 100):
                chunk = needed_norms[start : start + 100]
                q_auth = Q()
                for code in chunk:
                    q_auth |= (
                        Q(authorization_code__icontains=code)
                        | Q(erp_transaction__authorization_code__icontains=code)
                        | Q(transbank_transaction__authorization_code__icontains=code)
                    )
                cand_qs = (
                    CrossMatchResult.objects.filter(
                        system_result="MATCHED",
                        erp_transaction_id__isnull=False,
                        erp_transaction__source_currency__in=currencies,
                    )
                    .filter(q_auth)
                    .select_related("transbank_transaction", "erp_transaction")
                )
                for m in cand_qs:
                    if not m.erp_transaction:
                        continue
                    m_norm = normalize_auth_code(m.authorization_code or m.erp_transaction.authorization_code or "")
                    if m_norm:
                        split_matches_by_key.setdefault((m.erp_transaction.source_currency, m_norm), []).append(m)

            for row_info, row_norm in rows_with_norm:
                for m in split_matches_by_key.get((row_info["source_currency"], row_norm), ()):
                    if _match_claims_secondary_row(m, row_info, row_norm):
                        sale_key = _sale_key_for_match(m)
                        if sale_key is not None:
                            claimed.setdefault(row_info["id"], set()).add(sale_key)

    return claimed


def _manual_rows_for_codes(source_currency: str, voucher_codes: Optional[Set[str]]) -> List[ErpTransaction]:
    """Active hand-added rows whose authorization code belongs to one of the uploaded vouchers."""
    clean_codes = {c for c in (voucher_codes or set()) if c}
    if not clean_codes:
        return []
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
        "db_id": row.id,
        "db_process_id": row.process_id,
        "db_reconciled": bool(row.reconciled),
        "is_manual": True,
    }


def _should_include_manual_row(
    row: ErpTransaction,
    claimed_sales: Set[Tuple[str, str]],
    tbk_sale_keys: Optional[Set[Tuple[str, str]]],
    later_installment_codes: Optional[Set[str]],
) -> bool:
    if not row.reconciled or not claimed_sales:
        return True
    if tbk_sale_keys is None:
        return True
    if claimed_sales & tbk_sale_keys:
        return True
    if later_installment_codes:
        norm = normalize_auth_code(row.authorization_code or "")
        if norm and norm in later_installment_codes:
            return True
    return False


def _stamp_db_claim_dict(
    e_data: Dict[str, Any],
    db_row: ErpTransaction,
    claimed_by_id: Dict[int, Set[Tuple[str, str]]],
) -> None:
    claimed_sales = claimed_by_id.get(db_row.id, ())
    e_data["db_id"] = db_row.id
    e_data["db_process_id"] = db_row.process_id
    e_data["db_reconciled"] = bool(db_row.reconciled and claimed_sales)
    e_data["db_claimed_sales"] = sorted(claimed_sales)


def _stamp_db_claim_instance(
    inst: ErpTransaction,
    db_row: ErpTransaction,
    claimed_by_id: Dict[int, Set[Tuple[str, str]]],
) -> None:
    claimed_sales = set(claimed_by_id.get(db_row.id, ()))
    inst.db_id = db_row.id
    inst.db_process_id = db_row.process_id
    inst._db_id = db_row.id
    inst._db_process_id = db_row.process_id
    inst._db_reconciled = bool(db_row.reconciled and claimed_sales)
    inst._db_claimed_sales = claimed_sales


def apply_saved_corrections(
    erp_rows: List[Dict[str, Any]],
    source_currency: str,
    doc_field: str = "document_number",
    voucher_codes: Optional[Set[str]] = None,
    tbk_sale_keys: Optional[Set[Tuple[str, str]]] = None,
    later_installment_codes: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Stamps every preview row with its `source_key`, applies the corrections saved in the database
    (corrected fields take their DB value, soft-deleted rows leave the cross-match), and appends the
    hand-added rows of the uploaded vouchers. Returns (rows to cross-match, report for the preview).
    """
    for e_data in erp_rows:
        e_data["source_key"] = source_key_for_raw(e_data, source_currency, doc_field)

    incoming_raw_keys = {r["source_key"] for r in erp_rows if r.get("source_key")}
    modified_curr = modified_excel_rows_by_current_key(source_currency)
    for e_data in erp_rows:
        raw_key = e_data.get("source_key")
        curr_db = modified_curr.get(raw_key) if raw_key else None
        if curr_db is not None and curr_db.source_key not in incoming_raw_keys:
            e_data["raw_source_key"] = raw_key
            e_data["source_key"] = curr_db.source_key

    all_keys = {r["source_key"] for r in erp_rows if r.get("source_key")}
    corrected = _corrected_rows_by_key(source_currency, all_keys)
    reconciled_map = _reconciled_rows_by_key(source_currency, all_keys - set(corrected.keys()))
    manual_rows = _manual_rows_for_codes(source_currency, voucher_codes)

    claim_lookup_ids = [
        r.id
        for r in (list(corrected.values()) + list(reconciled_map.values()) + manual_rows)
        if not r.is_deleted and (r.reconciled or r.origin == ErpTransaction.ORIGIN_MANUAL)
    ]
    claimed_by_id = active_claimed_sales_by_erp_id(claim_lookup_ids)

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

    kept: List[Dict[str, Any]] = []
    for e_data in erp_rows:
        skey = e_data["source_key"]
        db_row = corrected.get(skey)
        if db_row is not None:
            if db_row.is_deleted:
                report["deleted"] += 1
                continue
            _overlay_db_values(e_data, db_row, doc_field)
            _stamp_db_claim_dict(e_data, db_row, claimed_by_id)
            e_data["is_corrected"] = True
            report["corrected"] += 1
            kept.append(e_data)
            continue

        rec_row = reconciled_map.get(skey)
        if rec_row is not None:
            _stamp_db_claim_dict(e_data, rec_row, claimed_by_id)
        kept.append(e_data)

    for row in manual_rows:
        row_claims = claimed_by_id.get(row.id, set())
        if not _should_include_manual_row(row, row_claims, tbk_sale_keys, later_installment_codes):
            continue
        raw_manual = _clp_raw_from_model(row)
        raw_manual["db_reconciled"] = bool(row.reconciled and row_claims)
        raw_manual["db_claimed_sales"] = sorted(row_claims)
        kept.append(raw_manual)
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
    tbk_sale_keys: Optional[Set[Tuple[str, str]]] = None,
) -> Tuple[List[ErpTransaction], Dict[str, Any]]:
    """Same as `apply_saved_corrections`, for the unsaved ErpTransaction instances of the USD flow."""
    for inst in instances:
        if not getattr(inst, "source_key", None):
            inst.source_key = erp_source_key(
                source_currency,
                inst.document_number or inst.account_code or "",
                inst.invoice_number,
                inst.authorization_code,
                inst.amount,
                inst.transaction_date,
                inst.transaction_time,
            )

    incoming_raw_keys = {i.source_key for i in instances if getattr(i, "source_key", None)}
    modified_curr = modified_excel_rows_by_current_key(source_currency)
    for inst in instances:
        curr_db = modified_curr.get(inst.source_key) if inst.source_key else None
        if curr_db is not None and curr_db.source_key not in incoming_raw_keys:
            inst._raw_source_key = inst.source_key
            inst.source_key = curr_db.source_key

    all_keys = {i.source_key for i in instances if getattr(i, "source_key", None)}
    corrected = _corrected_rows_by_key(source_currency, all_keys)
    reconciled_map = _reconciled_rows_by_key(source_currency, all_keys - set(corrected.keys()))
    manual_rows = _manual_rows_for_codes(source_currency, voucher_codes)

    claim_lookup_ids = [
        r.id
        for r in (list(corrected.values()) + list(reconciled_map.values()) + manual_rows)
        if not r.is_deleted and (r.reconciled or r.origin == ErpTransaction.ORIGIN_MANUAL)
    ]
    claimed_by_id = active_claimed_sales_by_erp_id(claim_lookup_ids)

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

    kept: List[ErpTransaction] = []
    for inst in instances:
        db_row: Optional[ErpTransaction] = corrected.get(inst.source_key)
        if db_row is not None:
            if db_row.is_deleted:
                report["deleted"] += 1
                continue
            for field_name in CORRECTABLE_FIELDS:
                setattr(inst, field_name, getattr(db_row, field_name))
            inst.card_type = db_row.card_type
            inst.account_code = db_row.account_code or db_row.document_number
            _stamp_db_claim_instance(inst, db_row, claimed_by_id)
            inst.is_corrected = True
            report["corrected"] += 1
            kept.append(inst)
            continue

        rec_row = reconciled_map.get(inst.source_key)
        if rec_row is not None:
            _stamp_db_claim_instance(inst, rec_row, claimed_by_id)
        kept.append(inst)

    for row in manual_rows:
        row_claims = claimed_by_id.get(row.id, set())
        if not _should_include_manual_row(row, row_claims, tbk_sale_keys, None):
            continue
        _stamp_db_claim_instance(row, row, claimed_by_id)
        row.source_file = MANUAL_SOURCE_LABEL
        kept.append(row)
        report["manual"] += 1
    return kept, report
