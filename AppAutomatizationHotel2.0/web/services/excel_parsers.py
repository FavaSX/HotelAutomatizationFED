"""
Shared Excel Parsers for Hotel Plaza San Francisco Financial Reconciliation (USD & CLP).
Extracts, cleans, and indexes records in memory from:
  - Bank Statements (Banco de Chile & BancoEstado)
  - Hotel ERP / Bordero (USD & CLP)
  - Foreign Card Operator Statements (Amex, Diners, Visa, MasterCard)
  - Transbank CLP Settlements (Consolidated 'Todos los medios de pago' or Separate Modalities)
"""

from collections import OrderedDict
from datetime import date
from decimal import Decimal
import hashlib
import math
from typing import Any, Dict, List, Optional, Tuple
import uuid
import pandas as pd

from ..models import (
    CardType,
    BankStatementMovement,
    ErpTransaction,
    CardOperatorTransaction,
)
from .erp_corrections import erp_source_key
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


def read_excel_or_html_df(file_or_df: Any, header: Optional[int] = None) -> pd.DataFrame:
    """Reads an Excel file (.xlsx, BIFF .xls, or HTML-table .xls) into a DataFrame with automatic fallback."""
    if isinstance(file_or_df, pd.DataFrame):
        return file_or_df
    if hasattr(file_or_df, "seek"):
        file_or_df.seek(0)
    try:
        return pd.read_excel(file_or_df, header=header)
    except Exception:
        if hasattr(file_or_df, "seek"):
            file_or_df.seek(0)
        try:
            tables = pd.read_html(file_or_df)
            return tables[0] if tables else pd.DataFrame()
        except Exception:
            return pd.DataFrame()


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

_FAST_NOT_NA_TYPES = (str, int, Decimal, bool)


def _tuple_cell(row_tuple: tuple, column_index: int) -> Any:
    """Fast O(1) cell lookup on a DataFrame tuple row, avoiding pandas Series allocation."""
    if column_index < 0 or column_index >= len(row_tuple):
        return None
    val = row_tuple[column_index]
    if val is None or val is pd.NaT or val is pd.NA:
        return None
    if isinstance(val, _FAST_NOT_NA_TYPES):
        return val
    if isinstance(val, float):
        return None if math.isnan(val) else val
    return None if pd.isna(val) else val


def parse_erp_transactions(
    erp_dataframe: pd.DataFrame,
    card_types: Dict[str, CardType],
) -> Tuple[List[ErpTransaction], Dict[str, List[Dict[str, Any]]]]:
    """
    Parses the Hotel ERP / Bordero Excel file in memory using hybrid column detection,
    filters USD foreign card payments, and returns an O(1) hash map indexed by authorization_code.
    """
    bundle = parse_bordero_bundle(erp_dataframe, card_types=card_types)
    return bundle["usd_instances"], bundle["usd_by_auth_code"]


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
    bundle = parse_bordero_bundle(bordero_dataframe, card_types=None, source_file=source_file)
    return bundle["clp_rows"], bundle["clp_rows_by_auth_code"]


_ADDITIONAL_ERP_CACHE: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
_MAX_ADDITIONAL_ERP_CACHE_ENTRIES = 16


def store_additional_erp_movements(rows: List[Dict[str, Any]]) -> str:
    """Stores full-Bordero movements in an in-memory bounded LRU cache so session payload stays lightweight."""
    if not rows:
        return ""
    token = uuid.uuid4().hex
    _ADDITIONAL_ERP_CACHE[token] = rows
    while len(_ADDITIONAL_ERP_CACHE) > _MAX_ADDITIONAL_ERP_CACHE_ENTRIES:
        _ADDITIONAL_ERP_CACHE.popitem(last=False)
    return token


def get_additional_erp_movements(token: Optional[str]) -> List[Dict[str, Any]]:
    """Retrieves cached full-Bordero movements by token without evicting them (supports idempotent re-commit)."""
    if not token:
        return []
    return _ADDITIONAL_ERP_CACHE.get(token, [])


def parse_all_bordero_movements(
    erp_dataframe: pd.DataFrame,
    source_file: str = "",
) -> List[Dict[str, Any]]:
    """
    Parses ALL valid movement rows from a Hotel Bordero ("Cargos de Débitos y Créditos") DataFrame
    regardless of currency or payment medium (USD cards, CLP cards, cash, room charges HOA, IVA,
    restaurant/bar charges, tips, transfers, etc.), discarding only blank rows, repeated page
    headers/footers, and subtotal/total summary lines.
    """
    bundle = parse_bordero_bundle(erp_dataframe, card_types=None, source_file=source_file)
    return bundle["all_movements"]


def parse_bordero_bundle(
    erp_dataframe: pd.DataFrame,
    card_types: Optional[Dict[str, CardType]] = None,
    source_file: str = "",
) -> Dict[str, Any]:
    """
    Single-pass Bordero extractor: in ONE fast tuple scan over the DataFrame, builds:
      - `usd_instances` + `usd_by_auth_code` (when `card_types` is passed)
      - `clp_rows` + `clp_rows_by_auth_code`
      - `all_movements` (with cached `_date_obj` / `_time_obj` to avoid re-parsing during DB sync)
      - `stats` (min/max dates, period header, card vs non-card movement counts for coverage validation)
    """
    empty_stats = {
        "period_header": (None, None),
        "min_date": None,
        "max_date": None,
        "total_movements": 0,
        "card_movements": 0,
        "non_card_movements": 0,
        "usd_card_movements": 0,
        "clp_card_movements": 0,
    }
    if erp_dataframe is None or len(erp_dataframe) == 0:
        return {
            "usd_instances": [],
            "usd_by_auth_code": {},
            "clp_rows": [],
            "clp_rows_by_auth_code": {},
            "all_movements": [],
            "stats": empty_stats,
        }

    columns = resolve_dataframe_columns(erp_dataframe, ERP_COLUMN_RULES)
    c_room = columns["room_number"]
    c_rtype = columns["room_type"]
    c_res = columns["reservation_number"]
    c_acc = columns["account_code"]
    c_pcode = columns["payment_code"]
    c_pdesc = columns["payment_description"]
    c_inv = columns["invoice_number"]
    c_inv_alt = c_inv - 1 if c_inv - 1 > c_pdesc else -1
    c_amt = columns["amount"]
    c_auth = columns["authorization_code"]
    c_date = columns["transaction_date"]
    c_time = columns["transaction_time"]
    c_user = columns["cashier_username"]
    c_guest = columns["guest_name"]

    usd_instances: List[ErpTransaction] = []
    usd_by_auth_code: Dict[str, List[Dict[str, Any]]] = {}
    clp_rows: List[Dict[str, Any]] = []
    clp_rows_by_auth_code: Dict[str, List[int]] = {}
    all_movements: List[Dict[str, Any]] = []
    seen_key_counts: Dict[str, int] = {}

    min_date: Optional[date] = None
    max_date: Optional[date] = None
    total_movements = 0
    card_movements = 0
    non_card_movements = 0
    usd_card_movements = 0
    clp_card_movements = 0

    for excel_row_number, row in enumerate(erp_dataframe.itertuples(index=False, name=None), start=1):
        raw_desc = _tuple_cell(row, c_pdesc)
        description = " ".join(str(raw_desc or "").split())
        payment_code = str(_tuple_cell(row, c_pcode) or "").strip().upper()
        is_header_row = not payment_code or ("COD" in payment_code and "DEB" in payment_code)

        amt_col = c_amt
        auth_col = c_auth
        date_col = c_date
        time_col = c_time
        user_col = c_user
        guest_col = c_guest

        tx_date = safe_date(_tuple_cell(row, date_col))
        amt_cell = _tuple_cell(row, amt_col)
        if tx_date is None and amt_cell is None:
            shifted_date = safe_date(_tuple_cell(row, date_col + 1))
            if shifted_date is not None:
                tx_date = shifted_date
                amt_col += 1
                auth_col += 1
                date_col += 1
                time_col += 1
                user_col += 1
                guest_col += 1
                amt_cell = _tuple_cell(row, amt_col)

        raw_amount = safe_decimal(amt_cell)
        upper_desc = description.upper()

        usd_card_code = (
            "MC" if ("US$" in upper_desc and ("MASTER" in upper_desc or payment_code in ("MCUS$", "MUS$"))) else
            "VI" if ("US$" in upper_desc and ("VISA" in upper_desc or payment_code == "VUS$")) else
            "AX" if ("US$" in upper_desc and ("AMEX" in upper_desc or "AMERICAN" in upper_desc or payment_code == "AUS$")) else
            "DI" if ("US$" in upper_desc and ("DINER" in upper_desc or payment_code == "DUS$")) else
            ""
        )
        is_usd_card_row = bool(usd_card_code and "US$" in description and raw_amount < 0)
        is_clp_card_row, clp_payment_group, clp_card_code = classify_bordero_clp_payment(description)
        is_clp_card_row = bool(is_clp_card_row and raw_amount != 0)
        include_in_all = bool(not is_header_row and tx_date is not None)

        if not (include_in_all or (card_types is not None and is_usd_card_row) or is_clp_card_row):
            continue

        account_code = clean_code_string(_tuple_cell(row, c_acc))
        auth_code = clean_code_string(_tuple_cell(row, auth_col))
        invoice_number = clean_code_string(_tuple_cell(row, c_inv))
        if not invoice_number and c_inv_alt >= 0:
            invoice_number = clean_code_string(_tuple_cell(row, c_inv_alt))

        tx_time = safe_time(_tuple_cell(row, time_col))
        tx_date_str = tx_date.strftime("%d/%m/%Y") if tx_date else None
        tx_time_str = tx_time.strftime("%H:%M:%S") if tx_time else None

        room_number = clean_code_string(_tuple_cell(row, c_room))
        room_type = str(_tuple_cell(row, c_rtype) or "").strip()
        reservation_number = clean_code_string(_tuple_cell(row, c_res))
        cashier_username = str(_tuple_cell(row, user_col) or "").strip()
        guest_name = " ".join(str(_tuple_cell(row, guest_col) or "").split())

        if include_in_all:
            if min_date is None or tx_date < min_date:
                min_date = tx_date
            if max_date is None or tx_date > max_date:
                max_date = tx_date
            total_movements += 1

            if usd_card_code and raw_amount != 0:
                movement_category = "USD_CARD"
                source_currency = "USD"
                mov_card_code = usd_card_code
                mov_payment_group = "CREDIT"
                mov_amount_val = float(-raw_amount)
                card_movements += 1
                usd_card_movements += 1
            elif is_clp_card_row:
                movement_category = "CLP_CARD"
                source_currency = "CLP"
                mov_card_code = clp_card_code or ""
                mov_payment_group = clp_payment_group
                mov_amount_val = float(-raw_amount)
                card_movements += 1
                clp_card_movements += 1
            else:
                movement_category = "OTHER"
                source_currency = "USD" if ("US$" in upper_desc or payment_code.endswith("US$")) else "CLP"
                mov_card_code = ""
                mov_payment_group = None
                mov_amount_val = float(raw_amount)
                non_card_movements += 1

            base_mov_key = erp_source_key(
                source_currency,
                account_code,
                invoice_number,
                auth_code,
                mov_amount_val,
                tx_date,
                tx_time,
            )
            occurrence = seen_key_counts.get(base_mov_key, 0) + 1
            seen_key_counts[base_mov_key] = occurrence
            mov_source_key = (
                base_mov_key
                if occurrence == 1
                else hashlib.sha1(f"{base_mov_key}|#{occurrence}".encode("utf-8")).hexdigest()
            )
            all_movements.append({
                "excel_row": excel_row_number,
                "source_file": source_file,
                "movement_category": movement_category,
                "source_currency": source_currency,
                "card_code": mov_card_code,
                "payment_group": mov_payment_group,
                "payment_code": payment_code[:20],
                "payment_description": description[:100],
                "room_number": room_number[:20],
                "room_type": room_type[:20],
                "reservation_number": reservation_number[:50],
                "account_code": account_code[:50],
                "document_number": account_code[:50],
                "invoice_number": invoice_number[:50],
                "authorization_code": auth_code[:50],
                "transaction_date": tx_date_str,
                "transaction_time": tx_time_str,
                "_date_obj": tx_date,
                "_time_obj": tx_time,
                "cashier_username": cashier_username[:50],
                "guest_name": guest_name[:150],
                "amount": mov_amount_val,
                "reconciled": False,
                "source_key": mov_source_key,
            })

        if card_types is not None and is_usd_card_row:
            amount_dec = -raw_amount
            usd_key = (
                base_mov_key
                if (include_in_all and movement_category == "USD_CARD" and source_currency == "USD")
                else erp_source_key(
                    "USD",
                    account_code,
                    invoice_number,
                    auth_code,
                    amount_dec,
                    tx_date,
                    tx_time,
                )
            )
            erp_instance = ErpTransaction(
                card_type=card_types[usd_card_code],
                room_number=room_number,
                room_type=room_type,
                reservation_number=reservation_number,
                account_code=account_code,
                payment_code=payment_code,
                payment_description=description[:100],
                invoice_number=invoice_number,
                document_number=account_code,
                authorization_code=auth_code,
                transaction_date=tx_date,
                transaction_time=tx_time,
                cashier_username=cashier_username,
                guest_name=guest_name,
                amount=amount_dec,
                source_currency="USD",
                reconciled=False,
                source_key=usd_key,
            )
            erp_instances_append = usd_instances.append
            erp_instances_append(erp_instance)
            if auth_code:
                usd_by_auth_code.setdefault(auth_code, []).append({
                    "instance": erp_instance,
                    "document_number": account_code,
                    "invoice_number": invoice_number,
                    "amount_clp": amount_dec,
                    "payment_code": payment_code,
                    "card_code": usd_card_code,
                })

        if is_clp_card_row:
            clp_amount = float(-raw_amount)
            clp_key = (
                base_mov_key
                if (include_in_all and movement_category == "CLP_CARD" and source_currency == "CLP")
                else erp_source_key(
                    "CLP",
                    account_code,
                    invoice_number,
                    auth_code,
                    clp_amount,
                    tx_date,
                    tx_time,
                )
            )
            position = len(clp_rows)
            clp_rows.append({
                "excel_row": excel_row_number,
                "source_file": source_file,
                "card_code": clp_card_code or "",
                "payment_group": clp_payment_group,
                "payment_code": payment_code,
                "payment_description": description,
                "room_number": room_number,
                "room_type": room_type,
                "reservation_number": reservation_number,
                "account_code": account_code,
                "invoice_number": invoice_number,
                "authorization_code": auth_code,
                "transaction_date": tx_date_str,
                "transaction_time": tx_time_str,
                "cashier_username": cashier_username,
                "guest_name": guest_name,
                "amount": clp_amount,
                "is_reversal": bool(raw_amount > 0),
                "reconciled": False,
                "source_key": clp_key,
            })
            normalized_code = normalize_auth_code(auth_code)
            if normalized_code:
                clp_rows_by_auth_code.setdefault(normalized_code, []).append(position)

    period_header = parse_bordero_period(erp_dataframe)
    return {
        "usd_instances": usd_instances,
        "usd_by_auth_code": usd_by_auth_code,
        "clp_rows": clp_rows,
        "clp_rows_by_auth_code": clp_rows_by_auth_code,
        "all_movements": all_movements,
        "stats": {
            "period_header": period_header,
            "min_date": min_date,
            "max_date": max_date,
            "total_movements": total_movements,
            "card_movements": card_movements,
            "non_card_movements": non_card_movements,
            "usd_card_movements": usd_card_movements,
            "clp_card_movements": clp_card_movements,
        },
    }


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
        df = read_excel_or_html_df(item, header=None)

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
        file_row_totals: Dict[str, Dict[str, Decimal]] = {
            mod: {"total_sales": Decimal("0"), "commission": Decimal("0"), "commission_vat": Decimal("0"), "total_deposit": Decimal("0")}
            for mod, _ in clp_modalities_map
        }

        for row_index in range(detail_header_row + 1, len(df)):
            row = df.iloc[row_index]
            auth_code = clean_code_string(safe_cell(row, columns["authorization_code"]))
            if not auth_code:
                continue

            # Skip secondary detail header rows (e.g. Row 30 carrying "Código de autorización de venta")
            norm_auth = normalize_text(auth_code)
            if (
                norm_auth in ("autorizacion", "codigo autorizacion", "codigo de autorizacion", "codigo de autorizacion de venta")
                or ("codigo" in norm_auth and "autorizacion" in norm_auth)
                or "monto original" in normalize_text(safe_cell(row, columns["gross_amount"]))
            ):
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
            file_row_totals[modality]["total_sales"] += valid_amount
            file_row_totals[modality]["commission"] += commission
            file_row_totals[modality]["commission_vat"] += commission_vat
            file_row_totals[modality]["total_deposit"] += net_deposit
            if settlement_date and not h["settlement_date"]:
                h["settlement_date"] = settlement_date
            if company and not h["company"]:
                h["company"] = company
            if bank_account and not h["bank_account"]:
                h["bank_account"] = bank_account

        # If not consolidated, read official header amounts from rows 16-25 and accumulate across multiple files
        if not is_consolidated:
            target_mod = "CREDIT" if "cred" in header_medium else ("DEBIT" if "deb" in header_medium else "PREPAID")
            h = settlements[target_mod]["header"]
            f_row = file_row_totals[target_mod]
            seen_fields: set = set()
            for row_index in range(min(len(df), detail_header_row)):
                row = df.iloc[row_index]
                cells = [(col, val) for col, val in enumerate(row.values) if pd.notna(val) and str(val).strip()]
                for position, (col, val) in enumerate(cells):
                    label = normalize_text(val)
                    if label in TBK_HEADER_AMOUNT_LABELS and position + 1 < len(cells):
                        amt = safe_decimal(cells[position + 1][1])
                        field = TBK_HEADER_AMOUNT_LABELS[label]
                        if field in seen_fields or amt <= 0:
                            continue
                        seen_fields.add(field)
                        if field == "total_sales":
                            delta = amt - f_row["total_sales"]
                            h["total_sales"] += delta
                            h["sales_total"] += delta
                        elif field == "commission":
                            h["commission"] += amt - f_row["commission"]
                        elif field == "commission_vat":
                            h["commission_vat"] += amt - f_row["commission_vat"]
                        elif field == "total_deposit":
                            delta = amt - f_row["total_deposit"]
                            h["total_deposit"] += delta
                            h["calculated_deposit"] += delta

    return settlements
