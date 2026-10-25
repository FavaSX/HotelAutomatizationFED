"""
Database Persistence & Deduplication Service for the CLP (Pesos) Card Reconciliation (Phase 2).
Delegates shared natural-key bulk deduplication and audit-log tracking to `persistence_common.py`.
"""

import json
from decimal import Decimal
from typing import Any, Dict, Optional
from django.contrib.auth.models import User
from django.db import transaction

from ..models import (
    BankDepositValidation,
    CardType,
    ReconciliationProcess,
    ReconciliationStatus,
    ReconciliationSummary,
    TransbankTransaction,
)
from .column_detector import safe_date
from .persistence_common import (
    get_model_at,
    sync_bank_statement_movements,
    sync_erp_transactions_by_currency,
    sync_transbank_transactions_for_process,
    to_decimal,
    upsert_cross_match_and_audit_logs,
)

VOUCHER_BUCKETS = ("matched_vouchers", "discrepancy_vouchers", "orphan_vouchers", "out_of_period_vouchers")


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
                to_decimal(t_data.get("original_sale_amount")),
                safe_date(t_data.get("sale_date")),
                t_data.get("masked_card_number", ""),
                t_data.get("installment_number") or "",
            )

        # 1. Settlement-level process matching (only against previous CLP processes)
        tbk_raw = preview.get("transbank_transactions", [])
        incoming_auths = {t.get("authorization_code", "") for t in tbk_raw}
        existing_tbk_map = {
            (
                t.card_type_id,
                t.authorization_code or "",
                t.original_sale_amount,
                t.sale_date,
                t.masked_card_number or "",
                t.installment_number or "",
            ): t
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

        validations_raw = [
            v for v in preview.get("bank_validations", [])
            if v.get("calculated_deposit") or v.get("total_sales") or v.get("bank_deposit_amount")
        ]
        incoming_deposits = sorted(to_decimal(v.get("calculated_deposit")) for v in validations_raw)
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
            "tolerance_applied": preview.get("tolerance"),
            "erp_file_name": files.get("erp"),
            "bank_statement_file_name": files.get("bank"),
            "transbank_credit_file_name": files.get("tbk_credit"),
            "transbank_debit_file_name": files.get("tbk_debit"),
            "transbank_prepaid_file_name": files.get("tbk_prepaid"),
        }
        if existing_process is not None:
            for attr, val in process_fields.items():
                setattr(existing_process, attr, val)
            existing_process.save()
            process = existing_process
        else:
            process = ReconciliationProcess.objects.create(process_currency="CLP", **process_fields)

        # 2. Bank statement movements & the 3 Transbank deposit validations
        account_number = preview.get("bank_account_number", "")
        bank_movement_models = sync_bank_statement_movements(
            process, account_number, preview.get("bank_movements", [])
        )

        for validation in validations_raw:
            movement_index = validation.get("bank_movement_index")
            BankDepositValidation.objects.update_or_create(
                process=process,
                payment_modality=validation["deposit_modality"],
                defaults={
                    "bank_movement": get_model_at(bank_movement_models, movement_index),
                    "transbank_calculated_deposit": to_decimal(validation.get("calculated_deposit")),
                    "bank_statement_deposit": to_decimal(validation.get("bank_deposit_amount")),
                    "deposit_date": safe_date(validation.get("bank_movement_date")),
                    "is_matched": validation.get("is_matched", False),
                },
            )

        # 3. Bordero rows (natural key: cuenta + factura + código + monto + fecha + hora)
        erp_models = sync_erp_transactions_by_currency(
            process,
            preview.get("erp_transactions", []),
            lambda code: card_types.get(code or ""),
            source_currency="CLP",
            doc_field="account_code",
        )

        # 4. Transbank vouchers of the 3 settlements
        tbk_models = sync_transbank_transactions_for_process(
            process, tbk_raw, existing_tbk_map, _tbk_natural_key, _card
        )

        # 5. Summaries by payment modality + card brand
        for row in preview.get("clp_table", []):
            ReconciliationSummary.objects.update_or_create(
                process=process,
                card_type=_card(row["card_code"]),
                payment_modality=f"CLP_{row['payment_modality']}",
                currency_table="CLP",
                defaults={
                    "erp_sales_total": to_decimal(row["erp_sales"]),
                    "transbank_sales_total": to_decimal(row["tbk_sales"]),
                    "total_commissions": to_decimal(row["commissions"]),
                    "net_deposit_total": to_decimal(row["net_deposit"]),
                    "difference": to_decimal(row["difference"]),
                },
            )

        # 6. Cross Match Results + AuditResolutionLog when a re-upload changes the result
        prepared_entries = []
        for bucket in VOUCHER_BUCKETS:
            for v_dict in preview.get(bucket, []):
                sys_result = v_dict["system_result"]
                default_status = accepted_status if sys_result == "MATCHED" else pending_status
                suggestion = v_dict.get("suggestion")
                observation = v_dict.get("observation", "")
                if suggestion:
                    observation = f"{observation} | Sugerencia: {suggestion['headline']}"
                tbk_obj = get_model_at(tbk_models, v_dict.get("tbk_index"))
                match_fields = {
                    "process": process,
                    "card_type": _card(v_dict.get("card_code")),
                    "erp_transaction": get_model_at(erp_models, v_dict.get("erp_index")),
                    "card_operator_transaction": None,
                    "authorization_code": v_dict["authorization_code"],
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
                    "transbank_amount": to_decimal(v_dict.get("tbk_amount")),
                    "operator_amount_usd": Decimal("0"),
                    "erp_amount_clp": to_decimal(v_dict.get("erp_amount")),
                    "difference_usd": Decimal("0"),
                    "difference_clp": to_decimal(v_dict.get("difference")),
                    "system_result": sys_result,
                    "system_status_detail": (
                        f"{v_dict.get('modality_label', '')} · {v_dict.get('error_type', 'Cuadrado')} · "
                        f"{v_dict.get('action', 'Sin acción')}"
                    )[:255],
                    "system_observation": observation[:255],
                    "auditor_comment": json.dumps(suggestion, ensure_ascii=False) if suggestion else None,
                }
                prepared_entries.append((tbk_obj, match_fields, default_status))

        upsert_cross_match_and_audit_logs(user, tbk_models, prepared_entries, accepted_status)
        return process
