"""
Phase 1: US Dollars (USD) Financial Reconciliation Calculation Service.
Executes the in-memory triple cross-match (Transbank vs. ERP vs. Card Operators + Bank Statement)
without writing to the database.
Database persistence lives in `reconciliation_persistence.py` and Excel generation in `report_exporter.py`.
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
from django.contrib.auth.models import User

from ..models import (
    CardType,
    SystemConfiguration,
    ReconciliationProcess,
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
from .reconciliation_persistence import commit_usd_preview_to_database
from .report_exporter import generate_usd_preview_excel_bytes


def calculate_usd_preview(
    erp_file,
    transbank_file,
    bank_statement_file,
    amex_file,
    diners_file,
    visa_file,
    mastercard_file,
    accounting_period: str = "",
) -> Dict[str, Any]:
    """
    Executes the complete US Dollars reconciliation workflow purely in memory.
    Returns a JSON-serializable dictionary with all summary tables, bank validation,
    extracted source rows, and classified vouchers without touching the database.
    """
    config = SystemConfiguration.objects.first()
    usd_tolerance = config.usd_rounding_tolerance if config else Decimal("0.50")
    card_types = {card.code: card for card in CardType.objects.all()}

    for uploaded_file in [erp_file, transbank_file, bank_statement_file, amex_file, diners_file, visa_file, mastercard_file]:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)

    erp_dataframe = pd.read_excel(erp_file)
    transbank_dataframe = pd.read_excel(transbank_file)
    bank_dataframe = pd.read_excel(bank_statement_file)
    operator_dataframes = {
        "AX": pd.read_excel(amex_file),
        "DI": pd.read_excel(diners_file),
        "VI": pd.read_excel(visa_file),
        "MC": pd.read_excel(mastercard_file),
    }

    # 1. Parse Bank Statement Abono & Dynamic Bank Metadata
    calculated_transbank_deposit = Decimal("0.00")
    detected_bank_name = ""
    detected_account_number = ""

    for _, row in transbank_dataframe.iloc[:38].iterrows():
        label_col = str(safe_cell(row, 1) or "").strip().lower()
        val_col = str(safe_cell(row, 2) or "").strip()
        if "abono calculado" in label_col:
            calculated_transbank_deposit = safe_decimal(safe_cell(row, 2))
        elif label_col.startswith("banco") and val_col and val_col.lower() != "nan":
            detected_bank_name = val_col.title().replace("De ", "de ")
        elif ("numero" in label_col or "número" in label_col) and val_col and not detected_account_number:
            detected_account_number = clean_code_string(val_col)

    for _, row in bank_dataframe.iloc[:15].iterrows():
        non_empty = [str(c).strip() for c in row if pd.notna(c) and str(c).strip()]
        for idx, cell_text in enumerate(non_empty):
            lower_text = cell_text.lower()
            if "cuenta" in lower_text and idx + 1 < len(non_empty):
                detected_account_number = non_empty[idx + 1]
            if not detected_bank_name:
                for bank_kw in ("banco de chile", "banco santander", "banco bci", "scotiabank", "banco estado", "itau", "itaú"):
                    if bank_kw in lower_text:
                        detected_bank_name = bank_kw.title().replace("De ", "de ")
                        break

    bank_validations = parse_and_validate_bank_deposits(
        process=None,
        bank_dataframe=bank_dataframe,
        expected_deposits=[("USD_GENERAL", calculated_transbank_deposit)],
        tolerance=usd_tolerance,
    )
    bank_matched = bank_validations[0]["is_matched"] if bank_validations else False
    bank_deposit_amount = bank_validations[0].get("bank_deposit", 0.0) if bank_validations else 0.0
    bank_movement_date = bank_validations[0].get("movement_date") if bank_validations else None
    bank_movements_data = bank_validations[0].get("movements_data", []) if bank_validations else []

    # 2. Parse Card Operator Statements & ERP Transactions in Memory
    card_operator_index = parse_card_operator_statements(None, operator_dataframes, card_types)
    erp_instances, erp_by_auth_code = parse_erp_transactions(None, erp_dataframe, card_types, currency_mode="USD")

    # Index ERP instances so CrossMatchResult can link to the exact ErpTransaction on commit
    for idx, erp_item in enumerate(erp_instances):
        erp_item._memory_index = idx

    # Flatten and index CardOperatorTransaction instances for serialization and FK linking
    card_operator_transactions_data: List[Dict[str, Any]] = []
    for card_code, doc_dict in card_operator_index.items():
        for doc_num, candidates in doc_dict.items():
            for candidate in candidates:
                op_idx = len(card_operator_transactions_data)
                candidate["operator_index"] = op_idx
                op_inst = candidate["instance"]
                card_operator_transactions_data.append({
                    "card_code": card_code,
                    "document_number": op_inst.document_number,
                    "sequence_number": op_inst.sequence_number or "",
                    "authorization_code": op_inst.authorization_code or "",
                    "foreign_currency_amount": float(op_inst.foreign_currency_amount),
                    "balance_amount": float(op_inst.balance_amount),
                    "corrected_balance_amount": float(op_inst.corrected_balance_amount),
                })

    # 3. Cross-Match Transbank (Source of Truth) -> ERP -> Card Operator
    tbk_columns = resolve_dataframe_columns(transbank_dataframe, TRANSBANK_USD_COLUMN_RULES)
    matched_vouchers: List[Dict[str, Any]] = []
    discrepancy_vouchers: List[Dict[str, Any]] = []
    orphan_vouchers: List[Dict[str, Any]] = []
    transbank_transactions_data: List[Dict[str, Any]] = []

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
        merchant_code = clean_code_string(safe_cell(row, tbk_columns.get("merchant_code", 1)))
        installment_type = str(safe_cell(row, tbk_columns.get("installment_type", 5)) or "").strip()
        installment_number = clean_code_string(safe_cell(row, tbk_columns.get("installment_number", 8)))
        card_model = card_types.get(raw_card_type)
        card_name = card_model.name if card_model else raw_card_type

        tbk_index = len(transbank_transactions_data)

        matching_erp_records = erp_by_auth_code.get(auth_code, [])
        if not matching_erp_records:
            for erp_key, records in erp_by_auth_code.items():
                if auth_code in erp_key:
                    matching_erp_records = records
                    break

        if matching_erp_records:
            invoiced_erp_records = [
                r for r in matching_erp_records if getattr(r["instance"], "invoice_number", "")
            ]
            active_erp_records = invoiced_erp_records if invoiced_erp_records else matching_erp_records

            def _resolve_operator_for_erp(erp_entry):
                doc_num = erp_entry["document_number"]
                erp_clp = erp_entry["amount_clp"]
                candidates = card_operator_index.get(raw_card_type, {}).get(doc_num, [])
                chosen_op = None
                obs = "No encontrado en archivo Operadora"
                for cand in candidates:
                    if cand["balance_clp"] == erp_clp:
                        chosen_op = cand
                        obs = f"Coincide con SALDO {cand['balance_clp']}"
                        break
                    if cand["corrected_balance_clp"] == erp_clp:
                        chosen_op = cand
                        obs = f"Coincide con SALDO CORREGIDO {cand['corrected_balance_clp']}"
                        break
                if not chosen_op and candidates:
                    chosen_op = candidates[0]
                op_usd = chosen_op["foreign_currency_usd"] if chosen_op else Decimal("0.00")
                op_clp = (
                    chosen_op["balance_clp"]
                    if chosen_op and chosen_op["balance_clp"] > 0
                    else chosen_op["corrected_balance_clp"]
                    if chosen_op
                    else Decimal("0.00")
                )
                return chosen_op, op_usd, op_clp, obs

            resolved_candidates = [
                (erp_rec, *_resolve_operator_for_erp(erp_rec)) for erp_rec in active_erp_records
            ]

            single_match = None
            for item in resolved_candidates:
                erp_rec, chosen_op, op_usd, op_clp, obs = item
                if chosen_op is not None and abs(round(gross_amount_usd - op_usd, 2)) <= usd_tolerance:
                    single_match = item
                    break

            if single_match is not None or len(resolved_candidates) == 1:
                matched_erp, selected_operator, operator_usd, operator_clp, observation_text = (
                    single_match if single_match is not None else resolved_candidates[0]
                )
                erp_item = matched_erp["instance"]
                erp_item.reconciled = True
                erp_index = getattr(erp_item, "_memory_index", None)
                document_number = matched_erp["document_number"]
                erp_amount_clp = matched_erp["amount_clp"]
                operator_index = selected_operator["operator_index"] if selected_operator else None
                if selected_operator and operator_index is not None:
                    card_operator_transactions_data[operator_index]["authorization_code"] = auth_code
            else:
                # Multiple ERP invoices sharing a single Transbank voucher (split invoice)
                primary_erp = resolved_candidates[0][0]
                erp_item = primary_erp["instance"]
                erp_index = getattr(erp_item, "_memory_index", None)
                selected_operator = resolved_candidates[0][1]
                operator_index = selected_operator["operator_index"] if selected_operator else None

                doc_numbers = []
                erp_amount_clp = Decimal("0.00")
                operator_usd = Decimal("0.00")
                operator_clp = Decimal("0.00")
                all_ops_found = True

                for erp_rec, chosen_op, op_usd, op_clp, _ in resolved_candidates:
                    erp_rec["instance"].reconciled = True
                    if erp_rec["document_number"] not in doc_numbers:
                        doc_numbers.append(erp_rec["document_number"])
                    erp_amount_clp += erp_rec["amount_clp"]
                    operator_usd += op_usd
                    operator_clp += op_clp
                    if chosen_op and chosen_op.get("operator_index") is not None:
                        card_operator_transactions_data[chosen_op["operator_index"]]["authorization_code"] = auth_code
                    else:
                        all_ops_found = False

                document_number = " / ".join(doc_numbers)[:50]
                if all_ops_found:
                    observation_text = (
                        f"Voucher dividido en {len(resolved_candidates)} facturas "
                        f"({document_number}) | Suma SALDO {operator_clp}"
                    )
                else:
                    observation_text = "No encontrado en archivo Operadora"

            difference_usd = round(gross_amount_usd - operator_usd, 2)
            difference_clp = round(operator_clp - erp_amount_clp, 2)
            is_within_tolerance = abs(difference_usd) <= usd_tolerance and selected_operator is not None
            sys_result = "MATCHED" if is_within_tolerance else "AMOUNT_MISMATCH"
            guest_name = erp_item.guest_name or "—"
            room_number = erp_item.room_number or "—"
            cashier_username = erp_item.cashier_username or "—"

            summary_accumulators[raw_card_type]["usd_erp_total"] += gross_amount_usd
            summary_accumulators[raw_card_type]["usd_tbk_total"] += operator_usd
            summary_accumulators[raw_card_type]["clp_erp_total"] += erp_amount_clp
            summary_accumulators[raw_card_type]["clp_tbk_total"] += operator_clp
        else:
            erp_index = None
            operator_index = None
            document_number = ""
            guest_name = "—"
            room_number = "—"
            cashier_username = "—"
            operator_usd = Decimal("0.00")
            erp_amount_clp = Decimal("0.00")
            operator_clp = Decimal("0.00")
            difference_usd = gross_amount_usd
            difference_clp = Decimal("0.00")
            is_within_tolerance = False
            sys_result = "NOT_FOUND_IN_ERP"
            observation_text = f"Voucher {auth_code} no encontrado en ERP Hotel"

        sale_date_str = sale_date.strftime("%d/%m/%Y") if sale_date else None
        transbank_transactions_data.append({
            "card_code": raw_card_type,
            "payment_modality": "CREDIT",
            "merchant_code": merchant_code,
            "merchant_location_name": "HOTEL PLAZA SAN FRANCISCO",
            "masked_card_number": masked_card,
            "installment_type": installment_type,
            "installment_number": installment_number,
            "authorization_code": auth_code,
            "sale_date": sale_date_str,
            "original_sale_amount": float(gross_amount_usd),
            "reconciled": is_within_tolerance,
        })

        voucher_dict = {
            "tbk_index": tbk_index,
            "erp_index": erp_index,
            "operator_index": operator_index,
            "authorization_code": auth_code,
            "document_number": document_number,
            "card_code": raw_card_type,
            "card_name": card_name,
            "masked_card": masked_card,
            "sale_date": sale_date_str or "",
            "merchant_location": "HOTEL PLAZA SAN FRANCISCO",
            "guest_name": guest_name,
            "room_number": room_number,
            "cashier_username": cashier_username,
            "gross_amount_usd": float(gross_amount_usd),
            "operator_amount_usd": float(operator_usd),
            "erp_amount_clp": float(erp_amount_clp),
            "operator_amount_clp": float(operator_clp),
            "difference_usd": float(difference_usd),
            "difference_clp": float(difference_clp),
            "system_result": sys_result,
            "observation": observation_text,
        }

        if sys_result == "MATCHED":
            matched_vouchers.append(voucher_dict)
        elif sys_result == "AMOUNT_MISMATCH":
            discrepancy_vouchers.append(voucher_dict)
        else:
            orphan_vouchers.append(voucher_dict)

    # 4. Serialize ERP transactions (with transaction_time for natural key deduplication)
    erp_transactions_data: List[Dict[str, Any]] = []
    for erp_item in erp_instances:
        card_code = erp_item.card_type.code if erp_item.card_type else "VI"
        erp_transactions_data.append({
            "card_code": card_code,
            "room_number": erp_item.room_number,
            "room_type": erp_item.room_type,
            "reservation_number": erp_item.reservation_number,
            "account_code": erp_item.account_code,
            "payment_code": erp_item.payment_code,
            "invoice_number": erp_item.invoice_number,
            "document_number": erp_item.document_number,
            "authorization_code": erp_item.authorization_code,
            "transaction_date": erp_item.transaction_date.strftime("%d/%m/%Y") if erp_item.transaction_date else None,
            "transaction_time": erp_item.transaction_time.strftime("%H:%M:%S") if erp_item.transaction_time else None,
            "cashier_username": erp_item.cashier_username,
            "guest_name": erp_item.guest_name,
            "amount": float(erp_item.amount),
            "source_currency": erp_item.source_currency,
            "reconciled": erp_item.reconciled,
        })

    # 5. Build Summary Tables
    usd_table = []
    clp_table = []
    tot_usd_erp, tot_usd_tbk = Decimal("0.00"), Decimal("0.00")
    tot_clp_erp, tot_clp_tbk = Decimal("0.00"), Decimal("0.00")

    for code in ["AX", "DI", "MC", "VI"]:
        card_model = card_types.get(code)
        card_name = card_model.name if card_model else code
        data = summary_accumulators[code]

        diff_usd = round(data["usd_tbk_total"] - data["usd_erp_total"], 2)
        diff_clp = round(data["clp_tbk_total"] - data["clp_erp_total"], 2)

        usd_table.append({
            "card_code": code,
            "card_name": f"{card_name} US$",
            "erp_sales": float(round(data["usd_erp_total"], 2)),
            "tbk_sales": float(round(data["usd_tbk_total"], 2)),
            "difference": float(diff_usd),
        })
        clp_table.append({
            "card_code": code,
            "card_name": f"{card_name} (CLP)",
            "erp_sales": float(round(data["clp_erp_total"], 0)),
            "tbk_sales": float(round(data["clp_tbk_total"], 0)),
            "difference": float(round(diff_clp, 0)),
        })

        tot_usd_erp += data["usd_erp_total"]
        tot_usd_tbk += data["usd_tbk_total"]
        tot_clp_erp += data["clp_erp_total"]
        tot_clp_tbk += data["clp_tbk_total"]

    usd_totals = {
        "erp": float(round(tot_usd_erp, 2)),
        "tbk": float(round(tot_usd_tbk, 2)),
        "diff": float(round(tot_usd_tbk - tot_usd_erp, 2)),
    }
    clp_totals = {
        "erp": float(round(tot_clp_erp, 0)),
        "tbk": float(round(tot_clp_tbk, 0)),
        "diff": float(round(tot_clp_tbk - tot_clp_erp, 0)),
    }

    return {
        "accounting_period": accounting_period or "Cierre Dólares (USD)",
        "file_names": {
            "erp": getattr(erp_file, "name", "TERJETAS ERP.xlsx"),
            "transbank": getattr(transbank_file, "name", "ABONO TBK.xlsx"),
            "bank": getattr(bank_statement_file, "name", "cartola.xls"),
            "amex": getattr(amex_file, "name", "AMEX DOLAR.XLS"),
            "diners": getattr(diners_file, "name", "DINERS DOLAR.XLS"),
            "visa": getattr(visa_file, "name", "VISA DOLAR.XLS"),
            "mastercard": getattr(mastercard_file, "name", "MASTERCARD DOLAR.XLS"),
        },
        "bank_validation": {
            "deposit_modality": "USD_GENERAL",
            "bank_name": detected_bank_name or "Banco Cuenta Corriente",
            "bank_account_number": detected_account_number,
            "calculated_deposit": float(calculated_transbank_deposit),
            "bank_deposit_amount": float(bank_deposit_amount),
            "bank_movement_date": bank_movement_date.strftime("%d/%m/%Y") if bank_movement_date else None,
            "is_matched": bank_matched,
        },
        "bank_movements": bank_movements_data,
        "erp_transactions": erp_transactions_data,
        "transbank_transactions": transbank_transactions_data,
        "card_operator_transactions": card_operator_transactions_data,
        "usd_table": usd_table,
        "clp_table": clp_table,
        "usd_totals": usd_totals,
        "clp_totals": clp_totals,
        "matched_vouchers": matched_vouchers,
        "discrepancy_vouchers": discrepancy_vouchers,
        "orphan_vouchers": orphan_vouchers,
        "counts": {
            "matched": len(matched_vouchers),
            "discrepancies": len(discrepancy_vouchers),
            "orphans": len(orphan_vouchers),
            "total": len(matched_vouchers) + len(discrepancy_vouchers) + len(orphan_vouchers),
        },
    }


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
    Backwards-compatible convenience function: calculates in memory and immediately commits to database.
    """
    preview = calculate_usd_preview(
        erp_file=erp_file,
        transbank_file=transbank_file,
        bank_statement_file=bank_statement_file,
        amex_file=amex_file,
        diners_file=diners_file,
        visa_file=visa_file,
        mastercard_file=mastercard_file,
        accounting_period=accounting_period,
    )
    process = commit_usd_preview_to_database(user, preview)
    return process, preview

