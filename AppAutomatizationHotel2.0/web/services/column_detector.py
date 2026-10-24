"""
Hybrid Column Detector and Data Sanitizers.

Solves both column renaming and column reordering in Excel files:
1. Scans header rows for semantic keywords (handles column reordering, inserted columns, etc.).
2. Prioritizes exact keyword matches over substring matches to prevent collisions with inserted columns.
3. Falls back to default positional indices if headers are missing or renamed.
"""

import math
import unicodedata
from decimal import Decimal
from typing import Any, Dict
import pandas as pd


# ==============================================================================
# 1. HYBRID COLUMN DETECTION RULES (KEYWORDS + DEFAULT FALLBACK INDEX)
# ==============================================================================

ERP_COLUMN_RULES = {
    "room_number": {"keywords": ["hab"], "default_index": 0},
    "room_type": {"keywords": ["tipo hab"], "default_index": 1},
    "reservation_number": {"keywords": ["reserva"], "default_index": 2},
    "account_code": {"keywords": ["cuenta"], "default_index": 3},
    "payment_code": {"keywords": ["cod. deb", "cod deb"], "default_index": 4},
    "payment_description": {"keywords": ["descripcion"], "default_index": 5},
    "invoice_number": {"keywords": ["factura"], "default_index": 6},
    "amount": {"keywords": ["valor"], "default_index": 8},
    "authorization_code": {"keywords": ["documento"], "default_index": 9},
    "transaction_date": {"keywords": ["fecha"], "default_index": 10},
    "transaction_time": {"keywords": ["hora"], "default_index": 11},
    "cashier_username": {"keywords": ["usuario"], "default_index": 12},
    "guest_name": {"keywords": ["designacion"], "default_index": 13},
}

BANK_STATEMENT_COLUMN_RULES = {
    "movement_date": {"keywords": ["fecha"], "default_index": 1},
    "description": {"keywords": ["descripcion"], "default_index": 3},
    "branch_or_channel": {"keywords": ["canal", "sucursal"], "default_index": 5},
    "document_number": {"keywords": ["docto", "documento"], "default_index": 6},
    "charge_amount": {"keywords": ["cargos"], "default_index": 7},
    "deposit_amount": {"keywords": ["abonos"], "default_index": 8},
    "balance_amount": {"keywords": ["saldo"], "default_index": 9},
}

CARD_OPERATOR_COLUMN_RULES = {
    "document_number": {"keywords": ["documento"], "default_index": 2},
    "sequence_number": {"keywords": ["parcela", "cuota", "secuencia"], "default_index": 3},
    "foreign_currency_amount": {"keywords": ["otra moneda"], "default_index": 12},
    "balance_amount": {"keywords": ["saldo"], "default_index": 13},
    "corrected_balance_amount": {"keywords": ["saldo corregido"], "default_index": 14},
}

TRANSBANK_USD_COLUMN_RULES = {
    "merchant_code": {"keywords": ["codigo comercio", "codigo de comercio"], "default_index": 1},
    "sale_date": {"keywords": ["fecha venta"], "default_index": 2},
    "card_type": {"keywords": ["tipo tarjeta"], "default_index": 3},
    "masked_card_number": {"keywords": ["identificador"], "default_index": 4},
    "installment_type": {"keywords": ["tipo cuota"], "default_index": 5},
    "gross_amount": {"keywords": ["monto original"], "default_index": 6},
    "authorization_code": {"keywords": ["codigo autorizacion", "autorizacion venta"], "default_index": 7},
    "installment_number": {"keywords": ["n cuota", "n° cuota"], "default_index": 8},
}

# Transbank "Abonos por día" report (Crédito / Débito / Prepago en Pesos).
# The detail header is split across two rows (29 and 30); both are scanned.
TRANSBANK_CLP_COLUMN_RULES = {
    "sale_date": {"keywords": ["fecha de venta"], "default_index": 1},
    "movement_type": {"keywords": ["tipo de movimiento"], "default_index": 2},
    "merchant_code": {"keywords": ["codigo de comercio"], "default_index": 3},
    "merchant_location": {"keywords": ["nombre local"], "default_index": 4},
    "gross_amount": {"keywords": ["monto original de venta"], "default_index": 6},
    "installment_type": {"keywords": ["tipo de venta/cuota", "tipo de venta"], "default_index": 8},
    "installment_number": {"keywords": ["n° de cuota", "n de cuota"], "default_index": 9},
    "valid_amount": {"keywords": ["monto venta valido para abono"], "default_index": 10},
    "commission": {"keywords": ["comision transbank (-)"], "default_index": 11},
    "commission_vat": {"keywords": ["iva comision transbank (-)"], "default_index": 12},
    "net_deposit": {"keywords": ["total abono (=)"], "default_index": 13},
    "masked_card_number": {"keywords": ["n° de tarjeta", "n de tarjeta"], "default_index": 24},
    "authorization_code": {"keywords": ["codigo de autorizacion de venta"], "default_index": 25},
    "transaction_identifier": {"keywords": ["id transaccion (tid)", "id transaccion"], "default_index": 30},
    "receipt_number": {"keywords": ["n° de boleta", "n de boleta"], "default_index": 31},
}


# ==============================================================================
# 2. HYBRID COLUMN RESOLVER
# ==============================================================================

def normalize_text(raw_value: Any) -> str:
    """Removes accents, converts to lowercase, and strips whitespace for comparison."""
    if pd.isna(raw_value):
        return ""
    text = str(raw_value).strip().lower()
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(character for character in normalized if not unicodedata.combining(character))


def resolve_dataframe_columns(
    dataframe: pd.DataFrame,
    column_rules: Dict[str, Dict[str, Any]],
    max_scan_rows: int = 40
) -> Dict[str, int]:
    """
    Dynamically detects column positions by scanning the top rows for header keywords.
    Resilient to extra inserted columns, column reordering, and shifted data:
    1. First pass searches for exact keyword matches.
    2. Second pass falls back to substring matches for descriptive multi-word keywords.
    3. Unmatched fields retain their default_index.
    """
    resolved_indices = {
        field_name: rule["default_index"]
        for field_name, rule in column_rules.items()
    }

    scan_limit = min(len(dataframe), max_scan_rows)
    for row_index in range(scan_limit):
        row_values = [normalize_text(val) for val in dataframe.iloc[row_index].values]
        non_empty_cells = [val for val in row_values if val]
        if len(non_empty_cells) < 3:
            continue

        matches_in_row = {}
        for field_name, rule in column_rules.items():
            # 1. Exact match takes top priority (immune to inserted columns)
            exact_match = next(
                (col_index for col_index, cell_text in enumerate(row_values) if cell_text and cell_text in rule["keywords"]),
                None,
            )
            if exact_match is not None:
                matches_in_row[field_name] = exact_match
                continue

            # 2. Substring match fallback for descriptive multi-word keywords (> 5 characters)
            sub_match = next(
                (
                    col_index for col_index, cell_text in enumerate(row_values)
                    if cell_text and any(len(kw) > 5 and kw in cell_text for kw in rule["keywords"])
                ),
                None,
            )
            if sub_match is not None:
                matches_in_row[field_name] = sub_match

        # If at least 3 expected headers were found in this row, update the mapping
        if len(matches_in_row) >= 3:
            resolved_indices.update(matches_in_row)

    return resolved_indices


# ==============================================================================
# 3. DATA SANITIZERS
# ==============================================================================

def clean_code_string(raw_value: Any) -> str:
    """Sanitizes authorization and document codes removing trailing '.0', spaces, and normalizing to uppercase."""
    if pd.isna(raw_value):
        return ""
    code_string = str(raw_value).strip().upper()
    if code_string.endswith(".0"):
        code_string = code_string[:-2]
    return code_string


def safe_decimal(raw_value: Any, default: Decimal = Decimal("0.00")) -> Decimal:
    """Converts raw cell values to Decimal safely, treating NaN and text as zero."""
    if pd.isna(raw_value):
        return default
    try:
        if isinstance(raw_value, float) and math.isnan(raw_value):
            return default
        string_value = str(raw_value).strip().replace(",", ".")
        return Decimal(string_value)
    except Exception:
        return default


def normalize_auth_code(raw_value: Any) -> str:
    """
    Authorization code key used to cross Transbank against the ERP / Bordero:
    uppercase and without leading zeros (Excel drops them from numeric codes,
    while cashiers often prepend an extra '0' to alphanumeric ones).
    """
    return clean_code_string(raw_value).lstrip("0")


SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def _parse_spanish_long_date(text: str):
    """Parses Transbank long dates such as '04 septiembre 2026'."""
    parts = normalize_text(text).replace(" de ", " ").split()
    if len(parts) != 3 or parts[1] not in SPANISH_MONTHS:
        return None
    try:
        return pd.Timestamp(year=int(parts[2]), month=SPANISH_MONTHS[parts[1]], day=int(parts[0])).date()
    except (ValueError, TypeError):
        return None


def safe_date(raw_value: Any):
    """Parses date values safely, strictly returning None instead of pd.NaT."""
    if pd.isna(raw_value):
        return None
    if isinstance(raw_value, str):
        spanish_date = _parse_spanish_long_date(raw_value)
        if spanish_date:
            return spanish_date
    try:
        parsed_timestamp = pd.to_datetime(raw_value, dayfirst=True, errors="coerce")
        if pd.isna(parsed_timestamp):
            return None
        return parsed_timestamp.date()
    except Exception:
        return None


def safe_time(raw_value: Any):
    """Parses time values safely (HH:MM:SS), returning None if invalid or empty."""
    if pd.isna(raw_value):
        return None
    try:
        if hasattr(raw_value, "hour") and hasattr(raw_value, "minute"):
            return raw_value
        parsed = pd.to_datetime(str(raw_value).strip(), errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.time()
    except Exception:
        return None


def safe_cell(row: pd.Series, column_index: int) -> Any:
    """Safely retrieves a cell by index without raising IndexError, returning None for NaN/NaT."""
    if 0 <= column_index < len(row):
        value = row.iloc[column_index]
        return None if pd.isna(value) else value
    return None
