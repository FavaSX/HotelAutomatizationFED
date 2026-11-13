"""
Shared Database Persistence & Deduplication Primitives for Financial Reconciliation (USD & CLP).
Centralizes natural-key bulk deduplication for:
  1. BankStatementMovement
  2. ErpTransaction
  3. TransbankTransaction
  4. CrossMatchResult + AuditResolutionLog state-transition tracking
"""

from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from django.contrib.auth.models import User
from django.utils import timezone

from ..models import (
    AuditResolutionLog,
    BankStatementMovement,
    CardType,
    CrossMatchResult,
    ErpTransaction,
    ReconciliationProcess,
    ReconciliationStatus,
    TransbankTransaction,
)
from .column_detector import safe_date, safe_time
from .erp_corrections import source_key_for_raw
from .excel_parsers import get_additional_erp_movements


def to_decimal(value: Any) -> Decimal:
    """Safely converts a numeric/string value into a 2-decimal-place Decimal."""
    return round(Decimal(str(value or 0)), 2)


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
            if existing_obj.process_id != process.id:
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

    erp_models: List[ErpTransaction] = []
    new_erp: List[ErpTransaction] = []
    erp_to_update: List[ErpTransaction] = []
    seen_erp_update_ids: set = set()

    for e_data in erp_raw:
        rec_val = e_data.get("reconciled", False)
        desc_val = (e_data.get("payment_description") or "").strip()[:100] or None

        db_id_val = e_data.get("db_id") or e_data.get("manual_id")
        linked_obj = existing_by_id.get(db_id_val) if db_id_val else None
        if linked_obj is not None:
            is_manual = bool(e_data.get("manual_id"))
            can_adopt_cross_currency = (
                linked_obj.process_id != process.id
                and linked_obj.process.process_currency != process.process_currency
                and linked_obj.source_currency == process.process_currency
            )
            changed = False
            if rec_val and not linked_obj.reconciled:
                linked_obj.reconciled = True
                if is_manual or can_adopt_cross_currency:
                    linked_obj.process = process
                changed = True
            elif not linked_obj.reconciled and linked_obj.process_id != process.id:
                if is_manual or can_adopt_cross_currency:
                    linked_obj.process = process
                    changed = True
            if desc_val and not linked_obj.payment_description:
                linked_obj.payment_description = desc_val
                changed = True
            if changed and linked_obj.pk and linked_obj.pk not in seen_erp_update_ids:
                seen_erp_update_ids.add(linked_obj.pk)
                erp_to_update.append(linked_obj)
            erp_models.append(linked_obj)
            continue

        doc_val = e_data.get(doc_field) or e_data.get("account_code") or ""
        inv_val = e_data.get("invoice_number") or ""
        auth_val = e_data.get("authorization_code") or ""
        amt_val = to_decimal(e_data.get("amount"))
        date_val = safe_date(e_data.get("transaction_date"))
        time_val = safe_time(e_data.get("transaction_time"))

        natural_key = source_key_for_raw(e_data, source_currency, doc_field)
        existing_obj = existing_map.get(natural_key)
        if existing_obj is not None:
            can_adopt_cross_currency = (
                existing_obj.process_id != process.id
                and existing_obj.process.process_currency != process.process_currency
                and existing_obj.source_currency == process.process_currency
            )
            changed = False
            if rec_val and not existing_obj.reconciled:
                existing_obj.reconciled = True
                # Preserve original closing ownership for same-currency rows; adopt only auxiliary cross-currency rows.
                if can_adopt_cross_currency:
                    existing_obj.process = process
                changed = True
            elif not existing_obj.reconciled and can_adopt_cross_currency:
                existing_obj.process = process
                changed = True
            if desc_val and not existing_obj.payment_description:
                existing_obj.payment_description = desc_val
                changed = True
            if changed and existing_obj.pk and existing_obj.pk not in seen_erp_update_ids:
                seen_erp_update_ids.add(existing_obj.pk)
                erp_to_update.append(existing_obj)
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

    new_erp: List[ErpTransaction] = []
    to_update: List[ErpTransaction] = []
    seen_update_ids: set = set()
    card_get = card_types.get
    proc_currency = process.process_currency

    for m_data in additional_rows:
        skey = m_data.get("source_key")
        if not skey:
            continue
        existing_obj = existing_by_key.get(skey)
        if existing_obj is not None:
            if not existing_obj.payment_description:
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
            if existing_tbk.reconciled != t_rec:
                existing_tbk.reconciled = t_rec
                changed = True
            if existing_tbk.process_id != process.id:
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
) -> None:
    """
    Bulk-creates or bulk-updates CrossMatchResult records keyed by transbank_transaction_id,
    and emits AuditResolutionLog records whenever a re-upload transitions a voucher's system_result.
    """
    tbk_ids = [t.id for t in tbk_models if t and t.id]
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

