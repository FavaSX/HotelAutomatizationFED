"""
Level 2 Assisted Suggestion Engine for CLP (Pesos) Card Reconciliation.
Looks for unclaimed Bordero rows that explain an orphan voucher (NOT_FOUND_IN_ERP) or an
amount discrepancy (AMOUNT_MISMATCH), using amount, date, payment medium and the OCR/human-typo
authorization code similarity shared with the USD engine.
Also searches unreconciled historical ERP transactions stored in the database.

It never reconciles anything automatically: suggestions are a reference for the auditor's manual review.
"""

from typing import Any, Dict, List, Optional, Tuple

from ..models import ErpTransaction
from ..templatetags.currency_filters import fmt_clp
from .clp_reconciliation import _is_compatible
from .column_detector import clean_code_string, normalize_auth_code, safe_date
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
        "payment_group": payment_group,
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

    erp_code = str(erp_row.get("authorization_code") or "").strip()
    match_ref = f"Cód. {erp_code}" if erp_code else "Sin código"

    return {
        "confidence": "ALTA" if score >= HIGH_CONFIDENCE_SCORE else "MEDIA",
        "score": score,
        "erp_index": erp_idx,
        "headline": f"Coincidencia sugerida en Borderó ERP ({match_ref})",
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


def _find_db_suggestion_for_clp_voucher(
    voucher: Dict[str, Any],
    tolerance: float,
    tbk_file: str,
    default_erp_file: str,
    db_candidates: Optional[List[ErpTransaction]] = None,
    claimed_db_ids: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """
    Searches unreconciled historical CLP ERP transactions stored in the database
    to provide assisted suggestions for orphan or out-of-period vouchers.
    """
    auth_code = clean_code_string(voucher.get("authorization_code") or "")
    tbk_amount = float(voucher.get("tbk_amount") or 0.0)
    modality = voucher.get("payment_modality", "")
    voucher_date = str(voucher.get("sale_date") or "")

    if db_candidates is None:
        try:
            db_candidates = list(
                ErpTransaction.objects.filter(
                    reconciled=False,
                    source_currency="CLP",
                ).select_related("process", "card_type").order_by("-transaction_date")[:500]
            )
        except Exception:
            return None

    best_match = None
    best_score = 0

    for db_tx in db_candidates:
        if claimed_db_ids is not None and db_tx.id in claimed_db_ids:
            continue
        db_amount = float(db_tx.amount or 0.0)
        amount_gap = abs(tbk_amount - db_amount)
        if amount_gap > tolerance:
            continue

        db_auth = clean_code_string(db_tx.authorization_code or "")
        exact_code = bool(auth_code and db_auth and normalize_auth_code(auth_code) == normalize_auth_code(db_auth))
        sim_ratio, typo_badge = (
            (1.0, f"Mismo código en BD ({db_auth})")
            if exact_code
            else (_compute_code_similarity(auth_code, db_auth) if db_auth else (0.0, None))
        )

        exact_amount = amount_gap < 1
        missing_code = not db_auth
        if sim_ratio < 0.55 and not (missing_code and exact_amount):
            continue

        is_rc = (db_tx.payment_code in ("RCO", "RC", "REDCOMPRA")) or (db_tx.card_type and db_tx.card_type.code in ("RCO", "RC"))
        compat = is_rc == (modality in ("DEBIT", "PREPAID"))

        db_date_str = db_tx.transaction_date.strftime("%d/%m/%Y") if db_tx.transaction_date else ""
        day_gap = _day_gap(voucher_date, db_date_str)

        # Guard against weak matches when auth code is missing
        if missing_code and not (compat and day_gap is not None and 0 <= day_gap <= 2):
            continue

        score = (60 if exact_amount else 40) + int(sim_ratio * 65) + (20 if compat else 0)
        if day_gap == 0:
            score += 20
        elif day_gap and 0 <= day_gap <= 2:
            score += 10

        if score > best_score and score >= 80:
            best_score = score
            best_match = (db_tx, sim_ratio, typo_badge, exact_code, exact_amount, amount_gap, missing_code, is_rc, db_date_str)

    if not best_match:
        return None

    db_tx, sim_ratio, typo_badge, exact_code, exact_amount, amount_gap, missing_code, is_rc, db_date = best_match
    if claimed_db_ids is not None:
        claimed_db_ids.add(db_tx.id)
    db_auth = clean_code_string(db_tx.authorization_code or "")
    db_amount = float(db_tx.amount or 0.0)
    db_proc_id = db_tx.process_id
    db_proc_period = db_tx.process.accounting_period if db_tx.process else f"Proceso #{db_proc_id}"
    db_time = db_tx.transaction_time.strftime("%H:%M:%S") if db_tx.transaction_time else ""
    db_guest = db_tx.guest_name or "—"
    db_room = db_tx.room_number or "—"
    db_cashier = db_tx.cashier_username or "—"
    db_inv = db_tx.invoice_number or "—"
    db_acc = db_tx.account_code or "—"
    db_card_name = db_tx.card_type.name if db_tx.card_type else ("Redcompra" if is_rc else "Tarjeta de Crédito")
    db_pay_code = db_tx.payment_code or (db_tx.card_type.code if db_tx.card_type else "—")
    source_label = f"Base de Datos (Proceso #{db_proc_id})"

    reasons = [
        _chip(f"💾 Venta previa en Base de Datos (Proceso #{db_proc_id})", "bi-database-fill-check", "purple"),
        _chip(
            f"Monto Exacto ({fmt_clp(db_amount)})" if exact_amount else f"Monto Cercano ({fmt_clp(db_amount)} · Dif: {fmt_clp(amount_gap)})",
            "bi-cash-stack",
            "emerald",
        ),
        _chip(f"Fecha Venta ERP: {db_date}", "bi-calendar-check", "cyan"),
    ]
    if missing_code:
        cause = f"Venta pendiente en BD (Proceso #{db_proc_id}) sin código de autorización"
        reasons.append(_chip("Fila en BD sin código de autorización", "bi-exclamation-triangle", "gold"))
        action = f"Vincular con venta pendiente de BD Proceso #{db_proc_id} y anotar código en ERP"
    elif exact_code:
        cause = f"Código exacto ({db_auth}) hallado en BD Proceso #{db_proc_id}"
        reasons.append(_chip(cause, "bi-check2-circle", "emerald"))
        action = f"Vincular con venta pendiente de BD Proceso #{db_proc_id} (Doc #{db_tx.document_number}, Cta. {db_acc})"
    else:
        cause = typo_badge or f"Código digitado como {db_auth} en BD Proceso #{db_proc_id}"
        reasons.append(_chip(cause, "bi-upc-scan", "gold"))
        action = f"Vincular con venta pendiente de BD Proceso #{db_proc_id} y corregir código"

    headline = f"Venta pendiente en BD (Proceso #{db_proc_id} · {db_proc_period})"

    erp_row_view = {
        "excel_row": f"ID #{db_tx.id}",
        "source_file": source_label,
        "authorization_code": db_auth or "— (sin código)",
        "payment_code": db_pay_code,
        "card_code": db_tx.card_type.code if db_tx.card_type else "",
        "card_name": db_card_name,
        "payment_group": "REDCOMPRA" if is_rc else "CREDIT",
        "amount": db_amount,
        "transaction_date": db_date,
        "transaction_time": db_time,
        "cashier_username": db_cashier,
        "guest_name": db_guest,
        "room_number": db_room,
        "account_code": db_acc,
        "invoice_number": db_inv,
    }

    return {
        "confidence": "ALTA" if best_score >= HIGH_CONFIDENCE_SCORE else "MEDIA",
        "score": best_score,
        "erp_index": None,
        "headline": headline,
        "cause": cause,
        "missing_code": missing_code,
        "action": action,
        "candidate_summary": f"{db_guest} · Hab. {db_room} · {fmt_clp(db_amount)} · {db_date} ({db_cashier})",
        "manual_check_note": (
            f"El voucher '{voucher['authorization_code']}' ({fmt_clp(voucher['tbk_amount'])}) no figura en el Borderó actual, "
            f"pero fue localizado en la Base de Datos como venta pendiente en el proceso #{db_proc_id} ({db_proc_period}, fecha {db_date}). "
            f"Corresponde a la Habitación {db_room} (Huésped: {db_guest}), registrado por el cajero '{db_cashier}' "
            f"con código '{db_auth or 'sin código'}' (Cta. {db_acc} · Factura {db_inv})."
        ),
        "reasons": reasons,
        "files_involved": {"tbk": tbk_file, "erp": source_label},
        "erp_row": erp_row_view,
        "db_match": {
            "transaction_id": db_tx.id,
            "process_id": db_proc_id,
            "accounting_period": db_proc_period,
        },
    }


def enrich_clp_preview_with_suggestions(preview_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adds a `suggestion` dictionary to orphan, discrepancy, and out-of-period vouchers in-place,
    assigning each unclaimed Bordero row to at most one voucher (highest score first).
    Also runs database queries to suggest pending sales from earlier reconciliations.
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
        return (
            file_names.get(f"tbk_{voucher['payment_modality'].lower()}")
            or file_names.get("tbk")
            or "Transbank"
        )

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

    # 3. Out-of-Period Vouchers: check unclaimed Bordero rows (including auxiliary historical Bordero files merged into erp_rows).
    oop_proposals = []
    for oop_idx, voucher in enumerate(out_of_period):
        for erp_idx, erp_row in enumerate(erp_rows):
            if erp_idx in claimed or float(erp_row.get("amount") or 0) <= 0 or not _is_compatible(voucher["payment_modality"], erp_row):
                continue
            scored = _score_orphan_candidate(voucher, erp_row, tolerance)
            if scored:
                bonus = 5 if erp_row.get("is_auxiliary") else 0
                oop_proposals.append((scored[0] + bonus, oop_idx, erp_idx, scored[1]))

    oop_proposals.sort(key=lambda item: item[0], reverse=True)
    assigned_oop = set()
    for score, oop_idx, erp_idx, detail in oop_proposals:
        if oop_idx in assigned_oop or erp_idx in claimed:
            continue
        assigned_oop.add(oop_idx)
        claimed.add(erp_idx)
        target_row = erp_rows[erp_idx]
        voucher = out_of_period[oop_idx]
        voucher["suggestion"] = _build_candidate_suggestion(
            voucher, target_row, erp_idx, score, detail, _tbk_file(voucher), default_erp_file
        )

    # 4. Database Assisted Suggestions for remaining Orphans and Out-of-Period vouchers (single pre-fetched query)
    try:
        db_candidates = list(
            ErpTransaction.objects.filter(
                reconciled=False,
                source_currency="CLP",
            ).select_related("process", "card_type").order_by("-transaction_date")[:500]
        )
    except Exception:
        db_candidates = []
    claimed_db_ids: set = set()

    for voucher in orphans:
        if not voucher.get("suggestion"):
            db_sug = _find_db_suggestion_for_clp_voucher(
                voucher, tolerance, _tbk_file(voucher), default_erp_file, db_candidates, claimed_db_ids
            )
            if db_sug:
                voucher["suggestion"] = db_sug

    for voucher in out_of_period:
        if not voucher.get("suggestion"):
            db_sug = _find_db_suggestion_for_clp_voucher(
                voucher, tolerance, _tbk_file(voucher), default_erp_file, db_candidates, claimed_db_ids
            )
            if db_sug:
                voucher["suggestion"] = db_sug

    # 5. Exception report columns (Tipo de error / Acción sugerida) for every non-matched voucher.
    for bucket in ("discrepancy_vouchers", "orphan_vouchers", "out_of_period_vouchers"):
        for voucher in preview_data.get(bucket, []):
            suggestion = voucher.get("suggestion")
            result = voucher["system_result"]
            if suggestion and result == "NOT_FOUND_IN_ERP":
                if suggestion.get("db_match"):
                    voucher["error_type"] = "Venta previa en BD"
                elif suggestion.get("missing_code"):
                    voucher["error_type"] = "Falta código en ERP"
                else:
                    voucher["error_type"] = "Error de tipeo"
            elif suggestion and result == "OUT_OF_PERIOD":
                if suggestion.get("db_match"):
                    voucher["error_type"] = "Fuera de período (En BD)"
                else:
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
    return preview_data
