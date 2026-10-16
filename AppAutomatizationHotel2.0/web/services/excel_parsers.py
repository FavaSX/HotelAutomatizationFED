"""
Shared Excel Parsers for Hotel Plaza San Francisco USD Reconciliation.
Extracts, cleans, and indexes records in memory from ERP/Bordero, Bank Statement, and Card Operator files.
"""

from decimal import Decimal
from typing import Any, Dict, List, Tuple
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
    resolve_dataframe_columns,
    clean_code_string,
    normalize_auth_code,
    safe_decimal,
    safe_date,
    safe_time,
    safe_cell,
)


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
            "movement_date": matched_movement.movement_date if matched_movement else None,
            "is_matched": matched_movement is not None,
            "movements_data": [
                serialize_bank_movement(m, m is matched_movement) for m in bank_movements
            ],
        })

    return validations_summary


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
                "sequence_number": sequence_number,
                "foreign_currency_usd": foreign_currency,
                "balance_clp": balance_clp,
                "corrected_balance_clp": corrected_balance_clp,
                "instance": operator_record,
            })

    return card_operator_index



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
            "reconciled": False,
        })

        normalized_code = normalize_auth_code(raw_auth_code)
        if normalized_code:
            rows_by_auth_code.setdefault(normalized_code, []).append(position)

    return rows, rows_by_auth_code
