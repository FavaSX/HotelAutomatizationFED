"""
Accepting a suggested correction in step 2 (the in-memory preview), before the closing is saved.

The accountant may adjust the suggested value. The correction is kept in `preview["pending_corrections"]`:
  - CLP with a row of this Bordero: the preview row takes the new value and the voucher is crossed again in
    memory with the upload rules, so it moves to "Cuadrados" right away and the totals follow.
  - USD, or a sale found in the database: the voucher is marked and crossed again when the closing is saved.
On commit, the rows are saved with their ORIGINAL Excel values (so the fingerprint and the history stay
right) and every pending correction is then applied through `erp_editing.edit_row`: history marked as an
accepted suggestion, re-cross and "Corregido en ERP".
"""

import copy
import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..models import CrossMatchResult, ErpTransaction, ErpTransactionChange
from .clp_reconciliation import _cross_match_sale
from .column_detector import normalize_auth_code, safe_date
from .erp_editing import CorrectionError, _as_text, _parse_value, edit_row
from .suggestion_actions import ACCEPTED, load_suggestion, proposal_label

BUCKET_BY_RESULT = {
    "MATCHED": "matched_vouchers",
    "AMOUNT_MISMATCH": "discrepancy_vouchers",
    "CARD_TYPE_MISMATCH": "discrepancy_vouchers",
    "NOT_FOUND_IN_ERP": "orphan_vouchers",
    "OUT_OF_PERIOD": "out_of_period_vouchers",
}
BUCKETS = ("matched_vouchers", "discrepancy_vouchers", "orphan_vouchers", "out_of_period_vouchers")
DOC_FIELD = {"CLP": "account_code", "USD": "document_number"}


class PreviewCorrectionError(ValueError):
    pass


def _find_voucher(preview: Dict[str, Any], tbk_index: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    for bucket in BUCKETS:
        for voucher in preview.get(bucket, []):
            if voucher.get("tbk_index") == tbk_index:
                return voucher, bucket
    return None, None


def _set_preview_value(e_data: Dict[str, Any], field_name: str, text: str, doc_field: str) -> None:
    """Writes a history-format value (2-decimal amount, ISO date) onto a preview row, in the preview's formats."""
    if field_name == "amount":
        e_data["amount"] = float(Decimal(text))
        if "is_reversal" in e_data:
            e_data["is_reversal"] = Decimal(text) < 0
    elif field_name == "transaction_date":
        parsed = safe_date(text)
        e_data["transaction_date"] = parsed.strftime("%d/%m/%Y") if parsed else None
    elif field_name == "payment_code":
        e_data["payment_code"] = text
        if "payment_group" in e_data:
            e_data["payment_group"] = "REDCOMPRA" if text == "RCO" else "CREDIT"
    elif field_name == "document_number":
        e_data[doc_field] = text
    else:
        e_data[field_name] = text


# ------------------------------------------------------------------------------
# CLP: cross one voucher again inside the preview
# ------------------------------------------------------------------------------

def _recross_clp_in_preview(preview: Dict[str, Any], voucher: Dict[str, Any]) -> None:
    erp_rows = preview["erp_transactions"]
    tbk = preview["transbank_transactions"][voucher["tbk_index"]]
    gross = Decimal(str(tbk["original_sale_amount"]))
    sale = {
        "payment_modality": tbk["payment_modality"],
        "gross_amount": gross,
        "valid_amount": Decimal(str(tbk.get("valid_amount") or tbk["original_sale_amount"])),
        "authorization_code": tbk["authorization_code"],
        "sale_date": safe_date(tbk.get("sale_date")) if tbk.get("sale_date") else None,
        "installment_number": tbk.get("installment_number") or "",
    }
    claimed = set()
    for bucket in BUCKETS:
        for other in preview.get(bucket, []):
            if other is not voucher:
                claimed.update(other.get("erp_indices") or [])
    for idx in voucher.get("erp_indices") or []:
        erp_rows[idx]["reconciled"] = False

    rows_by_code: Dict[str, List[int]] = {}
    for pos, row in enumerate(erp_rows):
        code = normalize_auth_code(row.get("authorization_code") or "")
        if code:
            rows_by_code.setdefault(code, []).append(pos)
    period_dict = preview.get("bordero_period") or {}
    period = (safe_date(period_dict.get("start")) if period_dict.get("start") else None,
              safe_date(period_dict.get("end")) if period_dict.get("end") else None)
    tolerance = Decimal(str(preview.get("tolerance", 1000)))

    match = _cross_match_sale(sale, erp_rows, rows_by_code, claimed, tolerance, period)
    linked = match["linked"]
    primary_idx = (
        min(linked, key=lambda i: (
            erp_rows[i].get("is_reversal", False) or erp_rows[i]["amount"] <= 0,
            abs(gross - Decimal(str(erp_rows[i]["amount"]))),
            not bool(erp_rows[i].get("invoice_number")),
        ))
        if linked else None
    )
    primary = erp_rows[primary_idx] if primary_idx is not None else None
    voucher.update({
        "erp_index": primary_idx,
        "erp_indices": linked,
        "erp_authorization_code": primary["authorization_code"] if primary else "",
        "erp_date": primary["transaction_date"] if primary else "",
        "account_code": primary["account_code"] if primary else "—",
        "invoice_number": primary["invoice_number"] if primary else "—",
        "room_number": primary["room_number"] if primary else "—",
        "guest_name": primary["guest_name"] if primary else "—",
        "cashier_username": primary["cashier_username"] if primary else "—",
        "erp_payment_code": primary["payment_code"] if primary else "—",
        "erp_amount": float(match["erp_amount"]),
        "difference": float(match["difference"]),
        "system_result": match["result"],
        "observation": match["observation"],
    })
    matched = match["result"] == "MATCHED"
    tbk["reconciled"] = matched
    if matched:
        for idx in linked:
            erp_rows[idx]["reconciled"] = True
        voucher.pop("error_type", None)
        voucher.pop("action", None)


def _refresh_clp_totals(preview: Dict[str, Any]) -> None:
    """Counts and the ERP side of the summary tables, from the vouchers after a correction."""
    vouchers = [v for bucket in BUCKETS for v in preview.get(bucket, [])]
    preview["counts"] = {
        "matched": len(preview.get("matched_vouchers", [])),
        "discrepancies": len(preview.get("discrepancy_vouchers", [])),
        "orphans": len(preview.get("orphan_vouchers", [])),
        "out_of_period": len(preview.get("out_of_period_vouchers", [])),
        "total": len(vouchers),
    }
    for row in preview.get("modality_table", []):
        group = [v for v in vouchers if v["payment_modality"] == row["payment_modality"]]
        row["matched"] = sum(1 for v in group if v["system_result"] == "MATCHED")
        row["exceptions"] = len(group) - row["matched"]
        row["erp_sales"] = float(sum(Decimal(str(v["erp_amount"])) for v in group))
        row["difference"] = float(Decimal(str(row["tbk_sales"])) - Decimal(str(row["erp_sales"])))
    for row in preview.get("clp_table", []):
        group = [v for v in vouchers if v["payment_modality"] == row["payment_modality"] and v["card_code"] == row["card_code"]]
        row["erp_sales"] = float(sum(Decimal(str(v["erp_amount"])) for v in group))
        row["difference"] = float(Decimal(str(row["tbk_sales"])) - Decimal(str(row["erp_sales"])))
    if preview.get("modality_table"):
        preview["clp_totals"] = {
            key: sum(r[key] for r in preview["modality_table"])
            for key in ("tbk_sales", "erp_sales", "difference", "commissions", "net_deposit")
        }
    counts = preview.get("suggestion_counts")
    if counts is not None:
        for key, bucket in (("discrepancies", "discrepancy_vouchers"), ("orphans", "orphan_vouchers"), ("out_of_period", "out_of_period_vouchers")):
            items = preview.get(bucket, [])
            with_sug = sum(1 for v in items if v.get("suggestion"))
            counts[f"{key}_with"] = with_sug
            counts[f"{key}_without"] = len(items) - with_sug


def _refresh_usd_counts(preview: Dict[str, Any]) -> None:
    preview["counts"] = {
        "matched": len(preview.get("matched_vouchers", [])),
        "discrepancies": len(preview.get("discrepancy_vouchers", [])),
        "orphans": len(preview.get("orphan_vouchers", [])),
        "total": sum(len(preview.get(b, [])) for b in ("matched_vouchers", "discrepancy_vouchers", "orphan_vouchers")),
    }


# ------------------------------------------------------------------------------
# Step 2: apply
# ------------------------------------------------------------------------------

def apply_preview_correction(preview: Dict[str, Any], currency: str, tbk_index: int, raw_value: Any) -> Dict[str, Any]:
    """Accepts (possibly adjusted) the suggested change of one voucher in the preview. Returns what happened."""
    voucher, bucket = _find_voucher(preview, tbk_index)
    if voucher is None:
        raise PreviewCorrectionError("No encontré ese voucher en la vista previa. Vuelve a cargar la página.")
    proposal = (voucher.get("suggestion") or {}).get("proposed_change")
    if not proposal:
        raise PreviewCorrectionError("Este voucher no tiene un cambio sugerido.")
    if proposal.get("status"):
        raise PreviewCorrectionError("Esta corrección ya se aplicó.")

    try:
        new_value = _parse_value(proposal["field"], raw_value if raw_value not in (None, "") else proposal["new"], currency)
    except CorrectionError as exc:
        raise PreviewCorrectionError(str(exc))
    new_text = _as_text(new_value)
    if proposal["field"] == "amount":
        new_text = f"{Decimal(new_text):.2f}"
    if new_text == proposal["old"]:
        raise PreviewCorrectionError("El valor es igual al que ya tiene la fila.")

    doc_field = DOC_FIELD[currency]
    erp_rows = preview.get("erp_transactions", [])
    document = proposal["label"].split(" · ")[0].removeprefix("Doc ")
    final = {**proposal, "new": new_text, "label": proposal_label(document, proposal["field"], proposal["old"], new_text, currency)}
    in_memory = currency == "CLP" and proposal.get("erp_index") is not None

    entry = {
        "tbk_index": tbk_index,
        "erp_index": proposal.get("erp_index"),
        "erp_id": proposal.get("erp_id"),
        "field": proposal["field"],
        "old": proposal["old"],
        "new": new_text,
        "previous_result": voucher["system_result"],
        "in_memory": in_memory,
        "proposal": final,
    }
    voucher["suggestion"]["proposed_change"] = {**final, "status": "applied"}
    preview.setdefault("pending_corrections", []).append(entry)

    if not in_memory:
        return {"result": voucher["system_result"], "matched": False, "applied_on_save": True}

    _set_preview_value(erp_rows[proposal["erp_index"]], proposal["field"], new_text, doc_field)
    _recross_clp_in_preview(preview, voucher)
    target = BUCKET_BY_RESULT[voucher["system_result"]]
    if target != bucket:
        preview[bucket].remove(voucher)
        preview.setdefault(target, []).append(voucher)
    _refresh_clp_totals(preview)
    return {"result": voucher["system_result"], "matched": voucher["system_result"] == "MATCHED", "applied_on_save": False,
            "remaining": abs(voucher["difference"])}


# ------------------------------------------------------------------------------
# Commit
# ------------------------------------------------------------------------------

def original_erp_rows(preview: Dict[str, Any], currency: str) -> List[Dict[str, Any]]:
    """The preview ERP rows with the step-2 corrections undone, so they are saved as the Excel had them."""
    rows = preview.get("erp_transactions", [])
    entries = [e for e in preview.get("pending_corrections", []) if e.get("in_memory")]
    if not entries:
        return rows
    rows = copy.deepcopy(rows)
    for entry in entries:
        _set_preview_value(rows[entry["erp_index"]], entry["field"], entry["old"], DOC_FIELD[currency])
    return rows


def apply_pending_corrections(preview: Dict[str, Any], erp_models: List[Any], tbk_models: List[Any], user) -> int:
    """After the closing is saved: writes every step-2 correction to the ERP with its history and re-cross."""
    applied = 0
    for entry in preview.get("pending_corrections", []):
        row = None
        if entry.get("erp_index") is not None and 0 <= entry["erp_index"] < len(erp_models):
            row = erp_models[entry["erp_index"]]
        elif entry.get("erp_id"):
            row = ErpTransaction.objects.filter(pk=entry["erp_id"]).first()
        tbk = tbk_models[entry["tbk_index"]] if 0 <= entry["tbk_index"] < len(tbk_models) else None
        match = (
            CrossMatchResult.objects.filter(transbank_transaction=tbk)
            .select_related("process", "transbank_transaction", "card_type", "management_status").first()
            if tbk is not None and tbk.pk else None
        )
        if row is None or row.pk is None or match is None:
            continue
        if _as_text(getattr(row, entry["field"])) == entry["new"] or (
            entry["field"] == "amount" and f"{row.amount:.2f}" == entry["new"]
        ):
            continue  # this row already carries the correction (e.g. saved in an earlier upload)
        try:
            edit_row(
                row, {entry["field"]: entry["new"]}, user, match=match,
                source=ErpTransactionChange.SOURCE_SUGGESTION,
                reason="Sugerencia aceptada en la vista previa (paso 2)",
                previous_result=entry["previous_result"],
            )
        except CorrectionError:
            continue
        suggestion = load_suggestion(match) or {}
        suggestion["proposed_change"] = {**entry["proposal"], "erp_id": row.pk, "status": ACCEPTED}
        match.auditor_comment = json.dumps(suggestion, ensure_ascii=False)
        match.save(update_fields=["auditor_comment"])
        applied += 1
    return applied
