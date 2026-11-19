"""
Single source of truth for Chilean/European number formatting and template filters:
- Dots (.) for thousands separator
- Comma (,) for decimal separator
Reused across both Django templates and backend reconciliation/suggestion services.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from django import template

register = template.Library()


@register.filter(name="num_usd")
def num_usd(value: Any) -> str:
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
def num_clp(value: Any) -> str:
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


def fmt_usd(value: Any) -> str:
    """Formats a numeric value with 'US$ ' prefix, reusing num_usd."""
    return f"US$ {num_usd(value)}"


def fmt_clp(value: Any) -> str:
    """Formats a numeric value with '$' prefix, reusing num_clp."""
    return f"${num_clp(value)}"


def _normalize_bordero_text(text: str) -> str:
    """Rewrites legacy 'ERP' references inside stored observations to 'Borderó'."""
    text = re.sub(r"\bBorderó\s+ERP\b", "Borderó", text)
    text = re.sub(r"\bERP\s+Hotel\b", "Borderó", text)
    return re.sub(r"\bERP\b", "Borderó", text)


@register.filter(name="format_observation")
def format_observation(value: str) -> str:
    """
    Formats SALDO numbers inside diagnostic observations using dots for thousands
    and unifies legacy ERP references to Borderó.
    """
    if not value:
        return ""
    text = _normalize_bordero_text(str(value))

    def _replace_saldo(match):
        return f"{match.group(1)}${num_clp(match.group(2))}"

    return re.sub(r"(SALDO(?:\s+CORREGIDO)?\s+)\$?([\d\.]+)", _replace_saldo, text)


@register.filter(name="to_json")
def to_json(value: Any) -> str:
    """Serializes a dictionary or list into JSON for frontend modal consumption."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return "{}"


@register.filter(name="file_name")
def file_name(value: Any) -> str:
    """Last part of a stored file path (Windows or POSIX), to show "cartola.xlsx" instead of the full folder."""
    text = str(value or "")
    return text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or text


# Color of each reconciliation status chip, so every state reads differently in the audit trail.
STATUS_CHIP_CLASSES = {
    "PENDING": "status-pending",
    "REJECTED": "status-danger",
    "RESOLVED_ERP_FIXED": "status-success",
    "ACCEPTED_ROUNDING": "status-info",
    "RESOLVED_ACCEPTED": "status-info",
    "RESOLVED_ADJUSTMENT": "status-primary",
    "RESOLVED_REVERSAL": "status-purple",
    "RESOLVED_CASHIER": "status-orange",
}


@register.filter(name="status_chip_class")
def status_chip_class(status: Any) -> str:
    """CSS class for a ReconciliationStatus (or its code); unknown or empty statuses stay neutral."""
    code = getattr(status, "code", status) or ""
    return STATUS_CHIP_CLASSES.get(str(code), "status-neutral")


# Color of each exception type (system_result), so "Diferencia de monto" and "No está en el Borderó" never look alike.
EXCEPTION_CHIP_CLASSES = {
    "NOT_FOUND_IN_ERP": "status-danger",
    "AMOUNT_MISMATCH": "status-primary",
    "CARD_TYPE_MISMATCH": "status-purple",
    "OUT_OF_PERIOD": "status-neutral",
    "MATCHED": "status-success",
}


@register.filter(name="exception_chip_class")
def exception_chip_class(system_result: Any) -> str:
    """CSS class for an exception type; unknown types use the amber warning."""
    return EXCEPTION_CHIP_CLASSES.get(str(system_result or ""), "status-warning")
