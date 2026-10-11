"""
Database Persistence & Deduplication Service for Financial Reconciliation.
Handles atomic persistence across all 8 operational tables with:
  1. Settlement-level process matching (prevents overlapping-date collisions).
  2. Targeted SQL filtering (__in) on natural unique keys.
  3. Automatic AuditResolutionLog tracking when re-uploaded files resolve discrepancies.
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User

from ..models import (
    CardType,
    ReconciliationStatus,
    ReconciliationProcess,
    BankStatementMovement,
    BankDepositValidation,
    ReconciliationSummary,
    ErpTransaction,
    TransbankTransaction,
    CardOperatorTransaction,
    CrossMatchResult,
    AuditResolutionLog,
)
from .column_detector import safe_date, safe_time


def commit_usd_preview_to_database(user: User, preview: Dict[str, Any]) -> ReconciliationProcess:
    """
    Persists a verified in-memory USD reconciliation preview into the database,
    deduplicating ALL 8 tables with 3 production-hardening safeguards:
      1. Settlement-level process matching (>=80% voucher overlap + deposit match or 100% overlap).
      2. Targeted SQL filtering (__in) instead of loading entire tables into memory.
      3. Automatic AuditResolutionLog tracking whenever a re-uploaded voucher changes status.
    """
    with transaction.atomic():
        pending_status = ReconciliationStatus.objects.filter(code="PENDING").first()
        accepted_status = ReconciliationStatus.objects.filter(code="ACCEPTED_ROUNDING").first()
        card_types = {card.code: card for card in CardType.objects.all()}

        def _tbk_natural_key(t_data: Dict[str, Any]):
            c_model = card_types.get(t_data.get("card_code"), card_types.get("VI"))
            return (
                c_model.id,
                t_data.get("authorization_code", ""),
                round(Decimal(str(t_data.get("original_sale_amount", 0.0))), 2),
                safe_date(t_data.get("sale_date")),
                t_data.get("masked_card_number", ""),
            )

        tbk_raw = preview.get("transbank_transactions", [])
        incoming_tbk_auths = {t.get("authorization_code", "") for t in tbk_raw}

        existing_tbk_qs = (
            TransbankTransaction.objects.select_related("process")
            .filter(authorization_code__in=incoming_tbk_auths)
            if incoming_tbk_auths
            else TransbankTransaction.objects.none()
        )
        existing_tbk_map = {
            (
                t.card_type_id,
                t.authorization_code or "",
                t.original_sale_amount,
                t.sale_date,
                t.masked_card_number or "",
            ): t
            for t in existing_tbk_qs
        }

        # Count how many incoming vouchers belong to each candidate process
        process_voucher_counts: Dict[int, int] = {}
        candidate_processes: Dict[int, ReconciliationProcess] = {}
        for t_data in tbk_raw:
            natural_key = _tbk_natural_key(t_data)
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
                process=candidate_proc,
                payment_modality=bv.get("deposit_modality", "USD_GENERAL"),
            ).first()
            same_deposit_total = (
                existing_bv is not None
                and round(existing_bv.transbank_calculated_deposit, 2) == incoming_calc_deposit
            )
            if matched_count == total_incoming_tbk or (
                matched_count >= max(1, int(total_incoming_tbk * 0.8)) and same_deposit_total
            ):
                existing_process = candidate_proc

        files = preview.get("file_names", {})
        if existing_process is not None:
            process = existing_process
            process.user = user
            process.accounting_period = preview.get("accounting_period", process.accounting_period)
            process.erp_file_name = files.get("erp", process.erp_file_name)
            process.transbank_general_file_name = files.get("transbank", process.transbank_general_file_name)
            process.bank_statement_file_name = files.get("bank", process.bank_statement_file_name)
            process.american_express_file_name = files.get("amex", process.american_express_file_name)
            process.diners_file_name = files.get("diners", process.diners_file_name)
            process.visa_file_name = files.get("visa", process.visa_file_name)
            process.mastercard_file_name = files.get("mastercard", process.mastercard_file_name)
            process.save()
        else:
            process = ReconciliationProcess.objects.create(
                user=user,
                process_currency="USD",
                accounting_period=preview.get("accounting_period", "Cierre Dólares (USD)"),
                erp_file_name=files.get("erp", "TERJETAS ERP.xlsx"),
                transbank_general_file_name=files.get("transbank", "ABONO TBK.xlsx"),
                bank_statement_file_name=files.get("bank", "cartola.xls"),
                american_express_file_name=files.get("amex", "AMEX DOLAR.XLS"),
                diners_file_name=files.get("diners", "DINERS DOLAR.XLS"),
                visa_file_name=files.get("visa", "VISA DOLAR.XLS"),
                mastercard_file_name=files.get("mastercard", "MASTERCARD DOLAR.XLS"),
            )

        # 1. Bank Statement Movements (Targeted filter by movement_date__in) & Bank Deposit Validation
        bank_movements_raw = preview.get("bank_movements", [])
        incoming_bank_dates = {
            safe_date(m.get("movement_date"))
            for m in bank_movements_raw
            if safe_date(m.get("movement_date")) is not None
        }
        existing_bank_qs = (
            BankStatementMovement.objects.filter(movement_date__in=incoming_bank_dates)
            if incoming_bank_dates
            else BankStatementMovement.objects.none()
        )
        existing_bank_map = {
            (
                m.movement_date,
                m.description or "",
                m.document_number or "",
                m.charge_amount,
                m.deposit_amount,
                m.balance_amount,
            ): m
            for m in existing_bank_qs
        }
        bank_movement_models: List[BankStatementMovement] = []
        new_bank_movements: List[BankStatementMovement] = []
        matched_target_index: Optional[int] = None

        for idx, m_data in enumerate(bank_movements_raw):
            if m_data.get("is_matched_target"):
                matched_target_index = idx
            m_date = safe_date(m_data.get("movement_date"))
            m_desc = m_data.get("description", "")
            m_doc = m_data.get("document_number", "")
            m_charge = round(Decimal(str(m_data.get("charge_amount", 0.0))), 2)
            m_deposit = round(Decimal(str(m_data.get("deposit_amount", 0.0))), 2)
            m_balance = round(Decimal(str(m_data.get("balance_amount", 0.0))), 2)

            natural_key = (m_date, m_desc, m_doc, m_charge, m_deposit, m_balance)
            if natural_key in existing_bank_map:
                bank_movement_models.append(existing_bank_map[natural_key])
            else:
                new_obj = BankStatementMovement(
                    process=process,
                    bank_account_number=bv.get("bank_account_number", ""),
                    movement_date=m_date,
                    description=m_desc,
                    branch_or_channel=m_data.get("branch_or_channel", ""),
                    document_number=m_doc,
                    charge_amount=m_charge,
                    deposit_amount=m_deposit,
                    balance_amount=m_balance,
                    is_transbank_deposit=m_data.get("is_transbank_deposit", False),
                )
                existing_bank_map[natural_key] = new_obj
                new_bank_movements.append(new_obj)
                bank_movement_models.append(new_obj)

        if new_bank_movements:
            BankStatementMovement.objects.bulk_create(new_bank_movements)

        matched_bank_movement = (
            bank_movement_models[matched_target_index]
            if matched_target_index is not None and matched_target_index < len(bank_movement_models)
            else None
        )

        BankDepositValidation.objects.update_or_create(
            process=process,
            payment_modality=bv.get("deposit_modality", "USD_GENERAL"),
            defaults={
                "bank_movement": matched_bank_movement,
                "transbank_calculated_deposit": Decimal(str(bv.get("calculated_deposit", 0.0))),
                "bank_statement_deposit": Decimal(str(bv.get("bank_deposit_amount", 0.0))),
                "deposit_date": safe_date(bv.get("bank_movement_date")),
                "is_matched": bv.get("is_matched", False),
            },
        )

        # 2A. ERP Transactions (Targeted filter by document_number__in)
        erp_raw = preview.get("erp_transactions", [])
        incoming_erp_docs = {e.get("document_number") or "" for e in erp_raw}
        existing_erp_qs = (
            ErpTransaction.objects.filter(document_number__in=incoming_erp_docs)
            if incoming_erp_docs
            else ErpTransaction.objects.none()
        )
        existing_erp_map = {
            (
                e.document_number or "",
                e.invoice_number or "",
                e.authorization_code or "",
                e.amount,
                e.transaction_date,
                e.transaction_time,
            ): e
            for e in existing_erp_qs
        }
        erp_models: List[ErpTransaction] = []
        new_erp_models: List[ErpTransaction] = []
        erp_to_update: List[ErpTransaction] = []

        for e_data in erp_raw:
            e_doc = e_data.get("document_number") or ""
            e_inv = e_data.get("invoice_number") or ""
            e_auth = e_data.get("authorization_code") or ""
            e_amt = round(Decimal(str(e_data.get("amount", 0.0))), 2)
            e_date = safe_date(e_data.get("transaction_date"))
            e_time = safe_time(e_data.get("transaction_time"))
            e_rec = e_data.get("reconciled", False)

            natural_key = (e_doc, e_inv, e_auth, e_amt, e_date, e_time)
            if natural_key in existing_erp_map:
                existing_obj = existing_erp_map[natural_key]
                if e_rec and not existing_obj.reconciled:
                    existing_obj.reconciled = True
                    erp_to_update.append(existing_obj)
                erp_models.append(existing_obj)
            else:
                new_obj = ErpTransaction(
                    process=process,
                    card_type=card_types.get(e_data.get("card_code"), card_types.get("VI")),
                    room_number=e_data.get("room_number"),
                    room_type=e_data.get("room_type"),
                    reservation_number=e_data.get("reservation_number"),
                    account_code=e_data.get("account_code"),
                    payment_code=e_data.get("payment_code"),
                    invoice_number=e_inv,
                    document_number=e_doc,
                    authorization_code=e_auth,
                    transaction_date=e_date,
                    transaction_time=e_time,
                    cashier_username=e_data.get("cashier_username"),
                    guest_name=e_data.get("guest_name"),
                    amount=e_amt,
                    source_currency=e_data.get("source_currency", "USD"),
                    reconciled=e_rec,
                )
                existing_erp_map[natural_key] = new_obj
                new_erp_models.append(new_obj)
                erp_models.append(new_obj)

        if new_erp_models:
            ErpTransaction.objects.bulk_create(new_erp_models)
        if erp_to_update:
            ErpTransaction.objects.bulk_update(erp_to_update, ["reconciled"])

        # 2B. Transbank Transactions (Deduplicated by CardType + Auth + Amount + Date + MaskedCard)
        tbk_models: List[TransbankTransaction] = []
        new_tbk_models: List[TransbankTransaction] = []
        tbk_to_update: List[TransbankTransaction] = []

        for t_data in tbk_raw:
            c_model = card_types.get(t_data.get("card_code"), card_types.get("VI"))
            natural_key = _tbk_natural_key(t_data)
            _, t_auth, t_amt, t_date, t_mask = natural_key
            t_rec = t_data.get("reconciled", False)

            if natural_key in existing_tbk_map:
                existing_tbk = existing_tbk_map[natural_key]
                if existing_tbk.reconciled != t_rec:
                    existing_tbk.reconciled = t_rec
                    tbk_to_update.append(existing_tbk)
                tbk_models.append(existing_tbk)
            else:
                new_obj = TransbankTransaction(
                    process=process,
                    card_type=c_model,
                    payment_modality=t_data.get("payment_modality", "CREDIT"),
                    merchant_code=t_data.get("merchant_code", ""),
                    merchant_location_name=t_data.get("merchant_location_name", "HOTEL PLAZA SAN FRANCISCO"),
                    masked_card_number=t_mask,
                    installment_type=t_data.get("installment_type", ""),
                    installment_number=t_data.get("installment_number", ""),
                    authorization_code=t_auth,
                    sale_date=t_date,
                    original_sale_amount=t_amt,
                    reconciled=t_rec,
                )
                existing_tbk_map[natural_key] = new_obj
                new_tbk_models.append(new_obj)
                tbk_models.append(new_obj)

        if new_tbk_models:
            TransbankTransaction.objects.bulk_create(new_tbk_models)
        if tbk_to_update:
            TransbankTransaction.objects.bulk_update(tbk_to_update, ["reconciled"])

        # 2C. Card Operator Transactions (Targeted filter by document_number__in)
        operator_raw = preview.get("card_operator_transactions", [])
        incoming_op_docs = {o.get("document_number", "") for o in operator_raw}
        existing_op_qs = (
            CardOperatorTransaction.objects.filter(document_number__in=incoming_op_docs)
            if incoming_op_docs
            else CardOperatorTransaction.objects.none()
        )
        existing_operator_map = {
            (
                o.card_type_id,
                o.document_number or "",
                o.sequence_number or "",
            ): o
            for o in existing_op_qs
        }
        operator_models: List[CardOperatorTransaction] = []
        new_operator_models: List[CardOperatorTransaction] = []
        operators_to_update: List[CardOperatorTransaction] = []

        for o_data in operator_raw:
            c_model = card_types.get(o_data.get("card_code"), card_types.get("VI"))
            o_doc = o_data.get("document_number", "")
            o_seq = o_data.get("sequence_number", "")
            o_auth = o_data.get("authorization_code", "")

            natural_key = (c_model.id, o_doc, o_seq)
            if natural_key in existing_operator_map:
                existing_obj = existing_operator_map[natural_key]
                if o_auth and not existing_obj.authorization_code:
                    existing_obj.authorization_code = o_auth
                    operators_to_update.append(existing_obj)
                operator_models.append(existing_obj)
            else:
                new_obj = CardOperatorTransaction(
                    process=process,
                    card_type=c_model,
                    document_number=o_doc,
                    sequence_number=o_seq,
                    authorization_code=o_auth,
                    foreign_currency_amount=round(Decimal(str(o_data.get("foreign_currency_amount", 0.0))), 2),
                    balance_amount=round(Decimal(str(o_data.get("balance_amount", 0.0))), 2),
                    corrected_balance_amount=round(Decimal(str(o_data.get("corrected_balance_amount", 0.0))), 2),
                )
                existing_operator_map[natural_key] = new_obj
                new_operator_models.append(new_obj)
                operator_models.append(new_obj)

        if new_operator_models:
            CardOperatorTransaction.objects.bulk_create(new_operator_models)
        if operators_to_update:
            CardOperatorTransaction.objects.bulk_update(operators_to_update, ["authorization_code"])

        # 3. Reconciliation Summaries (Deduplicated by process + card_type + payment_modality + currency_table)
        for currency_code, table_key in [("USD", "usd_table"), ("CLP", "clp_table")]:
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

        def _get_model(model_list, index_val):
            return model_list[index_val] if index_val is not None and 0 <= index_val < len(model_list) else None

        # 4. Cross Match Results (Targeted filter by transbank_transaction_id__in + AuditResolutionLog)
        tbk_ids = [t.id for t in tbk_models if t and t.id]
        existing_matches_by_tbk_id = {
            m.transbank_transaction_id: m
            for m in CrossMatchResult.objects.select_related("management_status").filter(
                transbank_transaction_id__in=tbk_ids
            )
        }
        matches_to_create: List[CrossMatchResult] = []
        matches_to_update: List[CrossMatchResult] = []
        audit_logs_to_create: List[AuditResolutionLog] = []

        from ..templatetags.currency_filters import format_observation

        def _upsert_match(v_dict, sys_result, status_detail, default_mgmt_status):
            tbk_obj = _get_model(tbk_models, v_dict.get("tbk_index"))
            match_fields = {
                "process": process,
                "card_type": card_types.get(v_dict["card_code"], card_types.get("VI")),
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
                "system_result": sys_result,
                "system_status_detail": status_detail,
                "system_observation": format_observation(v_dict["observation"])[:255],
            }

            if tbk_obj and tbk_obj.id in existing_matches_by_tbk_id:
                existing_match = existing_matches_by_tbk_id[tbk_obj.id]
                prev_sys_result = existing_match.system_result
                prev_mgmt_status = existing_match.management_status

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
                                cross_match_result=existing_match,
                                user=user,
                                previous_status=prev_mgmt_status,
                                new_status=new_mgmt_status,
                                comment=(
                                    f"Actualización automática al re-subir archivos de origen: "
                                    f"{prev_sys_result} -> {sys_result} ({status_detail})"
                                ),
                            )
                        )

                matches_to_update.append(existing_match)
            else:
                new_match = CrossMatchResult(
                    transbank_transaction=tbk_obj,
                    management_status=default_mgmt_status,
                    **match_fields,
                )
                if tbk_obj and tbk_obj.id:
                    existing_matches_by_tbk_id[tbk_obj.id] = new_match
                matches_to_create.append(new_match)

        for v in preview.get("matched_vouchers", []):
            _upsert_match(v, "MATCHED", "Coincidencia exacta de voucher y monto", accepted_status)

        for v in preview.get("discrepancy_vouchers", []):
            _upsert_match(v, "AMOUNT_MISMATCH", f"Diferencia de USD {v['difference_usd']}", pending_status)

        for v in preview.get("orphan_vouchers", []):
            _upsert_match(
                v,
                "NOT_FOUND_IN_ERP",
                f"Voucher {v['authorization_code']} no registrado en ERP",
                pending_status,
            )

        if matches_to_create:
            CrossMatchResult.objects.bulk_create(matches_to_create)
        if matches_to_update:
            CrossMatchResult.objects.bulk_update(
                matches_to_update,
                [
                    "process",
                    "card_type",
                    "erp_transaction",
                    "card_operator_transaction",
                    "authorization_code",
                    "document_number",
                    "sale_date",
                    "merchant_location_name",
                    "guest_name",
                    "room_number",
                    "cashier_username",
                    "transbank_amount",
                    "operator_amount_usd",
                    "erp_amount_clp",
                    "difference_usd",
                    "difference_clp",
                    "system_result",
                    "system_status_detail",
                    "system_observation",
                    "management_status",
                    "resolved_by",
                    "resolution_date",
                ],
            )
        if audit_logs_to_create:
            AuditResolutionLog.objects.bulk_create(audit_logs_to_create)

        return process
