"""
Opción A of the corrections flow: assisted suggestions the accountant accepts or rejects with one click.

A suggestion used to be text only ("Corregir el código de autorización en el ERP"). To accept it, it must
say exactly which row, which field and which value. `with_proposed_change` adds that at commit time,
when the preview rows already have their database ids:

    "proposed_change": {"erp_id": 812, "field": "authorization_code", "old": "0FOFUAZ", "new": "F0FUAZ", "label": "..."}

  - Aceptar: applies the change through `erp_editing.edit_row` (history + re-cross), marked as SUGGESTION.
  - Rechazar: the ERP is not touched; the case moves to "Rechazada" and stays open to fix by hand.
"""

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction

from ..models import AuditResolutionLog, CrossMatchResult, ErpTransaction, ErpTransactionChange, ReconciliationStatus
from .column_detector import normalize_auth_code, safe_date
from .erp_editing import (
    FIELD_LABELS,
    OPEN_STATUS_CODES,
    CorrectionError,
    _as_text,
    display_value,
    edit_row,
    payment_code_choices,
)

ACCEPTED = "accepted"
REJECTED = "rejected"


def _target_row(suggestion: Dict[str, Any], erp_models: List[Optional[ErpTransaction]]) -> Optional[ErpTransaction]:
    db_match = suggestion.get("db_match") or {}
    if db_match.get("transaction_id"):
        return ErpTransaction.objects.filter(pk=db_match["transaction_id"]).first()
    idx = suggestion.get("erp_index")
    if idx is not None and 0 <= idx < len(erp_models):
        return erp_models[idx]
    return None


def _credit_code_for(currency: str, card_code: str) -> Optional[str]:
    for code, _, card in payment_code_choices(currency):
        if code != "RCO" and card and card.code == card_code:
            return code
    return None


def _concrete_change(currency: str, voucher: Dict[str, Any], suggestion: Dict[str, Any], row_code: Any):
    """(field, new value) the suggestion implies for the row with code `row_code`, or None when it is not a one-field fix."""
    result = voucher.get("system_result")
    voucher_code = str(voucher.get("authorization_code") or "").strip().upper()

    if result in ("NOT_FOUND_IN_ERP", "OUT_OF_PERIOD"):
        if voucher_code and normalize_auth_code(row_code or "") != normalize_auth_code(voucher_code):
            return "authorization_code", voucher_code
        return None
    if currency != "CLP":
        return None  # a dollar difference lives in the operator file, not in one ERP field
    if result == "AMOUNT_MISMATCH" and suggestion.get("erp_index") == voucher.get("erp_index"):
        if len(voucher.get("erp_indices") or []) <= 1:
            return "amount", _as_text(voucher.get("tbk_amount"))
        return None
    if result == "CARD_TYPE_MISMATCH":
        if voucher.get("payment_modality") in ("DEBIT", "PREPAID"):
            return "payment_code", "RCO"
        code = _credit_code_for(currency, voucher.get("card_code") or "")
        return ("payment_code", code) if code else None
    return None


def with_proposed_change(
    currency: str,
    voucher: Dict[str, Any],
    suggestion: Optional[Dict[str, Any]],
    erp_models: List[Optional[ErpTransaction]],
) -> Optional[Dict[str, Any]]:
    """Returns the suggestion with `proposed_change` added when it maps to one field of one saved row."""
    if not suggestion:
        return suggestion
    # The preview proposal points at preview positions; the saved one must point at database ids
    suggestion = {k: v for k, v in suggestion.items() if k != "proposed_change"}
    row = _target_row(suggestion, erp_models)
    if row is None or row.pk is None:
        return suggestion
    change = _concrete_change(currency, voucher, suggestion, row.authorization_code)
    if change is None:
        return suggestion
    field_name, new_value = change
    old_value = getattr(row, field_name)
    new_value = _normalized_text(field_name, new_value)
    if _as_text(old_value) == new_value:
        return suggestion
    return {
        **suggestion,
        "proposed_change": {
            "erp_id": row.pk,
            "field": field_name,
            "old": _as_text(old_value),
            "new": _as_text(new_value),
            "label": (
                f"Doc {row.document_number} · {FIELD_LABELS[field_name]}: "
                f"{display_value(field_name, old_value, currency)} → {display_value(field_name, new_value, currency)}"
            ),
        },
    }


def _normalized_text(field_name: str, value: Any) -> str:
    """Value as the history stores it: amounts with 2 decimals, dates in ISO format."""
    if value in (None, ""):
        return ""
    if field_name == "amount":
        return f"{Decimal(str(value)):.2f}"
    if field_name == "transaction_date":
        parsed = safe_date(value)
        return parsed.isoformat() if parsed else str(value)
    return str(value)


def preview_proposal(currency: str, voucher: Dict[str, Any], erp_rows: List[Dict[str, Any]], doc_field: str):
    """
    Same concrete change as `with_proposed_change`, computed in the preview (step 2) before anything is saved.
    The target is a preview row (`erp_index`) or, for a sale found in the database, that row (`erp_id`).
    """
    suggestion = voucher.get("suggestion")
    if not suggestion:
        return None
    db_id = (suggestion.get("db_match") or {}).get("transaction_id")
    erp_index = None
    if db_id:
        row = ErpTransaction.objects.filter(pk=db_id).first()
        if row is None:
            return None
        row_code, document = row.authorization_code, row.document_number
        values = {f: getattr(row, f) for f in ("amount", "authorization_code", "transaction_date", "payment_code")}
    else:
        erp_index = suggestion.get("erp_index")
        if erp_index is None or not (0 <= erp_index < len(erp_rows)):
            return None
        e_data = erp_rows[erp_index]
        row_code, document = e_data.get("authorization_code"), e_data.get(doc_field) or e_data.get("account_code")
        values = {f: e_data.get(f) for f in ("amount", "authorization_code", "transaction_date", "payment_code")}

    change = _concrete_change(currency, voucher, suggestion, row_code)
    if change is None:
        return None
    field_name, new_value = change
    old_text = _normalized_text(field_name, values[field_name])
    new_text = _normalized_text(field_name, new_value)
    if old_text == new_text:
        return None
    return {
        "erp_index": erp_index,
        "erp_id": db_id,
        "field": field_name,
        "old": old_text,
        "new": new_text,
        "label": proposal_label(document, field_name, old_text, new_text, currency),
    }


def proposal_label(document: Any, field_name: str, old: Any, new: Any, currency: str) -> str:
    return (
        f"Doc {document or '—'} · {FIELD_LABELS[field_name]}: "
        f"{display_value(field_name, old, currency)} → {display_value(field_name, new, currency)}"
    )


def attach_preview_proposals(preview: Dict[str, Any], currency: str, doc_field: str) -> None:
    """Adds `proposed_change` to every preview suggestion that is a one-field fix (keeps the ones already applied)."""
    applied = {entry["tbk_index"]: entry for entry in preview.get("pending_corrections", [])}
    erp_rows = preview.get("erp_transactions", [])
    for bucket in ("discrepancy_vouchers", "orphan_vouchers", "out_of_period_vouchers"):
        for voucher in preview.get(bucket, []):
            suggestion = voucher.get("suggestion")
            if not suggestion:
                continue
            entry = applied.get(voucher.get("tbk_index"))
            if entry:
                suggestion["proposed_change"] = {**entry["proposal"], "status": "applied"}
                continue
            proposal = preview_proposal(currency, voucher, erp_rows, doc_field)
            if proposal:
                suggestion["proposed_change"] = proposal
            else:
                suggestion.pop("proposed_change", None)


# ------------------------------------------------------------------------------
# Accept / reject from the inbox
# ------------------------------------------------------------------------------

def load_suggestion(match: CrossMatchResult) -> Optional[Dict[str, Any]]:
    if not match.auditor_comment:
        return None
    try:
        data = json.loads(match.auditor_comment)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def open_proposal(match: CrossMatchResult) -> Optional[Dict[str, Any]]:
    """The concrete change the accountant can still accept or reject on this case, if any."""
    suggestion = load_suggestion(match)
    proposal = (suggestion or {}).get("proposed_change")
    if not proposal or proposal.get("status"):
        return None
    return proposal


def _mark(match: CrossMatchResult, suggestion: Dict[str, Any], status: str) -> None:
    suggestion["proposed_change"]["status"] = status
    match.auditor_comment = json.dumps(suggestion, ensure_ascii=False)
    match.save(update_fields=["auditor_comment"])


def _require_open(match: CrossMatchResult) -> Dict[str, Any]:
    if match.management_status and match.management_status.code not in OPEN_STATUS_CODES:
        raise CorrectionError("Este caso ya está resuelto.")
    if open_proposal(match) is None:
        raise CorrectionError("Este caso no tiene una sugerencia pendiente de aceptar o rechazar.")
    return load_suggestion(match)


@transaction.atomic
def accept_suggestion(match: CrossMatchResult, user, value: Any = None) -> Dict[str, Any]:
    """Applies the suggested change; `value` lets the accountant adjust the suggested value before accepting."""
    suggestion = _require_open(match)
    proposal = suggestion["proposed_change"]
    new_value = value if value not in (None, "") else proposal["new"]
    row = ErpTransaction.objects.filter(pk=proposal["erp_id"]).first()
    if row is None:
        raise CorrectionError("La fila de la sugerencia ya no está en el ERP (fue eliminada). Corrígelo a mano.")
    if _as_text(getattr(row, proposal["field"])) != proposal["old"]:
        raise CorrectionError("La fila cambió desde que se hizo la sugerencia. Revísala y corrígela a mano.")
    outcome = edit_row(
        row, {proposal["field"]: new_value}, user, match=match,
        source=ErpTransactionChange.SOURCE_SUGGESTION,
        reason="Sugerencia aceptada" if new_value == proposal["new"] else "Sugerencia aceptada con el valor ajustado",
    )
    _mark(match, suggestion, ACCEPTED)
    return outcome


@transaction.atomic
def reject_suggestion(match: CrossMatchResult, user, comment: str = "") -> None:
    suggestion = _require_open(match)
    previous_status = match.management_status
    rejected = ReconciliationStatus.objects.get(code="REJECTED")
    match.management_status = rejected
    match.save(update_fields=["management_status"])
    _mark(match, suggestion, REJECTED)
    note = f"Sugerencia rechazada ({suggestion['proposed_change']['label']}). El ERP no se modificó"
    comment = (comment or "").strip()
    AuditResolutionLog.objects.create(
        cross_match_result=match, user=user, previous_status=previous_status, new_status=rejected,
        comment=f"{note}. {comment}" if comment else note + ".",
    )
