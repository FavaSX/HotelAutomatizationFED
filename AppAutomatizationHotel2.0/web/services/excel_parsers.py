"""
Shared Excel Parsers for Hotel Plaza San Francisco Financial Reconciliation (USD & CLP).
Extracts, cleans, and indexes records in memory from:
  - Bank Statements (Banco de Chile & BancoEstado)
  - Hotel ERP / Bordero (USD & CLP)
  - Foreign Card Operator Statements (Amex, Diners, Visa, MasterCard)
  - Transbank CLP Settlements (Consolidated 'Todos los medios de pago' or Separate Modalities)
"""

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from ..models import (
    CardType,
    BankStatementMovement,
    ErpTransaction,
    CardOperatorTransaction,
)
from .column_detector import (
    ERP_COLUMN_RULES,
    BANK_STATEMENT_COLUMN_RULES,
    CARD_OPERATOR_COLUMN_RULES,
    TRANSBANK_CLP_COLUMN_RULES,
    resolve_dataframe_columns,
    normalize_text,
    clean_code_string,
    normalize_auth_code,
    safe_decimal,
    safe_date,
    safe_time,
    safe_cell,
)


# ==============================================================================
# 1. BANK STATEMENT PARSERS (USD & CLP)
# ==============================================================================

def parse_bank_statement_movements(bank_dataframe: pd.DataFrame) -> List[BankStatementMovement]:
    """
    Parses every charge / deposit row of a Bank Statement (cartola.xls or cartola.xlsx)
    in memory using hybrid column detection.
    """
    columns = resolve_dataframe_columns(bank_dataframe, BANK_STATEMENT_COLUMN_RULES)
    bank_movements: List[BankStatementMovement] = []

    for _, row in bank_dataframe.iterrows():
        deposit_amount = safe_decimal(safe_cell(row, columns["deposit_amount"]))
        charge_amount = safe_decimal(safe_cell(row, columns["charge_amount"]))
        date_cell = safe_cell(row, columns["movement_date"])

        if (deposit_amount <= 0 and charge_amount <= 0) or str(date_cell).strip().lower() == "fecha":
            continue

        description = str(safe_cell(row, columns["description"]) or "").strip()
        is_tbk = "TRANSBANK" in description.upper() or "TBK" in description.upper() or "DIVISAS" in description.upper()

        movement = BankStatementMovement(
            movement_date=safe_date(date_cell),
            description=description,
            branch_or_channel=str(safe_cell(row, columns["branch_or_channel"]) or "").strip(),
            document_number=clean_code_string(safe_cell(row, columns["document_number"])),
            charge_amount=charge_amount,
            deposit_amount=deposit_amount,
            balance_amount=safe_decimal(safe_cell(row, columns["balance_amount"])),
            is_transbank_deposit=is_tbk,
        )
        bank_movements.append(movement)

    return bank_movements


def serialize_bank_movement(movement: BankStatementMovement, is_matched_target: bool) -> Dict[str, Any]:
    """JSON-serializable representation of a bank movement for the in-memory preview session."""
    return {
        "movement_date": movement.movement_date.strftime("%d/%m/%Y") if movement.movement_date else None,
        "description": movement.description,
        "branch_or_channel": movement.branch_or_channel,
        "document_number": movement.document_number,
        "charge_amount": float(movement.charge_amount),
        "deposit_amount": float(movement.deposit_amount),
        "balance_amount": float(movement.balance_amount),
        "is_transbank_deposit": movement.is_transbank_deposit,
        "is_matched_target": is_matched_target,
    }


def parse_and_validate_bank_deposits(
    bank_dataframe: pd.DataFrame,
    expected_deposits: List[Tuple[str, Decimal]],
    tolerance: Decimal = Decimal("0.50")
) -> List[Dict[str, Any]]:
    """
    Parses the Bank Statement (cartola.xls or cartola.xlsx) in memory using hybrid column detection
    and validates one or multiple expected Transbank deposits.
    """
    bank_movements = parse_bank_statement_movements(bank_dataframe)

    validations_summary = []
    for modality_code, expected_amount in expected_deposits:
        matched_movement = next(
            (m for m in bank_movements if m.deposit_amount > 0 and abs(m.deposit_amount - expected_amount) <= tolerance),
            None,
        )
        validations_summary.append({
            "modality": modality_code,
            "transbank_deposit": float(expected_amount),
            "bank_deposit": float(matched_movement.deposit_amount) if matched_movement else 0.0,
            "movement_date": (
                matched_movement.movement_date.strftime("%d/%m/%Y")
                if (matched_movement and hasattr(matched_movement.movement_date, "strftime"))
                else str(matched_movement.movement_date or "") if matched_movement else None
            ),
            "is_matched": matched_movement is not None,
            "movements_data": [
                serialize_bank_movement(m, m is matched_movement) for m in bank_movements
            ],
        })

    return validations_summary


# ==============================================================================
# 2. USD ERP & CARD OPERATOR PARSERS (PHASE 1)
# ==============================================================================

def parse_erp_transactions(
    erp_dataframe: pd.DataFrame,
    card_types: Dict[str, CardType],
) -> Tuple[List[ErpTransaction], Dict[str, List[Dict[str, Any]]]]:
    """
    Parses the Hotel ERP / Bordero Excel file in memory using hybrid column detection,
    filters USD foreign card payments, and returns an O(1) hash map indexed by authorization_code.
    """
    columns = resolve_dataframe_columns(erp_dataframe, ERP_COLUMN_RULES)
    erp_by_auth_code: Dict[str, List[Dict[str, Any]]] = {}
    erp_instances: List[ErpTransaction] = []

    for _, row in erp_dataframe.iterrows():
        payment_code = str(safe_cell(row, columns["payment_code"]) or "").strip().upper()
        description = str(safe_cell(row, columns["payment_description"]) or "").strip()

        if "US$" not in description:
            continue
        raw_amount = safe_decimal(safe_cell(row, columns["amount"]))
        if raw_amount >= 0:
            continue
        amount_value = abs(raw_amount)
        upper_desc = description.upper()
        card_code = (
            "MC" if "MASTER" in upper_desc else
            "VI" if "VISA" in upper_desc else
            "AX" if "AMEX" in upper_desc else
            "DI"
        )
        document_number = clean_code_string(safe_cell(row, columns["account_code"]))
        auth_code = clean_code_string(safe_cell(row, columns["authorization_code"]))

        erp_instance = ErpTransaction(
            card_type=card_types[card_code],
            room_number=clean_code_string(safe_cell(row, columns["room_number"])),
            room_type=str(safe_cell(row, columns["room_type"]) or "").strip(),
            reservation_number=clean_code_string(safe_cell(row, columns["reservation_number"])),
            account_code=document_number,
            payment_code=payment_code,
            invoice_number=clean_code_string(safe_cell(row, columns["invoice_number"])),
            document_number=document_number,
            authorization_code=auth_code,
            transaction_date=safe_date(safe_cell(row, columns["transaction_date"])),
            transaction_time=safe_time(safe_cell(row, columns["transaction_time"])),
            cashier_username=str(safe_cell(row, columns["cashier_username"]) or "").strip(),
            guest_name=str(safe_cell(row, columns["guest_name"]) or "").strip(),
            amount=amount_value,
            source_currency="USD",
            reconciled=False,
        )
        erp_instances.append(erp_instance)

        if auth_code:
            erp_by_auth_code.setdefault(auth_code, []).append({
                "instance": erp_instance,
                "document_number": document_number,
                "invoice_number": erp_instance.invoice_number,
                "amount_clp": amount_value,
                "payment_code": payment_code,
                "card_code": card_code,
            })

    return erp_instances, erp_by_auth_code


def parse_card_operator_statements(
    operator_dataframes: Dict[str, pd.DataFrame],
    card_types: Dict[str, CardType]
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Parses the 4 Card Operator statements (Amex, Diners, Visa, MasterCard) in memory
    using hybrid column detection and returns an O(1) lookup dictionary indexed by [card_code][document_number].
    """
    card_operator_index: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        code: {} for code in operator_dataframes.keys()
    }

    for card_code, card_df in operator_dataframes.items():
        columns = resolve_dataframe_columns(card_df, CARD_OPERATOR_COLUMN_RULES)
        card_model = card_types[card_code]

        for _, row in card_df.iterrows():
            document_number = clean_code_string(safe_cell(row, columns["document_number"]))
            if not document_number or not document_number.isdigit():
                continue

            sequence_number = clean_code_string(safe_cell(row, columns["sequence_number"]))
            raw_col_12 = safe_cell(row, columns["foreign_currency_amount"])
            raw_col_13 = safe_cell(row, columns["balance_amount"])
            raw_col_14 = safe_cell(row, columns["corrected_balance_amount"])

            foreign_currency = safe_decimal(raw_col_12)
            balance_clp = safe_decimal(raw_col_13)
            corrected_balance_clp = safe_decimal(raw_col_14)

            # Handle legacy fixed-width column shift when USD < 10.00 (Col 12 is NaN, Col 13 has USD, Col 14 has CLP)
            if pd.isna(raw_col_12) and not pd.isna(raw_col_13) and not pd.isna(raw_col_14):
                foreign_currency = balance_clp
                balance_clp = Decimal("0.00")

            operator_record = CardOperatorTransaction(
                card_type=card_model,
                document_number=document_number,
                sequence_number=sequence_number,
                foreign_currency_amount=foreign_currency,
                balance_amount=balance_clp,
                corrected_balance_amount=corrected_balance_clp,
            )

            card_operator_index[card_code].setdefault(document_number, []).append({
                "document_number": document_number,
                "sequence_number": sequence_number,
                "foreign_currency_usd": foreign_currency,
                "balance_clp": balance_clp,
                "corrected_balance_clp": corrected_balance_clp,
                "instance": operator_record,
            })

    return card_operator_index


# ==============================================================================
# 3. CLP BORDERO & TRANSBANK SETTLEMENT PARSERS (PHASE 2)
# ==============================================================================

# Peso card payment codes of the Bordero, classified by the description text.
# Red Compra (RCO) carries both debit and prepaid cards; every other card code is credit.
CLP_BORDERO_CARD_KEYWORDS = (
    ("REDCOMPRA", None),
    ("RED COMPRA", None),
    ("VISA", "VI"),
    ("MASTER", "MC"),
    ("AMEX", "AX"),
    ("AMERICAN", "AX"),
    ("DINERS", "DI"),
)

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


def classify_bordero_clp_payment(description: str):
    """
    Returns (is_peso_card_payment, payment_group, card_code) for a Bordero 'Descripción'.
    payment_group is 'REDCOMPRA' (debit / prepaid) or 'CREDIT'. US$ rows belong to Phase 1.
    """
    upper_desc = description.upper()
    if "US$" in upper_desc or ("$" not in upper_desc and "COMPRA" not in upper_desc):
        return False, None, None
    for keyword, card_code in CLP_BORDERO_CARD_KEYWORDS:
        if keyword in upper_desc:
            return True, ("REDCOMPRA" if card_code is None else "CREDIT"), card_code
    return False, None, None


def parse_bordero_clp_transactions(
    bordero_dataframe: pd.DataFrame,
    source_file: str = "",
) -> Tuple[List[Dict[str, Any]], Dict[str, List[int]]]:
    """
    Parses the Hotel Bordero ("Cargos de Débitos y Créditos") in memory, keeping only peso card
    payments (Red Compra, Visa $, Master Card $, Amex $, Diners $) including their reversals.
    Repeated page headers, 'Total VHF' and footer rows are skipped because they carry no card code.

    Returns the serialized rows and an index {normalized_auth_code: [row positions]}.
    """
    columns = resolve_dataframe_columns(bordero_dataframe, ERP_COLUMN_RULES)
    rows: List[Dict[str, Any]] = []
    rows_by_auth_code: Dict[str, List[int]] = {}

    for excel_row_number, (_, row) in enumerate(bordero_dataframe.iterrows(), start=1):
        description = str(safe_cell(row, columns["payment_description"]) or "").strip()
        is_card_payment, payment_group, card_code = classify_bordero_clp_payment(description)
        if not is_card_payment:
            continue
        raw_amount = safe_decimal(safe_cell(row, columns["amount"]))
        if raw_amount == 0:
            continue

        # The 'Factura' header sits one column to the right of its merged data cell.
        invoice_number = clean_code_string(safe_cell(row, columns["invoice_number"]))
        if not invoice_number:
            invoice_number = clean_code_string(safe_cell(row, columns["invoice_number"] - 1))

        raw_auth_code = clean_code_string(safe_cell(row, columns["authorization_code"]))
        transaction_date = safe_date(safe_cell(row, columns["transaction_date"]))
        transaction_time = safe_time(safe_cell(row, columns["transaction_time"]))

        position = len(rows)
        rows.append({
            "excel_row": excel_row_number,
            "source_file": source_file,
            "card_code": card_code or "",
            "payment_group": payment_group,
            "payment_code": str(safe_cell(row, columns["payment_code"]) or "").strip().upper(),
            "payment_description": description,
            "room_number": clean_code_string(safe_cell(row, columns["room_number"])),
            "room_type": str(safe_cell(row, columns["room_type"]) or "").strip(),
            "reservation_number": clean_code_string(safe_cell(row, columns["reservation_number"])),
            "account_code": clean_code_string(safe_cell(row, columns["account_code"])),
            "invoice_number": invoice_number,
            "authorization_code": raw_auth_code,
            "transaction_date": transaction_date.strftime("%d/%m/%Y") if transaction_date else None,
            "transaction_time": transaction_time.strftime("%H:%M:%S") if transaction_time else None,
            "cashier_username": str(safe_cell(row, columns["cashier_username"]) or "").strip(),
            "guest_name": " ".join(str(safe_cell(row, columns["guest_name"]) or "").split()),
            # Payments are negative in the Bordero; reversals come as positive values.
            "amount": float(-raw_amount),
            "is_reversal": bool(raw_amount > 0),
            "reconciled": False,
        })

        normalized_code = normalize_auth_code(raw_auth_code)
        if normalized_code:
            rows_by_auth_code.setdefault(normalized_code, []).append(position)

    return rows, rows_by_auth_code


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


def parse_universal_transbank_clp(files_or_dfs: List[Any]) -> Dict[str, Dict[str, Any]]:
    """
    Universal Transbank Chilean Pesos (CLP) Parser.
    Accepts:
      - A single consolidated file ('Todos los medios de pago') containing Débito, Crédito, and Prepago.
      - OR multiple separate files (e.g. Crédito, Débito, Prepago uploaded individually).
    Correctly splits transactions into the 3 modalities, computes sub-totals,
    and isolates installment sales using 'valid_amount' (monto venta válido para abono).
    """
    clp_modalities_map = [
        ("CREDIT", "Crédito"),
        ("DEBIT", "Débito"),
        ("PREPAID", "Prepago"),
    ]
    settlements: Dict[str, Dict[str, Any]] = {
        modality: {
            "header": {
                "total_sales": Decimal("0"),
                "sales_total": Decimal("0"),
                "commission": Decimal("0"),
                "commission_vat": Decimal("0"),
                "total_deposit": Decimal("0"),
                "calculated_deposit": Decimal("0"),
                "settlement_date": None,
                "company": "",
                "bank_account": "",
                "currency": "Pesos",
                "payment_medium": label,
            },
            "sales": [],
            "skipped_movements": 0,
        }
        for modality, label in clp_modalities_map
    }

    if not files_or_dfs:
        return settlements

    for item in files_or_dfs:
        if item is None:
            continue
        if isinstance(item, pd.DataFrame):
            df = item
        else:
            if hasattr(item, "seek"):
                item.seek(0)
            try:
                df = pd.read_excel(item, header=None)
            except Exception:
                if hasattr(item, "seek"):
                    item.seek(0)
                try:
                    tables = pd.read_html(item)
                    df = tables[0] if tables else pd.DataFrame()
                except Exception:
                    df = pd.DataFrame()

        if df is None or len(df) == 0:
            continue

        detail_header_row = _find_detail_header_row(df)

        settlement_date = None
        header_medium = ""
        company = ""
        bank_account = ""

        for row_index in range(min(len(df), detail_header_row)):
            row = df.iloc[row_index]
            cells = [(col, val) for col, val in enumerate(row.values) if pd.notna(val) and str(val).strip()]
            for position, (col, val) in enumerate(cells):
                label = normalize_text(val)
                if label.startswith("periodo de consulta") and position + 1 < len(cells):
                    settlement_date = safe_date(cells[position + 1][1])
                elif label.startswith("medio de pago") and row_index + 1 < len(df):
                    header_medium = normalize_text(df.iloc[row_index + 1, col])
                elif label.startswith("empresa") and row_index + 1 < len(df):
                    company = str(df.iloc[row_index + 1, col] or "").strip()
                elif label.startswith("cuenta corriente") and row_index + 1 < len(df):
                    bank_account = str(df.iloc[row_index + 1, col] or "").strip()

        columns = resolve_dataframe_columns(df, TRANSBANK_CLP_COLUMN_RULES, max_scan_rows=detail_header_row + 3)
        is_consolidated = "todos los medios" in header_medium or not header_medium

        for row_index in range(detail_header_row + 1, len(df)):
            row = df.iloc[row_index]
            auth_code = clean_code_string(safe_cell(row, columns["authorization_code"]))
            if not auth_code:
                continue

            movement_type = normalize_text(safe_cell(row, columns["movement_type"]))
            is_void = "anulac" in movement_type or "devoluc" in movement_type
            if movement_type and not movement_type.startswith("venta") and not is_void:
                continue

            # Modality check: first check row-level medium column, fallback to header
            row_medium = normalize_text(safe_cell(row, columns.get("payment_medium", 5)))
            if "deb" in row_medium:
                modality = "DEBIT"
            elif "prep" in row_medium:
                modality = "PREPAID"
            elif "cred" in row_medium:
                modality = "CREDIT"
            else:
                if "deb" in header_medium:
                    modality = "DEBIT"
                elif "prep" in header_medium:
                    modality = "PREPAID"
                else:
                    modality = "CREDIT"

            gross_amount = safe_decimal(safe_cell(row, columns["gross_amount"]))
            valid_amount = safe_decimal(safe_cell(row, columns["valid_amount"])) or gross_amount
            commission = safe_decimal(safe_cell(row, columns["commission"]))
            commission_vat = safe_decimal(safe_cell(row, columns["commission_vat"]))
            net_deposit = safe_decimal(safe_cell(row, columns["net_deposit"]))

            if is_void:
                gross_amount = -abs(gross_amount)
                valid_amount = -abs(valid_amount)
                net_deposit = -abs(net_deposit)

            sale_date = safe_date(safe_cell(row, columns["sale_date"]))
            masked_card = str(safe_cell(row, columns["masked_card_number"]) or "").strip()
            location = str(safe_cell(row, columns["merchant_location"]) or "").split(",")[0].strip()
            installment_number = clean_code_string(safe_cell(row, columns["installment_number"]))

            settlements[modality]["sales"].append({
                "excel_row": row_index + 1,
                "payment_modality": modality,
                "card_code": card_code_from_masked_number(masked_card),
                "sale_date": sale_date,
                "merchant_code": clean_code_string(safe_cell(row, columns["merchant_code"])),
                "merchant_location": location,
                "gross_amount": gross_amount,
                "installment_type": str(safe_cell(row, columns["installment_type"]) or "").strip(),
                "installment_number": installment_number,
                "valid_amount": valid_amount,
                "commission": commission,
                "commission_vat": commission_vat,
                "net_deposit": net_deposit,
                "masked_card": masked_card,
                "authorization_code": auth_code,
                "transaction_identifier": clean_code_string(safe_cell(row, columns["transaction_identifier"])),
                "receipt_number": clean_code_string(safe_cell(row, columns["receipt_number"])),
                "is_void": is_void,
            })

            h = settlements[modality]["header"]
            h["total_sales"] += valid_amount
            h["sales_total"] += valid_amount
            h["commission"] += commission
            h["commission_vat"] += commission_vat
            h["total_deposit"] += net_deposit
            h["calculated_deposit"] += net_deposit
            if settlement_date and not h["settlement_date"]:
                h["settlement_date"] = settlement_date
            if company and not h["company"]:
                h["company"] = company
            if bank_account and not h["bank_account"]:
                h["bank_account"] = bank_account

        # If not consolidated, read official header amounts from rows 16-25
        if not is_consolidated:
            target_mod = "CREDIT" if "cred" in header_medium else ("DEBIT" if "deb" in header_medium else "PREPAID")
            h = settlements[target_mod]["header"]
            for row_index in range(min(len(df), detail_header_row)):
                row = df.iloc[row_index]
                cells = [(col, val) for col, val in enumerate(row.values) if pd.notna(val) and str(val).strip()]
                for position, (col, val) in enumerate(cells):
                    label = normalize_text(val)
                    if label in TBK_HEADER_AMOUNT_LABELS and position + 1 < len(cells):
                        amt = safe_decimal(cells[position + 1][1])
                        field = TBK_HEADER_AMOUNT_LABELS[label]
                        if field == "total_sales" and amt > 0:
                            h["total_sales"] = amt
                            h["sales_total"] = amt
                        elif field == "commission" and amt > 0:
                            h["commission"] = amt
                        elif field == "commission_vat" and amt > 0:
                            h["commission_vat"] = amt
                        elif field == "total_deposit" and amt > 0:
                            h["total_deposit"] = amt
                            h["calculated_deposit"] = amt

    return settlements


def parse_transbank_clp_settlement(dataframe: pd.DataFrame, modality: str) -> Dict[str, Any]:
    """Legacy helper for backward compatibility: delegates to parse_universal_transbank_clp."""
    all_settlements = parse_universal_transbank_clp([dataframe])
    return all_settlements.get(modality) or {
        "header": {key: Decimal("0") for key in TBK_HEADER_AMOUNT_LABELS.values()},
        "sales": [],
        "skipped_movements": 0,
    }
