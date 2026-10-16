"""
Level 2 Assisted Suggestion Engine for CLP (Pesos) Card Reconciliation.
Looks for unclaimed Bordero rows that explain an orphan voucher (NOT_FOUND_IN_ERP) or an
amount discrepancy (AMOUNT_MISMATCH), using amount, date, payment medium and the OCR/human-typo
authorization code similarity shared with the USD engine.

It never reconciles anything: suggestions are a reference for the auditor's manual review.
"""

from typing import Any, Dict, List, Optional, Tuple

from ..templatetags.currency_filters import fmt_clp
from .column_detector import safe_date
from .suggestion_engine import _chip, _compute_code_similarity

HIGH_CONFIDENCE_SCORE = 120

EXCEPTION_ERROR_TYPES = {
    "AMOUNT_MISMATCH": "No cuadra monto",
    "CARD_TYPE_MISMATCH": "Medio de pago distinto",
    "NOT_FOUND_IN_ERP": "Falta en ERP",
    "OUT_OF_PERIOD": "Fuera de período",
}
EXCEPTION_ACTIONS = {
    "AMOUNT_MISMATCH": "Revisar el monto con recepción",
    "CARD_TYPE_MISMATCH": "Corregir el medio de pago en el ERP",
    "NOT_FOUND_IN_ERP": "Buscar la venta con el cajero y registrarla en el ERP",
    "OUT_OF_PERIOD": "Buscar en el Borderó del período de la venta",
}


def _is_compatible(modality: str, erp_row: Dict[str, Any]) -> bool:
    return (erp_row.get("payment_group") == "REDCOMPRA") == (modality in ("DEBIT", "PREPAID"))


def _day_gap(sale_date: str, erp_date: str) -> Optional[int]:
    sale = safe_date(sale_date) if sale_date else None
    erp = safe_date(erp_date) if erp_date else None
    if not sale or not erp:
        return None
    return (erp - sale).days


def _erp_row_view(erp_row: Dict[str, Any]) -> Dict[str, Any]:
    """Compact Bordero row shown side by side with the Transbank voucher in the modal."""
    return {
        "excel_row": erp_row.get("excel_row"),
        "authorization_code": erp_row.get("authorization_code") or "— (sin código)",
        "payment_code": erp_row.get("payment_code"),
        "amount": erp_row.get("amount"),
        "transaction_date": erp_row.get("transaction_date") or "—",
        "transaction_time": erp_row.get("transaction_time") or "",
        "cashier_username": erp_row.get("cashier_username") or "—",
        "guest_name": erp_row.get("guest_name") or "—",
        "room_number": erp_row.get("room_number") or "—",
        "invoice_number": erp_row.get("invoice_number") or "—",
    }


def _score_orphan_candidate(
    voucher: Dict[str, Any], erp_row: Dict[str, Any], tolerance: float
) -> Optional[Tuple[int, Dict[str, Any]]]:
    tbk_amount = float(voucher["tbk_amount"])
    erp_amount = float(erp_row.get("amount") or 0)
    amount_gap = abs(tbk_amount - erp_amount)
    if amount_gap > tolerance:
        return None

    day_gap = _day_gap(voucher.get("sale_date", ""), erp_row.get("transaction_date", ""))
    if day_gap is None or not 0 <= day_gap <= 1:
        return None

    erp_code = erp_row.get("authorization_code") or ""
    similarity, typo_badge = _compute_code_similarity(voucher["authorization_code"], erp_code) if erp_code else (0.0, None)
    exact_amount = amount_gap < 1
    same_day = day_gap == 0
    missing_code = not erp_code
    if similarity < 0.55 and not (missing_code and exact_amount and same_day):
        return None

    score = (55 if exact_amount else 35) + (25 if same_day else 10) + int(similarity * 65)
    reasons = [
        _chip(
            f"Monto Exacto ({fmt_clp(erp_amount)})" if exact_amount else f"Monto Cercano ({fmt_clp(erp_amount)} · Dif: {fmt_clp(amount_gap)})",
            "bi-cash-stack",
            "emerald",
        ),
        _chip(
            f"Misma Fecha ({erp_row.get('transaction_date')})" if same_day else f"Registrado al día siguiente ({erp_row.get('transaction_date')})",
            "bi-calendar-check",
            "cyan",
        ),
        _chip(f"Mismo Medio ({erp_row.get('payment_code')})", "bi-credit-card-2-front", "purple"),
    ]
    if missing_code:
        cause = "El cajero no anotó el código de autorización"
        reasons.append(_chip("Fila del Borderó sin código de autorización", "bi-exclamation-triangle", "gold"))
    else:
        cause = typo_badge or f"Código digitado como {erp_code}"
        reasons.append(_chip(cause, "bi-upc-scan", "gold"))

    return score, {"reasons": reasons, "cause": cause, "similarity": similarity}


def enrich_clp_preview_with_suggestions(preview_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adds a `suggestion` dictionary to orphan and discrepancy vouchers in-place, assigning each
    unclaimed Bordero row to at most one voucher (highest score first).
    """
    if not preview_data:
        return preview_data

    erp_rows: List[Dict[str, Any]] = preview_data.get("erp_transactions", [])
    orphans = preview_data.get("orphan_vouchers", [])
    discrepancies = preview_data.get("discrepancy_vouchers", [])
    tolerance = float(preview_data.get("tolerance", 1000))
    file_names = preview_data.get("file_names", {})

    claimed = set()
    for bucket in ("matched_vouchers", "discrepancy_vouchers", "orphan_vouchers", "out_of_period_vouchers"):
        for voucher in preview_data.get(bucket, []):
            claimed.update(voucher.get("erp_indices") or [])
            voucher.pop("suggestion", None)

    def _tbk_file(voucher):
        return file_names.get(f"tbk_{voucher['payment_modality'].lower()}", "Transbank")

    # 1. Orphans: an unclaimed Bordero row with the same amount, date and medium, and a similar (or missing) code.
    proposals = []
    for orphan_idx, voucher in enumerate(orphans):
        for erp_idx, erp_row in enumerate(erp_rows):
            if erp_idx in claimed or not _is_compatible(voucher["payment_modality"], erp_row):
                continue
            scored = _score_orphan_candidate(voucher, erp_row, tolerance)
            if scored:
                proposals.append((scored[0], orphan_idx, erp_idx, scored[1]))

    proposals.sort(key=lambda item: item[0], reverse=True)
    assigned = set()
    for score, orphan_idx, erp_idx, detail in proposals:
        if orphan_idx in assigned or erp_idx in claimed:
            continue
        voucher, erp_row = orphans[orphan_idx], erp_rows[erp_idx]
        assigned.add(orphan_idx)
        claimed.add(erp_idx)
        erp_code = erp_row.get("authorization_code") or "— (sin código)"
        voucher["suggestion"] = {
            "confidence": "ALTA" if score >= HIGH_CONFIDENCE_SCORE else "MEDIA",
            "score": score,
            "erp_index": erp_idx,
            "headline": f"Posible registro en Borderó fila {erp_row.get('excel_row')} (código {erp_code})",
            "cause": detail["cause"],
            "missing_code": not erp_row.get("authorization_code"),
            "action": (
                "Anotar el código de autorización en el ERP"
                if not erp_row.get("authorization_code")
                else "Corregir el código de autorización en el ERP"
            ),
            "candidate_summary": (
                f"{erp_row.get('guest_name') or '—'} · Hab. {erp_row.get('room_number') or '—'} · "
                f"{fmt_clp(erp_row.get('amount'))} · {erp_row.get('transaction_date')} ({erp_row.get('cashier_username') or '—'})"
            ),
            "manual_check_note": (
                f"El voucher '{voucher['authorization_code']}' ({fmt_clp(voucher['tbk_amount'])}) no cruzó en Nivel 1. "
                f"En el Borderó, fila {erp_row.get('excel_row')}, el cajero '{erp_row.get('cashier_username') or '—'}' "
                f"registró {fmt_clp(erp_row.get('amount'))} como {erp_row.get('payment_code')} con el código '{erp_code}'. "
                f"{detail['cause']}. Verifique el voucher físico antes de corregir."
            ),
            "reasons": detail["reasons"],
            "files_involved": {"tbk": _tbk_file(voucher), "erp": file_names.get("erp", "BORDERO.xlsx")},
            "erp_row": _erp_row_view(erp_row),
        }

    # 2. Amount discrepancies: the missing difference recorded as a separate charge the same day.
    for voucher in discrepancies:
        if voucher.get("system_result") != "AMOUNT_MISMATCH":
            continue
        gap = float(voucher["tbk_amount"]) - float(voucher["erp_amount"])
        base_row = erp_rows[voucher["erp_index"]] if voucher.get("erp_index") is not None else {}
        best = None
        for erp_idx, erp_row in enumerate(erp_rows):
            if erp_idx in claimed or not _is_compatible(voucher["payment_modality"], erp_row):
                continue
            if erp_row.get("transaction_date") != base_row.get("transaction_date"):
                continue
            if abs(float(erp_row.get("amount") or 0) - gap) <= tolerance:
                best = erp_idx
                break
        if best is None:
            continue
        claimed.add(best)
        erp_row = erp_rows[best]
        voucher["suggestion"] = {
            "confidence": "MEDIA",
            "score": 0,
            "erp_index": best,
            "headline": f"La diferencia ({fmt_clp(gap)}) aparece como otro cargo en la fila {erp_row.get('excel_row')}",
            "cause": "Pago dividido en dos cargos del Borderó",
            "action": "Unir ambos cargos bajo el mismo código en el ERP",
            "candidate_summary": f"{fmt_clp(voucher['erp_amount'])} + {fmt_clp(erp_row.get('amount'))} = {fmt_clp(float(voucher['erp_amount']) + float(erp_row.get('amount') or 0))}",
            "manual_check_note": (
                f"El voucher '{voucher['authorization_code']}' es de {fmt_clp(voucher['tbk_amount'])}, pero el Borderó lo registra por "
                f"{fmt_clp(voucher['erp_amount'])}. El mismo día hay otro cargo de {fmt_clp(erp_row.get('amount'))} "
                f"(fila {erp_row.get('excel_row')}, código '{erp_row.get('authorization_code') or 'sin código'}') que completa la diferencia."
            ),
            "reasons": [
                _chip(f"Suma exacta ({fmt_clp(voucher['tbk_amount'])})", "bi-layers", "emerald"),
                _chip(f"Misma Fecha ({erp_row.get('transaction_date')})", "bi-calendar-check", "cyan"),
                _chip("Pago dividido en dos cargos", "bi-scissors", "gold"),
            ],
            "files_involved": {"tbk": _tbk_file(voucher), "erp": file_names.get("erp", "BORDERO.xlsx")},
            "erp_row": _erp_row_view(erp_row),
        }

    # 3. Bordero card payments that no voucher claimed (e.g. credit sales settled in later deposits).
    preview_data["unmatched_erp_rows"] = [
        {**_erp_row_view(row), "erp_index": idx, "payment_group": row.get("payment_group")}
        for idx, row in enumerate(erp_rows)
        if idx not in claimed and float(row.get("amount") or 0) > 0
    ]

    # 4. Exception report columns (Tipo de error / Acción sugerida) for every non-matched voucher.
    for bucket in ("discrepancy_vouchers", "orphan_vouchers", "out_of_period_vouchers"):
        for voucher in preview_data.get(bucket, []):
            suggestion = voucher.get("suggestion")
            result = voucher["system_result"]
            if suggestion and result == "NOT_FOUND_IN_ERP":
                voucher["error_type"] = "Falta código en ERP" if suggestion["missing_code"] else "Error de tipeo"
            else:
                voucher["error_type"] = EXCEPTION_ERROR_TYPES[result]
            voucher["action"] = suggestion["action"] if suggestion else EXCEPTION_ACTIONS[result]

    orphans_with = sum(1 for v in orphans if v.get("suggestion"))
    disc_with = sum(1 for v in discrepancies if v.get("suggestion"))
    preview_data["suggestion_counts"] = {
        "orphans_with": orphans_with,
        "orphans_without": len(orphans) - orphans_with,
        "discrepancies_with": disc_with,
        "discrepancies_without": len(discrepancies) - disc_with,
    }
    preview_data["counts"]["unmatched_erp"] = len(preview_data["unmatched_erp_rows"])
    return preview_data
