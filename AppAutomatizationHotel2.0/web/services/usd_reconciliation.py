"""
Phase 1: US Dollars (USD) Financial Reconciliation Calculation Service.
Executes the in-memory triple cross-match (Transbank vs. ERP vs. Card Operators + Bank Statement)
without writing to the database.
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from ..models import CardType, SystemConfiguration
from .column_detector import (
    TRANSBANK_USD_COLUMN_RULES,
    clean_code_string,
    resolve_dataframe_columns,
    safe_cell,
    safe_date,
    safe_decimal,
)
from .excel_parsers import (
    parse_and_validate_bank_deposits,
    parse_card_operator_statements,
    parse_erp_transactions,
)
from .suggestion_engine import enrich_preview_with_suggestions

CARD_CODES = ("AX", "DI", "MC", "VI")
BANK_KEYWORDS = (
    "banco de chile", "banco santander", "banco bci",
    "scotiabank", "banco estado", "itau", "itaú",
)


def _extract_bank_and_settlement_metadata(
    tbk_df: pd.DataFrame, bank_df: pd.DataFrame
) -> Tuple[Decimal, str, str, Any]:
    """Extracts calculated deposit, bank name, account number, and settlement date from header rows."""
    calc_deposit, bank_name, account_num, settlement_date = Decimal("0.00"), "", "", None

    for _, row in tbk_df.iloc[:38].iterrows():
        label_col = str(safe_cell(row, 1) or "").strip().lower()
        val_col = str(safe_cell(row, 2) or "").strip()
        if "abono calculado" in label_col:
            calc_deposit = safe_decimal(safe_cell(row, 2))
        elif label_col.startswith("banco") and val_col and val_col.lower() != "nan":
            bank_name = val_col.title().replace("De ", "de ")
        elif ("numero" in label_col or "número" in label_col) and val_col and not account_num:
            account_num = clean_code_string(val_col)
        elif "fecha abono" in label_col and not settlement_date:
            settlement_date = safe_date(safe_cell(row, 2))

    for _, row in bank_df.iloc[:15].iterrows():
        non_empty = [str(c).strip() for c in row if pd.notna(c) and str(c).strip()]
        for idx, cell_text in enumerate(non_empty):
            lower_text = cell_text.lower()
            if "cuenta" in lower_text and idx + 1 < len(non_empty):
                account_num = non_empty[idx + 1]
            if not bank_name:
                for kw in BANK_KEYWORDS:
                    if kw in lower_text:
                        bank_name = kw.title().replace("De ", "de ")
                        break

    return calc_deposit, bank_name, account_num, settlement_date


def _resolve_operator_for_erp(
    erp_entry: Dict[str, Any],
    card_code: str,
    card_operator_index: Dict[str, Dict[str, List[Dict[str, Any]]]],
) -> Tuple[Optional[Dict[str, Any]], Decimal, Decimal, str]:
    """Finds the matching Card Operator record for an ERP entry by document number and CLP balance."""
    erp_clp = erp_entry["amount_clp"]
    candidates = card_operator_index.get(card_code, {}).get(erp_entry["document_number"], [])

    chosen_op, obs = None, "No encontrado en archivo Operadora"
    for cand in candidates:
        if cand["balance_clp"] == erp_clp:
            chosen_op, obs = cand, f"Coincide con SALDO {cand['balance_clp']}"
            break
        if cand["corrected_balance_clp"] == erp_clp:
            chosen_op, obs = cand, f"Coincide con SALDO CORREGIDO {cand['corrected_balance_clp']}"
            break

    if not chosen_op and candidates:
        chosen_op = candidates[0]

    op_usd = chosen_op["foreign_currency_usd"] if chosen_op else Decimal("0.00")
    op_clp = (
        chosen_op["balance_clp"]
        if chosen_op and chosen_op["balance_clp"] > 0
        else (chosen_op["corrected_balance_clp"] if chosen_op else Decimal("0.00"))
    )
    return chosen_op, op_usd, op_clp, obs


def _build_summary_tables(
    summary_accumulators: Dict[str, Dict[str, Decimal]], card_types: Dict[str, CardType]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, float], Dict[str, float]]:
    """Builds the USD and CLP summary tables and their grand totals."""
    usd_table, clp_table = [], []
    tot_usd_erp = tot_usd_tbk = tot_clp_erp = tot_clp_tbk = Decimal("0.00")

    for code in CARD_CODES:
        card_model = card_types.get(code)
        card_name = card_model.name if card_model else code
        acc = summary_accumulators[code]
        diff_usd = round(acc["usd_tbk_total"] - acc["usd_erp_total"], 2)
        diff_clp = round(acc["clp_tbk_total"] - acc["clp_erp_total"], 0)

        usd_table.append({
            "card_code": code, "card_name": f"{card_name} US$",
            "erp_sales": float(round(acc["usd_erp_total"], 2)),
            "tbk_sales": float(round(acc["usd_tbk_total"], 2)),
            "difference": float(diff_usd),
        })
        clp_table.append({
            "card_code": code, "card_name": f"{card_name} (CLP)",
            "erp_sales": float(round(acc["clp_erp_total"], 0)),
            "tbk_sales": float(round(acc["clp_tbk_total"], 0)),
            "difference": float(diff_clp),
        })
        tot_usd_erp += acc["usd_erp_total"]
        tot_usd_tbk += acc["usd_tbk_total"]
        tot_clp_erp += acc["clp_erp_total"]
        tot_clp_tbk += acc["clp_tbk_total"]

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
    return usd_table, clp_table, usd_totals, clp_totals


def _format_accounting_period(
    accounting_period: str, settlement_date: Any, bank_movement_date: Any, tbk_txs: List[Dict[str, Any]]
) -> str:
    """Generates a descriptive accounting period title when none is explicitly provided."""
    if accounting_period:
        return accounting_period
    settlement_dt = settlement_date or bank_movement_date
    valid_dates = sorted({safe_date(tx["sale_date"]) for tx in tbk_txs if tx.get("sale_date")} - {None})
    range_str = ""
    if valid_dates:
        min_s, max_s = valid_dates[0].strftime("%d/%m/%Y"), valid_dates[-1].strftime("%d/%m/%Y")
        range_str = min_s if min_s == max_s else f"{min_s} al {max_s}"

    if settlement_dt and range_str:
        return f"Cierre Dólares — Abono {settlement_dt.strftime('%d/%m/%Y')} (Ventas {range_str})"
    if settlement_dt:
        return f"Cierre Dólares — Abono {settlement_dt.strftime('%d/%m/%Y')}"
    return f"Cierre Dólares — Ventas {range_str}" if range_str else "Cierre Dólares (USD)"


def calculate_usd_preview(
    erp_file, transbank_file, bank_statement_file,
    amex_file, diners_file, visa_file, mastercard_file,
    accounting_period: str = "",
) -> Dict[str, Any]:
    """Executes the complete US Dollars reconciliation workflow purely in memory."""
    usd_tolerance = SystemConfiguration.get_solo().usd_rounding_tolerance
    card_types = {card.code: card for card in CardType.objects.all()}

    for f in (erp_file, transbank_file, bank_statement_file, amex_file, diners_file, visa_file, mastercard_file):
        if hasattr(f, "seek"):
            f.seek(0)

    erp_df = pd.read_excel(erp_file)
    tbk_df = pd.read_excel(transbank_file)
    bank_df = pd.read_excel(bank_statement_file)
    operator_dfs = {
        "AX": pd.read_excel(amex_file), "DI": pd.read_excel(diners_file),
        "VI": pd.read_excel(visa_file), "MC": pd.read_excel(mastercard_file),
    }

    # 1. Bank Statement Abono & Metadata
    calc_deposit, bank_name, account_num, settlement_date = _extract_bank_and_settlement_metadata(tbk_df, bank_df)
    bank_validations = parse_and_validate_bank_deposits(
        bank_dataframe=bank_df,
        expected_deposits=[("USD_GENERAL", calc_deposit)],
        tolerance=usd_tolerance,
    )
    first_bv = bank_validations[0] if bank_validations else {}
    bank_movement_date = first_bv.get("movement_date")

    # 2. Parse Card Operator Statements & ERP Transactions
    card_operator_index = parse_card_operator_statements(operator_dfs, card_types)
    erp_instances, erp_by_auth_code = parse_erp_transactions(erp_df, card_types)
    for idx, erp_item in enumerate(erp_instances):
        erp_item._memory_index = idx

    card_operator_transactions_data: List[Dict[str, Any]] = []
    for card_code, doc_dict in card_operator_index.items():
        for candidates in doc_dict.values():
            for candidate in candidates:
                candidate["operator_index"] = len(card_operator_transactions_data)
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

    # 3. Cross-Match Transbank -> ERP -> Card Operator
    tbk_cols = resolve_dataframe_columns(tbk_df, TRANSBANK_USD_COLUMN_RULES)
    matched_vouchers, discrepancy_vouchers, orphan_vouchers, tbk_txs_data = [], [], [], []
    summary_accumulators = {
        code: {k: Decimal("0.00") for k in ("usd_erp_total", "usd_tbk_total", "clp_erp_total", "clp_tbk_total")}
        for code in CARD_CODES
    }

    for _, row in tbk_df.iterrows():
        raw_card_type = str(safe_cell(row, tbk_cols["card_type"]) or "").strip().upper()
        if raw_card_type not in summary_accumulators:
            continue

        auth_code = clean_code_string(safe_cell(row, tbk_cols["authorization_code"]))
        gross_amount_usd = safe_decimal(safe_cell(row, tbk_cols["gross_amount"]))
        if not auth_code or gross_amount_usd <= 0:
            continue

        sale_date = safe_date(safe_cell(row, tbk_cols["sale_date"]))
        masked_card = str(safe_cell(row, tbk_cols["masked_card_number"]) or "").strip()
        card_model = card_types.get(raw_card_type)
        card_name = card_model.name if card_model else raw_card_type
        tbk_index = len(tbk_txs_data)

        matching_erp_records = erp_by_auth_code.get(auth_code, [])
        if not matching_erp_records:
            matching_erp_records = next(
                (recs for erp_key, recs in erp_by_auth_code.items() if auth_code in erp_key), []
            )

        if matching_erp_records:
            active_erp_records = [r for r in matching_erp_records if getattr(r["instance"], "invoice_number", "")] or matching_erp_records
            resolved_candidates = [
                (erp_rec, *_resolve_operator_for_erp(erp_rec, raw_card_type, card_operator_index))
                for erp_rec in active_erp_records
            ]
            single_match = next(
                (c for c in resolved_candidates if c[1] is not None and abs(round(gross_amount_usd - c[2], 2)) <= usd_tolerance),
                None,
            )

            if single_match is not None or len(resolved_candidates) == 1:
                matched_erp, selected_op, operator_usd, operator_clp, observation_text = (
                    single_match if single_match is not None else resolved_candidates[0]
                )
                erp_item = matched_erp["instance"]
                erp_item.reconciled = True
                erp_index = getattr(erp_item, "_memory_index", None)
                document_number = matched_erp["document_number"]
                erp_amount_clp = matched_erp["amount_clp"]
                operator_doc_number = selected_op["instance"].document_number if selected_op and selected_op.get("instance") else ""
                operator_index = selected_op["operator_index"] if selected_op else None
                if selected_op and operator_index is not None:
                    card_operator_transactions_data[operator_index]["authorization_code"] = auth_code
            else:
                primary_erp, selected_op, _, _, _ = resolved_candidates[0]
                erp_item = primary_erp["instance"]
                erp_index = getattr(erp_item, "_memory_index", None)
                operator_doc_number = selected_op["instance"].document_number if selected_op and selected_op.get("instance") else ""
                operator_index = selected_op["operator_index"] if selected_op else None

                doc_numbers: List[str] = []
                erp_amount_clp = operator_usd = operator_clp = Decimal("0.00")
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
                observation_text = (
                    f"Voucher dividido en {len(resolved_candidates)} facturas ({document_number}) | Suma SALDO {operator_clp}"
                    if all_ops_found else "No encontrado en archivo Operadora"
                )

            difference_usd = round(gross_amount_usd - operator_usd, 2)
            difference_clp = round(operator_clp - erp_amount_clp, 2)
            is_within_tolerance = abs(difference_usd) <= usd_tolerance and selected_op is not None
            sys_result = "MATCHED" if is_within_tolerance else "AMOUNT_MISMATCH"
            guest_name, room_number = erp_item.guest_name or "—", erp_item.room_number or "—"
            cashier_username, erp_auth_code = erp_item.cashier_username or "—", erp_item.authorization_code or auth_code
        else:
            erp_index = operator_index = None
            document_number = erp_auth_code = operator_doc_number = ""
            guest_name = room_number = cashier_username = "—"
            operator_usd = erp_amount_clp = operator_clp = difference_clp = Decimal("0.00")
            difference_usd, is_within_tolerance = gross_amount_usd, False
            sys_result = "NOT_FOUND_IN_ERP"
            observation_text = f"Voucher {auth_code} no encontrado en ERP Hotel"

        acc = summary_accumulators[raw_card_type]
        acc["usd_erp_total"] += gross_amount_usd
        acc["usd_tbk_total"] += operator_usd
        acc["clp_erp_total"] += erp_amount_clp
        acc["clp_tbk_total"] += operator_clp

        sale_date_str = sale_date.strftime("%d/%m/%Y") if sale_date else None
        tbk_txs_data.append({
            "card_code": raw_card_type, "payment_modality": "CREDIT",
            "merchant_code": clean_code_string(safe_cell(row, tbk_cols.get("merchant_code", 1))),
            "merchant_location_name": "HOTEL PLAZA SAN FRANCISCO",
            "masked_card_number": masked_card,
            "installment_type": str(safe_cell(row, tbk_cols.get("installment_type", 5)) or "").strip(),
            "installment_number": clean_code_string(safe_cell(row, tbk_cols.get("installment_number", 8))),
            "authorization_code": auth_code, "sale_date": sale_date_str,
            "original_sale_amount": float(gross_amount_usd), "reconciled": is_within_tolerance,
        })

        voucher_dict = {
            "tbk_index": tbk_index, "erp_index": erp_index, "operator_index": operator_index,
            "authorization_code": auth_code, "erp_authorization_code": erp_auth_code,
            "document_number": document_number, "operator_document_number": operator_doc_number or document_number,
            "card_code": raw_card_type, "card_name": card_name, "masked_card": masked_card,
            "sale_date": sale_date_str or "", "merchant_location": "HOTEL PLAZA SAN FRANCISCO",
            "guest_name": guest_name, "room_number": room_number, "cashier_username": cashier_username,
            "gross_amount_usd": float(gross_amount_usd), "operator_amount_usd": float(operator_usd),
            "erp_amount_clp": float(erp_amount_clp), "operator_amount_clp": float(operator_clp),
            "difference_usd": float(difference_usd), "difference_clp": float(difference_clp),
            "system_result": sys_result, "observation": observation_text,
        }
        if sys_result == "MATCHED":
            matched_vouchers.append(voucher_dict)
        elif sys_result == "AMOUNT_MISMATCH":
            discrepancy_vouchers.append(voucher_dict)
        else:
            orphan_vouchers.append(voucher_dict)

    erp_transactions_data = [
        {
            "card_code": e.card_type.code if e.card_type else "VI",
            "room_number": e.room_number, "room_type": e.room_type,
            "reservation_number": e.reservation_number, "account_code": e.account_code,
            "payment_code": e.payment_code, "invoice_number": e.invoice_number,
            "document_number": e.document_number, "authorization_code": e.authorization_code,
            "transaction_date": e.transaction_date.strftime("%d/%m/%Y") if e.transaction_date else None,
            "transaction_time": e.transaction_time.strftime("%H:%M:%S") if e.transaction_time else None,
            "cashier_username": e.cashier_username, "guest_name": e.guest_name,
            "amount": float(e.amount), "source_currency": e.source_currency, "reconciled": e.reconciled,
        }
        for e in erp_instances
    ]

    usd_table, clp_table, usd_totals, clp_totals = _build_summary_tables(summary_accumulators, card_types)
    accounting_period = _format_accounting_period(
        accounting_period, settlement_date, bank_movement_date, tbk_txs_data
    )

    preview_payload = {
        "accounting_period": accounting_period,
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
            "bank_name": bank_name or "Banco Cuenta Corriente",
            "bank_account_number": account_num,
            "calculated_deposit": float(calc_deposit),
            "bank_deposit_amount": float(first_bv.get("bank_deposit", 0.0)),
            "bank_movement_date": bank_movement_date.strftime("%d/%m/%Y") if bank_movement_date else None,
            "is_matched": first_bv.get("is_matched", False),
        },
        "bank_movements": first_bv.get("movements_data", []),
        "erp_transactions": erp_transactions_data,
        "transbank_transactions": tbk_txs_data,
        "card_operator_transactions": card_operator_transactions_data,
        "usd_table": usd_table, "clp_table": clp_table,
        "usd_totals": usd_totals, "clp_totals": clp_totals,
        "matched_vouchers": matched_vouchers,
        "discrepancy_vouchers": discrepancy_vouchers,
        "orphan_vouchers": orphan_vouchers,
        "tolerance": float(usd_tolerance),
        "counts": {
            "matched": len(matched_vouchers),
            "discrepancies": len(discrepancy_vouchers),
            "orphans": len(orphan_vouchers),
            "total": len(matched_vouchers) + len(discrepancy_vouchers) + len(orphan_vouchers),
        },
    }
    return enrich_preview_with_suggestions(preview_payload)
