"""
Database Persistence & Deduplication Service for the CLP (Pesos) Card Reconciliation.
Mirrors the USD persistence safeguards:
  1. Settlement-level process matching (re-uploading the same deposit updates its process).
  2. Targeted SQL filtering (__in) on natural unique keys.
  3. Automatic AuditResolutionLog tracking when re-uploaded files change a voucher's result.
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
    CrossMatchResult,
    AuditResolutionLog,
)
from .column_detector import safe_date, safe_time

VOUCHER_BUCKETS = ("matched_vouchers", "discrepancy_vouchers", "orphan_vouchers", "out_of_period_vouchers")


def _decimal(value: Any) -> Decimal:
    return round(Decimal(str(value or 0)), 2)


def commit_clp_preview_to_database(user: User, preview: Dict[str, Any]) -> ReconciliationProcess:
    """
    Persists a verified in-memory CLP reconciliation preview into the database,
    deduplicating bank movements, Bordero rows, Transbank vouchers, summaries and cross-match results.
    """
    with transaction.atomic():
        pending_status = ReconciliationStatus.objects.filter(code="PENDING").first()
        accepted_status = ReconciliationStatus.objects.filter(code="ACCEPTED_ROUNDING").first()
        card_types = {card.code: card for card in CardType.objects.all()}
        if not card_types:
            raise ValueError("El catálogo de tarjetas está vacío. Ejecute 'python manage.py migrate' para cargarlo.")

        def _card(code: Optional[str]) -> CardType:
            return card_types.get(code or "") or card_types.get("VI") or next(iter(card_types.values()))

        def _tbk_natural_key(t_data: Dict[str, Any]):
            return (
                _card(t_data.get("card_code")).id,
                t_data.get("authorization_code", ""),
                _decimal(t_data.get("original_sale_amount")),
                safe_date(t_data.get("sale_date")),
                t_data.get("masked_card_number", ""),
            )

        # 1. Settlement-level process matching (only against previous CLP processes)
        tbk_raw = preview.get("transbank_transactions", [])
        incoming_auths = {t.get("authorization_code", "") for t in tbk_raw}
        existing_tbk_map = {
            (t.card_type_id, t.authorization_code or "", t.original_sale_amount, t.sale_date, t.masked_card_number or ""): t
            for t in TransbankTransaction.objects.select_related("process").filter(
                process__process_currency="CLP", authorization_code__in=incoming_auths
            )
        } if incoming_auths else {}

        process_voucher_counts: Dict[int, int] = {}
        candidate_processes: Dict[int, ReconciliationProcess] = {}
        for t_data in tbk_raw:
            existing = existing_tbk_map.get(_tbk_natural_key(t_data))
            if existing:
                process_voucher_counts[existing.process_id] = process_voucher_counts.get(existing.process_id, 0) + 1
                candidate_processes[existing.process_id] = existing.process

        validations_raw = preview.get("bank_validations", [])
        incoming_deposits = sorted(_decimal(v.get("calculated_deposit")) for v in validations_raw)
        existing_process: Optional[ReconciliationProcess] = None
        if process_voucher_counts and tbk_raw:
            best_proc_id = max(process_voucher_counts, key=process_voucher_counts.get)
            matched_count = process_voucher_counts[best_proc_id]
            existing_deposits = sorted(
                round(v.transbank_calculated_deposit, 2)
                for v in BankDepositValidation.objects.filter(process_id=best_proc_id)
            )
            if matched_count == len(tbk_raw) or (
                matched_count >= max(1, int(len(tbk_raw) * 0.8)) and existing_deposits == incoming_deposits
            ):
                existing_process = candidate_processes[best_proc_id]

        files = preview.get("file_names", {})
        process_fields = {
            "user": user,
            "accounting_period": preview.get("accounting_period", "Cierre Pesos (CLP)"),
            "erp_file_name": files.get("erp"),
            "bank_statement_file_name": files.get("bank"),
            "transbank_credit_file_name": files.get("tbk_credit"),
            "transbank_debit_file_name": files.get("tbk_debit"),
            "transbank_prepaid_file_name": files.get("tbk_prepaid"),
        }
        if existing_process is not None:
            process = existing_process
            for field_name, field_val in process_fields.items():
                setattr(process, field_name, field_val)
            process.save()
        else:
            process = ReconciliationProcess.objects.create(process_currency="CLP", **process_fields)

        # 2. Bank Statement Movements & one Bank Deposit Validation per settlement
        account_number = preview.get("bank_account_number", "")
        bank_movements_raw = preview.get("bank_movements", [])
        incoming_bank_dates = {safe_date(m.get("movement_date")) for m in bank_movements_raw} - {None}
        existing_bank_map = {
            (m.movement_date, m.description or "", m.document_number or "", m.charge_amount, m.deposit_amount, m.balance_amount): m
            for m in BankStatementMovement.objects.filter(movement_date__in=incoming_bank_dates)
        } if incoming_bank_dates else {}

        bank_movement_models: List[BankStatementMovement] = []
        new_bank_movements: List[BankStatementMovement] = []
        for m_data in bank_movements_raw:
            natural_key = (
                safe_date(m_data.get("movement_date")),
                m_data.get("description", ""),
                m_data.get("document_number", ""),
                _decimal(m_data.get("charge_amount")),
                _decimal(m_data.get("deposit_amount")),
                _decimal(m_data.get("balance_amount")),
            )
            if natural_key not in existing_bank_map:
                existing_bank_map[natural_key] = BankStatementMovement(
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
                new_bank_movements.append(existing_bank_map[natural_key])
            bank_movement_models.append(existing_bank_map[natural_key])
        if new_bank_movements:
            BankStatementMovement.objects.bulk_create(new_bank_movements)

        for validation in validations_raw:
            movement_index = validation.get("bank_movement_index")
            BankDepositValidation.objects.update_or_create(
                process=process,
                payment_modality=validation["deposit_modality"],
                defaults={
                    "bank_movement": (
                        bank_movement_models[movement_index]
                        if movement_index is not None and movement_index < len(bank_movement_models)
                        else None
                    ),
                    "transbank_calculated_deposit": _decimal(validation.get("calculated_deposit")),
                    "bank_statement_deposit": _decimal(validation.get("bank_deposit_amount")),
                    "deposit_date": safe_date(validation.get("bank_movement_date")),
                    "is_matched": validation.get("is_matched", False),
                },
            )

        # 3. Bordero rows (natural key: cuenta + factura + código + monto + fecha + hora)
        erp_raw = preview.get("erp_transactions", [])
        incoming_accounts = {e.get("account_code") or "" for e in erp_raw}
        existing_erp_map = {
            (e.document_number or "", e.invoice_number or "", e.authorization_code or "", e.amount, e.transaction_date, e.transaction_time): e
            for e in ErpTransaction.objects.filter(source_currency="CLP", document_number__in=incoming_accounts)
        } if incoming_accounts else {}

        erp_models: List[ErpTransaction] = []
        new_erp_models: List[ErpTransaction] = []
        erp_to_update: List[ErpTransaction] = []
        for e_data in erp_raw:
            natural_key = (
                e_data.get("account_code") or "",
                e_data.get("invoice_number") or "",
                e_data.get("authorization_code") or "",
                _decimal(e_data.get("amount")),
                safe_date(e_data.get("transaction_date")),
                safe_time(e_data.get("transaction_time")),
            )
            existing_obj = existing_erp_map.get(natural_key)
            if existing_obj is not None:
                if e_data.get("reconciled") and not existing_obj.reconciled:
                    existing_obj.reconciled = True
                    erp_to_update.append(existing_obj)
                erp_models.append(existing_obj)
                continue
            new_obj = ErpTransaction(
                process=process,
                card_type=card_types.get(e_data.get("card_code") or ""),
                room_number=e_data.get("room_number"),
                room_type=e_data.get("room_type"),
                reservation_number=e_data.get("reservation_number"),
                account_code=natural_key[0],
                payment_code=e_data.get("payment_code"),
                invoice_number=natural_key[1],
                document_number=natural_key[0],
                authorization_code=natural_key[2],
                transaction_date=natural_key[4],
                transaction_time=natural_key[5],
                cashier_username=e_data.get("cashier_username"),
                guest_name=e_data.get("guest_name"),
                amount=natural_key[3],
                source_currency="CLP",
                reconciled=e_data.get("reconciled", False),
            )
            existing_erp_map[natural_key] = new_obj
            new_erp_models.append(new_obj)
            erp_models.append(new_obj)
        if new_erp_models:
            ErpTransaction.objects.bulk_create(new_erp_models)
        if erp_to_update:
            ErpTransaction.objects.bulk_update(erp_to_update, ["reconciled"])

        # 4. Transbank vouchers of the 3 settlements
        tbk_models: List[TransbankTransaction] = []
        new_tbk_models: List[TransbankTransaction] = []
        tbk_to_update: List[TransbankTransaction] = []
        for t_data in tbk_raw:
            natural_key = _tbk_natural_key(t_data)
            t_rec = t_data.get("reconciled", False)
            existing_tbk = existing_tbk_map.get(natural_key)
            if existing_tbk is not None:
                if existing_tbk.reconciled != t_rec:
                    existing_tbk.reconciled = t_rec
                    tbk_to_update.append(existing_tbk)
                tbk_models.append(existing_tbk)
                continue
            new_obj = TransbankTransaction(
                process=process,
                card_type=_card(t_data.get("card_code")),
                payment_modality=t_data.get("payment_modality", "CREDIT"),
                merchant_code=t_data.get("merchant_code", ""),
                merchant_location_name=t_data.get("merchant_location_name", ""),
                masked_card_number=natural_key[4],
                transaction_identifier=t_data.get("transaction_identifier", ""),
                receipt_number=t_data.get("receipt_number", ""),
                installment_type=t_data.get("installment_type", ""),
                installment_number=t_data.get("installment_number", ""),
                authorization_code=natural_key[1],
                sale_date=natural_key[3],
                original_sale_amount=natural_key[2],
                transbank_commission=_decimal(t_data.get("transbank_commission")),
                commission_vat_tax=_decimal(t_data.get("commission_vat_tax")),
                net_deposit_amount=_decimal(t_data.get("net_deposit_amount")),
                reconciled=t_rec,
            )
            existing_tbk_map[natural_key] = new_obj
            new_tbk_models.append(new_obj)
            tbk_models.append(new_obj)
        if new_tbk_models:
            TransbankTransaction.objects.bulk_create(new_tbk_models)
        if tbk_to_update:
            TransbankTransaction.objects.bulk_update(tbk_to_update, ["reconciled"])

        # 5. Summaries by payment modality + card brand
        for row in preview.get("clp_table", []):
            ReconciliationSummary.objects.update_or_create(
                process=process,
                card_type=_card(row["card_code"]),
                payment_modality=f"CLP_{row['payment_modality']}",
                currency_table="CLP",
                defaults={
                    "erp_sales_total": _decimal(row["erp_sales"]),
                    "transbank_sales_total": _decimal(row["tbk_sales"]),
                    "total_commissions": _decimal(row["commissions"]),
                    "net_deposit_total": _decimal(row["net_deposit"]),
                    "difference": _decimal(row["difference"]),
                },
            )

        # 6. Cross Match Results + AuditResolutionLog when a re-upload changes the result
        def _get_model(model_list, index_val):
            return model_list[index_val] if index_val is not None and 0 <= index_val < len(model_list) else None

        existing_matches_by_tbk_id = {
            m.transbank_transaction_id: m
            for m in CrossMatchResult.objects.select_related("management_status").filter(
                transbank_transaction_id__in=[t.id for t in tbk_models if t.id]
            )
        }
        matches_to_create: List[CrossMatchResult] = []
        matches_to_update: List[CrossMatchResult] = []
        audit_logs_to_create: List[AuditResolutionLog] = []

        for bucket in VOUCHER_BUCKETS:
            for v_dict in preview.get(bucket, []):
                sys_result = v_dict["system_result"]
                default_status = accepted_status if sys_result == "MATCHED" else pending_status
                suggestion = v_dict.get("suggestion")
                observation = v_dict.get("observation", "")
                if suggestion:
                    observation = f"{observation} | Sugerencia: {suggestion['headline']}"
                tbk_obj = _get_model(tbk_models, v_dict.get("tbk_index"))
                match_fields = {
                    "process": process,
                    "card_type": _card(v_dict.get("card_code")),
                    "erp_transaction": _get_model(erp_models, v_dict.get("erp_index")),
                    "card_operator_transaction": None,
                    "authorization_code": v_dict["authorization_code"],
                    # Bordero code as typed by the cashier (or the suggested one for orphans).
                    "document_number": v_dict.get("erp_authorization_code") or (
                        suggestion["erp_row"]["authorization_code"]
                        if suggestion and not suggestion.get("missing_code") and v_dict.get("erp_index") is None
                        else ""
                    ),
                    "sale_date": safe_date(v_dict.get("sale_date")),
                    "merchant_location_name": v_dict.get("merchant_location", ""),
                    "guest_name": v_dict.get("guest_name", "—"),
                    "room_number": v_dict.get("room_number", "—"),
                    "cashier_username": v_dict.get("cashier_username", "—"),
                    "transbank_amount": _decimal(v_dict.get("tbk_amount")),
                    "operator_amount_usd": Decimal("0"),
                    "erp_amount_clp": _decimal(v_dict.get("erp_amount")),
                    "difference_usd": Decimal("0"),
                    "difference_clp": _decimal(v_dict.get("difference")),
                    "system_result": sys_result,
                    "system_status_detail": (
                        f"{v_dict.get('modality_label', '')} · {v_dict.get('error_type', 'Cuadrado')} · "
                        f"{v_dict.get('action', 'Sin acción')}"
                    )[:255],
                    "system_observation": observation[:255],
                }

                existing_match = existing_matches_by_tbk_id.get(tbk_obj.id) if tbk_obj and tbk_obj.id else None
                if existing_match is None:
                    new_match = CrossMatchResult(transbank_transaction=tbk_obj, management_status=default_status, **match_fields)
                    if tbk_obj and tbk_obj.id:
                        existing_matches_by_tbk_id[tbk_obj.id] = new_match
                    matches_to_create.append(new_match)
                    continue

                prev_sys_result = existing_match.system_result
                prev_status = existing_match.management_status
                if sys_result == "MATCHED":
                    new_status = accepted_status
                elif not existing_match.management_status_id or prev_sys_result == "MATCHED":
                    new_status = default_status
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
                                    f"{prev_sys_result} -> {sys_result}"
                                ),
                            )
                        )
                matches_to_update.append(existing_match)

        if matches_to_create:
            CrossMatchResult.objects.bulk_create(matches_to_create)
        if matches_to_update:
            CrossMatchResult.objects.bulk_update(
                matches_to_update,
                [
                    "process", "card_type", "erp_transaction", "card_operator_transaction",
                    "authorization_code", "document_number", "sale_date", "merchant_location_name",
                    "guest_name", "room_number", "cashier_username", "transbank_amount",
                    "operator_amount_usd", "erp_amount_clp", "difference_usd", "difference_clp",
                    "system_result", "system_status_detail", "system_observation",
                    "management_status", "resolved_by", "resolution_date",
                ],
            )
        if audit_logs_to_create:
            AuditResolutionLog.objects.bulk_create(audit_logs_to_create)

        return process
