"""
Phase 1: US Dollars (USD) Financial Reconciliation Service.
Provides two-phase execution:
  1. In-Memory Calculation & Preview (calculate_usd_preview) - ZERO database writes.
  2. Database Persistence (commit_usd_preview_to_database) - Atomic DB transaction.
  3. In-Memory Excel Draft Exporter (generate_usd_preview_excel_bytes).
"""

from decimal import Decimal
import io
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User

from ..models import (
    CardType,
    ReconciliationStatus,
    SystemConfiguration,
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
from .column_detector import (
    TRANSBANK_USD_COLUMN_RULES,
    resolve_dataframe_columns,
    clean_code_string,
    safe_decimal,
    safe_date,
    safe_time,
    safe_cell,
)
from .excel_parsers import (
    parse_and_validate_bank_deposits,
    parse_erp_transactions,
    parse_card_operator_statements,
)


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

    # 1. Parse Bank Statement Abono
    calculated_transbank_deposit = Decimal("0.00")
    for _, row in transbank_dataframe.iloc[:38].iterrows():
        if "abono calculado" in str(safe_cell(row, 1) or "").strip().lower():
            calculated_transbank_deposit = safe_decimal(safe_cell(row, 2))
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

            transbank_transactions_data.append({
                "card_code": raw_card_type,
                "payment_modality": "CREDIT",
                "merchant_code": merchant_code,
                "merchant_location_name": "HOTEL PLAZA SAN FRANCISCO",
                "masked_card_number": masked_card,
                "installment_type": installment_type,
                "installment_number": installment_number,
                "authorization_code": auth_code,
                "sale_date": sale_date.strftime("%d/%m/%Y") if sale_date else None,
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
                "sale_date": sale_date.strftime("%d/%m/%Y") if sale_date else "",
                "merchant_location": "HOTEL PLAZA SAN FRANCISCO",
                "guest_name": erp_item.guest_name or "—",
                "room_number": erp_item.room_number or "—",
                "cashier_username": erp_item.cashier_username or "—",
                "gross_amount_usd": float(gross_amount_usd),
                "operator_amount_usd": float(operator_usd),
                "erp_amount_clp": float(erp_amount_clp),
                "operator_amount_clp": float(operator_clp),
                "difference_usd": float(difference_usd),
                "difference_clp": float(difference_clp),
                "system_result": "MATCHED" if is_within_tolerance else "AMOUNT_MISMATCH",
                "observation": observation_text,
            }

            if is_within_tolerance:
                matched_vouchers.append(voucher_dict)
            else:
                discrepancy_vouchers.append(voucher_dict)

            summary_accumulators[raw_card_type]["usd_erp_total"] += gross_amount_usd
            summary_accumulators[raw_card_type]["usd_tbk_total"] += operator_usd
            summary_accumulators[raw_card_type]["clp_erp_total"] += erp_amount_clp
            summary_accumulators[raw_card_type]["clp_tbk_total"] += operator_clp
        else:
            transbank_transactions_data.append({
                "card_code": raw_card_type,
                "payment_modality": "CREDIT",
                "merchant_code": merchant_code,
                "merchant_location_name": "HOTEL PLAZA SAN FRANCISCO",
                "masked_card_number": masked_card,
                "installment_type": installment_type,
                "installment_number": installment_number,
                "authorization_code": auth_code,
                "sale_date": sale_date.strftime("%d/%m/%Y") if sale_date else None,
                "original_sale_amount": float(gross_amount_usd),
                "reconciled": False,
            })
            orphan_vouchers.append({
                "tbk_index": tbk_index,
                "erp_index": None,
                "operator_index": None,
                "authorization_code": auth_code,
                "document_number": "",
                "card_code": raw_card_type,
                "card_name": card_name,
                "masked_card": masked_card,
                "sale_date": sale_date.strftime("%d/%m/%Y") if sale_date else "",
                "merchant_location": "HOTEL PLAZA SAN FRANCISCO",
                "guest_name": "—",
                "room_number": "—",
                "cashier_username": "—",
                "gross_amount_usd": float(gross_amount_usd),
                "operator_amount_usd": 0.0,
                "erp_amount_clp": 0.0,
                "operator_amount_clp": 0.0,
                "difference_usd": float(gross_amount_usd),
                "difference_clp": 0.0,
                "system_result": "NOT_FOUND_IN_ERP",
                "observation": f"Voucher {auth_code} no encontrado en ERP Hotel",
            })

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


def commit_usd_preview_to_database(user: User, preview: Dict[str, Any]) -> ReconciliationProcess:
    """
    Persists a verified in-memory USD reconciliation preview into the database,
    deduplicating ALL 8 tables with 3 production-hardening safeguards:
      1. Settlement-level process matching: only reuses an existing ReconciliationProcess
         when the majority (>=80%) of incoming ABONO TBK vouchers belong to that process
         AND its BankDepositValidation matches the incoming settlement total (or 100% of
         vouchers match), preventing accidental overwrite when two multi-day exports share
         an overlapping date.
      2. Targeted SQL filtering (__in) instead of loading entire tables into memory.
      3. Automatic AuditResolutionLog tracking whenever a re-uploaded voucher changes its
         system_result (e.g., from NOT_FOUND_IN_ERP or AMOUNT_MISMATCH to MATCHED).
    """
    with transaction.atomic():
        pending_status = ReconciliationStatus.objects.filter(code="PENDING").first()
        accepted_status = ReconciliationStatus.objects.filter(code="ACCEPTED_ROUNDING").first()
        card_types = {card.code: card for card in CardType.objects.all()}

        tbk_raw = preview.get("transbank_transactions", [])
        incoming_tbk_auths = {t.get("authorization_code", "") for t in tbk_raw}

        # Targeted query: only load Transbank transactions matching incoming authorization codes
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
            c_model = card_types.get(t_data.get("card_code"), card_types.get("VI"))
            t_auth = t_data.get("authorization_code", "")
            t_amt = round(Decimal(str(t_data.get("original_sale_amount", 0.0))), 2)
            t_date = safe_date(t_data.get("sale_date"))
            t_mask = t_data.get("masked_card_number", "")
            natural_key = (c_model.id, t_auth, t_amt, t_date, t_mask)
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

            # Verify it is genuinely the same settlement (not just 1-2 overlapping rows from a multi-day export)
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
            t_auth = t_data.get("authorization_code", "")
            t_amt = round(Decimal(str(t_data.get("original_sale_amount", 0.0))), 2)
            t_date = safe_date(t_data.get("sale_date"))
            t_mask = t_data.get("masked_card_number", "")
            t_rec = t_data.get("reconciled", False)

            natural_key = (c_model.id, t_auth, t_amt, t_date, t_mask)
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
        for row in preview.get("usd_table", []):
            code = row["card_code"]
            card_model = card_types.get(code)
            if card_model:
                ReconciliationSummary.objects.update_or_create(
                    process=process,
                    card_type=card_model,
                    payment_modality="INTERNATIONAL_USD",
                    currency_table="USD",
                    defaults={
                        "erp_sales_total": Decimal(str(row["erp_sales"])),
                        "transbank_sales_total": Decimal(str(row["tbk_sales"])),
                        "difference": Decimal(str(row["difference"])),
                    },
                )

        for row in preview.get("clp_table", []):
            code = row["card_code"]
            card_model = card_types.get(code)
            if card_model:
                ReconciliationSummary.objects.update_or_create(
                    process=process,
                    card_type=card_model,
                    payment_modality="INTERNATIONAL_USD",
                    currency_table="CLP",
                    defaults={
                        "erp_sales_total": Decimal(str(row["erp_sales"])),
                        "transbank_sales_total": Decimal(str(row["tbk_sales"])),
                        "difference": Decimal(str(row["difference"])),
                    },
                )

        # Helper to safely resolve ForeignKey objects by index
        def _get_model(model_list, index_val):
            if index_val is not None and 0 <= index_val < len(model_list):
                return model_list[index_val]
            return None

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
            card_model = card_types.get(v_dict["card_code"], card_types.get("VI"))
            tbk_obj = _get_model(tbk_models, v_dict.get("tbk_index"))
            erp_obj = _get_model(erp_models, v_dict.get("erp_index"))
            op_obj = _get_model(operator_models, v_dict.get("operator_index"))
            obs_formatted = format_observation(v_dict["observation"])[:255]

            tbk_amt = Decimal(str(v_dict.get("gross_amount_usd", 0.0)))
            op_amt = Decimal(str(v_dict.get("operator_amount_usd", 0.0)))
            erp_clp = Decimal(str(v_dict.get("erp_amount_clp", 0.0)))
            diff_usd = Decimal(str(v_dict.get("difference_usd", 0.0)))
            diff_clp = Decimal(str(v_dict.get("difference_clp", 0.0)))

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

                existing_match.process = process
                existing_match.card_type = card_model
                existing_match.erp_transaction = erp_obj
                existing_match.card_operator_transaction = op_obj
                existing_match.authorization_code = v_dict["authorization_code"]
                existing_match.document_number = v_dict.get("document_number", "")
                existing_match.sale_date = safe_date(v_dict.get("sale_date"))
                existing_match.merchant_location_name = v_dict.get("merchant_location", "HOTEL PLAZA SAN FRANCISCO")
                existing_match.guest_name = v_dict.get("guest_name", "—")
                existing_match.room_number = v_dict.get("room_number", "—")
                existing_match.cashier_username = v_dict.get("cashier_username", "—")
                existing_match.transbank_amount = tbk_amt
                existing_match.operator_amount_usd = op_amt
                existing_match.erp_amount_clp = erp_clp
                existing_match.difference_usd = diff_usd
                existing_match.difference_clp = diff_clp
                existing_match.system_result = sys_result
                existing_match.system_status_detail = status_detail
                existing_match.system_observation = obs_formatted
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
                    process=process,
                    card_type=card_model,
                    transbank_transaction=tbk_obj,
                    erp_transaction=erp_obj,
                    card_operator_transaction=op_obj,
                    authorization_code=v_dict["authorization_code"],
                    document_number=v_dict.get("document_number", ""),
                    sale_date=safe_date(v_dict.get("sale_date")),
                    merchant_location_name=v_dict.get("merchant_location", "HOTEL PLAZA SAN FRANCISCO"),
                    guest_name=v_dict.get("guest_name", "—"),
                    room_number=v_dict.get("room_number", "—"),
                    cashier_username=v_dict.get("cashier_username", "—"),
                    transbank_amount=tbk_amt,
                    operator_amount_usd=op_amt,
                    erp_amount_clp=erp_clp,
                    difference_usd=diff_usd,
                    difference_clp=diff_clp,
                    system_result=sys_result,
                    system_status_detail=status_detail,
                    system_observation=obs_formatted,
                    management_status=default_mgmt_status,
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


def generate_usd_preview_excel_bytes(preview: Dict[str, Any]) -> io.BytesIO:
    """
    Builds a styled openpyxl Excel report directly from the in-memory USD preview dictionary.
    """
    from ..templatetags.currency_filters import format_observation

    wb = openpyxl.Workbook()
    ws_summary = wb.active
    ws_summary.title = "Resumen Dólares"

    navy_fill = PatternFill(start_color="0C0C42", end_color="0C0C42", fill_type="solid")
    gold_fill = PatternFill(start_color="996909", end_color="996909", fill_type="solid")
    light_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_bold = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    navy_bold = Font(name="Calibri", size=11, bold=True, color="0C0C42")
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    # Title
    ws_summary.cell(row=1, column=1, value="HOTEL PLAZA SAN FRANCISCO — REPORTE PRELIMINAR DÓLARES (USD)").font = Font(size=14, bold=True, color="0C0C42")
    ws_summary.cell(row=2, column=1, value=f"Periodo: {preview.get('accounting_period', '')} | Estado: Borrador en Revisión").font = Font(size=10, italic=True)

    # Table 1: USD
    ws_summary.cell(row=4, column=1, value="1. CUADRATURA POR OPERADOR EN DÓLARES (US$)").font = navy_bold
    usd_headers = ["Operador", "Total TBK (US$)", "Total Operador (US$)", "Diferencia (US$)"]
    for col_idx, h in enumerate(usd_headers, 1):
        cell = ws_summary.cell(row=5, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    curr_row = 6
    for item in preview.get("usd_table", []):
        ws_summary.cell(row=curr_row, column=1, value=item["card_name"]).border = thin_border
        c2 = ws_summary.cell(row=curr_row, column=2, value=item["erp_sales"])
        c2.number_format = "$#,##0.00"
        c2.border = thin_border
        c3 = ws_summary.cell(row=curr_row, column=3, value=item["tbk_sales"])
        c3.number_format = "$#,##0.00"
        c3.border = thin_border
        c4 = ws_summary.cell(row=curr_row, column=4, value=item["difference"])
        c4.number_format = "$#,##0.00"
        c4.border = thin_border
        if item["difference"] != 0:
            c4.font = Font(bold=True, color="DC2626")
        curr_row += 1

    # Totals row
    t = preview.get("usd_totals", {})
    ws_summary.cell(row=curr_row, column=1, value="TOTAL USD").font = navy_bold
    ws_summary.cell(row=curr_row, column=2, value=t.get("erp", 0.0)).number_format = "$#,##0.00"
    ws_summary.cell(row=curr_row, column=3, value=t.get("tbk", 0.0)).number_format = "$#,##0.00"
    ws_summary.cell(row=curr_row, column=4, value=t.get("diff", 0.0)).number_format = "$#,##0.00"
    for c in range(1, 5):
        ws_summary.cell(row=curr_row, column=c).fill = light_fill
        ws_summary.cell(row=curr_row, column=c).border = thin_border

    # Tab 2: Vouchers Ubicados
    ws_matched = wb.create_sheet(title="Vouchers Ubicados")
    matched_headers = [
        "Autorización", "Documento", "Huésped", "Habitación",
        "Cajero", "Tarjeta", "Monto TBK USD", "Monto Operador USD",
        "Diferencia USD", "Monto ERP CLP", "Diagnóstico"
    ]
    for col_idx, h in enumerate(matched_headers, 1):
        cell = ws_matched.cell(row=1, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold

    for r_idx, v in enumerate(preview.get("matched_vouchers", []), 2):
        ws_matched.cell(row=r_idx, column=1, value=v["authorization_code"])
        ws_matched.cell(row=r_idx, column=2, value=v["document_number"])
        ws_matched.cell(row=r_idx, column=3, value=v["guest_name"])
        ws_matched.cell(row=r_idx, column=4, value=v["room_number"])
        ws_matched.cell(row=r_idx, column=5, value=v["cashier_username"])
        ws_matched.cell(row=r_idx, column=6, value=v.get("card_name", v["card_code"]))
        ws_matched.cell(row=r_idx, column=7, value=v["gross_amount_usd"]).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=8, value=v["operator_amount_usd"]).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=9, value=v["difference_usd"]).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=10, value=v["erp_amount_clp"]).number_format = "$#,##0"
        ws_matched.cell(row=r_idx, column=11, value=format_observation(v["observation"]))

    # Tab 3: Discrepancias y No Ubicados
    ws_disc = wb.create_sheet(title="Discrepancias y No Ubicados")
    disc_headers = [
        "Autorización", "Documento", "Huésped", "Habitación",
        "Tarjeta", "Monto TBK USD", "Monto Operador USD",
        "Diferencia USD", "Diagnóstico"
    ]
    for col_idx, h in enumerate(disc_headers, 1):
        cell = ws_disc.cell(row=1, column=col_idx, value=h)
        cell.fill = gold_fill
        cell.font = white_bold

    r_idx = 2
    for v in preview.get("discrepancy_vouchers", []) + preview.get("orphan_vouchers", []):
        ws_disc.cell(row=r_idx, column=1, value=v["authorization_code"])
        ws_disc.cell(row=r_idx, column=2, value=v["document_number"])
        ws_disc.cell(row=r_idx, column=3, value=v["guest_name"])
        ws_disc.cell(row=r_idx, column=4, value=v["room_number"])
        ws_disc.cell(row=r_idx, column=5, value=v.get("card_name", v["card_code"]))
        ws_disc.cell(row=r_idx, column=6, value=v["gross_amount_usd"]).number_format = "$#,##0.00"
        ws_disc.cell(row=r_idx, column=7, value=v["operator_amount_usd"]).number_format = "$#,##0.00"
        ws_disc.cell(row=r_idx, column=8, value=v["difference_usd"]).number_format = "$#,##0.00"
        ws_disc.cell(row=r_idx, column=9, value=format_observation(v["observation"]))
        r_idx += 1

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream


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
