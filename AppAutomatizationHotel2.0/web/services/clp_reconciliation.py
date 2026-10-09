"""
Phase 2: Chilean Pesos (CLP) Financial Reconciliation Service.
Orchestrates the 5-file CLP reconciliation (ERP Bordero + 3 Transbank files + Bank Statement)
using BIN detection (Column 24) and hybrid column resolution.
"""

from decimal import Decimal
from typing import Any, Dict, Tuple
import pandas as pd
from django.db import transaction
from django.contrib.auth.models import User

from ..models import (
    CardType,
    ReconciliationStatus,
    SystemConfiguration,
    ReconciliationProcess,
    ReconciliationSummary,
    ErpTransaction,
    TransbankTransaction,
    CrossMatchResult,
)
from .column_detector import (
    TRANSBANK_CLP_COLUMN_RULES,
    resolve_dataframe_columns,
    clean_code_string,
    safe_decimal,
    safe_date,
    safe_cell,
    identify_card_brand_from_bin,
)
from .excel_parsers import (
    parse_and_validate_bank_deposits,
    parse_erp_transactions,
)


def execute_clp_reconciliation(
    user: User,
    erp_bordero_file,
    transbank_credit_file,
    transbank_debit_file,
    transbank_prepaid_file,
    bank_statement_file,
    accounting_period: str = ""
) -> Tuple[ReconciliationProcess, Dict[str, Any]]:
    """
    Executes the complete Chilean Pesos reconciliation workflow (Phase 2).
    """
    with transaction.atomic():
        config = SystemConfiguration.objects.first()
        clp_tolerance = config.clp_rounding_tolerance if config else Decimal("5.00")
        pending_status = ReconciliationStatus.objects.filter(code="PENDING").first()
        accepted_rounding_status = ReconciliationStatus.objects.filter(code="ACCEPTED_ROUNDING").first()
        card_types = {card.code: card for card in CardType.objects.all()}

        for uploaded_file in [erp_bordero_file, transbank_credit_file, transbank_debit_file, transbank_prepaid_file, bank_statement_file]:
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)

        bordero_dataframe = pd.read_excel(erp_bordero_file)
        credit_dataframe = pd.read_excel(transbank_credit_file)
        debit_dataframe = pd.read_excel(transbank_debit_file)
        prepaid_dataframe = pd.read_excel(transbank_prepaid_file)
        bank_dataframe = pd.read_excel(bank_statement_file)

        process = ReconciliationProcess.objects.create(
            user=user,
            process_currency="CLP",
            accounting_period=accounting_period or "Cierre Pesos (CLP)",
            erp_file_name=getattr(erp_bordero_file, "name", "BORDERO.XLS"),
            transbank_credit_file_name=getattr(transbank_credit_file, "name", "CREDITOS TBK.xlsx"),
            transbank_debit_file_name=getattr(transbank_debit_file, "name", "DEBITOS TBK.xls"),
            transbank_prepaid_file_name=getattr(transbank_prepaid_file, "name", "PREPAGO TBK.xls"),
            bank_statement_file_name=getattr(bank_statement_file, "name", "cartola.xlsx"),
        )

        # 1. Extract the 3 Expected Transbank Deposits & Validate against Bank Statement
        modality_files = [
            ("CLP_CREDIT", credit_dataframe),
            ("CLP_DEBIT", debit_dataframe),
            ("CLP_PREPAID", prepaid_dataframe),
        ]

        expected_deposits = []
        for modality_code, tbk_df in modality_files:
            calculated_deposit = Decimal("0.00")
            for _, row in tbk_df.iloc[:27].iterrows():
                if str(safe_cell(row, 1) or "").strip().lower() == "total abono":
                    calculated_deposit = safe_decimal(safe_cell(row, 3))
                    break
            expected_deposits.append((modality_code, calculated_deposit))

        bank_validations_summary = parse_and_validate_bank_deposits(
            process=process,
            bank_dataframe=bank_dataframe,
            expected_deposits=expected_deposits,
            tolerance=clp_tolerance,
        )

        # 2. Parse ERP Bordero CLP Transactions
        erp_instances, erp_by_auth_code = parse_erp_transactions(
            process=process,
            erp_dataframe=bordero_dataframe,
            card_types=card_types,
            currency_mode="CLP",
        )

        # 3. Cross-Match the 3 Transbank CLP Files with BIN Card Brand Detection
        transbank_instances_to_create = []
        cross_match_results_to_create = []

        summary_by_brand = {
            code: {
                "erp_total": Decimal("0.00"),
                "tbk_total": Decimal("0.00"),
                "commissions": Decimal("0.00"),
                "net_total": Decimal("0.00"),
            }
            for code in ["VI", "MC", "AX", "DI", "RCO"]
        }

        for modality_code, tbk_df in modality_files:
            columns = resolve_dataframe_columns(tbk_df, TRANSBANK_CLP_COLUMN_RULES)

            for _, row in tbk_df.iloc[28:].iterrows():
                auth_code = clean_code_string(safe_cell(row, columns["authorization_code"]))
                gross_amount = safe_decimal(safe_cell(row, columns["gross_amount"]))
                if not auth_code or gross_amount <= 0:
                    continue

                masked_card = str(safe_cell(row, columns["masked_card_number"]) or "").strip()
                tid = str(safe_cell(row, columns["transaction_identifier"]) or "").strip()
                detected_brand_code = identify_card_brand_from_bin(masked_card, tid)
                card_model = card_types[detected_brand_code]

                merchant_code = clean_code_string(safe_cell(row, columns["merchant_code"]))
                merchant_name = str(safe_cell(row, columns["merchant_location_name"]) or "").strip()
                receipt_number = clean_code_string(safe_cell(row, columns["receipt_number"]))
                commission = safe_decimal(safe_cell(row, columns["transbank_commission"]))
                commission_vat = safe_decimal(safe_cell(row, columns["commission_vat_tax"]))
                net_deposit = safe_decimal(safe_cell(row, columns["net_deposit_amount"]))
                sale_date = safe_date(safe_cell(row, columns["sale_date"]))

                tbk_instance = TransbankTransaction(
                    process=process,
                    card_type=card_model,
                    payment_modality=modality_code,
                    merchant_code=merchant_code,
                    merchant_location_name=merchant_name,
                    masked_card_number=masked_card,
                    transaction_identifier=tid,
                    receipt_number=receipt_number,
                    authorization_code=auth_code,
                    sale_date=sale_date,
                    original_sale_amount=gross_amount,
                    transbank_commission=commission,
                    commission_vat_tax=commission_vat,
                    net_deposit_amount=net_deposit,
                    reconciled=False,
                )
                transbank_instances_to_create.append(tbk_instance)

                summary_by_brand[detected_brand_code]["tbk_total"] += gross_amount
                summary_by_brand[detected_brand_code]["commissions"] += (commission + commission_vat)
                summary_by_brand[detected_brand_code]["net_total"] += net_deposit

                matching_erp_list = [r for r in erp_by_auth_code.get(auth_code, []) if not r["instance"].reconciled]
                if not matching_erp_list:
                    for erp_key, records in erp_by_auth_code.items():
                        if auth_code in erp_key:
                            matching_erp_list = [r for r in records if not r["instance"].reconciled]
                            if matching_erp_list:
                                break

                if matching_erp_list:
                    matched_erp = matching_erp_list[0]
                    erp_item = matched_erp["instance"]
                    erp_item.reconciled = True
                    tbk_instance.reconciled = True

                    erp_amount = matched_erp["amount_clp"]
                    diff_clp = gross_amount - erp_amount
                    summary_by_brand[detected_brand_code]["erp_total"] += erp_amount

                    card_type_mismatch = (
                        (modality_code in ("CLP_DEBIT", "CLP_PREPAID") and matched_erp["payment_code"] != "RCO") or
                        (modality_code == "CLP_CREDIT" and matched_erp["card_code"] != detected_brand_code)
                    )

                    if abs(diff_clp) <= clp_tolerance and not card_type_mismatch:
                        system_result = "MATCHED"
                        status_detail = f"Cruce exacto en CLP ({card_model.name} - {modality_code})"
                        mgmt_status = accepted_rounding_status
                    elif card_type_mismatch:
                        system_result = "CARD_TYPE_MISMATCH"
                        status_detail = (
                            f"Tarjeta es {card_model.name} ({modality_code}) por BIN {masked_card[:7]}, "
                            f"pero recepción digitó {matched_erp['payment_code']}"
                        )
                        mgmt_status = pending_status
                    else:
                        system_result = "AMOUNT_MISMATCH"
                        status_detail = f"Diferencia de ${diff_clp} CLP entre Transbank (${gross_amount}) y Bordero (${erp_amount})"
                        mgmt_status = pending_status

                    cross_match_results_to_create.append(CrossMatchResult(
                        process=process,
                        card_type=card_model,
                        transbank_transaction=tbk_instance,
                        erp_transaction=erp_item,
                        authorization_code=auth_code,
                        document_number=erp_item.document_number,
                        sale_date=sale_date,
                        merchant_location_name=merchant_name,
                        room_number=erp_item.room_number,
                        guest_name=erp_item.guest_name,
                        cashier_username=erp_item.cashier_username,
                        transbank_amount=gross_amount,
                        erp_amount_clp=erp_amount,
                        difference_clp=diff_clp,
                        system_result=system_result,
                        system_status_detail=status_detail,
                        system_observation=f"Boleta TBK: {receipt_number} | BIN: {masked_card}",
                        management_status=mgmt_status,
                    ))
                else:
                    cross_match_results_to_create.append(CrossMatchResult(
                        process=process,
                        card_type=card_model,
                        transbank_transaction=tbk_instance,
                        authorization_code=auth_code,
                        document_number=receipt_number,
                        sale_date=sale_date,
                        merchant_location_name=merchant_name,
                        transbank_amount=gross_amount,
                        difference_clp=gross_amount,
                        system_result="NOT_FOUND_IN_ERP",
                        system_status_detail=f"Venta {card_model.name} ({modality_code}) en {merchant_name[:28]} no encontrada en Bordero ERP",
                        system_observation=f"Boleta TBK: {receipt_number} | BIN: {masked_card}",
                        management_status=pending_status,
                    ))

        # 4. Detect Ghost Charges in ERP Bordero
        for erp_item in erp_instances:
            if not erp_item.reconciled:
                cross_match_results_to_create.append(CrossMatchResult(
                    process=process,
                    card_type=erp_item.card_type,
                    erp_transaction=erp_item,
                    authorization_code=erp_item.authorization_code,
                    document_number=erp_item.document_number,
                    sale_date=erp_item.transaction_date,
                    room_number=erp_item.room_number,
                    guest_name=erp_item.guest_name,
                    cashier_username=erp_item.cashier_username,
                    erp_amount_clp=erp_item.amount,
                    difference_clp=-erp_item.amount,
                    system_result="GHOST_IN_ERP",
                    system_status_detail=f"Cobro en Bordero ERP (${erp_item.amount}) sin voucher en Transbank",
                    management_status=pending_status,
                ))

        TransbankTransaction.objects.bulk_create(transbank_instances_to_create)
        CrossMatchResult.objects.bulk_create(cross_match_results_to_create)
        ErpTransaction.objects.bulk_update(erp_instances, ["reconciled"])

        # 5. Persist Brand Summaries
        summary_instances = []
        for code, data in summary_by_brand.items():
            if data["tbk_total"] > 0 or data["erp_total"] > 0:
                summary_instances.append(ReconciliationSummary(
                    process=process,
                    card_type=card_types[code],
                    payment_modality="NATIONAL_CLP",
                    currency_table="CLP",
                    erp_sales_total=data["erp_total"],
                    transbank_sales_total=data["tbk_total"],
                    total_commissions=data["commissions"],
                    net_deposit_total=data["net_total"],
                    difference=data["tbk_total"] - data["erp_total"],
                ))
        ReconciliationSummary.objects.bulk_create(summary_instances)

        return process, {
            "process_id": process.id,
            "bank_validations": bank_validations_summary,
            "summary_by_brand": {k: {m: float(v) for m, v in d.items()} for k, d in summary_by_brand.items()},
            "orphan_count": len([r for r in cross_match_results_to_create if r.system_result == "NOT_FOUND_IN_ERP"]),
            "ghost_count": len([r for r in cross_match_results_to_create if r.system_result == "GHOST_IN_ERP"]),
            "card_mismatch_count": len([r for r in cross_match_results_to_create if r.system_result == "CARD_TYPE_MISMATCH"]),
            "matched_count": len([r for r in cross_match_results_to_create if r.system_result == "MATCHED"]),
        }

