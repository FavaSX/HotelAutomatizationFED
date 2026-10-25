"""
Phase 2: Chilean Pesos (CLP) Card Reconciliation Calculation Service.
Executes the in-memory cross-match of the 3 Transbank "Abonos por día" settlements
(Crédito, Débito, Prepago) against the Hotel Bordero, and validates each settlement
against its deposit in the Bank Statement, without writing to the database.
Database persistence lives in `clp_persistence.py` and Excel generation in `clp_report_exporter.py`.
Supports multiple ERP files (e.g. daily extracts, shifts, auxiliary borderos) with automatic deduplication.
"""

import os
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from ..models import CardType, SystemConfiguration
from .column_detector import (
    TRANSBANK_CLP_COLUMN_RULES,
    resolve_dataframe_columns,
    normalize_text,
    normalize_auth_code,
    clean_code_string,
    safe_decimal,
    safe_date,
    safe_cell,
)
from .excel_parsers import (
    parse_bank_statement_movements,
    serialize_bank_movement,
    parse_bordero_clp_transactions,
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

# Labels of the settlement header block (rows above the voucher detail).
TBK_HEADER_AMOUNT_LABELS = {
    "total ventas (+)": "total_sales",
    "devolucion comision por anulacion (+)": "commission_refund",
    "iva devolucion comision por anulacion (+)": "commission_refund_vat",
    "comision transbank (-)": "commission",
    "iva de comision transbank (-)": "commission_vat",
    "ventas anuladas (-)": "voided_sales",
    "cobros por servicio (-)": "service_charges",
    "iva de cobros por servicio (-)": "service_charges_vat",
    "total abono": "total_deposit",
}
TBK_HEADER_BELOW_LABELS = {
    "empresa:": "company",
    "cuenta corriente:": "bank_account",
    "moneda:": "currency",
    "medio de pago:": "payment_medium",
}


def get_clp_tolerance() -> Decimal:
    """CLP rounding tolerance from SystemConfiguration (editable by superusers from /settings/)."""
    return SystemConfiguration.get_solo().clp_rounding_tolerance


def card_code_from_masked_number(masked_card: str) -> str:
    """Infers the card brand from the BIN of a masked card number ('5525 61** **** 5410' -> 'MC')."""
    digits = "".join(ch for ch in masked_card if ch.isdigit())[:2]
    if not digits:
        return ""
    if digits.startswith("4"):
        return "VI"
    if digits[0] in ("2", "5"):
        return "MC"
    if digits in ("34", "37"):
        return "AX"
    if digits in ("30", "36", "38"):
        return "DI"
    return ""


def _find_detail_header_row(dataframe: pd.DataFrame) -> int:
    for row_index in range(min(len(dataframe), 80)):
        if any(normalize_text(val) == "fecha de venta" for val in dataframe.iloc[row_index].values):
            return row_index
    return min(len(dataframe), 28)


def parse_transbank_clp_settlement(dataframe: pd.DataFrame, modality: str) -> Dict[str, Any]:
    """
    Reads one Transbank 'Abonos por día' settlement (Crédito, Débito or Prepago):
    the header block (period, account, medium, totals) and every sale in the voucher detail.
    """
    detail_header_row = _find_detail_header_row(dataframe)
    header: Dict[str, Any] = {key: Decimal("0") for key in TBK_HEADER_AMOUNT_LABELS.values()}
    header.update({key: "" for key in TBK_HEADER_BELOW_LABELS.values()})
    header["settlement_date"] = None

    for row_index in range(detail_header_row):
        row = dataframe.iloc[row_index]
        cells = [(col, val) for col, val in enumerate(row.values) if pd.notna(val) and str(val).strip()]
        for position, (col, val) in enumerate(cells):
            label = normalize_text(val)
            if label.startswith("periodo de consulta") and position + 1 < len(cells):
                header["settlement_date"] = safe_date(cells[position + 1][1])
            elif label in TBK_HEADER_BELOW_LABELS and row_index + 1 < len(dataframe):
                below = safe_cell(dataframe.iloc[row_index + 1], col)
                header[TBK_HEADER_BELOW_LABELS[label]] = str(below or "").strip()
            elif label in TBK_HEADER_AMOUNT_LABELS and position + 1 < len(cells):
                header[TBK_HEADER_AMOUNT_LABELS[label]] = safe_decimal(cells[position + 1][1])

    columns = resolve_dataframe_columns(dataframe, TRANSBANK_CLP_COLUMN_RULES, max_scan_rows=detail_header_row + 3)
    sales: List[Dict[str, Any]] = []
    skipped_movements = 0

    for row_index in range(detail_header_row + 1, len(dataframe)):
        row = dataframe.iloc[row_index]
        movement_type = normalize_text(safe_cell(row, columns["movement_type"]))
        auth_code = clean_code_string(safe_cell(row, columns["authorization_code"]))
        gross_amount = safe_decimal(safe_cell(row, columns["gross_amount"]))
        if not auth_code or gross_amount <= 0:
            continue
        if movement_type and not movement_type.startswith("venta"):
            skipped_movements += 1
            continue

        sale_date = safe_date(safe_cell(row, columns["sale_date"]))
        masked_card = str(safe_cell(row, columns["masked_card_number"]) or "").strip()
        location = str(safe_cell(row, columns["merchant_location"]) or "").split(",")[0].strip()
        installment_number = clean_code_string(safe_cell(row, columns["installment_number"]))

        sales.append({
            "excel_row": row_index + 1,
            "payment_modality": modality,
            "card_code": card_code_from_masked_number(masked_card),
            "sale_date": sale_date,
            "merchant_code": clean_code_string(safe_cell(row, columns["merchant_code"])),
            "merchant_location": location,
            "gross_amount": gross_amount,
            "installment_type": str(safe_cell(row, columns["installment_type"]) or "").strip(),
            "installment_number": installment_number,
            "valid_amount": safe_decimal(safe_cell(row, columns["valid_amount"])),
            "commission": safe_decimal(safe_cell(row, columns["commission"])),
            "commission_vat": safe_decimal(safe_cell(row, columns["commission_vat"])),
            "net_deposit": safe_decimal(safe_cell(row, columns["net_deposit"])),
            "masked_card": masked_card,
            "authorization_code": auth_code,
            "transaction_identifier": clean_code_string(safe_cell(row, columns["transaction_identifier"])),
            "receipt_number": clean_code_string(safe_cell(row, columns["receipt_number"])),
        })

    return {"header": header, "sales": sales, "skipped_movements": skipped_movements}


def parse_bordero_period(dataframe: pd.DataFrame) -> Tuple[Optional[date], Optional[date]]:
    """Reads 'Periodo de: 04/09/2026 hasta 07/09/2026' from the Bordero header."""
    for row_index in range(min(len(dataframe), 15)):
        cells = [val for val in dataframe.iloc[row_index].values if pd.notna(val) and str(val).strip()]
        labels = [normalize_text(val) for val in cells]
        if labels and labels[0].startswith("periodo de"):
            dates = [safe_date(val) for val in cells[1:]]
            dates = [d for d in dates if d]
            if dates:
                return min(dates), max(dates)
    return None, None


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

        candidates = []
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
            "bank_description": matched.description if matched else "",
            "bank_movement_index": matched_idx,
            "difference": float(expected - matched.deposit_amount) if matched else float(expected),
            "is_matched": matched is not None,
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
    candidates = [i for i in rows_by_auth_code.get(normalize_auth_code(sale["authorization_code"]), []) if i not in claimed_erp]
    compatible = [i for i in candidates if _is_compatible(modality, erp_rows[i])]
    pool = compatible or candidates

    period_start, period_end = period
    sale_date = sale["sale_date"]
    outside_period = bool(
        sale_date and period_start and period_end and not (period_start <= sale_date <= period_end)
    ) or _installment_is_later(sale["installment_number"])

    def _amount(indices):
        return sum((Decimal(str(erp_rows[i]["amount"])) for i in indices), Decimal("0"))

    linked: List[int] = []
    if pool:
        # Net of every row of the code first (cancels reversals), then the closest single row.
        if abs(gross - _amount(pool)) <= tolerance:
            linked = pool
        else:
            closest = min(pool, key=lambda i: abs(gross - Decimal(str(erp_rows[i]["amount"]))))
            if abs(gross - Decimal(str(erp_rows[closest]["amount"]))) <= tolerance:
                linked = [closest]

    if linked:
        erp_amount = _amount(linked)
        if compatible:
            result = "MATCHED"
            observation = "Coincide código y monto en Borderó"
            if len(linked) > 1:
                observation = f"Coincide neto de {len(linked)} filas del Borderó (incluye reversas)"
        else:
            result = "CARD_TYPE_MISMATCH"
            observation = (
                f"Monto coincide, pero el Borderó lo registra como {erp_rows[linked[0]]['payment_code']} "
                f"(no corresponde a {MODALITY_LABELS[modality]})"
            )
    elif pool and not outside_period:
        linked = pool
        erp_amount = _amount(pool)
        result = "AMOUNT_MISMATCH"
        observation = f"Código en Borderó con diferencia de monto ({erp_amount:.0f} en Borderó)"
    elif outside_period:
        erp_amount = Decimal("0")
        result = "OUT_OF_PERIOD"
        if _installment_is_later(sale["installment_number"]):
            observation = f"Cuota {sale['installment_number']} de una venta anterior: buscar en el Borderó de la venta original"
        else:
            observation = "Venta fuera del período del Borderó cargado: buscar en el Borderó de esa fecha"
        if pool:
            observation += " (el código existe en este Borderó, pero para otra venta)"
    else:
        erp_amount = Decimal("0")
        result = "NOT_FOUND_IN_ERP"
        observation = f"Voucher {sale['authorization_code']} no encontrado en ERP Hotel"

    if _installment_parts(sale["installment_number"]) and result == "MATCHED":
        observation += f" · Venta en cuotas ({sale['installment_number']})"

    for i in linked:
        claimed_erp.add(i)
        if result == "MATCHED":
            erp_rows[i]["reconciled"] = True

    return {"linked": linked, "erp_amount": erp_amount, "result": result, "observation": observation}


def calculate_clp_preview(
    erp_file,
    bank_statement_file,
    transbank_credit_file,
    transbank_debit_file,
    transbank_prepaid_file,
    extra_erp_files: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """
    Executes the complete Chilean Pesos reconciliation workflow purely in memory.
    Consolidates primary Bordero and any auxiliary Borderos with automatic deduplication.
    """
    tolerance = get_clp_tolerance()
    card_names = {**DEFAULT_CARD_NAMES, **{card.code: card.name for card in CardType.objects.all()}}

    uploaded = {
        "erp": erp_file,
        "bank": bank_statement_file,
        "CREDIT": transbank_credit_file,
        "DEBIT": transbank_debit_file,
        "PREPAID": transbank_prepaid_file,
    }
    for uploaded_file in uploaded.values():
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)

    erp_file_name = os.path.basename(getattr(erp_file, "name", "BORDERO.xlsx"))
    bordero_dataframe = pd.read_excel(erp_file, header=None)
    bank_dataframe = pd.read_excel(bank_statement_file, header=None)
    settlements = {
        modality: parse_transbank_clp_settlement(pd.read_excel(uploaded[modality], header=None), modality)
        for modality, _, _, _ in CLP_MODALITIES
    }

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
            if sig in seen_clp_sigs:
                continue
            seen_clp_sigs.add(sig)
            pos = len(erp_rows)
            extra_row["is_auxiliary"] = True
            erp_rows.append(extra_row)
            norm_code = normalize_auth_code(extra_row.get("authorization_code"))
            if norm_code:
                rows_by_auth_code.setdefault(norm_code, []).append(pos)

    period = parse_bordero_period(bordero_dataframe)
    if not all(period):
        row_dates = sorted({safe_date(r["transaction_date"]) for r in erp_rows} - {None})
        period = (row_dates[0], row_dates[-1]) if row_dates else (None, None)

    # 3. Cross-match every Transbank voucher (source of truth) against the Bordero
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "MATCHED": [], "AMOUNT_MISMATCH": [], "CARD_TYPE_MISMATCH": [], "NOT_FOUND_IN_ERP": [], "OUT_OF_PERIOD": [],
    }
    transbank_transactions_data: List[Dict[str, Any]] = []
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

        difference = sale["gross_amount"] - match["erp_amount"] if linked else sale["gross_amount"]
        voucher = {
            "tbk_index": tbk_index,
            "erp_index": primary_idx,
            "erp_indices": linked,
            "tbk_excel_row": sale["excel_row"],
            "erp_excel_rows": [erp_rows[i]["excel_row"] for i in linked],
            "payment_modality": sale["payment_modality"],
            "modality_label": MODALITY_LABELS[sale["payment_modality"]],
            "authorization_code": sale["authorization_code"],
            "erp_authorization_code": primary_erp["authorization_code"] if primary_erp else "",
            "erp_payment_code": primary_erp["payment_code"] if primary_erp else "",
            "invoice_number": primary_erp["invoice_number"] if primary_erp else "",
            "document_number": primary_erp["account_code"] if primary_erp else "",
            "card_code": card_code,
            "card_name": card_names.get(card_code, card_code or "—"),

            "masked_card": sale["masked_card"],
            "sale_date": sale_date_str,
            "merchant_location": sale["merchant_location"],
            "installment_number": sale["installment_number"],
            "guest_name": (primary_erp["guest_name"] if primary_erp else "") or "—",
            "room_number": (primary_erp["room_number"] if primary_erp else "") or "—",
            "cashier_username": (primary_erp["cashier_username"] if primary_erp else "") or "—",
            "erp_date": primary_erp["transaction_date"] if primary_erp else "",
            "tbk_amount": float(sale["gross_amount"]),
            "valid_amount": float(sale["valid_amount"]),
            "net_deposit": float(sale["net_deposit"]),
            "commissions": float(sale["commission"] + sale["commission_vat"]),
            "erp_amount": float(match["erp_amount"]),
            "difference": float(difference),
            "system_result": match["result"],
            "observation": match["observation"],
        }
        buckets[match["result"]].append(voucher)

    # 4. Summaries by payment modality (screen) and by modality + card brand (persistence)
    all_vouchers = [v for bucket in buckets.values() for v in bucket]
    modality_table: List[Dict[str, Any]] = []
    brand_accumulators: Dict[Tuple[str, str], Dict[str, Decimal]] = {}

    for modality, label, deposit_modality, _ in CLP_MODALITIES:
        sales = settlements[modality]["sales"]
        header = settlements[modality]["header"]
        vouchers = [v for v in all_vouchers if v["payment_modality"] == modality]
        tbk_total = sum((s["gross_amount"] for s in sales), Decimal("0"))
        erp_total = sum((Decimal(str(v["erp_amount"])) for v in vouchers), Decimal("0"))
        valid_total = sum((s["valid_amount"] for s in sales), Decimal("0"))
        net_total = sum((s["net_deposit"] for s in sales), Decimal("0"))
        validation = next(b for b in bank_validations if b["payment_modality"] == modality)

        modality_table.append({
            "payment_modality": modality,
            "modality_label": label,
            "deposit_modality": deposit_modality,
            "vouchers": len(vouchers),
            "matched": sum(1 for v in vouchers if v["system_result"] == "MATCHED"),
            "exceptions": sum(1 for v in vouchers if v["system_result"] != "MATCHED"),
            "tbk_sales": float(tbk_total),
            "erp_sales": float(erp_total),
            "difference": float(tbk_total - erp_total),
            "valid_sales": float(valid_total),
            "commissions": float(header["commission"] + header["commission_vat"]),
            "net_deposit": float(net_total),
            "header_matches_detail": valid_total == header["total_sales"] and net_total == header["total_deposit"],
            "bank_matched": validation["is_matched"],
        })

        for voucher in vouchers:
            acc = brand_accumulators.setdefault(
                (modality, voucher["card_code"]),
                {"tbk": Decimal("0"), "erp": Decimal("0"), "commissions": Decimal("0"), "net": Decimal("0")},
            )
            acc["tbk"] += Decimal(str(voucher["tbk_amount"]))
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
            "tbk_credit": getattr(transbank_credit_file, "name", "CREDITOS TBK.xlsx"),
            "tbk_debit": getattr(transbank_debit_file, "name", "DEBITOS TBK.xlsx"),
            "tbk_prepaid": getattr(transbank_prepaid_file, "name", "PREPAGO TBK.xlsx"),
        },
        "bank_account_number": detected_account or next((b["bank_account"] for b in bank_validations if b["bank_account"]), ""),
        "bank_validations": bank_validations,
        "bank_movements": bank_movements_data,
        "erp_transactions": erp_rows,
        "auxiliary_erp_transactions": auxiliary_erp_rows,
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
