"""
Database Persistence & Deduplication Service for Financial Reconciliation.
Handles atomic persistence across all 8 operational tables with:
  1. Settlement-level process matching (prevents overlapping-date collisions).
  2. Targeted SQL filtering (__in) on natural unique keys.
  3. Automatic AuditResolutionLog tracking when re-uploaded files resolve discrepancies.
"""

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditResolutionLog, BankDepositValidation, BankStatementMovement,
    CardOperatorTransaction, CardType, CrossMatchResult, ErpTransaction,
    ReconciliationProcess, ReconciliationStatus, ReconciliationSummary, TransbankTransaction,
)
from ..templatetags.currency_filters import format_observation
from .column_detector import safe_date, safe_time


def _tbk_natural_key(t_data: Dict[str, Any], card_types: Dict[str, CardType]) -> Tuple[Any, ...]:
    c_model = card_types.get(t_data.get("card_code"), card_types.get("VI"))
    return (
        c_model.id, t_data.get("authorization_code", ""),
        round(Decimal(str(t_data.get("original_sale_amount", 0.0))), 2),
        safe_date(t_data.get("sale_date")), t_data.get("masked_card_number", ""),
    )


def _get_or_save_usd_process(
    user: User, preview: Dict[str, Any], tbk_raw: List[Dict[str, Any]],
    existing_tbk_map: Dict[Tuple[Any, ...], TransbankTransaction], card_types: Dict[str, CardType],
) -> ReconciliationProcess:
    """Finds an existing settlement process by voucher overlap + deposit match, or creates a new one."""
    process_voucher_counts: Dict[int, int] = {}
    candidate_processes: Dict[int, ReconciliationProcess] = {}

    for t_data in tbk_raw:
        natural_key = _tbk_natural_key(t_data, card_types)
        if natural_key in existing_tbk_map:
            proc = existing_tbk_map[natural_key].process
            process_voucher_counts[proc.id] = process_voucher_counts.get(proc.id, 0) + 1
            candidate_processes[proc.id] = proc

    existing_process: Optional[ReconciliationProcess] = None
    bv = preview.get("bank_validation", {})
    incoming_calc_deposit = round(Decimal(str(bv.get("calculated_deposit", 0.0))), 2)
    total_incoming_tbk = len(tbk_raw)

    if process_voucher_counts and total_incoming_tbk > 0:
        best_proc_id = max(process_voucher_counts, key=process_voucher_counts.get)
        matched_count = process_voucher_counts[best_proc_id]
        candidate_proc = candidate_processes[best_proc_id]
        existing_bv = BankDepositValidation.objects.filter(
            process=candidate_proc, payment_modality=bv.get("deposit_modality", "USD_GENERAL")
        ).first()
        same_deposit = existing_bv is not None and round(existing_bv.transbank_calculated_deposit, 2) == incoming_calc_deposit
        if matched_count == total_incoming_tbk or (matched_count >= max(1, int(total_incoming_tbk * 0.8)) and same_deposit):
            existing_process = candidate_proc

    files = preview.get("file_names", {})
    if existing_process is not None:
        existing_process.user = user
        existing_process.accounting_period = preview.get("accounting_period", existing_process.accounting_period)
        existing_process.tolerance_applied = preview.get("tolerance")
        for file_key, attr_name in (
            ("erp", "erp_file_name"), ("transbank", "transbank_general_file_name"),
            ("bank", "bank_statement_file_name"), ("amex", "american_express_file_name"),
            ("diners", "diners_file_name"), ("visa", "visa_file_name"), ("mastercard", "mastercard_file_name"),
        ):
            if file_key in files:
                setattr(existing_process, attr_name, files[file_key])
        existing_process.save()
        return existing_process

    return ReconciliationProcess.objects.create(
        user=user, process_currency="USD", tolerance_applied=preview.get("tolerance"),
        accounting_period=preview.get("accounting_period", "Cierre Dólares (USD)"),
        erp_file_name=files.get("erp", "TERJETAS ERP.xlsx"),
        transbank_general_file_name=files.get("transbank", "ABONO TBK.xlsx"),
        bank_statement_file_name=files.get("bank", "cartola.xls"),
        american_express_file_name=files.get("amex", "AMEX DOLAR.XLS"),
        diners_file_name=files.get("diners", "DINERS DOLAR.XLS"),
        visa_file_name=files.get("visa", "VISA DOLAR.XLS"),
        mastercard_file_name=files.get("mastercard", "MASTERCARD DOLAR.XLS"),
    )


def _sync_bank_movements_and_validation(process: ReconciliationProcess, preview: Dict[str, Any]) -> None:
    """Deduplicates BankStatementMovement rows and updates BankDepositValidation."""
    bv = preview.get("bank_validation", {})
    bank_movements_raw = preview.get("bank_movements", [])
    incoming_dates = {d for m in bank_movements_raw if (d := safe_date(m.get("movement_date"))) is not None}
    existing_qs = BankStatementMovement.objects.filter(movement_date__in=incoming_dates) if incoming_dates else BankStatementMovement.objects.none()
    existing_map = {
        (m.movement_date, m.description or "", m.document_number or "", m.charge_amount, m.deposit_amount, m.balance_amount): m
        for m in existing_qs
    }

    bank_models, new_movements, matched_idx = [], [], None
    for idx, m_data in enumerate(bank_movements_raw):
        if m_data.get("is_matched_target"):
            matched_idx = idx
        m_date = safe_date(m_data.get("movement_date"))
        m_desc, m_doc = m_data.get("description", ""), m_data.get("document_number", "")
        m_charge = round(Decimal(str(m_data.get("charge_amount", 0.0))), 2)
        m_deposit = round(Decimal(str(m_data.get("deposit_amount", 0.0))), 2)
        m_balance = round(Decimal(str(m_data.get("balance_amount", 0.0))), 2)

        key = (m_date, m_desc, m_doc, m_charge, m_deposit, m_balance)
        if key in existing_map:
            bank_models.append(existing_map[key])
        else:
            new_obj = BankStatementMovement(
                process=process, bank_account_number=bv.get("bank_account_number", ""),
                movement_date=m_date, description=m_desc, branch_or_channel=m_data.get("branch_or_channel", ""),
                document_number=m_doc, charge_amount=m_charge, deposit_amount=m_deposit,
                balance_amount=m_balance, is_transbank_deposit=m_data.get("is_transbank_deposit", False),
            )
            existing_map[key] = new_obj
            new_movements.append(new_obj)
            bank_models.append(new_obj)

    if new_movements:
        BankStatementMovement.objects.bulk_create(new_movements)

    matched_movement = bank_models[matched_idx] if matched_idx is not None and matched_idx < len(bank_models) else None
    BankDepositValidation.objects.update_or_create(
        process=process, payment_modality=bv.get("deposit_modality", "USD_GENERAL"),
        defaults={
            "bank_movement": matched_movement,
            "transbank_calculated_deposit": Decimal(str(bv.get("calculated_deposit", 0.0))),
            "bank_statement_deposit": Decimal(str(bv.get("bank_deposit_amount", 0.0))),
            "deposit_date": safe_date(bv.get("bank_movement_date")),
            "is_matched": bv.get("is_matched", False),
        },
    )


def _sync_erp_transactions(
    process: ReconciliationProcess, erp_raw: List[Dict[str, Any]], card_types: Dict[str, CardType]
) -> List[ErpTransaction]:
    """Deduplicates and bulk-syncs ErpTransaction records."""
    incoming_docs = {e.get("document_number") or "" for e in erp_raw}
    existing_qs = ErpTransaction.objects.filter(document_number__in=incoming_docs) if incoming_docs else ErpTransaction.objects.none()
    existing_map = {
        (e.document_number or "", e.invoice_number or "", e.authorization_code or "", e.amount, e.transaction_date, e.transaction_time): e
        for e in existing_qs
    }
    erp_models, new_erp, erp_to_update = [], [], []

    for e_data in erp_raw:
        e_doc, e_inv, e_auth = e_data.get("document_number") or "", e_data.get("invoice_number") or "", e_data.get("authorization_code") or ""
        e_amt = round(Decimal(str(e_data.get("amount", 0.0))), 2)
        e_date, e_time = safe_date(e_data.get("transaction_date")), safe_time(e_data.get("transaction_time"))
        e_rec = e_data.get("reconciled", False)

        key = (e_doc, e_inv, e_auth, e_amt, e_date, e_time)
        if key in existing_map:
            existing_obj = existing_map[key]
            if e_rec and not existing_obj.reconciled:
                existing_obj.reconciled = True
                erp_to_update.append(existing_obj)
            erp_models.append(existing_obj)
        else:
            new_obj = ErpTransaction(
                process=process, card_type=card_types.get(e_data.get("card_code"), card_types.get("VI")),
                room_number=e_data.get("room_number"), room_type=e_data.get("room_type"),
                reservation_number=e_data.get("reservation_number"), account_code=e_data.get("account_code"),
                payment_code=e_data.get("payment_code"), invoice_number=e_inv, document_number=e_doc,
                authorization_code=e_auth, transaction_date=e_date, transaction_time=e_time,
                cashier_username=e_data.get("cashier_username"), guest_name=e_data.get("guest_name"),
                amount=e_amt, source_currency=e_data.get("source_currency", "USD"), reconciled=e_rec,
            )
            existing_map[key] = new_obj
            new_erp.append(new_obj)
            erp_models.append(new_obj)

    if new_erp:
        ErpTransaction.objects.bulk_create(new_erp)
    if erp_to_update:
        ErpTransaction.objects.bulk_update(erp_to_update, ["reconciled"])
    return erp_models


def _sync_transbank_transactions(
    process: ReconciliationProcess, tbk_raw: List[Dict[str, Any]],
    existing_tbk_map: Dict[Tuple[Any, ...], TransbankTransaction], card_types: Dict[str, CardType],
) -> List[TransbankTransaction]:
    """Deduplicates and bulk-syncs TransbankTransaction records."""
    tbk_models, new_tbk, tbk_to_update = [], [], []
    for t_data in tbk_raw:
        c_model = card_types.get(t_data.get("card_code"), card_types.get("VI"))
        key = _tbk_natural_key(t_data, card_types)
        _, t_auth, t_amt, t_date, t_mask = key
        t_rec = t_data.get("reconciled", False)

        if key in existing_tbk_map:
            existing_tbk = existing_tbk_map[key]
            if existing_tbk.reconciled != t_rec:
                existing_tbk.reconciled = t_rec
                tbk_to_update.append(existing_tbk)
            tbk_models.append(existing_tbk)
        else:
            new_obj = TransbankTransaction(
                process=process, card_type=c_model, payment_modality=t_data.get("payment_modality", "CREDIT"),
                merchant_code=t_data.get("merchant_code", ""),
                merchant_location_name=t_data.get("merchant_location_name", "HOTEL PLAZA SAN FRANCISCO"),
                masked_card_number=t_mask, installment_type=t_data.get("installment_type", ""),
                installment_number=t_data.get("installment_number", ""),
                authorization_code=t_auth, sale_date=t_date, original_sale_amount=t_amt, reconciled=t_rec,
            )
            existing_tbk_map[key] = new_obj
            new_tbk.append(new_obj)
            tbk_models.append(new_obj)

    if new_tbk:
        TransbankTransaction.objects.bulk_create(new_tbk)
    if tbk_to_update:
        TransbankTransaction.objects.bulk_update(tbk_to_update, ["reconciled"])
    return tbk_models


def _sync_operator_transactions(
    process: ReconciliationProcess, operator_raw: List[Dict[str, Any]], card_types: Dict[str, CardType]
) -> List[CardOperatorTransaction]:
    """Deduplicates and bulk-syncs CardOperatorTransaction records."""
    incoming_docs = {o.get("document_number", "") for o in operator_raw}
    existing_qs = CardOperatorTransaction.objects.filter(document_number__in=incoming_docs) if incoming_docs else CardOperatorTransaction.objects.none()
    existing_map = {(o.card_type_id, o.document_number or "", o.sequence_number or ""): o for o in existing_qs}
    op_models, new_ops, ops_to_update = [], [], []

    for o_data in operator_raw:
        c_model = card_types.get(o_data.get("card_code"), card_types.get("VI"))
        o_doc, o_seq, o_auth = o_data.get("document_number", ""), o_data.get("sequence_number", ""), o_data.get("authorization_code", "")
        key = (c_model.id, o_doc, o_seq)
        if key in existing_map:
            existing_obj = existing_map[key]
            if o_auth and not existing_obj.authorization_code:
                existing_obj.authorization_code = o_auth
                ops_to_update.append(existing_obj)
            op_models.append(existing_obj)
        else:
            new_obj = CardOperatorTransaction(
                process=process, card_type=c_model, document_number=o_doc, sequence_number=o_seq,
                authorization_code=o_auth,
                foreign_currency_amount=round(Decimal(str(o_data.get("foreign_currency_amount", 0.0))), 2),
                balance_amount=round(Decimal(str(o_data.get("balance_amount", 0.0))), 2),
                corrected_balance_amount=round(Decimal(str(o_data.get("corrected_balance_amount", 0.0))), 2),
            )
            existing_map[key] = new_obj
            new_ops.append(new_obj)
            op_models.append(new_obj)

    if new_ops:
        CardOperatorTransaction.objects.bulk_create(new_ops)
    if ops_to_update:
        CardOperatorTransaction.objects.bulk_update(ops_to_update, ["authorization_code"])
    return op_models


def _sync_summaries(
    process: ReconciliationProcess, preview: Dict[str, Any], card_types: Dict[str, CardType]
) -> None:
    """Upserts USD and CLP ReconciliationSummary rows."""
    for currency_code, table_key in (("USD", "usd_table"), ("CLP", "clp_table")):
        for row in preview.get(table_key, []):
            card_model = card_types.get(row["card_code"])
            if card_model:
                ReconciliationSummary.objects.update_or_create(
                    process=process, card_type=card_model,
                    payment_modality="INTERNATIONAL_USD", currency_table=currency_code,
                    defaults={
                        "erp_sales_total": Decimal(str(row["erp_sales"])),
                        "transbank_sales_total": Decimal(str(row["tbk_sales"])),
                        "difference": Decimal(str(row["difference"])),
                    },
                )


def _sync_cross_match_results(
    process: ReconciliationProcess, user: User, preview: Dict[str, Any],
    erp_models: List[ErpTransaction], tbk_models: List[TransbankTransaction],
    operator_models: List[CardOperatorTransaction], card_types: Dict[str, CardType],
) -> None:
    """Syncs CrossMatchResult rows and generates AuditResolutionLog entries on status transitions."""
    pending_status = ReconciliationStatus.objects.filter(code="PENDING").first()
    accepted_status = ReconciliationStatus.objects.filter(code="ACCEPTED_ROUNDING").first()

    tbk_ids = [t.id for t in tbk_models if t and t.id]
    existing_matches_by_tbk_id = {
        m.transbank_transaction_id: m
        for m in CrossMatchResult.objects.select_related("management_status").filter(transbank_transaction_id__in=tbk_ids)
    }
    matches_to_create, matches_to_update, audit_logs_to_create, update_field_names = [], [], [], []

    def _get_model(model_list: List[Any], idx: Optional[int]) -> Optional[Any]:
        return model_list[idx] if idx is not None and 0 <= idx < len(model_list) else None

    def _upsert_match(
        v_dict: Dict[str, Any], sys_result: str, status_detail: str, default_mgmt_status: Optional[ReconciliationStatus]
    ) -> None:
        nonlocal update_field_names
        tbk_obj = _get_model(tbk_models, v_dict.get("tbk_index"))
        sug_payload = v_dict.get("suggestion")
        match_fields = {
            "process": process, "card_type": card_types.get(v_dict["card_code"], card_types.get("VI")),
            "erp_transaction": _get_model(erp_models, v_dict.get("erp_index")),
            "card_operator_transaction": _get_model(operator_models, v_dict.get("operator_index")),
            "authorization_code": v_dict["authorization_code"],
            "document_number": v_dict.get("document_number", ""),
            "sale_date": safe_date(v_dict.get("sale_date")),
            "merchant_location_name": v_dict.get("merchant_location", "HOTEL PLAZA SAN FRANCISCO"),
            "guest_name": v_dict.get("guest_name", "—"),
            "room_number": v_dict.get("room_number", "—"),
            "cashier_username": v_dict.get("cashier_username", "—"),
            "transbank_amount": Decimal(str(v_dict.get("gross_amount_usd", 0.0))),
            "operator_amount_usd": Decimal(str(v_dict.get("operator_amount_usd", 0.0))),
            "erp_amount_clp": Decimal(str(v_dict.get("erp_amount_clp", 0.0))),
            "difference_usd": Decimal(str(v_dict.get("difference_usd", 0.0))),
            "difference_clp": Decimal(str(v_dict.get("difference_clp", 0.0))),
            "system_result": sys_result, "system_status_detail": status_detail,
            "system_observation": format_observation(v_dict["observation"])[:255],
            "auditor_comment": json.dumps(sug_payload, ensure_ascii=False) if sug_payload else None,
        }
        if not update_field_names:
            update_field_names = list(match_fields.keys()) + ["management_status", "resolved_by", "resolution_date"]

        if tbk_obj and tbk_obj.id in existing_matches_by_tbk_id:
            existing_match = existing_matches_by_tbk_id[tbk_obj.id]
            prev_sys_result, prev_mgmt_status = existing_match.system_result, existing_match.management_status
            if sys_result == "MATCHED":
                new_mgmt_status = accepted_status
            elif not existing_match.management_status_id or prev_sys_result == "MATCHED":
                new_mgmt_status = default_mgmt_status
            else:
                new_mgmt_status = existing_match.management_status

            for field_name, field_val in match_fields.items():
                setattr(existing_match, field_name, field_val)
            existing_match.management_status = new_mgmt_status

            if prev_sys_result != sys_result:
                if sys_result == "MATCHED":
                    existing_match.resolved_by = user
                    existing_match.resolution_date = timezone.now()
                if new_mgmt_status is not None:
                    audit_logs_to_create.append(
                        AuditResolutionLog(
                            cross_match_result=existing_match, user=user,
                            previous_status=prev_mgmt_status, new_status=new_mgmt_status,
                            comment=f"Actualización automática al re-subir archivos de origen: {prev_sys_result} -> {sys_result} ({status_detail})",
                        )
                    )
            matches_to_update.append(existing_match)
        else:
            new_match = CrossMatchResult(transbank_transaction=tbk_obj, management_status=default_mgmt_status, **match_fields)
            if tbk_obj and tbk_obj.id:
                existing_matches_by_tbk_id[tbk_obj.id] = new_match
            matches_to_create.append(new_match)

    for v in preview.get("matched_vouchers", []):
        _upsert_match(v, "MATCHED", "Coincidencia exacta de voucher y monto", accepted_status)
    for v in preview.get("discrepancy_vouchers", []):
        _upsert_match(v, "AMOUNT_MISMATCH", f"Diferencia de USD {v['difference_usd']}", pending_status)
    for v in preview.get("orphan_vouchers", []):
        _upsert_match(v, "NOT_FOUND_IN_ERP", f"Voucher {v['authorization_code']} no registrado en ERP", pending_status)

    if matches_to_create:
        CrossMatchResult.objects.bulk_create(matches_to_create)
    if matches_to_update and update_field_names:
        CrossMatchResult.objects.bulk_update(matches_to_update, update_field_names)
    if audit_logs_to_create:
        AuditResolutionLog.objects.bulk_create(audit_logs_to_create)


def commit_usd_preview_to_database(user: User, preview: Dict[str, Any]) -> ReconciliationProcess:
    """Persists a verified in-memory USD reconciliation preview into the database."""
    with transaction.atomic():
        card_types = {card.code: card for card in CardType.objects.all()}
        tbk_raw = preview.get("transbank_transactions", [])
        incoming_tbk_auths = {t.get("authorization_code", "") for t in tbk_raw}
        existing_tbk_qs = (
            TransbankTransaction.objects.select_related("process").filter(authorization_code__in=incoming_tbk_auths)
            if incoming_tbk_auths else TransbankTransaction.objects.none()
        )
        existing_tbk_map = {
            (t.card_type_id, t.authorization_code or "", t.original_sale_amount, t.sale_date, t.masked_card_number or ""): t
            for t in existing_tbk_qs
        }

        process = _get_or_save_usd_process(user, preview, tbk_raw, existing_tbk_map, card_types)
        _sync_bank_movements_and_validation(process, preview)
        erp_models = _sync_erp_transactions(process, preview.get("erp_transactions", []), card_types)
        tbk_models = _sync_transbank_transactions(process, tbk_raw, existing_tbk_map, card_types)
        operator_models = _sync_operator_transactions(process, preview.get("card_operator_transactions", []), card_types)
        _sync_summaries(process, preview, card_types)
        _sync_cross_match_results(process, user, preview, erp_models, tbk_models, operator_models, card_types)
        return process
