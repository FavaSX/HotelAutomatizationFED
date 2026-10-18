"""
Level 2 Assisted Suggestion Engine for CLP (Pesos) Card Reconciliation.
Looks for unclaimed Bordero rows that explain an orphan voucher (NOT_FOUND_IN_ERP) or an
amount discrepancy (AMOUNT_MISMATCH), using amount, date, payment medium and the OCR/human-typo
authorization code similarity shared with the USD engine.

It never reconciles anything: suggestions are a reference for the auditor's manual review.
"""

from typing import Any, Dict, List, Optional, Tuple

from ..templatetags.currency_filters import fmt_clp
from .clp_reconciliation import _is_compatible
from .column_detector import normalize_auth_code, safe_date
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


def _day_gap(sale_date: str, erp_date: str) -> Optional[int]:
    sale = safe_date(sale_date) if sale_date else None
    erp = safe_date(erp_date) if erp_date else None
    if not sale or not erp:
        return None
    return (erp - sale).days


def _erp_row_view(erp_row: Dict[str, Any]) -> Dict[str, Any]:
    """Compact Bordero row shown side by side with the Transbank voucher in the modal."""
    card_code = erp_row.get("card_code") or ""
    payment_group = erp_row.get("payment_group")
    card_name = "Redcompra" if payment_group == "REDCOMPRA" else {
        "VI": "Visa", "MC": "MasterCard", "AX": "American Express", "DI": "Diners Club"
    }.get(card_code, card_code or "Sin marca")
    return {
        "excel_row": erp_row.get("excel_row"),
        "source_file": erp_row.get("source_file") or "",
        "authorization_code": erp_row.get("authorization_code") or "— (sin código)",
        "payment_code": erp_row.get("payment_code") or "—",
        "card_code": card_code,
        "card_name": card_name,
        "amount": erp_row.get("amount"),
        "transaction_date": erp_row.get("transaction_date") or "—",
        "transaction_time": erp_row.get("transaction_time") or "",
        "cashier_username": erp_row.get("cashier_username") or "—",
        "guest_name": erp_row.get("guest_name") or "—",
        "room_number": erp_row.get("room_number") or "—",
        "account_code": erp_row.get("account_code") or "—",
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
    exact_code = bool(erp_code) and normalize_auth_code(voucher["authorization_code"]) == normalize_auth_code(erp_code)
    similarity, typo_badge = (
        (1.0, f"Mismo código en Borderó ({erp_code})")
        if exact_code
        else (_compute_code_similarity(voucher["authorization_code"], erp_code) if erp_code else (0.0, None))
    )
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
    elif exact_code:
        cause = f"Código exacto ({erp_code}) en Borderó histórico"
        reasons.append(_chip(cause, "bi-check2-circle", "emerald"))
    else:
        cause = typo_badge or f"Código digitado como {erp_code}"
        reasons.append(_chip(cause, "bi-upc-scan", "gold"))

    return score, {"reasons": reasons, "cause": cause, "similarity": similarity, "exact_code": exact_code}


def _build_candidate_suggestion(
    voucher: Dict[str, Any],
    erp_row: Dict[str, Any],
    erp_idx: Optional[int],
    score: int,
    detail: Dict[str, Any],
    tbk_file: str,
    default_erp_file: str,
) -> Dict[str, Any]:
    erp_code = erp_row.get("authorization_code") or "— (sin código)"
    erp_file = erp_row.get("source_file") or default_erp_file
    missing_code = not erp_row.get("authorization_code")
    if missing_code:
        action = "Anotar el código de autorización en el ERP"
    elif detail.get("exact_code"):
        action = f"Validado en {erp_file} (fila {erp_row.get('excel_row')})"
    else:
        action = "Corregir el código de autorización en el ERP"

    account = str(erp_row.get("account_code") or "").strip()
    invoice = str(erp_row.get("invoice_number") or "").strip()
    if account and account not in ("—", "0", "None"):
        doc_ref = f"Doc #{account}"
    elif invoice and invoice not in ("—", "0", "None"):
        doc_ref = f"Fact #{invoice}"
    else:
        doc_ref = f"Fila {erp_row.get('excel_row')}"

    return {
        "confidence": "ALTA" if score >= HIGH_CONFIDENCE_SCORE else "MEDIA",
        "score": score,
        "erp_index": erp_idx,
        "headline": f"Coincidencia sugerida en Borderó ERP ({doc_ref})",
        "cause": detail["cause"],
        "missing_code": missing_code,
        "action": action,
        "candidate_summary": (
            f"{erp_row.get('guest_name') or '—'} · Hab. {erp_row.get('room_number') or '—'} · "
            f"{fmt_clp(erp_row.get('amount'))} · {erp_row.get('transaction_date')} ({erp_row.get('cashier_username') or '—'})"
        ),
        "manual_check_note": (
            f"El voucher '{voucher['authorization_code']}' ({fmt_clp(voucher['tbk_amount'])}) "
            f"coincide en {erp_file} (fila {erp_row.get('excel_row')}), donde el cajero '{erp_row.get('cashier_username') or '—'}' "
            f"registró {fmt_clp(erp_row.get('amount'))} como {erp_row.get('payment_code')} con el código '{erp_code}' "
            f"(Cta. {erp_row.get('account_code') or '—'} · Fact. {erp_row.get('invoice_number') or '—'}). "
            f"{detail['cause']}. Verifique el voucher físico antes de corregir."
        ),
        "reasons": detail["reasons"],
        "files_involved": {"tbk": tbk_file, "erp": erp_file},
        "erp_row": _erp_row_view(erp_row),
    }


def enrich_clp_preview_with_suggestions(preview_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adds a `suggestion` dictionary to orphan, discrepancy, and out-of-period vouchers in-place,
    assigning each unclaimed Bordero row to at most one voucher (highest score first).
    """
    if not preview_data:
        return preview_data

    erp_rows: List[Dict[str, Any]] = preview_data.get("erp_transactions", [])
    aux_erp_rows: List[Dict[str, Any]] = preview_data.get("auxiliary_erp_transactions", [])
    orphans = preview_data.get("orphan_vouchers", [])
    discrepancies = preview_data.get("discrepancy_vouchers", [])
    out_of_period = preview_data.get("out_of_period_vouchers", [])
    tolerance = float(preview_data.get("tolerance", 1000))
    file_names = preview_data.get("file_names", {})
    default_erp_file = file_names.get("erp", "BORDERO.xlsx")

    claimed = set()
    for bucket in ("matched_vouchers", "discrepancy_vouchers", "orphan_vouchers", "out_of_period_vouchers"):
        for voucher in preview_data.get(bucket, []):
            claimed.update(voucher.get("erp_indices") or [])
            voucher.pop("suggestion", None)
            obs = voucher.get("observation", "")
            if "no encontrado en el Borderó" in obs:
                voucher["observation"] = f"Voucher {voucher['authorization_code']} no encontrado en ERP Hotel"

    # Pre-cancel self-cancelling reversal pairs in the Bordero (+X and -X on same account & code)
    for rev_idx, rev_row in enumerate(erp_rows):
        if rev_idx in claimed or float(rev_row.get("amount") or 0) >= 0:
            continue
        target_amt = -float(rev_row.get("amount") or 0)
        for pos_idx, pos_row in enumerate(erp_rows):
            if pos_idx in claimed or pos_idx == rev_idx:
                continue
            if (
                pos_row.get("account_code") == rev_row.get("account_code")
                and normalize_auth_code(pos_row.get("authorization_code")) == normalize_auth_code(rev_row.get("authorization_code"))
                and abs(float(pos_row.get("amount") or 0) - target_amt) < 1
            ):
                claimed.add(rev_idx)
                claimed.add(pos_idx)
                break

    def _tbk_file(voucher):
        return file_names.get(f"tbk_{voucher['payment_modality'].lower()}", "Transbank")

    # 1. Orphans: an unclaimed Bordero row with the same amount, date and medium, and a similar (or missing) code.
    proposals = []
    for orphan_idx, voucher in enumerate(orphans):
        for erp_idx, erp_row in enumerate(erp_rows):
            if erp_idx in claimed or float(erp_row.get("amount") or 0) <= 0:
                continue
            if not _is_compatible(voucher["payment_modality"], erp_row):
                continue
            scored = _score_orphan_candidate(voucher, erp_row, tolerance)
            if scored:
                proposals.append((scored[0], orphan_idx, erp_idx, scored[1]))

    proposals.sort(key=lambda item: item[0], reverse=True)
    assigned_orphans = set()
    for score, orphan_idx, erp_idx, detail in proposals:
        if orphan_idx in assigned_orphans or erp_idx in claimed:
            continue
        voucher, erp_row = orphans[orphan_idx], erp_rows[erp_idx]
        assigned_orphans.add(orphan_idx)
        claimed.add(erp_idx)
        voucher["suggestion"] = _build_candidate_suggestion(
            voucher, erp_row, erp_idx, score, detail, _tbk_file(voucher), default_erp_file
        )

    # 2. Discrepancies: split payment in Bordero OR direct diagnostic comparison against the matched Bordero row.
    for voucher in discrepancies:
        sys_res = voucher.get("system_result")
        base_idx = voucher.get("erp_index")
        base_row = erp_rows[base_idx] if base_idx is not None and 0 <= base_idx < len(erp_rows) else None

        if sys_res == "AMOUNT_MISMATCH":
            gap = float(voucher["tbk_amount"]) - float(voucher["erp_amount"])
            best = None
            if base_row and gap > 0:
                for erp_idx, erp_row in enumerate(erp_rows):
                    if erp_idx in claimed or not _is_compatible(voucher["payment_modality"], erp_row):
                        continue
                    if erp_row.get("transaction_date") != base_row.get("transaction_date"):
                        continue
                    if abs(float(erp_row.get("amount") or 0) - gap) <= tolerance:
                        best = erp_idx
                        break
            if best is not None:
                claimed.add(best)
                erp_row = erp_rows[best]
                voucher["suggestion"] = {
                    "confidence": "ALTA",
                    "score": HIGH_CONFIDENCE_SCORE,
                    "erp_index": best,
                    "headline": f"La diferencia ({fmt_clp(gap)}) aparece como otro cargo en la fila {erp_row.get('excel_row')}",
                    "cause": "Pago dividido en dos cargos del Borderó",
                    "missing_code": False,
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
                    "files_involved": {"tbk": _tbk_file(voucher), "erp": default_erp_file},
                    "erp_row": _erp_row_view(erp_row),
                }
            elif base_row is not None:
                erp_file = base_row.get("source_file") or default_erp_file
                voucher["suggestion"] = {
                    "confidence": "ALTA",
                    "score": HIGH_CONFIDENCE_SCORE,
                    "erp_index": base_idx,
                    "headline": (
                        f"Cruce en fila {base_row.get('excel_row')} (Cta. {base_row.get('account_code') or '—'} · "
                        f"Fact. {base_row.get('invoice_number') or '—'}) con dif. de {fmt_clp(abs(gap))}"
                    ),
                    "cause": f"Diferencia de {fmt_clp(gap)} entre POS Transbank y monto digitado en ERP",
                    "missing_code": False,
                    "action": f"Revisar propina/cargo de {fmt_clp(abs(gap))} en Cta. {base_row.get('account_code') or '—'} con cajero {base_row.get('cashier_username') or '—'}",
                    "candidate_summary": (
                        f"{base_row.get('guest_name') or '—'} · Fact. {base_row.get('invoice_number') or '—'} · "
                        f"{fmt_clp(base_row.get('amount'))} ({base_row.get('cashier_username') or '—'})"
                    ),
                    "manual_check_note": (
                        f"El voucher '{voucher['authorization_code']}' se cobró en Transbank por {fmt_clp(voucher['tbk_amount'])}, "
                        f"pero en el Borderó (fila {base_row.get('excel_row')}, Cta. {base_row.get('account_code') or '—'}, "
                        f"Factura {base_row.get('invoice_number') or '—'}) el cajero '{base_row.get('cashier_username') or '—'}' "
                        f"registró {fmt_clp(base_row.get('amount'))} con código '{base_row.get('authorization_code')}'. "
                        f"La diferencia de {fmt_clp(abs(gap))} suele corresponder a propina o recargo digitado en el POS y omitido en la cuenta ERP."
                    ),
                    "reasons": [
                        _chip(f"Mismo Código ({voucher['authorization_code']} ⇄ {base_row.get('authorization_code')})", "bi-upc-scan", "emerald"),
                        _chip(f"Misma Fecha ({base_row.get('transaction_date')})", "bi-calendar-check", "cyan"),
                        _chip(f"Mismo Medio ({base_row.get('payment_code')})", "bi-credit-card-2-front", "purple"),
                        _chip(f"Diferencia POS vs ERP: {fmt_clp(gap)}", "bi-exclamation-triangle", "gold"),
                    ],
                    "files_involved": {"tbk": _tbk_file(voucher), "erp": erp_file},
                    "erp_row": _erp_row_view(base_row),
                }
        elif sys_res == "CARD_TYPE_MISMATCH" and base_row is not None:
            erp_file = base_row.get("source_file") or default_erp_file
            voucher["suggestion"] = {
                "confidence": "ALTA",
                "score": HIGH_CONFIDENCE_SCORE,
                "erp_index": base_idx,
                "headline": f"Monto y código exactos en fila {base_row.get('excel_row')}, digitado como {base_row.get('payment_code')}",
                "cause": f"Medio de pago registrado como {base_row.get('payment_code')} en lugar de {voucher.get('modality_label')}",
                "missing_code": False,
                "action": f"Cambiar medio de pago de {base_row.get('payment_code')} a {voucher.get('modality_label')} en el ERP",
                "candidate_summary": f"{base_row.get('guest_name') or '—'} · {fmt_clp(base_row.get('amount'))} ({base_row.get('cashier_username') or '—'})",
                "manual_check_note": (
                    f"El voucher '{voucher['authorization_code']}' ({fmt_clp(voucher['tbk_amount'])}) coincide en código y monto "
                    f"con la fila {base_row.get('excel_row')} del Borderó, pero fue ingresado bajo el código '{base_row.get('payment_code')}'."
                ),
                "reasons": [
                    _chip(f"Monto Exacto ({fmt_clp(base_row.get('amount'))})", "bi-cash-stack", "emerald"),
                    _chip(f"Mismo Código ({base_row.get('authorization_code')})", "bi-upc-scan", "emerald"),
                    _chip(f"Medio distinto ({base_row.get('payment_code')} ≠ {voucher.get('modality_label')})", "bi-credit-card-2-front", "gold"),
                ],
                "files_involved": {"tbk": _tbk_file(voucher), "erp": erp_file},
                "erp_row": _erp_row_view(base_row),
            }

    # 3. Out-of-Period Vouchers: check auxiliary historical Bordero files (and any unclaimed main Bordero rows on sale_date).
    claimed_aux = set()
    oop_proposals = []
    for oop_idx, voucher in enumerate(out_of_period):
        for aux_idx, aux_row in enumerate(aux_erp_rows):
            if float(aux_row.get("amount") or 0) <= 0 or not _is_compatible(voucher["payment_modality"], aux_row):
                continue
            scored = _score_orphan_candidate(voucher, aux_row, tolerance)
            if scored:
                oop_proposals.append((scored[0], oop_idx, ("aux", aux_idx), scored[1]))
        for erp_idx, erp_row in enumerate(erp_rows):
            if erp_idx in claimed or float(erp_row.get("amount") or 0) <= 0 or not _is_compatible(voucher["payment_modality"], erp_row):
                continue
            scored = _score_orphan_candidate(voucher, erp_row, tolerance)
            if scored:
                oop_proposals.append((scored[0], oop_idx, ("main", erp_idx), scored[1]))

    oop_proposals.sort(key=lambda item: item[0], reverse=True)
    assigned_oop = set()
    for score, oop_idx, (pool_type, row_idx), detail in oop_proposals:
        if oop_idx in assigned_oop:
            continue
        if pool_type == "aux" and row_idx in claimed_aux:
            continue
        if pool_type == "main" and row_idx in claimed:
            continue
        assigned_oop.add(oop_idx)
        if pool_type == "aux":
            claimed_aux.add(row_idx)
            target_row = aux_erp_rows[row_idx]
            erp_index_ref = None
        else:
            claimed.add(row_idx)
            target_row = erp_rows[row_idx]
            erp_index_ref = row_idx
        voucher = out_of_period[oop_idx]
        voucher["suggestion"] = _build_candidate_suggestion(
            voucher, target_row, erp_index_ref, score, detail, _tbk_file(voucher), default_erp_file
        )

    # 4. Bordero card payments that no voucher claimed (e.g. credit sales settled in later deposits).
    preview_data["unmatched_erp_rows"] = [
        {**_erp_row_view(row), "erp_index": idx, "payment_group": row.get("payment_group")}
        for idx, row in enumerate(erp_rows)
        if idx not in claimed and float(row.get("amount") or 0) > 0
    ]

    # 5. Exception report columns (Tipo de error / Acción sugerida) for every non-matched voucher.
    for bucket in ("discrepancy_vouchers", "orphan_vouchers", "out_of_period_vouchers"):
        for voucher in preview_data.get(bucket, []):
            suggestion = voucher.get("suggestion")
            result = voucher["system_result"]
            if suggestion and result == "NOT_FOUND_IN_ERP":
                voucher["error_type"] = "Falta código en ERP" if suggestion["missing_code"] else "Error de tipeo"
            elif suggestion and result == "OUT_OF_PERIOD":
                voucher["error_type"] = "Fuera de período (Con sugerencia)"
            else:
                voucher["error_type"] = EXCEPTION_ERROR_TYPES[result]
            voucher["action"] = suggestion["action"] if suggestion else EXCEPTION_ACTIONS[result]

    orphans_with = sum(1 for v in orphans if v.get("suggestion"))
    disc_with = sum(1 for v in discrepancies if v.get("suggestion"))
    oop_with = sum(1 for v in out_of_period if v.get("suggestion"))
    preview_data["suggestion_counts"] = {
        "orphans_with": orphans_with,
        "orphans_without": len(orphans) - orphans_with,
        "discrepancies_with": disc_with,
        "discrepancies_without": len(discrepancies) - disc_with,
        "out_of_period_with": oop_with,
        "out_of_period_without": len(out_of_period) - oop_with,
    }
    preview_data["counts"]["unmatched_erp"] = len(preview_data["unmatched_erp_rows"])
    return preview_data

