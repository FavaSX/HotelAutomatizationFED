"""
Phase 2: Chilean Pesos (CLP) Card Reconciliation Calculation Service.
Executes the in-memory cross-match of the 3 Transbank "Abonos por día" settlements
(Crédito, Débito, Prepago) against the Hotel Bordero, and validates each settlement
against its deposit in the Bank Statement, without writing to the database.
Excel parsing lives in `excel_parsers.py`, database persistence in `clp_persistence.py`,
and Excel generation in `clp_report_exporter.py`.
Supports multiple ERP files (e.g. daily extracts, shifts, auxiliary borderos) with automatic deduplication.
"""

import os
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from ..models import CardType, SystemConfiguration
from .column_detector import (
    normalize_text,
    normalize_auth_code,
    safe_date,
)
from .excel_parsers import (
    TBK_HEADER_AMOUNT_LABELS,
    TBK_HEADER_BELOW_LABELS,
    card_code_from_masked_number,
    parse_bank_statement_movements,
    serialize_bank_movement,
    parse_bordero_clp_transactions,
    parse_bordero_period,
    parse_transbank_clp_settlement,
    parse_universal_transbank_clp,
)

# (modality, label, deposit modality, bank statement description hint)
CLP_MODALITIES: List[Tuple[str, str, str, str]] = [
    ("CREDIT", "Crédito", "CLP_CREDIT", "TC TBK"),
    ("DEBIT", "Débito", "CLP_DEBIT", "ABONOS REALIZADOS POR TRANSBANK"),
    ("PREPAID", "Prepago", "CLP_PREPAID", "ABONOS REALIZADOS POR TRANSBANK"),
]
MODALITY_LABELS = {code: label for code, label, _, _ in CLP_MODALITIES}

# Used when the CardType catalog has not been loaded yet.
DEFAULT_CARD_NAMES = {"VI": "Visa", "MC": "MasterCard", "AX": "American Express", "DI": "Diners Club"}


def get_clp_tolerance() -> Decimal:
    """CLP rounding tolerance from SystemConfiguration (editable by superusers from /settings/)."""
    return SystemConfiguration.get_solo().clp_rounding_tolerance


def _installment_parts(installment_number: str) -> Optional[Tuple[int, int]]:
    """'1/3' -> (1, 3). 'S/C' (sin cuotas) and empty values -> None."""
    current, _, total = installment_number.partition("/")
    if current.isdigit() and total.isdigit():
        return int(current), int(total)
    return None


def _installment_is_later(installment_number: str) -> bool:
    """'2/3' or '3/3' are installments of a sale invoiced in an earlier Bordero."""
    parts = _installment_parts(installment_number)
    return parts is not None and parts[0] > 1


def _is_compatible(modality: str, erp_row: Dict[str, Any]) -> bool:
    """Débito and Prepago are charged as Red Compra (RCO); Crédito as Visa $, Master Card $, Amex $, Diners $."""
    is_redcompra = erp_row["payment_group"] == "REDCOMPRA"
    return is_redcompra == (modality in ("DEBIT", "PREPAID"))


def _match_bank_deposits(
    bank_movements: List[Any],
    settlements: Dict[str, Dict[str, Any]],
    tolerance: Decimal,
) -> Tuple[List[Dict[str, Any]], set]:
    """Finds the bank deposit of each settlement, never reusing the same movement twice."""
    claimed_movements: set = set()
    validations: List[Dict[str, Any]] = []

    for modality, label, deposit_modality, description_hint in CLP_MODALITIES:
        header = settlements[modality]["header"]
        expected = header["total_deposit"] or sum(
            (s["net_deposit"] for s in settlements[modality]["sales"]), Decimal("0")
        )
        settlement_date = header["settlement_date"]
        has_activity = bool(expected != 0 or settlements[modality]["sales"])

        candidates = []
        if has_activity:
            for idx, movement in enumerate(bank_movements):
                if idx in claimed_movements or movement.deposit_amount <= 0 or not movement.is_transbank_deposit:
                    continue
                difference = abs(movement.deposit_amount - expected)
                if difference > tolerance:
                    continue
                same_hint = description_hint in (movement.description or "").upper()
                same_date = settlement_date is not None and movement.movement_date == settlement_date
                candidates.append((difference, not same_hint, not same_date, idx))

        matched_idx = min(candidates)[3] if candidates else None
        matched = bank_movements[matched_idx] if matched_idx is not None else None
        if matched_idx is not None:
            claimed_movements.add(matched_idx)

        validations.append({
            "payment_modality": modality,
            "modality_label": label,
            "deposit_modality": deposit_modality,
            "bank_account": header["bank_account"],
            "settlement_date": settlement_date.strftime("%d/%m/%Y") if settlement_date else None,
            "total_sales": float(header["total_sales"]),
            "commission": float(header["commission"]),
            "commission_vat": float(header["commission_vat"]),
            "calculated_deposit": float(expected),
            "bank_deposit_amount": float(matched.deposit_amount) if matched else 0.0,
            "bank_movement_date": matched.movement_date.strftime("%d/%m/%Y") if matched and matched.movement_date else None,
            "bank_description": (
                matched.description if matched else ("Sin ventas ni abono en este período" if not has_activity else "")
            ),
            "bank_movement_index": matched_idx,
            "difference": float(expected - matched.deposit_amount) if matched else float(expected),
            "is_matched": matched is not None or not has_activity,
        })

    return validations, claimed_movements


def _cross_match_sale(
    sale: Dict[str, Any],
    erp_rows: List[Dict[str, Any]],
    rows_by_auth_code: Dict[str, List[int]],
    claimed_erp: set,
    tolerance: Decimal,
    period: Tuple[Optional[date], Optional[date]],
) -> Dict[str, Any]:
    """Level 1: Transbank voucher -> Bordero by normalized authorization code, then validates the amount."""
    modality = sale["payment_modality"]
    gross = sale["gross_amount"]
    normalized_code = normalize_auth_code(sale["authorization_code"])
    candidates = [idx for idx in rows_by_auth_code.get(normalized_code, []) if idx not in claimed_erp]

    def _same_or_next_day(row_idx: int) -> bool:
        erp_date = safe_date(erp_rows[row_idx]["transaction_date"])
        return sale["sale_date"] is None or erp_date is None or 0 <= (erp_date - sale["sale_date"]).days <= 1

    compatible = [idx for idx in candidates if _is_compatible(modality, erp_rows[idx])]
    incompatible = [idx for idx in candidates if idx not in compatible]

    # 1. Single compatible Bordero row with the same amount (within tolerance).
    exact = [
        idx for idx in compatible
        if abs(gross - Decimal(str(erp_rows[idx]["amount"]))) <= tolerance and _same_or_next_day(idx)
    ]
    if not exact:
        exact = [idx for idx in compatible if abs(gross - Decimal(str(erp_rows[idx]["amount"]))) <= tolerance]
    if exact:
        chosen = exact[0]
        claimed_erp.add(chosen)
        diff = gross - Decimal(str(erp_rows[chosen]["amount"]))
        return {
            "result": "MATCHED",
            "linked": [chosen],
            "erp_amount": Decimal(str(erp_rows[chosen]["amount"])),
            "difference": diff,
            "observation": "Cuadrado con Borderó" if diff == 0 else f"Cuadrado (redondeo ${diff:,.0f})",
        }

    # 2. Split folio or payment + reversal on the same account: net sum of the group matches the voucher.
    by_account: Dict[str, List[int]] = {}
    for idx in compatible:
        if _same_or_next_day(idx):
            by_account.setdefault(erp_rows[idx]["account_code"], []).append(idx)
    for group in by_account.values():
        if len(group) > 1:
            group_sum = sum((Decimal(str(erp_rows[i]["amount"])) for i in group), Decimal("0"))
            if abs(gross - group_sum) <= tolerance:
                claimed_erp.update(group)
                return {
                    "result": "MATCHED",
                    "linked": group,
                    "erp_amount": group_sum,
                    "difference": gross - group_sum,
                    "observation": f"Cuadrado sumando {len(group)} filas de la cuenta {erp_rows[group[0]]['account_code']}",
                }

    # 3. Same code and compatible medium, but the amount differs beyond the tolerance.
    same_window = [idx for idx in compatible if _same_or_next_day(idx)] or compatible
    if same_window:
        chosen = min(same_window, key=lambda idx: abs(gross - Decimal(str(erp_rows[idx]["amount"]))))
        claimed_erp.add(chosen)
        erp_amount = Decimal(str(erp_rows[chosen]["amount"]))
        diff = gross - erp_amount
        return {
            "result": "AMOUNT_MISMATCH",
            "linked": [chosen],
            "erp_amount": erp_amount,
            "difference": diff,
            "observation": f"Diferencia de ${diff:,.0f} entre Transbank (${gross:,.0f}) y Borderó (${erp_amount:,.0f})",
        }

    # 4. Same code and amount, but the cashier typed the wrong payment code (e.g. Red Compra as Visa $).
    if incompatible:
        chosen = min(incompatible, key=lambda idx: abs(gross - Decimal(str(erp_rows[idx]["amount"]))))
        claimed_erp.add(chosen)
        erp_amount = Decimal(str(erp_rows[chosen]["amount"]))
        return {
            "result": "CARD_TYPE_MISMATCH",
            "linked": [chosen],
            "erp_amount": erp_amount,
            "difference": gross - erp_amount,
            "observation": (
                f"Liquidado en Transbank {MODALITY_LABELS[modality]}, registrado en Borderó como "
                f"{erp_rows[chosen]['payment_code']} ({erp_rows[chosen]['payment_description']})"
            ),
        }

    # 5. Not found in this Bordero: distinguish out-of-period / later installments from true orphans.
    period_start, period_end = period
    if _installment_is_later(sale["installment_number"]):
        return {
            "result": "OUT_OF_PERIOD",
            "linked": [],
            "erp_amount": Decimal("0"),
            "difference": gross,
            "observation": (
                f"Cuota {sale['installment_number']} de venta del "
                f"{sale['sale_date'].strftime('%d/%m/%Y') if sale['sale_date'] else 'período anterior'} "
                f"(monto original ${gross:,.0f}; abono de la cuota ${sale['valid_amount']:,.0f})"
            ),
        }
    if sale["sale_date"] and period_start and period_end and not (period_start <= sale["sale_date"] <= period_end):
        return {
            "result": "OUT_OF_PERIOD",
            "linked": [],
            "erp_amount": Decimal("0"),
            "difference": gross,
            "observation": (
                f"Venta del {sale['sale_date'].strftime('%d/%m/%Y')} fuera del período del Borderó "
                f"({period_start.strftime('%d/%m/%Y')} al {period_end.strftime('%d/%m/%Y')})"
            ),
        }

    return {
        "result": "NOT_FOUND_IN_ERP",
        "linked": [],
        "erp_amount": Decimal("0"),
        "difference": gross,
        "observation": f"Voucher {sale['authorization_code']} no encontrado en ERP Hotel",
    }


def calculate_clp_preview(
    erp_file,
    bank_statement_file,
    transbank_credit_file=None,
    transbank_debit_file=None,
    transbank_prepaid_file=None,
    extra_erp_files: Optional[List[Any]] = None,
    tbk_files: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """
    Runs the CLP reconciliation in memory and returns a JSON-serializable dictionary for the session.
    Supports both:
      - Unified Transbank file(s) via `tbk_files` (1 consolidated file or 2-3 separate modality files).
      - Legacy individual file arguments (`transbank_credit_file`, `transbank_debit_file`, `transbank_prepaid_file`).
      - Optional auxiliary/extra ERP files (`extra_erp_files`) for multi-shift or historical Bordero matching.
    """
    tolerance = get_clp_tolerance()
    card_names = {**DEFAULT_CARD_NAMES, **{c.code: c.name for c in CardType.objects.all()}}

    all_tbk_files: List[Any] = []
    if tbk_files:
        all_tbk_files.extend([f for f in tbk_files if f is not None])
    for legacy_f in (transbank_credit_file, transbank_debit_file, transbank_prepaid_file):
        if legacy_f is not None and legacy_f not in all_tbk_files:
            all_tbk_files.append(legacy_f)

    all_upload_handles = [erp_file, bank_statement_file] + (extra_erp_files or []) + all_tbk_files
    for uploaded_file in all_upload_handles:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)

    erp_file_name = os.path.basename(getattr(erp_file, "name", "BORDERO.xlsx"))
    bordero_dataframe = pd.read_excel(erp_file, header=None)
    try:
        with pd.ExcelFile(bank_statement_file) as xl_bank:
            target_sheet = "Movimientos" if "Movimientos" in xl_bank.sheet_names else xl_bank.sheet_names[0]
            bank_dataframe = xl_bank.parse(target_sheet, header=None)
    except Exception:
        if hasattr(bank_statement_file, "seek"):
            bank_statement_file.seek(0)
        bank_dataframe = pd.read_excel(bank_statement_file, header=None)
    settlements = parse_universal_transbank_clp(all_tbk_files)

    # Optional auxiliary/historical Bordero files (e.g. BORDERO 04-07.XLS for earlier installment sales)
    auxiliary_erp_rows: List[Dict[str, Any]] = []
    extra_erp_names: List[str] = []
    for extra_file in (extra_erp_files or []):
        if not extra_file:
            continue
        if hasattr(extra_file, "seek"):
            extra_file.seek(0)
        extra_name = os.path.basename(getattr(extra_file, "name", "BORDERO_ADICIONAL.xlsx"))
        extra_erp_names.append(extra_name)
        try:
            extra_df = pd.read_excel(extra_file, header=None)
            if len(extra_df) > 0:
                parsed_extra, _ = parse_bordero_clp_transactions(extra_df, source_file=extra_name)
                auxiliary_erp_rows.extend(parsed_extra)
        except Exception:
            continue

    # 1. Bank deposits of the 3 settlements
    bank_movements = parse_bank_statement_movements(bank_dataframe)
    bank_validations, claimed_movements = _match_bank_deposits(bank_movements, settlements, tolerance)
    bank_movements_data = [
        serialize_bank_movement(m, idx in claimed_movements) for idx, m in enumerate(bank_movements)
    ]
    detected_account = ""
    for _, row in bank_dataframe.iloc[:15].iterrows():
        cells = [str(c).strip() for c in row if pd.notna(c) and str(c).strip()]
        for idx, text in enumerate(cells):
            if normalize_text(text).startswith("cuenta n") and idx + 1 < len(cells):
                detected_account = cells[idx + 1]

    # 2. Bordero peso card payments
    erp_rows, rows_by_auth_code = parse_bordero_clp_transactions(bordero_dataframe, source_file=erp_file_name)

    # Incorporate and deduplicate auxiliary / extra ERP files if provided
    if auxiliary_erp_rows:
        seen_clp_sigs = {
            (
                r.get("account_code"),
                r.get("invoice_number", ""),
                r.get("authorization_code"),
                str(r.get("transaction_date")),
                str(r.get("transaction_time")),
                str(r.get("amount")),
                r.get("card_code"),
            )
            for r in erp_rows
        }
        for extra_row in auxiliary_erp_rows:
            sig = (
                extra_row.get("account_code"),
                extra_row.get("invoice_number", ""),
                extra_row.get("authorization_code"),
                str(extra_row.get("transaction_date")),
                str(extra_row.get("transaction_time")),
                str(extra_row.get("amount")),
                extra_row.get("card_code"),
            )
            if sig not in seen_clp_sigs:
                seen_clp_sigs.add(sig)
                extra_row["is_auxiliary"] = True
                pos = len(erp_rows)
                erp_rows.append(extra_row)
                norm_code = normalize_auth_code(extra_row.get("authorization_code") or "")
                if norm_code:
                    rows_by_auth_code.setdefault(norm_code, []).append(pos)

    period = parse_bordero_period(bordero_dataframe)
    if period == (None, None):
        erp_dates = [d for d in (safe_date(r["transaction_date"]) for r in erp_rows if not r.get("is_auxiliary")) if d]
        if erp_dates:
            period = (min(erp_dates), max(erp_dates))

    # 3. Cross-match every Transbank voucher against the Bordero
    transbank_transactions_data: List[Dict[str, Any]] = []
    all_vouchers: List[Dict[str, Any]] = []
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "MATCHED": [],
        "AMOUNT_MISMATCH": [],
        "CARD_TYPE_MISMATCH": [],
        "NOT_FOUND_IN_ERP": [],
        "OUT_OF_PERIOD": [],
    }
    claimed_erp: set = set()

    # Exact-amount vouchers first so a shared or reused code is not consumed by the wrong sale.
    all_sales = [sale for modality, _, _, _ in CLP_MODALITIES for sale in settlements[modality]["sales"]]
    match_results: Dict[int, Dict[str, Any]] = {}
    for pass_exact in (True, False):
        for sale_idx, sale in enumerate(all_sales):
            if sale_idx in match_results:
                continue
            codes = rows_by_auth_code.get(normalize_auth_code(sale["authorization_code"]), [])
            has_exact = any(
                abs(sale["gross_amount"] - Decimal(str(erp_rows[i]["amount"]))) <= tolerance for i in codes
            )
            if pass_exact and not has_exact:
                continue
            match_results[sale_idx] = _cross_match_sale(sale, erp_rows, rows_by_auth_code, claimed_erp, tolerance, period)

    for sale_idx, sale in enumerate(all_sales):
        match = match_results[sale_idx]
        linked = match["linked"]
        primary_idx = (
            min(
                linked,
                key=lambda i: (
                    erp_rows[i].get("is_reversal", False) or erp_rows[i]["amount"] <= 0,
                    abs(sale["gross_amount"] - Decimal(str(erp_rows[i]["amount"]))),
                    not bool(erp_rows[i].get("invoice_number")),
                ),
            )
            if linked
            else None
        )
        primary_erp = erp_rows[primary_idx] if primary_idx is not None else None
        sale_date_str = sale["sale_date"].strftime("%d/%m/%Y") if sale["sale_date"] else ""
        card_code = sale["card_code"] or (primary_erp["card_code"] if primary_erp else "")
        tbk_index = len(transbank_transactions_data)

        transbank_transactions_data.append({
            "card_code": card_code,
            "payment_modality": sale["payment_modality"],
            "merchant_code": sale["merchant_code"],
            "merchant_location_name": sale["merchant_location"],
            "masked_card_number": sale["masked_card"],
            "transaction_identifier": sale["transaction_identifier"],
            "receipt_number": sale["receipt_number"],
            "installment_type": sale["installment_type"],
            "installment_number": sale["installment_number"],
            "authorization_code": sale["authorization_code"],
            "sale_date": sale_date_str or None,
            "original_sale_amount": float(sale["gross_amount"]),
            "valid_amount": float(sale["valid_amount"]),
            "transbank_commission": float(sale["commission"]),
            "commission_vat_tax": float(sale["commission_vat"]),
            "net_deposit_amount": float(sale["net_deposit"]),
            "reconciled": match["result"] == "MATCHED",
        })

        for idx in linked:
            if match["result"] == "MATCHED":
                erp_rows[idx]["reconciled"] = True

        voucher = {
            "tbk_index": tbk_index,
            "erp_index": primary_idx,
            "erp_indices": linked,
            "payment_modality": sale["payment_modality"],
            "modality_label": MODALITY_LABELS[sale["payment_modality"]],
            "card_code": card_code,
            "card_name": (
                "Redcompra"
                if sale["payment_modality"] in ("DEBIT", "PREPAID") and not card_code
                else card_names.get(card_code, card_code or "Sin marca")
            ),
            "authorization_code": sale["authorization_code"],
            "erp_authorization_code": primary_erp["authorization_code"] if primary_erp else "",
            "masked_card": sale["masked_card"],
            "sale_date": sale_date_str,
            "erp_date": primary_erp["transaction_date"] if primary_erp else "",
            "merchant_location": sale["merchant_location"],
            "installment_type": sale["installment_type"],
            "installment_number": sale["installment_number"],
            "account_code": primary_erp["account_code"] if primary_erp else "—",
            "invoice_number": primary_erp["invoice_number"] if primary_erp else "—",
            "room_number": primary_erp["room_number"] if primary_erp else "—",
            "guest_name": primary_erp["guest_name"] if primary_erp else "—",
            "cashier_username": primary_erp["cashier_username"] if primary_erp else "—",
            "erp_payment_code": primary_erp["payment_code"] if primary_erp else "—",
            "tbk_amount": float(sale["gross_amount"]),
            "valid_amount": float(sale["valid_amount"]),
            "erp_amount": float(match["erp_amount"]),
            "difference": float(match["difference"]),
            "commissions": float(sale["commission"] + sale["commission_vat"]),
            "net_deposit": float(sale["net_deposit"]),
            "system_result": match["result"],
            "observation": match["observation"],
        }
        all_vouchers.append(voucher)
        buckets[match["result"]].append(voucher)

    # 4. Summary tables: by payment medium (UI) and by medium + brand (for ReconciliationSummary in DB)
    modality_table = []
    brand_accumulators: Dict[Tuple[str, str], Dict[str, Decimal]] = {}
    for modality, label, _, _ in CLP_MODALITIES:
        header = settlements[modality]["header"]
        vouchers = [v for v in all_vouchers if v["payment_modality"] == modality]
        gross_total = sum((Decimal(str(v["tbk_amount"])) for v in vouchers), Decimal("0"))
        valid_total = sum((Decimal(str(v["valid_amount"])) for v in vouchers), Decimal("0"))
        erp_total = sum((Decimal(str(v["erp_amount"])) for v in vouchers), Decimal("0"))
        net_total = sum((Decimal(str(v["net_deposit"])) for v in vouchers), Decimal("0"))
        validation = next(b for b in bank_validations if b["payment_modality"] == modality)
        tbk_sales_basis = valid_total if valid_total > 0 else gross_total
        modality_table.append({
            "payment_modality": modality,
            "modality_label": label,
            "vouchers": len(vouchers),
            "matched": sum(1 for v in vouchers if v["system_result"] == "MATCHED"),
            "exceptions": sum(1 for v in vouchers if v["system_result"] != "MATCHED"),
            "tbk_sales": float(tbk_sales_basis),
            "gross_sales": float(gross_total),
            "erp_sales": float(erp_total),
            "difference": float(tbk_sales_basis - erp_total),
            "valid_sales": float(valid_total),
            "commissions": float(header["commission"] + header["commission_vat"]),
            "net_deposit": float(header["total_deposit"] if header["total_deposit"] > 0 else net_total),
            "header_matches_detail": valid_total == (header.get("total_sales") or header.get("sales_total")) and net_total == header["total_deposit"],
            "bank_matched": validation["is_matched"],
        })

        for voucher in vouchers:
            acc = brand_accumulators.setdefault(
                (modality, voucher["card_code"]),
                {"tbk": Decimal("0"), "erp": Decimal("0"), "commissions": Decimal("0"), "net": Decimal("0")},
            )
            acc["tbk"] += Decimal(str(voucher.get("valid_amount") or voucher["tbk_amount"]))
            acc["erp"] += Decimal(str(voucher["erp_amount"]))
            acc["commissions"] += Decimal(str(voucher["commissions"]))
            acc["net"] += Decimal(str(voucher["net_deposit"]))

    clp_table = [
        {
            "payment_modality": modality,
            "card_code": card_code,
            "card_name": f"{card_names.get(card_code, card_code or 'Sin marca')} · {MODALITY_LABELS[modality]}",
            "tbk_sales": float(acc["tbk"]),
            "erp_sales": float(acc["erp"]),
            "commissions": float(acc["commissions"]),
            "net_deposit": float(acc["net"]),
            "difference": float(acc["tbk"] - acc["erp"]),
        }
        for (modality, card_code), acc in sorted(brand_accumulators.items())
    ]
    clp_totals = {
        key: sum(row[key] for row in modality_table)
        for key in ("tbk_sales", "erp_sales", "difference", "commissions", "net_deposit")
    }

    # 5. Accounting period label
    settlement_dates = sorted({s["header"]["settlement_date"] for s in settlements.values()} - {None})
    period_start, period_end = period
    period_str = ""
    if period_start and period_end:
        period_str = f"Borderó {period_start.strftime('%d/%m/%Y')} al {period_end.strftime('%d/%m/%Y')}"
    if settlement_dates:
        accounting_period = f"Cierre Pesos — Abono {settlement_dates[0].strftime('%d/%m/%Y')}"
        if period_str:
            accounting_period += f" ({period_str})"
    else:
        accounting_period = f"Cierre Pesos — {period_str}" if period_str else "Cierre Pesos (CLP)"

    discrepancy_vouchers = buckets["AMOUNT_MISMATCH"] + buckets["CARD_TYPE_MISMATCH"]
    tbk_names = [os.path.basename(getattr(f, "name", "abono_tbk.xlsx")) for f in all_tbk_files]
    tbk_summary_label = ", ".join(tbk_names) if tbk_names else "Abono(s) Transbank CLP"

    preview_payload = {
        "currency": "CLP",
        "accounting_period": accounting_period,
        "tolerance": float(tolerance),
        "bordero_period": {
            "start": period_start.strftime("%d/%m/%Y") if period_start else None,
            "end": period_end.strftime("%d/%m/%Y") if period_end else None,
        },
        "file_names": {
            "erp": erp_file_name,
            "extra_erp": extra_erp_names,
            "bank": getattr(bank_statement_file, "name", "cartola.xlsx"),
            "tbk": tbk_summary_label,
            "tbk_files": tbk_names,
            "tbk_credit": getattr(transbank_credit_file, "name", None) or (tbk_names[0] if tbk_names else ""),
            "tbk_debit": getattr(transbank_debit_file, "name", None) or (tbk_names[0] if tbk_names else ""),
            "tbk_prepaid": getattr(transbank_prepaid_file, "name", None) or (tbk_names[0] if tbk_names else ""),
        },
        "bank_account_number": detected_account or next((b["bank_account"] for b in bank_validations if b["bank_account"]), ""),
        "bank_validations": bank_validations,
        "bank_movements": bank_movements_data,
        "erp_transactions": erp_rows,
        "transbank_transactions": transbank_transactions_data,
        "modality_table": modality_table,
        "clp_table": clp_table,
        "clp_totals": clp_totals,
        "matched_vouchers": buckets["MATCHED"],
        "discrepancy_vouchers": discrepancy_vouchers,
        "orphan_vouchers": buckets["NOT_FOUND_IN_ERP"],
        "out_of_period_vouchers": buckets["OUT_OF_PERIOD"],
        "skipped_movements": sum(s["skipped_movements"] for s in settlements.values()),
        "counts": {
            "matched": len(buckets["MATCHED"]),
            "discrepancies": len(discrepancy_vouchers),
            "orphans": len(buckets["NOT_FOUND_IN_ERP"]),
            "out_of_period": len(buckets["OUT_OF_PERIOD"]),
            "total": len(all_vouchers),
        },
    }
    from .clp_suggestion_engine import enrich_clp_preview_with_suggestions
    return enrich_clp_preview_with_suggestions(preview_payload)
