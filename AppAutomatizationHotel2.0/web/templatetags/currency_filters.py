"""
Custom template filters for Chilean/European number formatting:
- Dots (.) for thousands separator
- Comma (,) for decimal separator
And human-readable reconciliation diagnostic formatting.
"""

import re
from decimal import Decimal, InvalidOperation
from django import template

register = template.Library()


@register.filter(name="num_usd")
def num_usd(value) -> str:
    """
    Formats a numeric value with dots for thousands and comma for 2 decimal places.
    Example: 23201.32 -> '23.201,32' | -879.82 -> '-879,82'
    """
    if value is None or value == "":
        return "0,00"
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return "0,00"

    sign = "-" if dec < 0 else ""
    dec = abs(dec)
    formatted_us = f"{dec:,.2f}"
    integer_part, decimal_part = formatted_us.split(".")
    integer_part_dots = integer_part.replace(",", ".")
    return f"{sign}{integer_part_dots},{decimal_part}"


@register.filter(name="num_clp")
def num_clp(value) -> str:
    """
    Formats a CLP numeric value with dots for thousands and 0 decimal places.
    Handles values that may already have thousand-separator dots (e.g. '107.984')
    or trailing '.00' from Decimal serialization.
    """
    if value is None or value == "":
        return "0"
    raw = str(value).strip()
    if re.match(r"^-?\d{1,3}(\.\d{3})+$", raw):
        raw = raw.replace(".", "")
    elif raw.endswith(".00") or raw.endswith(".0"):
        raw = raw.split(".")[0]

    try:
        dec = round(Decimal(raw))
    except (InvalidOperation, ValueError, TypeError):
        return "0"

    sign = "-" if dec < 0 else ""
    dec = abs(int(dec))
    formatted_us = f"{dec:,}"
    return f"{sign}{formatted_us.replace(',', '.')}"


@register.filter(name="format_observation")
def format_observation(value: str) -> str:
    """
    Formats SALDO numbers inside diagnostic observations using dots for thousands.
    """
    if not value:
        return ""
    text = str(value)

    def _replace_saldo(match):
        prefix = match.group(1)
        raw_num = match.group(2)
        return f"{prefix}${num_clp(raw_num)}"

    return re.sub(r"(SALDO(?:\s+CORREGIDO)?\s+)\$?([\d\.]+)", _replace_saldo, text)


@register.filter(name="clear_diagnosis")
def clear_diagnosis(item: dict) -> str:
    """
    Generates a self-explanatory, step-by-step diagnosis for a voucher dictionary,
    explaining both the CLP match (ERP <-> Operator) and the USD comparison (TBK <-> Operator).
    """
    if not isinstance(item, dict):
        return str(item or "")

    obs = str(item.get("observation", ""))
    sys_res = item.get("system_result", "")
    doc = item.get("document_number", "")
    card_name = item.get("card_name") or item.get("card_code", "Operadora")
    erp_clp = num_clp(item.get("erp_amount_clp", 0))
    tbk_usd = num_usd(item.get("gross_amount_usd", 0))
    diff_usd_val = Decimal(str(item.get("difference_usd", 0) or 0))
    diff_usd_str = num_usd(diff_usd_val)

    # Tab 3: Not found in ERP
    if sys_res == "NOT_FOUND_IN_ERP":
        return (
            f"Cobrado en Transbank (US$ {tbk_usd}), pero el voucher "
            f"{item.get('authorization_code', '')} no existe en el ERP del hotel"
        )

    # Split invoice case (e.g. 00503Z)
    if "dividido" in obs.lower() or "/" in str(doc):
        if diff_usd_val == 0:
            return f"Voucher dividido en 2 facturas ({doc}) · Suma CLP ${erp_clp} · USD exacto (US$ {tbk_usd})"
        return f"Voucher dividido en 2 facturas ({doc}) · Suma CLP ${erp_clp} · Dif. cambio US$ {diff_usd_str}"

    # Determine which column in Operator file matched the CLP amount
    col_label = "SALDO CORREGIDO" if "CORREGIDO" in obs.upper() else "SALDO"

    # Tab 2: Discrepancies
    if sys_res == "AMOUNT_MISMATCH":
        if "No encontrado" in obs:
            return (
                f"Ubicado en ERP (${erp_clp}), pero la Cuenta {doc} "
                f"no aparece en el archivo {card_name} Dólar"
            )
        return (
            f"CLP coincide en col. {col_label} (${erp_clp}), pero en Dólares "
            f"difiere por US$ {diff_usd_str} (supera tolerancia de ±US$ 0,50)"
        )

    # Tab 1: Exact / Within tolerance match
    if diff_usd_val == 0:
        return f"CLP coincide en col. {col_label} (${erp_clp}) · USD exacto sin diferencia (US$ {tbk_usd})"
    return (
        f"CLP coincide en col. {col_label} (${erp_clp}) · "
        f"USD cuadra por tipo de cambio (dif. US$ {diff_usd_str} ≤ ±0,50)"
    )
