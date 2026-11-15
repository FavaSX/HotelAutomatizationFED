"""
Shared Database Persistence & Deduplication Primitives for Financial Reconciliation (USD & CLP).
Centralizes natural-key bulk deduplication for:
  1. BankStatementMovement
  2. ErpTransaction
  3. TransbankTransaction
  4. CrossMatchResult + AuditResolutionLog state-transition tracking
"""

from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from django.contrib.auth.models import User
from django.utils import timezone

from ..models import (
    AuditResolutionLog,
    BankStatementMovement,
    CardType,
    CrossMatchResult,
    ErpTransaction,
    ErpTransactionChange,
    ReconciliationProcess,
    ReconciliationStatus,
    TransbankTransaction,
)
from .column_detector import safe_date, safe_time, safe_decimal
from .erp_corrections import (
    active_claimed_sales_by_erp_id,
    modified_excel_rows_by_current_key,
    source_key_for_raw,
)
from .excel_parsers import get_additional_erp_movements


def _modified_erp_ids(erp_ids: Sequence[int]) -> Set[int]:
    ids = [eid for eid in erp_ids if eid]
    if not ids:
        return set()
    return set(
        ErpTransactionChange.objects.filter(erp_transaction_id__in=ids)
        .values_list("erp_transaction_id", flat=True)
    )


def get_protected_tbk_match_ids(
    tbk_ids: Sequence[int],
    target_process_id: Optional[int] = None,
) -> Set[int]:
    """
    Returns TransbankTransaction IDs whose existing MATCHED CrossMatchResult must NOT be
    downgraded or overwritten by a raw re-upload (manual rows, manually edited ERP rows,
    manually RESOLVED_* cases, or cross-closing matches owned by another closing).
    Soft-deleted ERP links are intentionally NOT protected so deletions trigger recalculation.
    """
    ids = [tid for tid in tbk_ids if tid]
    if not ids:
        return set()

    matches = list(
        CrossMatchResult.objects.filter(
            transbank_transaction_id__in=ids,
            system_result="MATCHED",
        ).select_related("management_status")
    )
    if not matches:
        return set()

    all_erp_ids: Set[int] = {m.erp_transaction_id for m in matches if m.erp_transaction_id}
    active_erps = ErpTransaction.objects.in_bulk(all_erp_ids) if all_erp_ids else {}
    modified_ids = _modified_erp_ids(list(active_erps.keys()))

    protected: Set[int] = set()
    for m in matches:
        erp_row = active_erps.get(m.erp_transaction_id) if m.erp_transaction_id else None
        if erp_row is None:
            continue

        status_code = m.management_status.code if m.management_status else ""
        if status_code.startswith("RESOLVED_"):
            protected.add(m.transbank_transaction_id)
            continue

        if (
            erp_row.origin == ErpTransaction.ORIGIN_MANUAL
            or erp_row.id in modified_ids
            or (target_process_id is not None and erp_row.process_id != target_process_id)
        ):
            protected.add(m.transbank_transaction_id)

    return protected


def to_decimal(value: Any) -> Decimal:
    """Safely converts a numeric/string value into a 2-decimal-place Decimal using ROUND_HALF_UP."""
    from decimal import ROUND_HALF_UP
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, float):
        d = Decimal(f"{value:.6f}")
    else:
        d = Decimal(str(value or 0))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_model_at(model_list: List[Any], index_val: Optional[int]) -> Optional[Any]:
    """Safely indexes a list of model instances by position."""
    return model_list[index_val] if index_val is not None and 0 <= index_val < len(model_list) else None


def sync_bank_statement_movements(
    process: ReconciliationProcess,
    account_number: str,
    movements_raw: List[Dict[str, Any]],
) -> List[BankStatementMovement]:
    """
    Deduplicates and bulk-creates BankStatementMovement rows using their natural key
    scoped by process currency:
    (movement_date, description, document_number, charge_amount, deposit_amount, balance_amount).
    Re-links existing movements to `process` so they remain visible in the new closing's Cartola tab.
    """
    incoming_dates = {d for m in movements_raw if (d := safe_date(m.get("movement_date"))) is not None}
    existing_qs = (
        BankStatementMovement.objects.filter(
            process__process_currency=process.process_currency,
            movement_date__in=incoming_dates,
        )
        if incoming_dates
        else BankStatementMovement.objects.none()
    )
    existing_map = {
        (m.movement_date, m.description or "", m.document_number or "", m.charge_amount, m.deposit_amount, m.balance_amount): m
        for m in existing_qs
    }

    bank_models: List[BankStatementMovement] = []
    new_movements: List[BankStatementMovement] = []
    movements_to_update: List[BankStatementMovement] = []
    seen_update_ids: set = set()

    for m_data in movements_raw:
        natural_key = (
            safe_date(m_data.get("movement_date")),
            m_data.get("description", ""),
            m_data.get("document_number", ""),
            to_decimal(m_data.get("charge_amount")),
            to_decimal(m_data.get("deposit_amount")),
            to_decimal(m_data.get("balance_amount")),
        )
        existing_obj = existing_map.get(natural_key)
        if existing_obj is None:
            new_obj = BankStatementMovement(
                process=process,
                bank_account_number=account_number,
                movement_date=natural_key[0],
                description=natural_key[1],
                branch_or_channel=m_data.get("branch_or_channel", ""),
                document_number=natural_key[2],
                charge_amount=natural_key[3],
                deposit_amount=natural_key[4],
                balance_amount=natural_key[5],
                is_transbank_deposit=m_data.get("is_transbank_deposit", False),
            )
            existing_map[natural_key] = new_obj
            new_movements.append(new_obj)
            bank_models.append(new_obj)
        else:
            changed = False
            if existing_obj.process_id is None:
                existing_obj.process = process
                changed = True
            if account_number and not existing_obj.bank_account_number:
                existing_obj.bank_account_number = account_number
                changed = True
            if changed and existing_obj.pk and existing_obj.pk not in seen_update_ids:
                seen_update_ids.add(existing_obj.pk)
                movements_to_update.append(existing_obj)
            bank_models.append(existing_obj)

    if new_movements:
        BankStatementMovement.objects.bulk_create(new_movements)
    if movements_to_update:
        BankStatementMovement.objects.bulk_update(movements_to_update, ["process", "bank_account_number"])
    return bank_models


def _apply_existing_erp_updates(
    obj: ErpTransaction,
    *,
    process: ReconciliationProcess,
    rec_val: bool,
    desc_val: Optional[str],
    modified_erp_ids: Set[int],
    actively_claimed_ids: Set[int],
    seen_erp_update_ids: Set[int],
    erp_to_update: List[ErpTransaction],
) -> None:
    """Updates reconciliation/ownership/description on an existing ERP row without erasing manual edits."""
    is_editable_origin = (
        obj.origin == ErpTransaction.ORIGIN_EXCEL
        and obj.id not in modified_erp_ids
    )
    can_adopt_cross_currency = (
        not obj.reconciled
        and is_editable_origin
        and obj.process_id != process.id
        and obj.process.process_currency != process.process_currency
        and obj.source_currency == process.process_currency
    )
    changed = False
    if rec_val and not obj.reconciled:
        obj.reconciled = True
        # Preserve original closing ownership for same-currency rows; adopt only auxiliary cross-currency rows.
        if can_adopt_cross_currency:
            obj.process = process
        changed = True
    elif not rec_val and obj.reconciled and obj.id not in actively_claimed_ids:
        # Heal orphaned/stale reconciled=True flags when no active MATCHED CrossMatchResult claims this row.
        obj.reconciled = False
        changed = True
    elif not obj.reconciled and can_adopt_cross_currency:
        obj.process = process
        changed = True
    if desc_val and not obj.payment_description and is_editable_origin:
        obj.payment_description = desc_val
        changed = True
    if changed and obj.pk and obj.pk not in seen_erp_update_ids:
        seen_erp_update_ids.add(obj.pk)
        erp_to_update.append(obj)


def sync_erp_transactions_by_currency(
    process: ReconciliationProcess,
    erp_raw: List[Dict[str, Any]],
    card_resolver: Callable[[Optional[str]], Optional[CardType]],
    source_currency: str,
    doc_field: str = "document_number",
) -> List[ErpTransaction]:
    """
    Deduplicates and bulk-syncs ErpTransaction rows for a specific currency ('USD' or 'CLP') by their
    fingerprint (`source_key`): the natural key (document, invoice, authorization, amount, date, time) of
    the ORIGINAL Excel row. A row the accountant corrected is still found, and is never overwritten by the
    Excel values; a soft-deleted row is found too and stays deleted.
    """
    db_row_ids = [
        e.get("db_id") or e.get("manual_id")
        for e in erp_raw
        if e.get("db_id") or e.get("manual_id")
    ]
    existing_by_id = (
        ErpTransaction.all_objects.select_related("process").in_bulk(db_row_ids)
        if db_row_ids
        else {}
    )
    incoming_keys = [
        source_key_for_raw(e, source_currency, doc_field)
        for e in erp_raw
        if not e.get("manual_id") and not e.get("db_id")
    ]
    unique_keys = list(dict.fromkeys(k for k in incoming_keys if k))
    existing_map: Dict[str, ErpTransaction] = {}
    for start in range(0, len(unique_keys), 900):
        chunk = unique_keys[start : start + 900]
        qs = (
            ErpTransaction.all_objects.select_related("process")
            .filter(source_currency=source_currency, source_key__in=chunk)
            .order_by("id")
        )
        for existing in qs:
            existing_map.setdefault(existing.source_key, existing)  # oldest wins if old uploads left duplicates

    modified_curr_map = modified_excel_rows_by_current_key(source_currency)
    candidate_objs = list(existing_by_id.values()) + list(existing_map.values()) + list(modified_curr_map.values())
    candidate_ids = [obj.id for obj in candidate_objs if obj.id]
    modified_erp_ids = _modified_erp_ids(candidate_ids)
    stale_reconciled_ids = [obj.id for obj in candidate_objs if obj.id and obj.reconciled]
    actively_claimed_ids = (
        {eid for eid, sales in active_claimed_sales_by_erp_id(stale_reconciled_ids).items() if sales}
        if stale_reconciled_ids
        else set()
    )

    erp_models: List[Optional[ErpTransaction]] = []
    new_erp: List[ErpTransaction] = []
    erp_to_update: List[ErpTransaction] = []
    seen_erp_update_ids: set = set()

    for e_data in erp_raw:
        rec_val = e_data.get("reconciled", False)
        desc_val = (e_data.get("payment_description") or "").strip()[:100] or None

        db_id_val = e_data.get("db_id") or e_data.get("manual_id")
        linked_obj = existing_by_id.get(db_id_val) if db_id_val else None
        if linked_obj is not None:
            if linked_obj.is_deleted:
                erp_models.append(None)
                continue
            _apply_existing_erp_updates(
                linked_obj,
                process=process,
                rec_val=rec_val,
                desc_val=desc_val,
                modified_erp_ids=modified_erp_ids,
                actively_claimed_ids=actively_claimed_ids,
                seen_erp_update_ids=seen_erp_update_ids,
                erp_to_update=erp_to_update,
            )
            erp_models.append(linked_obj)
            continue

        doc_val = e_data.get(doc_field) or e_data.get("account_code") or ""
        inv_val = e_data.get("invoice_number") or ""
        auth_val = e_data.get("authorization_code") or ""
        amt_val = to_decimal(e_data.get("amount"))
        date_val = safe_date(e_data.get("transaction_date"))
        time_val = safe_time(e_data.get("transaction_time"))

        natural_key = source_key_for_raw(e_data, source_currency, doc_field)
        existing_obj = existing_map.get(natural_key) or modified_curr_map.get(natural_key)
        if existing_obj is not None:
            if existing_obj.is_deleted:
                erp_models.append(None)
                continue
            _apply_existing_erp_updates(
                existing_obj,
                process=process,
                rec_val=rec_val,
                desc_val=desc_val,
                modified_erp_ids=modified_erp_ids,
                actively_claimed_ids=actively_claimed_ids,
                seen_erp_update_ids=seen_erp_update_ids,
                erp_to_update=erp_to_update,
            )
            erp_models.append(existing_obj)
            continue

        new_obj = ErpTransaction(
            process=process,
            card_type=card_resolver(e_data.get("card_code")),
            room_number=e_data.get("room_number"),
            room_type=e_data.get("room_type"),
            reservation_number=e_data.get("reservation_number"),
            account_code=e_data.get("account_code") or doc_val,
            payment_code=e_data.get("payment_code"),
            payment_description=desc_val,
            invoice_number=inv_val,
            document_number=doc_val,
            authorization_code=auth_val,
            transaction_date=date_val,
            transaction_time=time_val,
            cashier_username=e_data.get("cashier_username"),
            guest_name=e_data.get("guest_name"),
            amount=amt_val,
            source_currency=e_data.get("source_currency", source_currency),
            reconciled=rec_val,
            origin=ErpTransaction.ORIGIN_EXCEL,
            source_key=natural_key,
        )
        existing_map[natural_key] = new_obj
        new_erp.append(new_obj)
        erp_models.append(new_obj)

    if new_erp:
        ErpTransaction.objects.bulk_create(new_erp, batch_size=1000)
    if erp_to_update:
        ErpTransaction.objects.bulk_update(
            erp_to_update, ["process", "reconciled", "payment_description"], batch_size=1000
        )
    return erp_models


def sync_additional_erp_movements(
    process: ReconciliationProcess,
    token: Optional[str],
    card_types: Dict[str, CardType],
) -> int:
    """
    Bulk-syncs all remaining Bordero movements (other currency's cards + non-card hotel movements
    such as HOA, IVA, BAR, PROP, E$, EUS$) into ErpTransaction without duplicating existing rows
    or altering historical closings, edits, or soft-deletes.
    """
    additional_rows = get_additional_erp_movements(token)
    if not additional_rows:
        return 0

    unique_keys = list(dict.fromkeys(r.get("source_key") for r in additional_rows if r.get("source_key")))
    existing_by_key: Dict[str, ErpTransaction] = {}
    for start in range(0, len(unique_keys), 900):
        chunk = unique_keys[start : start + 900]
        for existing in ErpTransaction.all_objects.filter(source_key__in=chunk).order_by("id"):
            existing_by_key.setdefault(existing.source_key, existing)

    modified_curr_map = modified_excel_rows_by_current_key()
    modified_ids = _modified_erp_ids([obj.id for obj in existing_by_key.values() if obj.id])

    new_erp: List[ErpTransaction] = []
    to_update: List[ErpTransaction] = []
    seen_update_ids: set = set()
    card_get = card_types.get
    proc_currency = process.process_currency

    for m_data in additional_rows:
        skey = m_data.get("source_key")
        if not skey:
            continue
        existing_obj = existing_by_key.get(skey) or modified_curr_map.get(skey)
        if existing_obj is not None:
            if (
                not existing_obj.is_deleted
                and existing_obj.origin == ErpTransaction.ORIGIN_EXCEL
                and existing_obj.id not in modified_ids
                and not existing_obj.payment_description
            ):
                desc_val = (m_data.get("payment_description") or "").strip()[:100] or None
                if desc_val and existing_obj.pk and existing_obj.pk not in seen_update_ids:
                    existing_obj.payment_description = desc_val
                    seen_update_ids.add(existing_obj.pk)
                    to_update.append(existing_obj)
            continue

        desc_val = (m_data.get("payment_description") or "").strip()[:100] or None
        doc_val = m_data.get("document_number") or m_data.get("account_code") or ""
        card_code = m_data.get("card_code") or ""
        tx_date_val = m_data.get("_date_obj")
        if tx_date_val is None and m_data.get("transaction_date") is not None:
            tx_date_val = safe_date(m_data.get("transaction_date"))
        tx_time_val = m_data.get("_time_obj")
        if tx_time_val is None and m_data.get("transaction_time") is not None:
            tx_time_val = safe_time(m_data.get("transaction_time"))

        new_obj = ErpTransaction(
            process=process,
            card_type=card_get(card_code) if card_code else None,
            room_number=m_data.get("room_number"),
            room_type=m_data.get("room_type"),
            reservation_number=m_data.get("reservation_number"),
            account_code=m_data.get("account_code") or doc_val,
            payment_code=m_data.get("payment_code"),
            payment_description=desc_val,
            invoice_number=m_data.get("invoice_number") or "",
            document_number=doc_val,
            authorization_code=m_data.get("authorization_code") or "",
            transaction_date=tx_date_val,
            transaction_time=tx_time_val,
            cashier_username=m_data.get("cashier_username"),
            guest_name=m_data.get("guest_name"),
            amount=to_decimal(m_data.get("amount")),
            source_currency=m_data.get("source_currency") or proc_currency,
            reconciled=False,
            origin=ErpTransaction.ORIGIN_EXCEL,
            source_key=skey,
        )
        existing_by_key[skey] = new_obj
        new_erp.append(new_obj)

    if new_erp:
        ErpTransaction.objects.bulk_create(new_erp, batch_size=1000)
    if to_update:
        ErpTransaction.objects.bulk_update(to_update, ["payment_description"], batch_size=1000)
    return len(new_erp)


def sync_transbank_transactions_for_process(
    process: ReconciliationProcess,
    tbk_raw: List[Dict[str, Any]],
    existing_tbk_map: Dict[Tuple[Any, ...], TransbankTransaction],
    natural_key_fn: Callable[[Dict[str, Any]], Tuple[Any, ...]],
    card_resolver: Callable[[Optional[str]], CardType],
    default_location: str = "",
) -> List[TransbankTransaction]:
    """
    Deduplicates and bulk-syncs TransbankTransaction rows using the caller-provided natural_key_fn.
    """
    protected_tbk_ids = get_protected_tbk_match_ids(
        [t.id for t in existing_tbk_map.values() if t and t.id],
        target_process_id=process.id,
    )
    tbk_models: List[TransbankTransaction] = []
    new_tbk: List[TransbankTransaction] = []
    tbk_to_update: List[TransbankTransaction] = []
    seen_tbk_update_ids: set = set()

    for t_data in tbk_raw:
        natural_key = natural_key_fn(t_data)
        t_rec = t_data.get("reconciled", False)
        existing_tbk = existing_tbk_map.get(natural_key)
        if existing_tbk is not None:
            changed = False
            if existing_tbk.reconciled != t_rec and not (
                existing_tbk.reconciled and existing_tbk.id in protected_tbk_ids
            ):
                existing_tbk.reconciled = t_rec
                changed = True
            if existing_tbk.process_id is None:
                existing_tbk.process = process
                changed = True
            if changed and existing_tbk.pk and existing_tbk.pk not in seen_tbk_update_ids:
                seen_tbk_update_ids.add(existing_tbk.pk)
                tbk_to_update.append(existing_tbk)
            tbk_models.append(existing_tbk)
            continue

        new_obj = TransbankTransaction(
            process=process,
            card_type=card_resolver(t_data.get("card_code")),
            payment_modality=t_data.get("payment_modality", "CREDIT"),
            merchant_code=t_data.get("merchant_code", ""),
            merchant_location_name=t_data.get("merchant_location_name", default_location),
            masked_card_number=natural_key[4],
            transaction_identifier=t_data.get("transaction_identifier", ""),
            receipt_number=t_data.get("receipt_number", ""),
            installment_type=t_data.get("installment_type", ""),
            installment_number=t_data.get("installment_number", ""),
            authorization_code=natural_key[1],
            sale_date=natural_key[3],
            original_sale_amount=natural_key[2],
            transbank_commission=to_decimal(t_data.get("transbank_commission")),
            commission_vat_tax=to_decimal(t_data.get("commission_vat_tax")),
            net_deposit_amount=to_decimal(t_data.get("net_deposit_amount")),
            reconciled=t_rec,
        )
        existing_tbk_map[natural_key] = new_obj
        new_tbk.append(new_obj)
        tbk_models.append(new_obj)

    if new_tbk:
        TransbankTransaction.objects.bulk_create(new_tbk)
    if tbk_to_update:
        TransbankTransaction.objects.bulk_update(tbk_to_update, ["process", "reconciled"])
    return tbk_models


def upsert_cross_match_and_audit_logs(
    user: User,
    tbk_models: List[TransbankTransaction],
    prepared_entries: Iterable[Tuple[Optional[TransbankTransaction], Dict[str, Any], Optional[ReconciliationStatus]]],
    accepted_status: Optional[ReconciliationStatus],
    target_process_id: Optional[int] = None,
) -> Set[int]:
    """
    Bulk-creates or bulk-updates CrossMatchResult records keyed by transbank_transaction_id,
    and emits AuditResolutionLog records whenever a re-upload transitions a voucher's system_result.
    Preserves protected matches (manual ERP rows/edits, RESOLVED_* cases, cross-closing matches).
    """
    tbk_ids = [t.id for t in tbk_models if t and t.id]
    protected_tbk_ids = get_protected_tbk_match_ids(tbk_ids, target_process_id=target_process_id)
    existing_matches_by_tbk_id = {
        m.transbank_transaction_id: m
        for m in CrossMatchResult.objects.select_related("management_status").filter(transbank_transaction_id__in=tbk_ids)
    }

    matches_to_create: List[CrossMatchResult] = []
    matches_to_update: List[CrossMatchResult] = []
    audit_logs_to_create: List[AuditResolutionLog] = []
    update_field_names: List[str] = []

    for tbk_obj, match_fields, default_mgmt_status in prepared_entries:
        sys_result = match_fields["system_result"]
        status_detail = match_fields.get("system_status_detail", "")
        if not update_field_names:
            update_field_names = list(match_fields.keys()) + ["management_status", "resolved_by", "resolution_date"]

        existing_match = existing_matches_by_tbk_id.get(tbk_obj.id) if tbk_obj and tbk_obj.id else None
        if existing_match is None:
            new_match = CrossMatchResult(
                transbank_transaction=tbk_obj,
                management_status=default_mgmt_status,
                **match_fields,
            )
            if tbk_obj and tbk_obj.id:
                existing_matches_by_tbk_id[tbk_obj.id] = new_match
            matches_to_create.append(new_match)
            continue

        if tbk_obj and tbk_obj.id in protected_tbk_ids:
            incoming_erp = match_fields.get("erp_transaction")
            incoming_erp_id = incoming_erp.id if incoming_erp is not None else match_fields.get("erp_transaction_id")
            if sys_result != "MATCHED" or (
                existing_match.erp_transaction_id
                and incoming_erp_id != existing_match.erp_transaction_id
            ):
                continue

        prev_sys_result = existing_match.system_result
        prev_status = existing_match.management_status
        if sys_result == "MATCHED":
            new_status = prev_status if prev_sys_result == "MATCHED" and prev_status else accepted_status
        elif not existing_match.management_status_id or prev_sys_result == "MATCHED":
            new_status = default_mgmt_status
        else:
            new_status = existing_match.management_status

        for field_name, field_val in match_fields.items():
            setattr(existing_match, field_name, field_val)
        existing_match.management_status = new_status

        if prev_sys_result != sys_result:
            if sys_result == "MATCHED":
                existing_match.resolved_by = user
                existing_match.resolution_date = timezone.now()
            if new_status is not None:
                audit_logs_to_create.append(
                    AuditResolutionLog(
                        cross_match_result=existing_match,
                        user=user,
                        previous_status=prev_status,
                        new_status=new_status,
                        comment=(
                            f"Actualización automática al re-subir archivos de origen: "
                            f"{prev_sys_result} -> {sys_result} ({status_detail})"
                        )[:255],
                    )
                )
        matches_to_update.append(existing_match)

    if matches_to_create:
        CrossMatchResult.objects.bulk_create(matches_to_create)
    if matches_to_update and update_field_names:
        CrossMatchResult.objects.bulk_update(matches_to_update, update_field_names)
    if audit_logs_to_create:
        AuditResolutionLog.objects.bulk_create(audit_logs_to_create)
    return protected_tbk_ids

