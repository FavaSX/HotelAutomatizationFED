"""
Database Persistence & Deduplication Service for USD Financial Reconciliation (Phase 1).
Delegates shared natural-key bulk deduplication and audit-log tracking to `persistence_common.py`.
"""

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from django.contrib.auth.models import User
from django.db import transaction

from ..models import (
    BankDepositValidation,
    CardOperatorTransaction,
    CardType,
    ErpTransaction,
    ReconciliationProcess,
    ReconciliationStatus,
    ReconciliationSummary,
    TransbankTransaction,
)
from ..templatetags.currency_filters import format_observation
from .column_detector import safe_date
from .persistence_common import (
    get_model_at,
    sync_bank_statement_movements,
    sync_erp_transactions_by_currency,
    sync_transbank_transactions_for_process,
    to_decimal,
    upsert_cross_match_and_audit_logs,
)


def _tbk_natural_key(t_data: Dict[str, Any], card_types: Dict[str, CardType]) -> Tuple[Any, ...]:
    c_model = card_types.get(t_data.get("card_code"), card_types.get("VI"))
    return (
        c_model.id,
        t_data.get("authorization_code", ""),
        to_decimal(t_data.get("original_sale_amount")),
        safe_date(t_data.get("sale_date")),
        t_data.get("masked_card_number", ""),
    )


def _get_or_save_usd_process(
    user: User,
    preview: Dict[str, Any],
    tbk_raw: List[Dict[str, Any]],
    existing_tbk_map: Dict[Tuple[Any, ...], TransbankTransaction],
    card_types: Dict[str, CardType],
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
    incoming_calc_deposit = to_decimal(bv.get("calculated_deposit"))
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
            ("erp", "erp_file_name"),
            ("transbank", "transbank_general_file_name"),
            ("bank", "bank_statement_file_name"),
            ("amex", "american_express_file_name"),
            ("diners", "diners_file_name"),
            ("visa", "visa_file_name"),
            ("mastercard", "mastercard_file_name"),
        ):
            if file_key in files:
                setattr(existing_process, attr_name, files[file_key])
        existing_process.save()
        return existing_process

    return ReconciliationProcess.objects.create(
        user=user,
        process_currency="USD",
        tolerance_applied=preview.get("tolerance"),
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
    bank_models = sync_bank_statement_movements(process, bv.get("bank_account_number", ""), bank_movements_raw)
    matched_idx = next((idx for idx, m in enumerate(bank_movements_raw) if m.get("is_matched_target")), None)
    matched_movement = get_model_at(bank_models, matched_idx)

    BankDepositValidation.objects.update_or_create(
        process=process,
        payment_modality=bv.get("deposit_modality", "USD_GENERAL"),
        defaults={
            "bank_movement": matched_movement,
            "transbank_calculated_deposit": Decimal(str(bv.get("calculated_deposit", 0.0))),
            "bank_statement_deposit": Decimal(str(bv.get("bank_deposit_amount", 0.0))),
            "deposit_date": safe_date(bv.get("bank_movement_date")),
            "is_matched": bv.get("is_matched", False),
        },
    )


def _sync_operator_transactions(
    process: ReconciliationProcess,
    operator_raw: List[Dict[str, Any]],
    card_types: Dict[str, CardType],
) -> List[CardOperatorTransaction]:
    """Deduplicates and bulk-syncs CardOperatorTransaction records."""
    incoming_docs = {o.get("document_number", "") for o in operator_raw}
    existing_qs = (
        CardOperatorTransaction.objects.filter(document_number__in=incoming_docs)
        if incoming_docs
        else CardOperatorTransaction.objects.none()
    )
    existing_map = {(o.card_type_id, o.document_number or "", o.sequence_number or ""): o for o in existing_qs}
    op_models, new_ops, ops_to_update = [], [], []

    for o_data in operator_raw:
        c_model = card_types.get(o_data.get("card_code"), card_types.get("VI"))
        o_doc = o_data.get("document_number", "")
        o_seq = o_data.get("sequence_number", "")
        o_auth = o_data.get("authorization_code", "")
        key = (c_model.id, o_doc, o_seq)
        if key in existing_map:
            existing_obj = existing_map[key]
            if o_auth and not existing_obj.authorization_code:
                existing_obj.authorization_code = o_auth
                ops_to_update.append(existing_obj)
            op_models.append(existing_obj)
        else:
            new_obj = CardOperatorTransaction(
                process=process,
                card_type=c_model,
                document_number=o_doc,
                sequence_number=o_seq,
                authorization_code=o_auth,
                foreign_currency_amount=to_decimal(o_data.get("foreign_currency_amount")),
                balance_amount=to_decimal(o_data.get("balance_amount")),
                corrected_balance_amount=to_decimal(o_data.get("corrected_balance_amount")),
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
    process: ReconciliationProcess,
    preview: Dict[str, Any],
    card_types: Dict[str, CardType],
) -> None:
    """Upserts USD and CLP ReconciliationSummary rows."""
    for currency_code, table_key in (("USD", "usd_table"), ("CLP", "clp_table")):
        for row in preview.get(table_key, []):
            card_model = card_types.get(row["card_code"])
            if card_model:
                ReconciliationSummary.objects.update_or_create(
                    process=process,
                    card_type=card_model,
                    payment_modality="INTERNATIONAL_USD",
                    currency_table=currency_code,
                    defaults={
                        "erp_sales_total": Decimal(str(row["erp_sales"])),
                        "transbank_sales_total": Decimal(str(row["tbk_sales"])),
                        "difference": Decimal(str(row["difference"])),
                    },
                )


def _sync_cross_match_results(
    process: ReconciliationProcess,
    user: User,
    preview: Dict[str, Any],
    erp_models: List[ErpTransaction],
    tbk_models: List[TransbankTransaction],
    operator_models: List[CardOperatorTransaction],
    card_types: Dict[str, CardType],
) -> None:
    """Syncs CrossMatchResult rows and generates AuditResolutionLog entries on status transitions."""
    pending_status = ReconciliationStatus.objects.filter(code="PENDING").first()
    accepted_status = ReconciliationStatus.objects.filter(code="ACCEPTED_ROUNDING").first()

    def _build_entry(v_dict: Dict[str, Any], sys_result: str, status_detail: str, default_status):
        tbk_obj = get_model_at(tbk_models, v_dict.get("tbk_index"))
        sug_payload = v_dict.get("suggestion")
        match_fields = {
            "process": process,
            "card_type": card_types.get(v_dict["card_code"], card_types.get("VI")),
            "erp_transaction": get_model_at(erp_models, v_dict.get("erp_index")),
            "card_operator_transaction": get_model_at(operator_models, v_dict.get("operator_index")),
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
            "system_result": sys_result,
            "system_status_detail": status_detail,
            "system_observation": format_observation(v_dict["observation"])[:255],
            "auditor_comment": json.dumps(sug_payload, ensure_ascii=False) if sug_payload else None,
        }
        return tbk_obj, match_fields, default_status

    entries = [
        *(
            _build_entry(v, "MATCHED", "Coincidencia exacta de voucher y monto", accepted_status)
            for v in preview.get("matched_vouchers", [])
        ),
        *(
            _build_entry(v, "AMOUNT_MISMATCH", f"Diferencia de USD {v['difference_usd']}", pending_status)
            for v in preview.get("discrepancy_vouchers", [])
        ),
        *(
            _build_entry(v, "NOT_FOUND_IN_ERP", f"Voucher {v['authorization_code']} no registrado en ERP", pending_status)
            for v in preview.get("orphan_vouchers", [])
        ),
    ]
    upsert_cross_match_and_audit_logs(user, tbk_models, entries, accepted_status)


def commit_usd_preview_to_database(user: User, preview: Dict[str, Any]) -> ReconciliationProcess:
    """Persists a verified in-memory USD reconciliation preview into the database."""
    with transaction.atomic():
        card_types = {card.code: card for card in CardType.objects.all()}
        tbk_raw = preview.get("transbank_transactions", [])
        incoming_tbk_auths = {t.get("authorization_code", "") for t in tbk_raw}
        existing_tbk_qs = (
            TransbankTransaction.objects.select_related("process").filter(
                process__process_currency="USD", authorization_code__in=incoming_tbk_auths
            )
            if incoming_tbk_auths
            else TransbankTransaction.objects.none()
        )
        existing_tbk_map = {
            (t.card_type_id, t.authorization_code or "", t.original_sale_amount, t.sale_date, t.masked_card_number or ""): t
            for t in existing_tbk_qs
        }

        def _card_resolver(code: Optional[str]) -> CardType:
            return card_types.get(code or "", card_types.get("VI"))

        process = _get_or_save_usd_process(user, preview, tbk_raw, existing_tbk_map, card_types)
        _sync_bank_movements_and_validation(process, preview)
        erp_models = sync_erp_transactions_by_currency(
            process, preview.get("erp_transactions", []), _card_resolver, source_currency="USD"
        )
        tbk_models = sync_transbank_transactions_for_process(
            process,
            tbk_raw,
            existing_tbk_map,
            lambda t_data: _tbk_natural_key(t_data, card_types),
            _card_resolver,
            default_location="HOTEL PLAZA SAN FRANCISCO",
        )
        operator_models = _sync_operator_transactions(process, preview.get("card_operator_transactions", []), card_types)
        _sync_summaries(process, preview, card_types)
        _sync_cross_match_results(process, user, preview, erp_models, tbk_models, operator_models, card_types)
        return process
