"""
Multi-File Merging and Deduplication Service for Financial Reconciliation.
Supports concatenating and deduplicating multiple daily/weekly Excel files
for ERP, Transbank, Card Operators, and Bank Statements across extended date ranges.
"""

from datetime import date
from decimal import Decimal
import io
import os
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import pandas as pd

from .column_detector import (
    TRANSBANK_USD_COLUMN_RULES,
    clean_code_string,
    normalize_auth_code,
    normalize_text,
    resolve_dataframe_columns,
    safe_cell,
    safe_date,
    safe_decimal,
)
from .excel_parsers import (
    parse_bank_statement_movements,
    parse_bordero_clp_transactions,
    parse_card_operator_statements,
    parse_erp_transactions,
    parse_transbank_clp_settlement,
    serialize_bank_movement,
)
from ..models import BankStatementMovement, CardType, ErpTransaction


def ensure_file_list(files_or_file: Any) -> List[Any]:
    """Normalizes a single file, None, or a list of files into a clean list of files."""
    if files_or_file is None:
        return []
    if isinstance(files_or_file, (list, tuple)):
        return [f for f in files_or_file if f]
    return [files_or_file]


# ==============================================================================
# 1. USD MULTI-FILE PARSING & DEDUPLICATION
# ==============================================================================

def merge_and_parse_usd_erp(
    erp_files: Union[Any, List[Any]],
    card_types: Dict[str, CardType],
) -> Tuple[List[ErpTransaction], Dict[str, List[Dict[str, Any]]]]:
    """
    Parses one or multiple USD ERP / Bordero files.
    Deduplicates overlapping rows across files by natural key:
    (account, document, invoice, normalized auth code, date, time, amount, card).
    """
    files = ensure_file_list(erp_files)
    if not files:
        return [], {}

    all_instances: List[ErpTransaction] = []
    erp_by_auth_code: Dict[str, List[Dict[str, Any]]] = {}
    seen_natural_keys: Set[Tuple] = set()

    for f in files:
        if hasattr(f, "seek"):
            f.seek(0)
        df = pd.read_excel(f)
        instances, _ = parse_erp_transactions(df, card_types)
        for inst in instances:
            key = (
                clean_code_string(inst.account_code),
                clean_code_string(inst.document_number),
                clean_code_string(inst.invoice_number),
                normalize_auth_code(inst.authorization_code),
                inst.transaction_date,
                inst.transaction_time,
                float(inst.amount),
                inst.card_type.code if inst.card_type else "",
            )
            if key in seen_natural_keys:
                continue
            seen_natural_keys.add(key)
            all_instances.append(inst)

            code = clean_code_string(inst.authorization_code)
            if code:
                erp_by_auth_code.setdefault(code, []).append({
                    "instance": inst,
                    "document_number": clean_code_string(inst.document_number or inst.account_code),
                    "amount_clp": inst.amount,
                })

    for idx, inst in enumerate(all_instances):
        inst._memory_index = idx

    return all_instances, erp_by_auth_code


def merge_and_parse_usd_operators(
    operator_files: Dict[str, Union[Any, List[Any]]],
    card_types: Dict[str, CardType],
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Parses one or multiple operator files per card code ('AX', 'DI', 'VI', 'MC').
    Deduplicates overlapping records across files by:
    (card_code, document_number, sequence_number, foreign_currency_usd, balance_clp, corrected_balance_clp).
    """
    operator_index: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        code: {} for code in ("AX", "DI", "VI", "MC")
    }

    for card_code, files_or_file in operator_files.items():
        files = ensure_file_list(files_or_file)
        seen_keys: Set[Tuple] = set()
        for f in files:
            if hasattr(f, "seek"):
                f.seek(0)
            df = pd.read_excel(f)
            single_index = parse_card_operator_statements({card_code: df}, card_types)
            for doc_num, candidates in single_index.get(card_code, {}).items():
                for cand in candidates:
                    inst = cand["instance"]
                    key = (
                        clean_code_string(inst.document_number),
                        clean_code_string(inst.sequence_number),
                        float(inst.foreign_currency_amount),
                        float(inst.balance_amount),
                        float(inst.corrected_balance_amount),
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    operator_index[card_code].setdefault(doc_num, []).append(cand)

    return operator_index


merge_and_parse_card_operators = merge_and_parse_usd_operators


def merge_and_parse_bank_statements(
    bank_files: Union[Any, List[Any]],
) -> List[BankStatementMovement]:
    """
    Parses one or multiple Bank Statement files (e.g. cartola_semana1.xlsx, cartola_semana2.xlsx).
    Deduplicates overlapping rows by (date, document_number, charge, deposit, balance, description).
    """
    files = ensure_file_list(bank_files)
    if not files:
        return []

    all_movements: List[BankStatementMovement] = []
    seen_keys: Set[Tuple] = set()

    for f in files:
        if hasattr(f, "seek"):
            f.seek(0)
        df = pd.read_excel(f)
        movements = parse_bank_statement_movements(df)
        for m in movements:
            key = (
                m.movement_date,
                clean_code_string(m.document_number),
                float(m.charge_amount),
                float(m.deposit_amount),
                float(m.balance_amount),
                normalize_text(m.description),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_movements.append(m)

    all_movements.sort(key=lambda m: (m.movement_date or date.min, m.document_number or ""))
    return all_movements


def merge_and_parse_transbank_usd(
    tbk_files: Union[Any, List[Any]],
) -> Dict[str, Any]:
    """
    Parses one or multiple Transbank USD settlement files.
    Aggregates headers (total calculated deposits, bank name, account, dates)
    and deduplicates sales vouchers across files by (auth_code, sale_date, gross_usd, card_type, masked_card).
    """
    files = ensure_file_list(tbk_files)
    if not files:
        return {
            "all_rows": [],
            "total_calc_deposit": Decimal("0.00"),
            "individual_deposits": [],
            "bank_name": "",
            "account_num": "",
            "settlement_dates": [],
        }

    all_rows: List[Dict[str, Any]] = []
    seen_vouchers: Set[Tuple] = set()
    total_calc_deposit = Decimal("0.00")
    individual_deposits: List[Tuple[Decimal, Optional[date]]] = []
    detected_bank_name = ""
    detected_account_num = ""
    settlement_dates: List[date] = []

    for f in files:
        if hasattr(f, "seek"):
            f.seek(0)
        tbk_df = pd.read_excel(f)

        # 1. Header metadata
        calc_deposit, b_name, acc_num, s_date = Decimal("0.00"), "", "", None
        for _, row in tbk_df.iloc[:38].iterrows():
            cells = [val for val in row.values if pd.notna(val) and str(val).strip()]
            for pos, val in enumerate(cells):
                label_col = normalize_text(val)
                next_val = cells[pos + 1] if pos + 1 < len(cells) else ""
                if "abono calculado" in label_col and next_val:
                    calc_deposit = safe_decimal(next_val)
                elif label_col.startswith("banco") and next_val and str(next_val).lower() != "nan":
                    b_name = str(next_val).strip().title().replace("De ", "de ")
                elif ("numero" in label_col or "cuenta" in label_col) and next_val and not acc_num:
                    acc_num = clean_code_string(next_val)
                elif "fecha abono" in label_col and next_val and not s_date:
                    s_date = safe_date(next_val)

        if calc_deposit > 0:
            total_calc_deposit += calc_deposit
            individual_deposits.append((calc_deposit, s_date))
        if b_name and not detected_bank_name:
            detected_bank_name = b_name
        if acc_num and not detected_account_num:
            detected_account_num = acc_num
        if s_date:
            settlement_dates.append(s_date)

        # 2. Sales rows
        cols = resolve_dataframe_columns(tbk_df, TRANSBANK_USD_COLUMN_RULES)
        for _, row in tbk_df.iterrows():
            raw_card = str(safe_cell(row, cols["card_type"]) or "").strip().upper()
            auth = clean_code_string(safe_cell(row, cols["authorization_code"]))
            gross = safe_decimal(safe_cell(row, cols["gross_amount"]))
            if not auth or gross <= 0 or raw_card not in ("AX", "DI", "MC", "VI"):
                continue

            sale_dt = safe_date(safe_cell(row, cols["sale_date"]))
            masked = str(safe_cell(row, cols["masked_card_number"]) or "").strip()
            v_key = (normalize_auth_code(auth), sale_dt, float(gross), raw_card, masked)
            if v_key in seen_vouchers:
                continue
            seen_vouchers.add(v_key)

            all_rows.append({
                "row": row,
                "cols": cols,
                "raw_card_type": raw_card,
                "auth_code": auth,
                "gross_amount_usd": gross,
                "sale_date": sale_dt,
                "masked_card": masked,
                "installment_type": str(safe_cell(row, cols.get("installment_type", 5)) or "").strip(),
                "installment_number": clean_code_string(safe_cell(row, cols.get("installment_number", 8))),
                "merchant_code": clean_code_string(safe_cell(row, cols.get("merchant_code", 1))),
            })

    return {
        "all_rows": all_rows,
        "total_calc_deposit": total_calc_deposit,
        "individual_deposits": individual_deposits,
        "bank_name": detected_bank_name,
        "account_num": detected_account_num,
        "settlement_dates": sorted(settlement_dates),
    }


def validate_multi_deposit_bank_movements(
    bank_movements: List[BankStatementMovement],
    expected_deposits: List[Tuple[str, Decimal]],
    total_consolidated: Optional[Decimal] = None,
    tolerance: Decimal = Decimal("0.50"),
) -> Tuple[List[Dict[str, Any]], Set[int]]:
    """
    Validates expected deposits against bank statement movements.
    Attempts individual matching first. If multiple daily deposits were lumped into
    a single bulk deposit by the bank, matches against total_consolidated as a fallback.
    """
    validations_summary = []
    claimed_movement_indices: Set[int] = set()

    for modality_code, expected_amount in expected_deposits:
        matched_idx = next(
            (
                idx for idx, m in enumerate(bank_movements)
                if idx not in claimed_movement_indices
                and m.deposit_amount > 0
                and abs(m.deposit_amount - expected_amount) <= tolerance
            ),
            None,
        )
        if matched_idx is not None:
            claimed_movement_indices.add(matched_idx)
            matched_m = bank_movements[matched_idx]
            m_date_str = matched_m.movement_date.strftime("%d/%m/%Y") if matched_m.movement_date else None
            validations_summary.append({
                "modality": modality_code,
                "transbank_deposit": float(expected_amount),
                "bank_deposit": float(matched_m.deposit_amount),
                "movement_date": m_date_str,
                "is_matched": True,
            })
        else:
            validations_summary.append({
                "modality": modality_code,
                "transbank_deposit": float(expected_amount),
                "bank_deposit": 0.0,
                "movement_date": None,
                "is_matched": False,
            })

    # If any deposit is unmatched and a consolidated total exists, attempt consolidated match
    unmatched = [v for v in validations_summary if not v["is_matched"]]
    if unmatched and total_consolidated and len(expected_deposits) > 1:
        consolidated_idx = next(
            (
                idx for idx, m in enumerate(bank_movements)
                if idx not in claimed_movement_indices
                and m.deposit_amount > 0
                and abs(m.deposit_amount - total_consolidated) <= tolerance
            ),
            None,
        )
        if consolidated_idx is not None:
            claimed_movement_indices.add(consolidated_idx)
            matched_m = bank_movements[consolidated_idx]
            m_date_str = matched_m.movement_date.strftime("%d/%m/%Y") if matched_m.movement_date else None
            for v in unmatched:
                v["bank_deposit"] = float(matched_m.deposit_amount)
                v["movement_date"] = m_date_str
                v["is_matched"] = True
                v["is_consolidated_match"] = True

    return validations_summary, claimed_movement_indices


# ==============================================================================
# 2. CLP MULTI-FILE PARSING & DEDUPLICATION
# ==============================================================================

def merge_and_parse_clp_tbk(
    modality_files: Dict[str, Union[Any, List[Any]]],
) -> Dict[str, Dict[str, Any]]:
    """
    Parses one or multiple Transbank settlement files per modality ('CREDIT', 'DEBIT', 'PREPAID').
    Aggregates financial header fields and deduplicates voucher sales across files.
    """
    merged_settlements: Dict[str, Dict[str, Any]] = {}

    for modality, files_or_file in modality_files.items():
        files = ensure_file_list(files_or_file)
        if not files:
            continue

        header_accumulators: Dict[str, Decimal] = {
            "total_sales": Decimal("0.00"),
            "commission": Decimal("0.00"),
            "commission_vat": Decimal("0.00"),
            "total_deposit": Decimal("0.00"),
            "voided_sales": Decimal("0.00"),
            "service_charges": Decimal("0.00"),
            "service_charges_vat": Decimal("0.00"),
            "commission_refund": Decimal("0.00"),
            "commission_refund_vat": Decimal("0.00"),
        }
        all_sales: List[Dict[str, Any]] = []
        seen_sale_keys: Set[Tuple] = set()
        first_header: Dict[str, Any] = {}
        individual_deposits: List[Tuple[Decimal, Optional[date]]] = []

        for f in files:
            if hasattr(f, "seek"):
                f.seek(0)
            df = pd.read_excel(f, header=None)
            single_settlement = parse_transbank_clp_settlement(df, modality)

            single_headers = single_settlement.get("headers", {})
            if not first_header:
                first_header = single_headers

            for k in header_accumulators:
                header_accumulators[k] += single_headers.get(k, Decimal("0.00"))

            dep = single_headers.get("total_deposit", Decimal("0.00"))
            s_date = single_headers.get("settlement_date")
            if dep > 0:
                individual_deposits.append((dep, s_date))

            for s in single_settlement.get("sales", []):
                s_key = (
                    normalize_auth_code(s.get("authorization_code", "")),
                    safe_date(s.get("sale_date")),
                    float(s.get("gross_amount", 0)),
                    s.get("card_code", ""),
                    s.get("installment_number", ""),
                )
                if s_key in seen_sale_keys:
                    continue
                seen_sale_keys.add(s_key)
                all_sales.append(s)

        merged_headers = dict(first_header)
        merged_headers.update(header_accumulators)
        merged_headers["individual_deposits"] = individual_deposits
        merged_headers["file_count"] = len(files)

        merged_settlements[modality] = {
            "header": merged_headers,
            "headers": merged_headers,
            "sales": all_sales,
        }

    return merged_settlements


def merge_and_parse_clp_bordero(
    bordero_files: Union[Any, List[Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, List[int]], Tuple[Optional[date], Optional[date]]]:
    """
    Parses one or multiple CLP Bordero files.
    Deduplicates identical payment rows across files by:
    (account, document, invoice, authorization_code, transaction_date, amount).
    """
    files = ensure_file_list(bordero_files)
    if not files:
        return [], {}, (None, None)

    all_rows: List[Dict[str, Any]] = []
    rows_by_auth_code: Dict[str, List[int]] = {}
    seen_keys: Set[Tuple] = set()
    all_dates: List[date] = []

    for f in files:
        if hasattr(f, "seek"):
            f.seek(0)
        source_name = getattr(f, "name", "BORDERO.xlsx")
        df = pd.read_excel(f, header=None)
        rows, _ = parse_bordero_clp_transactions(df, source_file=os.path.basename(source_name))
        for r in rows:
            dt = safe_date(r.get("transaction_date"))
            if dt:
                all_dates.append(dt)
            key = (
                clean_code_string(r.get("account_code")),
                clean_code_string(r.get("document_number")),
                clean_code_string(r.get("invoice_number")),
                normalize_auth_code(r.get("authorization_code")),
                dt,
                float(r.get("amount", 0)),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            row_idx = len(all_rows)
            all_rows.append(r)
            code = normalize_auth_code(r.get("authorization_code"))
            if code:
                rows_by_auth_code.setdefault(code, []).append(row_idx)

    min_date = min(all_dates) if all_dates else None
    max_date = max(all_dates) if all_dates else None
    return all_rows, rows_by_auth_code, (min_date, max_date)
