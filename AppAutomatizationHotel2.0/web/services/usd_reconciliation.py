"""
Phase 1: US Dollars (USD) Financial Reconciliation Calculation Service.
Executes the in-memory triple cross-match (Transbank vs. ERP vs. Card Operators + Bank Statement)
without writing to the database.
Supports multiple ERP files (e.g. daily extracts, shifts, auxiliary borderos) with automatic deduplication.
"""

import os
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from ..models import CardType, ErpTransaction, ErpTransactionChange, SystemConfiguration
from .erp_corrections import overlay_on_instances
from .erp_coverage import db_card_candidates_for_codes, evaluate_bordero_upload_warnings
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
    card_code_from_masked_number,
    parse_and_validate_bank_deposits,
    parse_bordero_bundle,
    parse_card_operator_statements,
    read_excel_or_html_df,
    store_additional_erp_movements,
)
from .suggestion_engine import enrich_preview_with_suggestions

CARD_CODES = ("AX", "DI", "MC", "VI")
BANK_KEYWORDS = (
    "banco de chile", "banco santander", "banco bci",
    "scotiabank", "banco estado", "itau", "itaú",
)


def _read_transbank_dataframe(transbank_file: Any) -> pd.DataFrame:
    """Reads a Transbank USD settlement file (.xlsx, BIFF .xls, or HTML-table .xls) with raw row indexing."""
    return read_excel_or_html_df(transbank_file, header=None)


def _find_tbk_usd_detail_header_row(df: pd.DataFrame) -> Tuple[int, Dict[str, int]]:
    """
    Dynamically locates the Transbank USD detail table header row and resolves column indices by title.
    Supports both the legacy Transbank USD format (with 'Tipo Tarjeta' / 'Abono Calculado')
    and the Transbank 'Abonos por día' portal format (with 'N° de tarjeta' / 'Total abono').
    Returns -1 for optional columns absent in a particular format.
    """
    header_idx: Optional[int] = None
    for r in range(min(len(df), 80)):
        row_norm = [normalize_text(val) for val in df.iloc[r] if pd.notna(val)]
        has_auth = any("autoriz" in x for x in row_norm)
        has_amt_or_sale = any("venta" in x or "monto" in x for x in row_norm)
        if has_auth and has_amt_or_sale:
            header_idx = r
            break

    if header_idx is None:
        fallback = resolve_dataframe_columns(df, TRANSBANK_USD_COLUMN_RULES)
        for opt_key in ("merchant_location", "commission", "commission_vat", "net_deposit", "transaction_identifier", "receipt_number"):
            fallback.setdefault(opt_key, -1)
        return 0, fallback

    col_names: List[str] = []
    for c in range(df.shape[1]):
        val_curr = str(df.iloc[header_idx, c]).strip() if pd.notna(df.iloc[header_idx, c]) else ""
        val_prev = (
            str(df.iloc[header_idx - 1, c]).strip()
            if header_idx > 0 and pd.notna(df.iloc[header_idx - 1, c])
            else ""
        )
        combined = f"{val_prev} {val_curr}".strip() if val_prev != val_curr else val_curr
        col_names.append(combined)

    def find_col(keywords: List[str], exclude: Optional[List[str]] = None) -> int:
        exclude_list = exclude or []
        for kw in keywords:
            for idx, name in enumerate(col_names):
                n_name = normalize_text(name)
                if kw in n_name and not any(ex in n_name for ex in exclude_list):
                    return idx
        return -1

    resolved = {
        "movement_type": find_col(["tipo de movimiento", "tipo transaccion", "tipo de transaccion"]),
        "authorization_code": find_col([
            "codigo de autorizacion de venta", "codigo autorizacion venta",
            "codigo autorizacion", "autorizacion venta", "autorizacion",
        ], exclude=["anulacion"]),
        "gross_amount": find_col([
            "monto venta valido para abono", "monto original de venta",
            "monto original venta", "monto original",
        ]),
        "card_type": find_col(["tipo tarjeta", "tipo de tarjeta"]),
        "masked_card_number": find_col(
            ["n de tarjeta", "n° de tarjeta", "no de tarjeta", "numero de tarjeta", "identificador"],
            exclude=["tipo"],
        ),
        "sale_date": find_col(["fecha de venta", "fecha venta", "fecha"], exclude=["anulacion", "cobro"]),
        "merchant_code": find_col(["codigo de comercio", "codigo comercio"]),
        "merchant_location": find_col(["nombre local", "local"]),
        "installment_type": find_col(["tipo de venta/cuota", "tipo venta/cuota", "tipo cuota", "tipo de cuota"]),
        "installment_number": find_col(["n de cuota", "n° de cuota", "no cuota", "n cuota"]),
        "commission": find_col(["comision transbank (-)", "comision transbank", "comision e iva comision"]),
        "commission_vat": find_col(
            ["iva comision transbank (-)", "iva de comision transbank", "iva comision transbank"],
            exclude=["devolucion", "comision e iva"],
        ),
        "net_deposit": find_col(["total abono (=)", "monto para abono", "total abono"]),
        "transaction_identifier": find_col(["id transaccion", "tid"]),
        "receipt_number": find_col(["n de boleta", "n° de boleta", "no boleta", "boleta"]),
    }
    return header_idx, resolved


def _extract_bank_and_settlement_metadata(
    tbk_df: pd.DataFrame, bank_df: pd.DataFrame, max_tbk_row: int = 45
) -> Tuple[Decimal, str, str, Any, str, str]:
    """
    Extracts calculated deposit, bank name, account number, settlement date,
    merchant code, and merchant location from Transbank and Bank header blocks.
    """
    calc_deposit, bank_name, account_num, settlement_date = Decimal("0.00"), "", "", None
    header_merchant_code, header_location = "", ""

    scan_limit = min(len(tbk_df), max_tbk_row if max_tbk_row > 0 else 45)
    for r_idx in range(scan_limit):
        row = tbk_df.iloc[r_idx]
        non_empty_cells = [
            (c_idx, val) for c_idx, val in enumerate(row.values)
            if pd.notna(val) and str(val).strip()
        ]
        for pos, (col_idx, raw_val) in enumerate(non_empty_cells):
            cell_str = str(raw_val).strip()
            label_norm = normalize_text(cell_str)
            next_raw = non_empty_cells[pos + 1][1] if pos + 1 < len(non_empty_cells) else None
            below_raw = (
                tbk_df.iloc[r_idx + 1, col_idx]
                if r_idx + 1 < scan_limit and col_idx < tbk_df.shape[1] and pd.notna(tbk_df.iloc[r_idx + 1, col_idx])
                else None
            )

            if ("abono calculado" in label_norm or label_norm.startswith("total abono")) and next_raw is not None:
                candidate_dep = safe_decimal(next_raw)
                if candidate_dep > 0:
                    calc_deposit = candidate_dep

            if not bank_name:
                if label_norm.startswith("banco") and next_raw is not None:
                    val_str = str(next_raw).strip()
                    if val_str and val_str.lower() != "nan":
                        bank_name = val_str.title().replace("De ", "de ")
                else:
                    for kw in BANK_KEYWORDS:
                        if kw in label_norm:
                            bank_name = kw.title().replace("De ", "de ")
                            break

            if not account_num:
                if label_norm.startswith(("numero", "cuenta corriente")):
                    target_str = str(next_raw or "") if label_norm.startswith("numero") else str(below_raw or next_raw or "")
                    digits = re.findall(r"\d{6,}", target_str)
                    if digits:
                        account_num = digits[0]
                        for kw in BANK_KEYWORDS:
                            if not bank_name and kw in normalize_text(target_str):
                                bank_name = kw.title().replace("De ", "de ")
                                break

            if not settlement_date and any(
                k in label_norm for k in ("fecha abono", "dia de abono", "periodo de consulta")
            ):
                if next_raw is not None:
                    settlement_date = safe_date(next_raw)

            if not header_merchant_code and "codigo local" in label_norm and next_raw is not None:
                header_merchant_code = clean_code_string(next_raw)
            if not header_location and "nombre fantasia" in label_norm and next_raw is not None:
                header_location = str(next_raw).strip()

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

    return calc_deposit, bank_name, account_num, settlement_date, header_merchant_code, header_location


def _resolve_operator_for_erp(
    erp_entry: Dict[str, Any],
    card_code: str,
    card_operator_index: Dict[str, Dict[str, List[Dict[str, Any]]]],
) -> Tuple[Optional[Dict[str, Any]], Decimal, Decimal, str]:
    """Finds the matching Card Operator record for an ERP entry by document number and CLP balance."""
    erp_clp = erp_entry["amount_clp"]
    candidates = card_operator_index.get(card_code, {}).get(erp_entry["document_number"], [])

    chosen_op, obs = None, "No encontrado en Posición por Clientes"
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
        diff_usd = round(acc["usd_erp_total"] - acc["usd_tbk_total"], 2)
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
        "diff": float(round(tot_usd_erp - tot_usd_tbk, 2)),
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

    settlement_str = ""
    if settlement_dt:
        settlement_str = settlement_dt.strftime("%d/%m/%Y") if hasattr(settlement_dt, "strftime") else str(settlement_dt)

    if settlement_str and range_str:
        return f"Cierre Dólares — Abono {settlement_str} (Ventas {range_str})"
    if settlement_str:
        return f"Cierre Dólares — Abono {settlement_str}"
    return f"Cierre Dólares — Ventas {range_str}" if range_str else "Cierre Dólares (USD)"


def calculate_usd_preview(
    erp_file, transbank_file, bank_statement_file,
    amex_file, diners_file, visa_file, mastercard_file,
    extra_erp_files: Optional[List[Any]] = None,
    accounting_period: str = "",
) -> Dict[str, Any]:
    """
    Executes the complete US Dollars reconciliation workflow purely in memory.
    Consolidates primary ERP file and any extra ERP files with automatic deduplication.
    """
    usd_tolerance = SystemConfiguration.get_solo().usd_rounding_tolerance
    card_types = {card.code: card for card in CardType.objects.all()}

    for f in (erp_file, transbank_file, bank_statement_file, amex_file, diners_file, visa_file, mastercard_file):
        if hasattr(f, "seek"):
            f.seek(0)

    erp_df = pd.read_excel(erp_file)
    tbk_df = _read_transbank_dataframe(transbank_file)
    bank_df = pd.read_excel(bank_statement_file)
    operator_dfs = {
        "AX": pd.read_excel(amex_file), "DI": pd.read_excel(diners_file),
        "VI": pd.read_excel(visa_file), "MC": pd.read_excel(mastercard_file),
    }

    # Collect all ERP files (primary + optional extra files)
    extra_erp_names: List[str] = []
    failed_extra_erp_files: List[str] = []
    primary_erp_name = os.path.basename(getattr(erp_file, "name", "TERJETAS ERP.xlsx"))
    all_erp_dfs: List[Tuple[str, pd.DataFrame]] = [(primary_erp_name, erp_df)]

    for extra in (extra_erp_files or []):
        if not extra:
            continue
        if hasattr(extra, "seek"):
            extra.seek(0)
        extra_name = os.path.basename(getattr(extra, "name", "ERP_EXTRA.xlsx"))
        try:
            extra_df = pd.read_excel(extra)
            if len(extra_df) > 0:
                all_erp_dfs.append((extra_name, extra_df))
                extra_erp_names.append(extra_name)
        except Exception:
            failed_extra_erp_files.append(extra_name)
            continue

    # 1. Detect Transbank Detail Header & Extract Bank Statement Abono / Metadata
    detail_header_row, tbk_cols = _find_tbk_usd_detail_header_row(tbk_df)
    (
        calc_deposit, bank_name, account_num,
        settlement_date, header_merchant_code, header_location,
    ) = _extract_bank_and_settlement_metadata(tbk_df, bank_df, max_tbk_row=detail_header_row)
    bank_validations = parse_and_validate_bank_deposits(
        bank_dataframe=bank_df,
        expected_deposits=[("USD_GENERAL", calc_deposit)],
        tolerance=usd_tolerance,
    )
    first_bv = bank_validations[0] if bank_validations else {}
    bank_movement_date = first_bv.get("movement_date")

    # 2. Parse Card Operator Statements & ERP Transactions (Deduplicating multiple ERPs)
    card_operator_index = parse_card_operator_statements(operator_dfs, card_types)

    seen_erp_signatures = set()
    erp_instances: List[ErpTransaction] = []
    erp_by_auth_code: Dict[str, List[Dict[str, Any]]] = {}
    erp_stats_list: List[Dict[str, Any]] = []
    raw_additional_movements: List[Dict[str, Any]] = []

    for source_name, df in all_erp_dfs:
        bundle = parse_bordero_bundle(df, card_types=card_types, source_file=source_name)
        erp_stats_list.append(bundle["stats"])
        raw_additional_movements.extend(bundle["all_movements"])
        for inst in bundle["usd_instances"]:
            sig = (
                inst.account_code,
                inst.invoice_number,
                inst.authorization_code,
                str(inst.transaction_date),
                str(inst.transaction_time),
                str(inst.amount),
                inst.card_type.code if inst.card_type else "",
            )
            if sig in seen_erp_signatures:
                continue
            seen_erp_signatures.add(sig)
            inst.source_file = source_name
            erp_instances.append(inst)

    erp_coverage_meta, erp_warnings = evaluate_bordero_upload_warnings(erp_stats_list)

    # Extract Transbank voucher authorization codes & sale dates for overlay + DB card lookup
    start_row = detail_header_row + 1 if detail_header_row > 0 else 0
    voucher_codes = set()
    voucher_dates_by_code: Dict[str, set] = {}
    c_tbk_auth = tbk_cols["authorization_code"]
    c_tbk_date = tbk_cols["sale_date"]
    for row in tbk_df.iloc[start_row:].itertuples(index=False, name=None):
        raw_auth = row[c_tbk_auth] if 0 <= c_tbk_auth < len(row) else None
        norm = normalize_auth_code(clean_code_string(raw_auth))
        if norm:
            voucher_codes.add(norm)
            raw_sd = row[c_tbk_date] if 0 <= c_tbk_date < len(row) else None
            sd = safe_date(raw_sd)
            if sd is not None:
                voucher_dates_by_code.setdefault(norm, set()).add(sd)

    # Corrections saved in the DB win over the Excel, so a re-upload does not undo them
    erp_instances, erp_corrections = overlay_on_instances(
        erp_instances, source_currency="USD", voucher_codes=voucher_codes
    )

    active_usd_keys = {inst.source_key for inst in erp_instances if getattr(inst, "source_key", None)}
    seen_additional_keys = set(active_usd_keys)
    additional_erp_rows: List[Dict[str, Any]] = []
    for mov in raw_additional_movements:
        skey = mov.get("source_key")
        if not skey or skey in seen_additional_keys:
            continue
        seen_additional_keys.add(skey)
        additional_erp_rows.append(mov)
    additional_erp_token = store_additional_erp_movements(additional_erp_rows)

    # Incorporate pending / eligible historical USD card rows already persisted in DB
    existing_manual_ids = {inst.pk for inst in erp_instances if getattr(inst, "pk", None)}
    db_candidates = db_card_candidates_for_codes(
        source_currency="USD",
        voucher_codes=voucher_codes,
        exclude_source_keys=active_usd_keys,
        exclude_ids=existing_manual_ids,
        voucher_dates_by_code=voucher_dates_by_code,
    )
    if db_candidates:
        corrected_db_ids = set(
            ErpTransactionChange.objects.filter(erp_transaction_id__in=[c.id for c in db_candidates])
            .exclude(field_name=ErpTransactionChange.FIELD_CREATED)
            .values_list("erp_transaction_id", flat=True)
        )
        for cand in db_candidates:
            cand.loaded_from_db = True
            cand.db_id = cand.id
            cand.db_process_id = cand.process_id
            cand.source_file = f"Base de Datos (#{cand.process_id})"
            cand.is_corrected = cand.id in corrected_db_ids
            erp_instances.append(cand)

    for inst in erp_instances:
        if inst.authorization_code:
            erp_by_auth_code.setdefault(inst.authorization_code, []).append({
                "instance": inst,
                "account_code": inst.account_code or inst.document_number,
                "document_number": inst.document_number,
                "invoice_number": inst.invoice_number,
                "amount_clp": inst.amount,
                "payment_code": inst.payment_code,
                "card_code": inst.card_type.code if inst.card_type else "",
                "source_file": inst.source_file,
            })

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
    matched_vouchers, discrepancy_vouchers, orphan_vouchers, tbk_txs_data = [], [], [], []
    summary_accumulators = {
        code: {k: Decimal("0.00") for k in ("usd_erp_total", "usd_tbk_total", "clp_erp_total", "clp_tbk_total")}
        for code in CARD_CODES
    }

    start_row = detail_header_row + 1 if detail_header_row > 0 else 0
    for _, row in tbk_df.iloc[start_row:].iterrows():
        auth_code = clean_code_string(safe_cell(row, tbk_cols["authorization_code"]))
        gross_amount_usd = safe_decimal(safe_cell(row, tbk_cols["gross_amount"]))
        if not auth_code or gross_amount_usd <= 0:
            continue

        movement_type = normalize_text(safe_cell(row, tbk_cols.get("movement_type", -1)))
        if movement_type and "venta" not in movement_type and "anulac" not in movement_type:
            continue

        masked_card = str(safe_cell(row, tbk_cols["masked_card_number"]) or "").strip()
        raw_card_type = str(safe_cell(row, tbk_cols.get("card_type", -1)) or "").strip().upper()
        if raw_card_type not in summary_accumulators and masked_card:
            raw_card_type = card_code_from_masked_number(masked_card)
        if raw_card_type not in summary_accumulators:
            continue

        sale_date = safe_date(safe_cell(row, tbk_cols["sale_date"]))
        card_model = card_types.get(raw_card_type)
        card_name = card_model.name if card_model else raw_card_type
        tbk_index = len(tbk_txs_data)

        matching_erp_records = erp_by_auth_code.get(auth_code, [])
        if not matching_erp_records:
            norm_auth = normalize_auth_code(auth_code)
            matching_erp_records = next(
                (recs for erp_key, recs in erp_by_auth_code.items() if norm_auth and normalize_auth_code(erp_key) == norm_auth), []
            )

        if matching_erp_records:
            active_erp_records = [r for r in matching_erp_records if getattr(r["instance"], "invoice_number", "")] or matching_erp_records
            resolved_candidates = [
                (erp_rec, *_resolve_operator_for_erp(erp_rec, raw_card_type, card_operator_index))
                for erp_rec in active_erp_records
            ]
            single_match = next(
                (
                    c for c in resolved_candidates
                    if c[0]["amount_clp"] > 0 and c[1] is not None and abs(round(gross_amount_usd - c[2], 2)) <= usd_tolerance
                ),
                None,
            )

            if single_match is not None or len(resolved_candidates) == 1:
                matched_erp, selected_op, operator_usd, operator_clp, observation_text = (
                    single_match if single_match is not None else resolved_candidates[0]
                )
                erp_item = matched_erp["instance"]
                erp_item.reconciled = True
                erp_index = getattr(erp_item, "_memory_index", None)
                erp_indices = [erp_index] if erp_index is not None else []
                account_code = matched_erp.get("account_code") or matched_erp["document_number"]
                document_number = matched_erp["document_number"]
                erp_amount_clp = matched_erp["amount_clp"]
                operator_doc_number = selected_op["instance"].document_number if selected_op and selected_op.get("instance") else ""
                operator_index = selected_op["operator_index"] if selected_op else None
                if selected_op and operator_index is not None:
                    card_operator_transactions_data[operator_index]["authorization_code"] = auth_code
            else:
                primary_cand = next((c for c in resolved_candidates if c[0]["amount_clp"] > 0), resolved_candidates[0])
                primary_erp, selected_op, _, _, _ = primary_cand
                erp_item = primary_erp["instance"]
                erp_index = getattr(erp_item, "_memory_index", None)
                erp_indices = [
                    idx
                    for r in resolved_candidates
                    if (idx := getattr(r[0]["instance"], "_memory_index", None)) is not None
                ]
                operator_index = selected_op["operator_index"] if selected_op else None

                doc_numbers: List[str] = []
                op_doc_numbers: List[str] = []
                erp_amount_clp = operator_usd = operator_clp = Decimal("0.00")
                all_ops_found = True

                for erp_rec, chosen_op, op_usd, op_clp, _ in resolved_candidates:
                    erp_rec["instance"].reconciled = True
                    rec_acc = erp_rec.get("account_code") or erp_rec["document_number"]
                    if rec_acc and rec_acc not in doc_numbers:
                        doc_numbers.append(rec_acc)
                    erp_amount_clp += erp_rec["amount_clp"]
                    operator_usd += op_usd
                    operator_clp += op_clp
                    if chosen_op and chosen_op.get("operator_index") is not None:
                        card_operator_transactions_data[chosen_op["operator_index"]]["authorization_code"] = auth_code
                        if chosen_op.get("instance") and chosen_op["instance"].document_number:
                            op_d = chosen_op["instance"].document_number
                            if op_d not in op_doc_numbers:
                                op_doc_numbers.append(op_d)
                    else:
                        all_ops_found = False

                account_code = " / ".join(doc_numbers)[:50]
                document_number = account_code
                operator_doc_number = " / ".join(op_doc_numbers)[:50]
                observation_text = (
                    f"Voucher dividido en {len(resolved_candidates)} facturas (Cta. {account_code}) | Suma SALDO {operator_clp}"
                    if all_ops_found else "No encontrado en Posición por Clientes"
                )

            difference_usd = round(gross_amount_usd - operator_usd, 2)
            difference_clp = round(operator_clp - erp_amount_clp, 2)
            is_within_tolerance = abs(difference_usd) <= usd_tolerance and selected_op is not None
            sys_result = "MATCHED" if is_within_tolerance else "AMOUNT_MISMATCH"
            cashier_username, erp_auth_code = erp_item.cashier_username or "—", erp_item.authorization_code or auth_code
        else:
            erp_index = operator_index = None
            erp_indices = []
            account_code = document_number = erp_auth_code = operator_doc_number = ""
            cashier_username = "—"
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
        row_merchant_code = clean_code_string(safe_cell(row, tbk_cols.get("merchant_code", -1))) or header_merchant_code
        raw_loc = str(safe_cell(row, tbk_cols.get("merchant_location", -1)) or "").split(",")[0].strip()
        merchant_loc = raw_loc or header_location or "HOTEL PLAZA SAN FRANCISCO"
        commission_val = safe_decimal(safe_cell(row, tbk_cols.get("commission", -1)))
        vat_val = safe_decimal(safe_cell(row, tbk_cols.get("commission_vat", -1)))
        net_val = safe_decimal(safe_cell(row, tbk_cols.get("net_deposit", -1)))

        tbk_txs_data.append({
            "card_code": raw_card_type,
            "payment_modality": "CREDIT",
            "merchant_code": row_merchant_code,
            "merchant_location_name": merchant_loc,
            "masked_card_number": masked_card,
            "transaction_identifier": clean_code_string(safe_cell(row, tbk_cols.get("transaction_identifier", -1))),
            "receipt_number": clean_code_string(safe_cell(row, tbk_cols.get("receipt_number", -1))),
            "installment_type": str(safe_cell(row, tbk_cols.get("installment_type", -1)) or "").strip(),
            "installment_number": clean_code_string(safe_cell(row, tbk_cols.get("installment_number", -1))),
            "authorization_code": auth_code,
            "sale_date": sale_date_str,
            "original_sale_amount": float(gross_amount_usd),
            "transbank_commission": float(commission_val),
            "commission_vat_tax": float(vat_val),
            "net_deposit_amount": float(net_val),
            "reconciled": is_within_tolerance,
        })

        voucher_dict = {
            "tbk_index": tbk_index, "erp_index": erp_index, "erp_indices": erp_indices, "operator_index": operator_index,
            "authorization_code": auth_code, "erp_authorization_code": erp_auth_code,
            "account_code": account_code, "document_number": document_number,
            "operator_document_number": operator_doc_number,
            "card_code": raw_card_type, "card_name": card_name, "masked_card": masked_card,
            "sale_date": sale_date_str or "", "merchant_location": merchant_loc,
            "cashier_username": cashier_username,
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
            "payment_code": e.payment_code, "payment_description": e.payment_description,
            "invoice_number": e.invoice_number,
            "document_number": e.document_number, "authorization_code": e.authorization_code,
            "transaction_date": e.transaction_date.strftime("%d/%m/%Y") if e.transaction_date else None,
            "transaction_time": e.transaction_time.strftime("%H:%M:%S") if e.transaction_time else None,
            "cashier_username": e.cashier_username, "guest_name": e.guest_name,
            "amount": float(e.amount), "source_currency": e.source_currency, "reconciled": e.reconciled,
            "source_key": e.source_key, "is_corrected": getattr(e, "is_corrected", False),
            "source_file": getattr(e, "source_file", "") or primary_erp_name,
            "manual_id": e.pk if e.origin == ErpTransaction.ORIGIN_MANUAL else None,
            "db_id": getattr(e, "db_id", None),
            "db_process_id": getattr(e, "db_process_id", None),
            "from_db": bool(getattr(e, "loaded_from_db", False)),
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
            "erp": primary_erp_name,
            "transbank": getattr(transbank_file, "name", "ABONO TBK.xlsx"),
            "bank": getattr(bank_statement_file, "name", "cartola.xls"),
            "amex": getattr(amex_file, "name", "AMEX DOLAR.XLS"),
            "diners": getattr(diners_file, "name", "DINERS DOLAR.XLS"),
            "visa": getattr(visa_file, "name", "VISA DOLAR.XLS"),
            "mastercard": getattr(mastercard_file, "name", "MASTERCARD DOLAR.XLS"),
        },
        "extra_erp_files": extra_erp_names,
        "failed_extra_erp_files": failed_extra_erp_files,
        "bank_validation": {
            "deposit_modality": "USD_GENERAL",
            "bank_name": bank_name or "Banco Cuenta Corriente",
            "bank_account_number": account_num,
            "calculated_deposit": float(calc_deposit),
            "bank_deposit_amount": float(first_bv.get("bank_deposit", 0.0)),
            "bank_movement_date": (
                bank_movement_date.strftime("%d/%m/%Y")
                if hasattr(bank_movement_date, "strftime")
                else str(bank_movement_date or "") if bank_movement_date else None
            ),
            "is_matched": first_bv.get("is_matched", False),
        },
        "bank_movements": first_bv.get("movements_data", []),
        "erp_transactions": erp_transactions_data,
        "additional_erp_token": additional_erp_token,
        "transbank_transactions": tbk_txs_data,
        "card_operator_transactions": card_operator_transactions_data,
        "usd_table": usd_table, "clp_table": clp_table,
        "usd_totals": usd_totals, "clp_totals": clp_totals,
        "matched_vouchers": matched_vouchers,
        "discrepancy_vouchers": discrepancy_vouchers,
        "orphan_vouchers": orphan_vouchers,
        "tolerance": float(usd_tolerance),
        "erp_corrections_applied": erp_corrections,
        "erp_coverage_meta": erp_coverage_meta,
        "erp_warnings": erp_warnings,
        "counts": {
            "matched": len(matched_vouchers),
            "discrepancies": len(discrepancy_vouchers),
            "orphans": len(orphan_vouchers),
            "total": len(matched_vouchers) + len(discrepancy_vouchers) + len(orphan_vouchers),
        },
    }
    return enrich_preview_with_suggestions(preview_payload)
