"""
Shared Excel Parsers for Hotel Plaza San Francisco Reconciliation.
Extracts, cleans, and indexes records from ERP/Bordero, Bank Statement, and Card Operator files.
"""

from decimal import Decimal
from typing import Any, Dict, List, Tuple
import pandas as pd

from ..models import (
    CardType,
    ReconciliationProcess,
    BankStatementMovement,
    BankDepositValidation,
    ErpTransaction,
    CardOperatorTransaction,
)
from .column_detector import (
    ERP_COLUMN_RULES,
    BANK_STATEMENT_COLUMN_RULES,
    CARD_OPERATOR_COLUMN_RULES,
    resolve_dataframe_columns,
    clean_code_string,
    safe_decimal,
    safe_date,
    safe_cell,
)


def parse_and_validate_bank_deposits(
    process: ReconciliationProcess,
    bank_dataframe: pd.DataFrame,
    expected_deposits: List[Tuple[str, Decimal]],
    tolerance: Decimal = Decimal("0.50")
) -> List[Dict[str, Any]]:
    """
    Parses the Bank Statement (cartola.xls or cartola.xlsx) using hybrid column detection,
    persists movements, and validates one or multiple expected Transbank deposits.
    """
    columns = resolve_dataframe_columns(bank_dataframe, BANK_STATEMENT_COLUMN_RULES)
    bank_movements_to_create: List[BankStatementMovement] = []

    for _, row in bank_dataframe.iterrows():
        deposit_amount = safe_decimal(safe_cell(row, columns["deposit_amount"]))
        charge_amount = safe_decimal(safe_cell(row, columns["charge_amount"]))
        date_cell = safe_cell(row, columns["movement_date"])

        if (deposit_amount <= 0 and charge_amount <= 0) or str(date_cell).strip().lower() == "fecha":
            continue

        description = str(safe_cell(row, columns["description"]) or "").strip()
        is_tbk = "TRANSBANK" in description.upper() or "TBK" in description.upper() or "DIVISAS" in description.upper()

        movement = BankStatementMovement(
            process=process,
            movement_date=safe_date(date_cell),
            description=description,
            branch_or_channel=str(safe_cell(row, columns["branch_or_channel"]) or "").strip(),
            document_number=clean_code_string(safe_cell(row, columns["document_number"])),
            charge_amount=charge_amount,
            deposit_amount=deposit_amount,
            balance_amount=safe_decimal(safe_cell(row, columns["balance_amount"])),
            is_transbank_deposit=is_tbk,
        )
        bank_movements_to_create.append(movement)

    BankStatementMovement.objects.bulk_create(bank_movements_to_create)

    validations_summary = []
    for modality_code, expected_amount in expected_deposits:
        matched_movement = None
        for movement in bank_movements_to_create:
            if movement.deposit_amount > 0 and abs(movement.deposit_amount - expected_amount) <= tolerance:
                matched_movement = movement
                break

        is_matched = matched_movement is not None
        BankDepositValidation.objects.create(
            process=process,
            bank_movement=matched_movement,
            payment_modality=modality_code,
            transbank_calculated_deposit=expected_amount,
            bank_statement_deposit=matched_movement.deposit_amount if matched_movement else Decimal("0.00"),
            deposit_date=matched_movement.movement_date if matched_movement else None,
            is_matched=is_matched,
        )
        validations_summary.append({
            "modality": modality_code,
            "transbank_deposit": float(expected_amount),
            "bank_deposit": float(matched_movement.deposit_amount) if matched_movement else 0.0,
            "is_matched": is_matched,
        })

    return validations_summary


def parse_erp_transactions(
    process: ReconciliationProcess,
    erp_dataframe: pd.DataFrame,
    card_types: Dict[str, CardType],
    currency_mode: str = "USD"
) -> Tuple[List[ErpTransaction], Dict[str, List[Dict[str, Any]]]]:
    """
    Parses the Hotel ERP / Bordero Excel file using hybrid column detection,
    filters by currency mode ('USD' or 'CLP'), saves records in bulk, and
    returns an O(1) hash map indexed by authorization_code.
    """
    columns = resolve_dataframe_columns(erp_dataframe, ERP_COLUMN_RULES)
    erp_by_auth_code: Dict[str, List[Dict[str, Any]]] = {}
    erp_instances_to_create: List[ErpTransaction] = []

    valid_clp_codes = {"AME$", "MC$", "V$", "DI$", "RCO"}

    for _, row in erp_dataframe.iterrows():
        payment_code = str(safe_cell(row, columns["payment_code"]) or "").strip().upper()
        description = str(safe_cell(row, columns["payment_description"]) or "").strip()

        if currency_mode == "USD":
            if "US$" not in description:
                continue
            amount_value = abs(safe_decimal(safe_cell(row, columns["amount"])))
            if amount_value <= 0:
                continue
            card_code = (
                "MC" if "MASTER" in description.upper() else
                "VI" if "VISA" in description.upper() else
                "AX" if "AMEX" in description.upper() else
                "DI"
            )
            document_number = clean_code_string(safe_cell(row, columns["account_code"]))
        else:
            if payment_code not in valid_clp_codes:
                continue
            raw_amount = safe_decimal(safe_cell(row, columns["amount"]))
            if raw_amount >= 0:
                continue
            amount_value = abs(raw_amount)
            card_code = (
                "AX" if payment_code == "AME$" else
                "MC" if payment_code == "MC$" else
                "VI" if payment_code == "V$" else
                "DI" if payment_code == "DI$" else
                "RCO"
            )
            document_number = (
                clean_code_string(safe_cell(row, columns["invoice_number"]))
                or clean_code_string(safe_cell(row, columns["account_code"]))
            )

        auth_code = clean_code_string(safe_cell(row, columns["authorization_code"]))
        card_model = card_types[card_code]

        erp_instance = ErpTransaction(
            process=process,
            card_type=card_model,
            room_number=clean_code_string(safe_cell(row, columns["room_number"])),
            room_type=str(safe_cell(row, columns["room_type"]) or "").strip(),
            reservation_number=clean_code_string(safe_cell(row, columns["reservation_number"])),
            account_code=clean_code_string(safe_cell(row, columns["account_code"])),
            payment_code=payment_code,
            invoice_number=clean_code_string(safe_cell(row, columns["invoice_number"])),
            document_number=document_number,
            authorization_code=auth_code,
            transaction_date=safe_date(safe_cell(row, columns["transaction_date"])),
            cashier_username=str(safe_cell(row, columns["cashier_username"]) or "").strip(),
            guest_name=str(safe_cell(row, columns["guest_name"]) or "").strip(),
            amount=amount_value,
            source_currency=currency_mode,
            reconciled=False,
        )
        erp_instances_to_create.append(erp_instance)

        if auth_code:
            if auth_code not in erp_by_auth_code:
                erp_by_auth_code[auth_code] = []
            erp_by_auth_code[auth_code].append({
                "instance": erp_instance,
                "document_number": document_number,
                "amount_clp": amount_value,
                "payment_code": payment_code,
                "card_code": card_code,
            })

    ErpTransaction.objects.bulk_create(erp_instances_to_create)
    return erp_instances_to_create, erp_by_auth_code


def parse_card_operator_statements(
    process: ReconciliationProcess,
    operator_dataframes: Dict[str, pd.DataFrame],
    card_types: Dict[str, CardType]
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Parses the 4 Card Operator statements (Amex, Diners, Visa, MasterCard)
    using hybrid column detection, saves records in bulk, and returns an O(1)
    lookup dictionary indexed by [card_code][document_number].
    """
    card_operator_index: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        code: {} for code in operator_dataframes.keys()
    }
    operator_instances_to_create: List[CardOperatorTransaction] = []

    for card_code, card_df in operator_dataframes.items():
        columns = resolve_dataframe_columns(card_df, CARD_OPERATOR_COLUMN_RULES)
        card_model = card_types[card_code]

        for _, row in card_df.iterrows():
            document_number = clean_code_string(safe_cell(row, columns["document_number"]))
            if not document_number or not document_number.isdigit():
                continue

            foreign_currency = safe_decimal(safe_cell(row, columns["foreign_currency_amount"]))
            balance_clp = safe_decimal(safe_cell(row, columns["balance_amount"]))
            corrected_balance_clp = safe_decimal(safe_cell(row, columns["corrected_balance_amount"]))

            operator_record = CardOperatorTransaction(
                process=process,
                card_type=card_model,
                document_number=document_number,
                foreign_currency_amount=foreign_currency,
                balance_amount=balance_clp,
                corrected_balance_amount=corrected_balance_clp,
            )
            operator_instances_to_create.append(operator_record)

            if document_number not in card_operator_index[card_code]:
                card_operator_index[card_code][document_number] = []
            card_operator_index[card_code][document_number].append({
                "foreign_currency_usd": foreign_currency,
                "balance_clp": balance_clp,
                "corrected_balance_clp": corrected_balance_clp,
                "instance": operator_record,
            })

    CardOperatorTransaction.objects.bulk_create(operator_instances_to_create)
    return card_operator_index

