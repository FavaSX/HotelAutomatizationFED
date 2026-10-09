"""
Phase 1: US Dollars (USD) Financial Reconciliation Service.
Orchestrates the 7-file USD reconciliation using shared parsers and hybrid column detection.
"""

from decimal import Decimal
from typing import Any, Dict, Optional, Tuple
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
    TRANSBANK_USD_COLUMN_RULES,
    resolve_dataframe_columns,
    clean_code_string,
    safe_decimal,
    safe_date,
    safe_cell,
)
from .excel_parsers import (
    parse_and_validate_bank_deposits,
    parse_erp_transactions,
    parse_card_operator_statements,
)


def execute_usd_reconciliation(
    user: User,
    erp_file,
    transbank_file,
    bank_statement_file,
    amex_file,
    diners_file,
    visa_file,
    mastercard_file,
    accounting_period: str = "",
    daily_dollar_rate: Optional[Decimal] = None
) -> Tuple[ReconciliationProcess, Dict[str, Any]]:
    """
    Executes the complete US Dollars reconciliation workflow (Phase 1).
    """
    with transaction.atomic():
        config = SystemConfiguration.objects.first()
        usd_tolerance = config.usd_rounding_tolerance if config else Decimal("0.50")
        pending_status = ReconciliationStatus.objects.filter(code="PENDING").first()
        accepted_rounding_status = ReconciliationStatus.objects.filter(code="ACCEPTED_ROUNDING").first()
        card_types = {card.code: card for card in CardType.objects.all()}

        for uploaded_file in [erp_file, transbank_file, bank_statement_file, amex_file, diners_file, visa_file, mastercard_file]:
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)

        erp_dataframe = pd.read_excel(erp_file, nrows=3000)
        transbank_dataframe = pd.read_excel(transbank_file, nrows=3500)
        bank_dataframe = pd.read_excel(bank_statement_file, nrows=1500)
        operator_dataframes = {
            "AX": pd.read_excel(amex_file, nrows=3000),
            "DI": pd.read_excel(diners_file, nrows=3000),
            "VI": pd.read_excel(visa_file, nrows=3000),
            "MC": pd.read_excel(mastercard_file, nrows=3000),
        }

        process = ReconciliationProcess.objects.create(
            user=user,
            process_currency="USD",
            accounting_period=accounting_period or "Cierre Dólares (USD)",
            daily_dollar_rate=daily_dollar_rate,
            erp_file_name=getattr(erp_file, "name", "TERJETAS ERP.xlsx"),
            transbank_general_file_name=getattr(transbank_file, "name", "ABONO TBK.xlsx"),
            bank_statement_file_name=getattr(bank_statement_file, "name", "cartola.xls"),
            american_express_file_name=getattr(amex_file, "name", "AMEX DOLAR.XLS"),
            diners_file_name=getattr(diners_file, "name", "DINERS DOLAR.XLS"),
            visa_file_name=getattr(visa_file, "name", "VISA DOLAR.XLS"),
            mastercard_file_name=getattr(mastercard_file, "name", "MASTERCARD DOLAR.XLS"),
        )

        # 1. Validate Bank Statement Deposit
        calculated_transbank_deposit = Decimal("0.00")
        for _, row in transbank_dataframe.iloc[:38].iterrows():
            if "abono calculado" in str(safe_cell(row, 1) or "").strip().lower():
                calculated_transbank_deposit = safe_decimal(safe_cell(row, 2))
                break

        bank_validations = parse_and_validate_bank_deposits(
            process=process,
            bank_dataframe=bank_dataframe,
            expected_deposits=[("USD_GENERAL", calculated_transbank_deposit)],
            tolerance=usd_tolerance,
        )
        bank_matched = bank_validations[0]["is_matched"] if bank_validations else False

        # 2. Parse Card Operator Statements & ERP Transactions
        card_operator_index = parse_card_operator_statements(process, operator_dataframes, card_types)
        erp_instances, erp_by_auth_code = parse_erp_transactions(process, erp_dataframe, card_types, currency_mode="USD")

        # 3. Cross-Match Transbank -> ERP -> Card Operator
        tbk_columns = resolve_dataframe_columns(transbank_dataframe, TRANSBANK_USD_COLUMN_RULES)
        transbank_instances_to_create = []
        cross_match_results_to_create = []

        summary_accumulators = {
            code: {
                "usd_erp_total": Decimal("0.00"),
                "usd_tbk_total": Decimal("0.00"),
                "clp_erp_total": Decimal("0.00"),
                "clp_tbk_total": Decimal("0.00"),
            }
            for code in ["AX", "DI", "MC", "VI"]
        }

        for _, row in transbank_dataframe.iterrows():
            raw_card_type = str(safe_cell(row, tbk_columns["card_type"]) or "").strip().upper()
            if raw_card_type not in summary_accumulators:
                continue

            auth_code = clean_code_string(safe_cell(row, tbk_columns["authorization_code"]))
            gross_amount_usd = safe_decimal(safe_cell(row, tbk_columns["gross_amount"]))
            if not auth_code or gross_amount_usd <= 0:
                continue

            sale_date = safe_date(safe_cell(row, tbk_columns["sale_date"]))
            masked_card = str(safe_cell(row, tbk_columns["masked_card_number"]) or "").strip()
            card_model = card_types[raw_card_type]

            tbk_instance = TransbankTransaction(
                process=process,
                card_type=card_model,
                payment_modality="INTERNATIONAL_USD",
                masked_card_number=masked_card,
                authorization_code=auth_code,
                sale_date=sale_date,
                original_sale_amount=gross_amount_usd,
                reconciled=False,
            )
            transbank_instances_to_create.append(tbk_instance)

            matching_erp_records = erp_by_auth_code.get(auth_code, [])
            if not matching_erp_records:
                for erp_key, records in erp_by_auth_code.items():
                    if auth_code in erp_key:
                        matching_erp_records = records
                        break

            if matching_erp_records:
                matched_erp = matching_erp_records[0]
                erp_item = matched_erp["instance"]
                erp_item.reconciled = True
                tbk_instance.reconciled = True

                document_number = matched_erp["document_number"]
                erp_amount_clp = matched_erp["amount_clp"]

                operator_candidates = card_operator_index.get(raw_card_type, {}).get(document_number, [])
                selected_operator = None
                observation_text = "No encontrado en archivo Operadora"

                for candidate in operator_candidates:
                    if int(candidate["balance_clp"]) == int(erp_amount_clp):
                        selected_operator = candidate
                        observation_text = f"El valor ERP {erp_amount_clp} coincide con SALDO {candidate['balance_clp']}"
                        break
                    if int(candidate["corrected_balance_clp"]) == int(erp_amount_clp):
                        selected_operator = candidate
                        observation_text = f"El valor ERP {erp_amount_clp} coincide con SALDO CORREGIDO {candidate['corrected_balance_clp']}"
                        break

                if not selected_operator and operator_candidates:
                    selected_operator = operator_candidates[0]

                operator_usd = selected_operator["foreign_currency_usd"] if selected_operator else Decimal("0.00")
                operator_clp = (
                    selected_operator["balance_clp"]
                    if selected_operator and selected_operator["balance_clp"] > 0
                    else selected_operator["corrected_balance_clp"]
                    if selected_operator
                    else Decimal("0.00")
                )
                operator_instance = selected_operator["instance"] if selected_operator else None

                difference_usd = round(gross_amount_usd - operator_usd, 2)
                difference_clp = round(operator_clp - erp_amount_clp, 2)
                is_within_tolerance = abs(difference_usd) <= usd_tolerance and selected_operator is not None

                cross_match_results_to_create.append(CrossMatchResult(
                    process=process,
                    card_type=card_model,
                    transbank_transaction=tbk_instance,
                    erp_transaction=erp_item,
                    card_operator_transaction=operator_instance,
                    authorization_code=auth_code,
                    document_number=document_number,
                    sale_date=sale_date,
                    merchant_location_name="HOTEL PLAZA SAN FRANCISCO",
                    room_number=erp_item.room_number,
                    guest_name=erp_item.guest_name,
                    cashier_username=erp_item.cashier_username,
                    transbank_amount=gross_amount_usd,
                    operator_amount_usd=operator_usd,
                    erp_amount_clp=erp_amount_clp,
                    difference_usd=difference_usd,
                    difference_clp=difference_clp,
                    system_result="MATCHED" if is_within_tolerance else "AMOUNT_MISMATCH",
                    system_status_detail=(
                        f"El monto original {gross_amount_usd} coincide con OTRA MONEDA {operator_usd}"
                        if is_within_tolerance
                        else f"Diferencia de USD {difference_usd} entre TBK ({gross_amount_usd}) y Operadora ({operator_usd})"
                    ),
                    system_observation=observation_text,
                    management_status=accepted_rounding_status if is_within_tolerance else pending_status,
                ))

                summary_accumulators[raw_card_type]["usd_erp_total"] += gross_amount_usd
                summary_accumulators[raw_card_type]["usd_tbk_total"] += operator_usd
                summary_accumulators[raw_card_type]["clp_erp_total"] += erp_amount_clp
                summary_accumulators[raw_card_type]["clp_tbk_total"] += operator_clp
            else:
                cross_match_results_to_create.append(CrossMatchResult(
                    process=process,
                    card_type=card_model,
                    transbank_transaction=tbk_instance,
                    authorization_code=auth_code,
                    sale_date=sale_date,
                    merchant_location_name="HOTEL PLAZA SAN FRANCISCO",
                    transbank_amount=gross_amount_usd,
                    difference_usd=gross_amount_usd,
                    system_result="NOT_FOUND_IN_ERP",
                    system_status_detail=f"NO ENCONTRÓ EL CÓDIGO: {auth_code} CUYO VALOR ES DE USD {gross_amount_usd}",
                    management_status=pending_status,
                ))

        # 4. Detect Ghost Charges in ERP
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
                    system_status_detail=f"Cobro en ERP (Doc {erp_item.document_number}) sin respaldo en Transbank",
                    management_status=pending_status,
                ))

        TransbankTransaction.objects.bulk_create(transbank_instances_to_create)
        CrossMatchResult.objects.bulk_create(cross_match_results_to_create)
        ErpTransaction.objects.bulk_update(erp_instances, ["reconciled"])

        # 5. Build Consolidated Summary Tables
        summary_instances, ui_usd_table, ui_clp_table = _build_usd_summary_tables(
            process, card_types, summary_accumulators
        )
        ReconciliationSummary.objects.bulk_create(summary_instances)

        return process, {
            "process_id": process.id,
            "bank_deposit_matched": bank_matched,
            "bank_deposit_amount": float(calculated_transbank_deposit),
            "usd_summary_table": ui_usd_table,
            "clp_summary_table": ui_clp_table,
            "orphan_count": len([r for r in cross_match_results_to_create if r.system_result == "NOT_FOUND_IN_ERP"]),
            "ghost_count": len([r for r in cross_match_results_to_create if r.system_result == "GHOST_IN_ERP"]),
            "discrepancy_count": len([r for r in cross_match_results_to_create if r.system_result == "AMOUNT_MISMATCH"]),
        }


def _build_usd_summary_tables(process, card_types, summary_accumulators):
    summary_instances = []
    ui_usd_table = []
    ui_clp_table = []

    total_usd_erp, total_usd_tbk = Decimal("0.00"), Decimal("0.00")
    total_clp_erp, total_clp_tbk = Decimal("0.00"), Decimal("0.00")

    for code in ["AX", "DI", "MC", "VI"]:
        card_model = card_types[code]
        data = summary_accumulators[code]

        diff_usd = round(data["usd_tbk_total"] - data["usd_erp_total"], 2)
        summary_instances.append(ReconciliationSummary(
            process=process,
            card_type=card_model,
            payment_modality="INTERNATIONAL_USD",
            currency_table="USD",
            erp_sales_total=data["usd_erp_total"],
            transbank_sales_total=data["usd_tbk_total"],
            difference=diff_usd,
        ))
        ui_usd_table.append({
            "card_name": f"{card_model.name} US$",
            "erp_sales": float(round(data["usd_erp_total"], 2)),
            "tbk_sales": float(round(data["usd_tbk_total"], 2)),
            "difference": float(diff_usd),
        })
        total_usd_erp += data["usd_erp_total"]
        total_usd_tbk += data["usd_tbk_total"]

        diff_clp = round(data["clp_tbk_total"] - data["clp_erp_total"], 2)
        summary_instances.append(ReconciliationSummary(
            process=process,
            card_type=card_model,
            payment_modality="INTERNATIONAL_USD",
            currency_table="CLP",
            erp_sales_total=data["clp_erp_total"],
            transbank_sales_total=data["clp_tbk_total"],
            difference=diff_clp,
        ))
        ui_clp_table.append({
            "card_name": f"{card_model.name} (CLP)",
            "erp_sales": float(round(data["clp_erp_total"], 0)),
            "tbk_sales": float(round(data["clp_tbk_total"], 0)),
            "difference": float(round(diff_clp, 0)),
        })
        total_clp_erp += data["clp_erp_total"]
        total_clp_tbk += data["clp_tbk_total"]

    ui_usd_table.append({
        "card_name": "Total",
        "erp_sales": float(round(total_usd_erp, 2)),
        "tbk_sales": float(round(total_usd_tbk, 2)),
        "difference": float(round(total_usd_tbk - total_usd_erp, 2)),
    })
    ui_clp_table.append({
        "card_name": "Total",
        "erp_sales": float(round(total_clp_erp, 0)),
        "tbk_sales": float(round(total_clp_tbk, 0)),
        "difference": float(round(total_clp_tbk - total_clp_erp, 0)),
    })

    return summary_instances, ui_usd_table, ui_clp_table

