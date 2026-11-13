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
        "room_number": "—",
        "guest_name": "—",
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


def _recross_usd_in_preview(preview: Dict[str, Any], voucher: Dict[str, Any]) -> None:
    """Crosses one USD voucher again inside the in-memory preview after an ERP row correction."""
    erp_rows = preview["erp_transactions"]
    op_rows = preview.get("card_operator_transactions", [])
    tbk = preview["transbank_transactions"][voucher["tbk_index"]]
    gross_usd = Decimal(str(tbk["original_sale_amount"]))
    card_code = voucher["card_code"]
    tolerance = Decimal(str(preview.get("tolerance", "0.50")))
    code = normalize_auth_code(tbk["authorization_code"])

    prev_erp_idx = voucher.get("erp_index")
    if prev_erp_idx is not None and 0 <= prev_erp_idx < len(erp_rows):
        erp_rows[prev_erp_idx]["reconciled"] = False

    candidates = [
        (idx, row) for idx, row in enumerate(erp_rows)
        if code and normalize_auth_code(row.get("authorization_code") or "") == code
    ]
    active = [c for c in candidates if c[1].get("invoice_number")] or candidates

    def _match_op(e_row: Dict[str, Any]):
        doc_num = e_row.get("document_number") or e_row.get("account_code") or ""
        erp_clp = Decimal(str(e_row.get("amount", 0)))
        op_cands = [
            (o_idx, o) for o_idx, o in enumerate(op_rows)
            if o.get("card_code") == card_code and (o.get("document_number") or "") == doc_num
        ]
        chosen_idx, chosen_op, obs = None, None, "No encontrado en Posición por Clientes"
        for o_idx, o in op_cands:
            bal = Decimal(str(o.get("balance_amount", 0)))
            corr = Decimal(str(o.get("corrected_balance_amount", 0)))
            if bal == erp_clp:
                chosen_idx, chosen_op, obs = o_idx, o, f"Coincide con SALDO {bal}"
                break
            if corr == erp_clp:
                chosen_idx, chosen_op, obs = o_idx, o, f"Coincide con SALDO CORREGIDO {corr}"
                break
        if chosen_op is None and op_cands:
            chosen_idx, chosen_op = op_cands[0]
        op_usd = Decimal(str(chosen_op.get("foreign_currency_amount", 0))) if chosen_op else Decimal("0.00")
        op_clp = (
            Decimal(str(chosen_op.get("balance_amount", 0)))
            if chosen_op and Decimal(str(chosen_op.get("balance_amount", 0))) > 0
            else (Decimal(str(chosen_op.get("corrected_balance_amount", 0))) if chosen_op else Decimal("0.00"))
        )
        return chosen_idx, chosen_op, op_usd, op_clp, obs

    if not active:
        voucher.update({
            "erp_index": None,
            "operator_index": None,
            "erp_authorization_code": "",
            "account_code": "",
            "document_number": "",
            "operator_document_number": "",
            "cashier_username": "—",
            "operator_amount_usd": 0.0,
            "erp_amount_clp": 0.0,
            "operator_amount_clp": 0.0,
            "difference_usd": float(gross_usd),
            "difference_clp": 0.0,
            "system_result": "NOT_FOUND_IN_ERP",
            "observation": f"Voucher {tbk['authorization_code']} no encontrado en ERP Hotel",
        })
        tbk["reconciled"] = False
        return

    resolved = [(idx, e_row, *_match_op(e_row)) for idx, e_row in active]
    single = next(
        (
            c for c in resolved
            if Decimal(str(c[1].get("amount", 0))) > 0
            and c[3] is not None
            and abs(round(gross_usd - c[4], 2)) <= tolerance
        ),
        None,
    )
    if single is not None or len(resolved) == 1:
        erp_idx, e_row, op_idx, op_row, op_usd, op_clp, obs = single if single is not None else resolved[0]
        e_row["reconciled"] = True
        if op_row is not None:
            op_row["authorization_code"] = tbk["authorization_code"]
        erp_clp = Decimal(str(e_row.get("amount", 0)))
        acc_code = e_row.get("account_code") or e_row.get("document_number") or ""
        doc_num = e_row.get("document_number") or acc_code
        op_doc = op_row.get("document_number", "") if op_row else ""
        cashier = e_row.get("cashier_username") or "—"
        erp_auth = e_row.get("authorization_code") or tbk["authorization_code"]
    else:
        primary_cand = next((c for c in resolved if Decimal(str(c[1].get("amount", 0))) > 0), resolved[0])
        erp_idx, e_row, op_idx, op_row, _, _, _ = primary_cand
        doc_nums, op_docs = [], []
        erp_clp = op_usd = op_clp = Decimal("0.00")
        all_ops = True
        for _, r_erp, r_op_idx, r_op, r_usd, r_clp, _ in resolved:
            r_erp["reconciled"] = True
            r_acc = r_erp.get("account_code") or r_erp.get("document_number") or ""
            if r_acc and r_acc not in doc_nums:
                doc_nums.append(r_acc)
            erp_clp += Decimal(str(r_erp.get("amount", 0)))
            op_usd += r_usd
            op_clp += r_clp
            if r_op is not None:
                r_op["authorization_code"] = tbk["authorization_code"]
                if r_op.get("document_number") and r_op["document_number"] not in op_docs:
                    op_docs.append(r_op["document_number"])
            else:
                all_ops = False
        acc_code = " / ".join(doc_nums)[:50]
        doc_num = acc_code
        op_doc = " / ".join(op_docs)[:50]
        cashier = e_row.get("cashier_username") or "—"
        erp_auth = e_row.get("authorization_code") or tbk["authorization_code"]
        obs = (
            f"Voucher dividido en {len(resolved)} facturas (Cta. {acc_code}) | Suma SALDO {op_clp}"
            if all_ops else "No encontrado en Posición por Clientes"
        )

    diff_usd = round(gross_usd - op_usd, 2)
    diff_clp = round(op_clp - erp_clp, 2)
    matched = abs(diff_usd) <= tolerance and op_row is not None
    tbk["reconciled"] = matched
    voucher.update({
        "erp_index": erp_idx,
        "operator_index": op_idx,
        "erp_authorization_code": erp_auth,
        "account_code": acc_code,
        "document_number": doc_num,
        "operator_document_number": op_doc,
        "cashier_username": cashier,
        "operator_amount_usd": float(op_usd),
        "erp_amount_clp": float(erp_clp),
        "operator_amount_clp": float(op_clp),
        "difference_usd": float(diff_usd),
        "difference_clp": float(diff_clp),
        "system_result": "MATCHED" if matched else "AMOUNT_MISMATCH",
        "observation": obs,
    })


def _refresh_usd_totals(preview: Dict[str, Any]) -> None:
    """Refreshes counts, USD/CLP summary tables, and suggestion counts in the USD preview."""
    usd_buckets = ("matched_vouchers", "discrepancy_vouchers", "orphan_vouchers")
    vouchers = [v for b in usd_buckets for v in preview.get(b, [])]
    preview["counts"] = {
        "matched": len(preview.get("matched_vouchers", [])),
        "discrepancies": len(preview.get("discrepancy_vouchers", [])),
        "orphans": len(preview.get("orphan_vouchers", [])),
        "total": len(vouchers),
    }
    for row in preview.get("usd_table", []):
        group = [v for v in vouchers if v.get("card_code") == row.get("card_code")]
        tbk_usd = round(sum((Decimal(str(v.get("gross_amount_usd", 0))) for v in group), Decimal("0")), 2)
        op_usd = round(sum((Decimal(str(v.get("operator_amount_usd", 0))) for v in group), Decimal("0")), 2)
        row["erp_sales"] = float(tbk_usd)
        row["tbk_sales"] = float(op_usd)
        row["difference"] = float(round(tbk_usd - op_usd, 2))
    for row in preview.get("clp_table", []):
        group = [v for v in vouchers if v.get("card_code") == row.get("card_code")]
        erp_clp = round(sum((Decimal(str(v.get("erp_amount_clp", 0))) for v in group), Decimal("0")), 0)
        op_clp = round(sum((Decimal(str(v.get("operator_amount_clp", 0))) for v in group), Decimal("0")), 0)
        row["erp_sales"] = float(erp_clp)
        row["tbk_sales"] = float(op_clp)
        row["difference"] = float(round(op_clp - erp_clp, 0))
    if preview.get("usd_table"):
        tot_u_erp = round(sum((Decimal(str(r["erp_sales"])) for r in preview["usd_table"]), Decimal("0")), 2)
        tot_u_tbk = round(sum((Decimal(str(r["tbk_sales"])) for r in preview["usd_table"]), Decimal("0")), 2)
        preview["usd_totals"] = {
            "erp": float(tot_u_erp),
            "tbk": float(tot_u_tbk),
            "diff": float(round(tot_u_erp - tot_u_tbk, 2)),
        }
    if preview.get("clp_table"):
        tot_c_erp = round(sum((Decimal(str(r["erp_sales"])) for r in preview["clp_table"]), Decimal("0")), 0)
        tot_c_tbk = round(sum((Decimal(str(r["tbk_sales"])) for r in preview["clp_table"]), Decimal("0")), 0)
        preview["clp_totals"] = {
            "erp": float(tot_c_erp),
            "tbk": float(tot_c_tbk),
            "diff": float(round(tot_c_tbk - tot_c_erp, 0)),
        }
    counts = preview.get("suggestion_counts")
    if counts is not None:
        for key, b in (("discrepancies", "discrepancy_vouchers"), ("orphans", "orphan_vouchers")):
            items = preview.get(b, [])
            with_sug = sum(1 for v in items if v.get("suggestion"))
            counts[f"{key}_with"] = with_sug
            counts[f"{key}_without"] = len(items) - with_sug


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
    in_memory = proposal.get("erp_index") is not None

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
        return {"result": voucher["system_result"], "matched": False, "applied_on_save": True,
                "proposal": voucher["suggestion"]["proposed_change"]}

    _set_preview_value(erp_rows[proposal["erp_index"]], proposal["field"], new_text, doc_field)
    if currency == "CLP":
        _recross_clp_in_preview(preview, voucher)
    else:
        _recross_usd_in_preview(preview, voucher)
    target = BUCKET_BY_RESULT[voucher["system_result"]]
    if target != bucket:
        preview[bucket].remove(voucher)
        preview.setdefault(target, []).append(voucher)
    if currency == "CLP":
        _refresh_clp_totals(preview)
        remaining = abs(voucher["difference"])
    else:
        _refresh_usd_totals(preview)
        remaining = abs(voucher["difference_usd"])
    return {"result": voucher["system_result"], "matched": voucher["system_result"] == "MATCHED", "applied_on_save": False,
            "remaining": remaining, "proposal": voucher["suggestion"]["proposed_change"]}


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
