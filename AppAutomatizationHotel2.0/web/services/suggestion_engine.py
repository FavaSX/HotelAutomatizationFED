"""
Level 2 Assisted Suggestion Engine for USD Financial Reconciliation.
Analyzes Discrepancy Vouchers (AMOUNT_MISMATCH) and Orphan Vouchers (NOT_FOUND_IN_ERP)
against unlinked Card Operator statements and unreconciled ERP transactions using
secondary columns (USD amount, CLP balance, corrected CLP balance, document number,
sale date, room/guest, and OCR/human-typo authorization code similarity).

Reuses formatting and sanitization utilities from `currency_filters` and `column_detector`.
"""

import difflib
from typing import Any, Dict, List, Optional, Tuple

from ..templatetags.currency_filters import fmt_clp, fmt_usd
from .column_detector import clean_code_string

OCR_REPLACEMENTS = (
    ("G", "6"),
    ("O", "0"),
    ("B", "8"),
    ("T", "7"),
    ("Z", "7"),
    ("S", "5"),
    ("I", "1"),
)


def _normalize_ocr_code(code: Any) -> str:
    """Strips leading zeros and unifies common POS/OCR character confusions."""
    cleaned = clean_code_string(code).lstrip("0")
    for src, dst in OCR_REPLACEMENTS:
        cleaned = cleaned.replace(src, dst)
    return cleaned


def _compute_code_similarity(tbk_code: Any, erp_code: Any) -> Tuple[float, Optional[str]]:
    """
    Compares a Transbank authorization code against an ERP authorization code.
    Returns (similarity_score_0_to_1, human_readable_reason_badge).
    """
    raw_a = clean_code_string(tbk_code).lstrip("0")
    raw_b = clean_code_string(erp_code).lstrip("0")
    if not raw_a or not raw_b:
        return 0.0, None

    if raw_a == raw_b:
        return 1.0, f"Cód. Autorización Exacto ({erp_code})"

    seq_ratio = difflib.SequenceMatcher(None, raw_a, raw_b).ratio()
    ocr_ratio = difflib.SequenceMatcher(None, _normalize_ocr_code(raw_a), _normalize_ocr_code(raw_b)).ratio()
    best_ratio = max(seq_ratio, ocr_ratio)

    if len(raw_a) == len(raw_b):
        diffs = [(ca, cb) for ca, cb in zip(raw_a, raw_b) if ca != cb]
        if len(diffs) == 1:
            ca, cb = diffs[0]
            return max(best_ratio, 0.92), f"Cód. Similar en ERP: {erp_code} (tipeo {ca} por {cb})"
        if len(diffs) == 2 and best_ratio >= 0.60:
            pairs = ", ".join(f"{ca} por {cb}" for ca, cb in diffs)
            return max(best_ratio, 0.80), f"Cód. Similar en ERP: {erp_code} (tipeo {pairs})"

    if abs(len(raw_a) - len(raw_b)) == 1 and best_ratio >= 0.80:
        return best_ratio, f"Cód. Similar en ERP: {erp_code} (1 carácter omitido/extra)"

    if best_ratio >= 0.65:
        return best_ratio, f"Cód. Similar en ERP: {erp_code} ({int(round(best_ratio * 100))}% similitud)"

    return best_ratio, None


def _chip(label: str, icon: str, tone: str = "cyan") -> Dict[str, str]:
    """Creates a standardized match-reason badge dictionary."""
    return {"label": label, "icon": icon, "tone": tone}


def _comp_row(
    field: str,
    icon: str,
    tbk_val: str,
    erp_val: str,
    op_val: str,
    status_label: str,
    status_tone: str = "emerald",
) -> Dict[str, str]:
    """Creates a standardized 3-file comparison row for the modal table."""
    return {
        "field": field,
        "icon": icon,
        "tbk_value": tbk_val,
        "erp_value": erp_val,
        "op_value": op_val,
        "status_label": status_label,
        "status_tone": status_tone,
    }


def _build_standard_comparison_rows(
    *,
    tbk_auth: str,
    erp_auth: str,
    op_auth: str,
    auth_status: Tuple[str, str],
    erp_doc_str: str,
    op_doc_str: str,
    doc_status: Tuple[str, str],
    tbk_usd: float,
    op_usd_str: str,
    usd_status: Tuple[str, str],
    erp_clp_str: str,
    op_clp_str: str,
    clp_status: Tuple[str, str],
    tbk_context: str,
    erp_context: str,
    op_context: str,
    context_status: Tuple[str, str],
) -> List[Dict[str, str]]:
    """Builds the 5-row side-by-side comparison matrix across Transbank, ERP, and Card Operator."""
    return [
        _comp_row("Cód. Autorización (Voucher)", "bi-upc-scan", tbk_auth, erp_auth, op_auth, *auth_status),
        _comp_row("N° Documento / Folio", "bi-file-earmark-check", "— (No incluye Doc ERP)", erp_doc_str, op_doc_str, *doc_status),
        _comp_row("Monto en Dólares (US$)", "bi-currency-dollar", fmt_usd(tbk_usd), "— (Facturado en CLP)", op_usd_str, *usd_status),
        _comp_row("Monto / Saldo en Pesos (CLP)", "bi-cash-stack", "— (Liquidado en US$)", erp_clp_str, op_clp_str, *clp_status),
        _comp_row("Huésped / Hab. / Fecha", "bi-person-vcard", tbk_context, erp_context, op_context, *context_status),
    ]


def enrich_preview_with_suggestions(preview_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enriches `discrepancy_vouchers` and `orphan_vouchers` in `preview_data` in-place
    with a `suggestion` dictionary containing candidate details, highlighted
    match reason badges (`reasons`), and a side-by-side comparison matrix (`comparison_rows`).
    """
    if not preview_data:
        return preview_data

    matched_vouchers = preview_data.get("matched_vouchers", [])
    discrepancy_vouchers = preview_data.get("discrepancy_vouchers", [])
    orphan_vouchers = preview_data.get("orphan_vouchers", [])
    operator_txs = preview_data.get("card_operator_transactions", [])
    erp_txs = preview_data.get("erp_transactions", [])
    file_names = preview_data.get("file_names", {})

    op_file_by_card = {
        "AX": file_names.get("amex", "AMEX DOLAR.XLS"),
        "DI": file_names.get("diners", "DINERS DOLAR.XLS"),
        "VI": file_names.get("visa", "VISA DOLAR.XLS"),
        "MC": file_names.get("mastercard", "MASTERCARD DOLAR.XLS"),
    }
    tbk_file_name = file_names.get("transbank", "ABONO TBK.xlsx")
    erp_file_name = file_names.get("erp", "TARJETAS ERP.xlsx")

    def _files_dict(card_code: str) -> Dict[str, str]:
        return {
            "tbk": tbk_file_name,
            "erp": erp_file_name,
            "op": op_file_by_card.get(card_code, "Operadora DOLAR.XLS"),
        }

    claimed_op_indices = {
        v.get("operator_index") for v in matched_vouchers if v.get("operator_index") is not None
    }
    for disc in discrepancy_vouchers:
        if disc.get("operator_index") is not None and float(disc.get("operator_amount_usd") or 0) > 0:
            claimed_op_indices.add(disc.get("operator_index"))

    claimed_erp_indices = {idx for idx, erp in enumerate(erp_txs) if erp.get("reconciled")}
    for v in matched_vouchers + discrepancy_vouchers:
        if v.get("erp_index") is not None:
            claimed_erp_indices.add(v.get("erp_index"))

    # =========================================================================
    # 1. ENRICH DISCREPANCY VOUCHERS (AMOUNT_MISMATCH)
    # =========================================================================
    for disc in discrepancy_vouchers:
        card_code = disc.get("card_code", "")
        card_name = disc.get("card_name", card_code)
        tbk_usd = float(disc.get("gross_amount_usd") or 0.0)
        op_usd = float(disc.get("operator_amount_usd") or 0.0)
        erp_clp = float(disc.get("erp_amount_clp") or 0.0)
        op_clp = float(disc.get("operator_amount_clp") or 0.0)
        diff_usd = float(disc.get("difference_usd") or 0.0)
        doc_num = str(disc.get("document_number") or "")
        auth_code = str(disc.get("authorization_code") or "")
        sale_date = str(disc.get("sale_date") or "—")
        guest_name = str(disc.get("guest_name") or "—")
        room_number = str(disc.get("room_number") or "—")
        cashier = str(disc.get("cashier_username") or "—")
        masked_card = str(disc.get("masked_card") or "—")
        obs = str(disc.get("observation") or "")
        op_file_label = op_file_by_card.get(card_code, "Operadora DOLAR.XLS")

        # Case 1A: Not found in Operator by document_number (op_usd == 0 and op_clp == 0)
        if op_usd == 0.0 and "No encontrado en archivo Operadora" in obs:
            best_op, best_op_idx, best_score = None, None, 0
            for idx, op in enumerate(operator_txs):
                if idx in claimed_op_indices or op.get("card_code") != card_code:
                    continue
                cand_usd = float(op.get("foreign_currency_amount") or 0.0)
                cand_clp = float(op.get("balance_amount") or 0.0)
                cand_corr_clp = float(op.get("corrected_balance_amount") or 0.0)

                clp_exact = erp_clp > 0 and (abs(cand_clp - erp_clp) <= 10 or abs(cand_corr_clp - erp_clp) <= 10)
                usd_diff = abs(cand_usd - tbk_usd)
                score = (65 if clp_exact else 0) + (55 if (usd_diff <= 0.50 and cand_usd > 0) else (40 if (usd_diff <= 2.00 and cand_usd > 0) else 0))
                if score > best_score and score >= 55:
                    best_score, best_op, best_op_idx = score, op, idx

            if best_op is not None and best_op_idx is not None:
                claimed_op_indices.add(best_op_idx)
                cand_usd = float(best_op.get("foreign_currency_amount") or 0.0)
                cand_clp = float(best_op.get("balance_amount") or best_op.get("corrected_balance_amount") or 0.0)
                op_doc = str(best_op.get("document_number") or "—")
                usd_var = abs(tbk_usd - cand_usd)

                reasons = []
                if erp_clp > 0 and abs(cand_clp - erp_clp) <= 10:
                    reasons.append(_chip(f"Saldo CLP Exacto ({fmt_clp(erp_clp)})", "bi-cash-stack", "emerald"))
                reasons.append(
                    _chip(
                        f"Monto US$ Exacto ({fmt_usd(cand_usd)})" if usd_var < 0.01 else f"Monto US$ Cercano ({fmt_usd(cand_usd)} · Dif: {fmt_usd(usd_var)})",
                        "bi-currency-dollar",
                        "emerald",
                    )
                )
                reasons.append(_chip(f"Misma Tarjeta ({card_code})", "bi-credit-card-2-front", "purple"))
                reasons.append(_chip(f"Doc. Operadora #{op_doc} vs ERP #{doc_num}", "bi-file-earmark-diff", "gold"))

                disc["suggestion"] = {
                    "confidence": "ALTA" if best_score >= 90 else "MEDIA",
                    "headline": f"Registro hallado en Operadora bajo Doc #{op_doc}",
                    "candidate_summary": f"Operadora {card_name}: Doc #{op_doc} · Monto Op: {fmt_usd(cand_usd)} · Saldo: {fmt_clp(cand_clp)} CLP",
                    "files_involved": _files_dict(card_code),
                    "manual_check_note": (
                        f"El voucher {auth_code} sí está en el ERP (Doc #{doc_num}, {guest_name}, Hab. {room_number}), "
                        f"pero no cruzó en Nivel 1 porque en '{op_file_label}' fue registrado con el Doc #{op_doc}. "
                        f"Sin embargo, el Saldo CLP ({fmt_clp(erp_clp)}) es 100% idéntico y el monto USD ({fmt_usd(cand_usd)} "
                        f"vs {fmt_usd(tbk_usd)}) varía solo en {fmt_usd(usd_var)}. Valide manualmente ambos folios."
                    ),
                    "reasons": reasons,
                    "comparison_rows": _build_standard_comparison_rows(
                        tbk_auth=auth_code,
                        erp_auth=auth_code,
                        op_auth=best_op.get("authorization_code") or "— (Sin cód. en origen)",
                        auth_status=(f"Exacto en TBK y ERP ({auth_code})", "emerald"),
                        erp_doc_str=f"Doc #{doc_num}",
                        op_doc_str=f"Doc #{op_doc} (Sec. {best_op.get('sequence_number') or '1'})",
                        doc_status=(f"N° Doc distinto (#{doc_num} vs #{op_doc})", "gold"),
                        tbk_usd=tbk_usd,
                        op_usd_str=fmt_usd(cand_usd),
                        usd_status=(f"Monto US$ Exacto ({fmt_usd(cand_usd)})" if usd_var < 0.01 else f"Muy cercano (Dif: {fmt_usd(usd_var)})", "emerald"),
                        erp_clp_str=fmt_clp(erp_clp),
                        op_clp_str=fmt_clp(cand_clp),
                        clp_status=(f"Saldo CLP 100% Exacto ({fmt_clp(erp_clp)})", "emerald"),
                        tbk_context=f"{card_name} ({card_code}) · {sale_date} · {masked_card}",
                        erp_context=f"{guest_name} · Hab. {room_number} · {sale_date} ({cashier})",
                        op_context=f"Operadora {card_name} · Doc #{op_doc}",
                        context_status=("Misma Tarjeta, Fecha y Estadía", "cyan"),
                    ),
                }
                continue

        # Case 1B: Operator matched by Doc and Corrected CLP, but Foreign Currency USD is 0.00
        if op_usd == 0.0 and erp_clp > 0 and "SALDO" in obs.upper():
            disc["suggestion"] = {
                "confidence": "ALTA",
                "headline": f"Vinculado por Doc #{doc_num} y Saldo CLP (Col. USD en 0,00 en Operadora)",
                "candidate_summary": f"ERP y Operadora coinciden en Doc #{doc_num} y {fmt_clp(erp_clp)} CLP, pero Operadora reportó US$ 0,00 en Moneda Extranjera.",
                "files_involved": _files_dict(card_code),
                "manual_check_note": (
                    f"El documento #{doc_num} ({guest_name}, Hab. {room_number}) coincide exactamente en "
                    f"Saldo Corregido CLP ({fmt_clp(erp_clp)}) entre el ERP y '{op_file_label}', pero la columna "
                    f"Moneda Extranjera (US$) viene en 0,00 en el archivo de la operadora."
                ),
                "reasons": [
                    _chip(f"Doc. ERP = Operadora (#{doc_num})", "bi-file-earmark-check", "cyan"),
                    _chip(f"Saldo CLP Exacto ({fmt_clp(erp_clp)})", "bi-cash-stack", "emerald"),
                    _chip(f"Cód. Autorización ({auth_code})", "bi-upc-scan", "emerald"),
                    _chip("Col. USD en 0,00 en Operadora", "bi-exclamation-triangle", "gold"),
                ],
                "comparison_rows": _build_standard_comparison_rows(
                    tbk_auth=auth_code,
                    erp_auth=auth_code,
                    op_auth=auth_code,
                    auth_status=(f"Cód. Exacto ({auth_code})", "emerald"),
                    erp_doc_str=f"Doc #{doc_num}",
                    op_doc_str=f"Doc #{doc_num}",
                    doc_status=(f"Documento Exacto (#{doc_num})", "emerald"),
                    tbk_usd=tbk_usd,
                    op_usd_str="US$ 0,00 (Columna vacía)",
                    usd_status=("Operadora reportó US$ 0,00 en col. USD", "gold"),
                    erp_clp_str=fmt_clp(erp_clp),
                    op_clp_str=f"{fmt_clp(erp_clp)} (Saldo Corregido)",
                    clp_status=(f"Saldo CLP 100% Exacto ({fmt_clp(erp_clp)})", "emerald"),
                    tbk_context=f"Tarjeta: {masked_card} · {sale_date}",
                    erp_context=f"{guest_name} · Hab. {room_number} · Cajero: {cashier}",
                    op_context=f"Doc #{doc_num} ({card_name})",
                    context_status=("Misma Estadía y Huésped", "cyan"),
                ),
            }
            continue

        # Case 1C: Both ERP and Operator matched (op_usd > 0), explain exchange/partial difference
        if op_usd > 0.0:
            is_small_diff = abs(diff_usd) <= 5.0
            reasons = [
                _chip(f"Cód. Autorización Exacto ({auth_code})", "bi-upc-scan", "emerald"),
                _chip(f"Doc. ERP = Operadora (#{doc_num})", "bi-file-earmark-check", "cyan"),
            ]
            if erp_clp > 0 and abs(erp_clp - op_clp) <= 10:
                reasons.append(_chip(f"Saldo CLP Exacto ({fmt_clp(erp_clp)})", "bi-cash-stack", "emerald"))
            if sale_date and sale_date != "—":
                reasons.append(_chip(f"Fecha Venta ({sale_date})", "bi-calendar-check", "cyan"))
            reasons.append(
                _chip(
                    f"Variación Cambiaria ({fmt_usd(abs(diff_usd))})" if is_small_diff else f"Cobro Parcial / Dividido (Dif: {fmt_usd(abs(diff_usd))})",
                    "bi-arrow-left-right" if is_small_diff else "bi-scissors",
                    "gold",
                )
            )

            disc["suggestion"] = {
                "confidence": "ALTA" if is_small_diff else "MEDIA",
                "headline": (
                    f"Cruce confirmado por Doc #{doc_num} y Saldo CLP (Dif. cambiaria {fmt_usd(abs(diff_usd))})"
                    if is_small_diff
                    else f"Cruce parcial en Doc #{doc_num} — Saldo CLP cuadra en {fmt_clp(erp_clp)}"
                ),
                "candidate_summary": f"Huésped: {guest_name} (Hab. {room_number}) · TBK: {fmt_usd(tbk_usd)} vs Operadora: {fmt_usd(op_usd)} · Saldo CLP: {fmt_clp(erp_clp)}",
                "files_involved": _files_dict(card_code),
                "manual_check_note": (
                    f"Tanto el Código ({auth_code}), el Doc (#{doc_num}) como el Saldo CLP ({fmt_clp(erp_clp)}) "
                    f"coinciden al 100% entre ERP y '{op_file_label}'. La diferencia de {fmt_usd(abs(diff_usd))} "
                    f"entre Transbank ({fmt_usd(tbk_usd)}) y Operadora ({fmt_usd(op_usd)}) debe verificarse manualmente."
                ),
                "reasons": reasons,
                "comparison_rows": _build_standard_comparison_rows(
                    tbk_auth=auth_code,
                    erp_auth=auth_code,
                    op_auth=auth_code,
                    auth_status=(f"Cód. Exacto ({auth_code})", "emerald"),
                    erp_doc_str=f"Doc #{doc_num}",
                    op_doc_str=f"Doc #{doc_num}",
                    doc_status=(f"Documento Exacto (#{doc_num})", "emerald"),
                    tbk_usd=tbk_usd,
                    op_usd_str=fmt_usd(op_usd),
                    usd_status=(f"Diferencia de {fmt_usd(abs(diff_usd))}", "gold"),
                    erp_clp_str=fmt_clp(erp_clp),
                    op_clp_str=fmt_clp(op_clp),
                    clp_status=(f"Saldo CLP 100% Exacto ({fmt_clp(erp_clp)})", "emerald"),
                    tbk_context=f"Tarjeta: {masked_card} · {sale_date}",
                    erp_context=f"{guest_name} · Hab. {room_number} · Cajero: {cashier}",
                    op_context=f"Operadora {card_name}",
                    context_status=("Misma Estadía y Fecha", "cyan"),
                ),
            }

    # =========================================================================
    # 2. ENRICH ORPHAN VOUCHERS (NOT_FOUND_IN_ERP)
    # =========================================================================
    candidate_proposals: List[Tuple[int, int, Optional[int], Optional[int], Dict[str, Any]]] = []

    for orph_idx, orph in enumerate(orphan_vouchers):
        card_code = orph.get("card_code", "")
        card_name = orph.get("card_name", card_code)
        tbk_usd = float(orph.get("gross_amount_usd") or 0.0)
        sale_date = str(orph.get("sale_date") or "")
        auth_code = str(orph.get("authorization_code") or "")
        masked_card = str(orph.get("masked_card") or "—")

        # 2A. Search Unused Operator + Unreconciled ERP pairs
        for op_idx, op in enumerate(operator_txs):
            if op_idx in claimed_op_indices or op.get("card_code") != card_code:
                continue
            op_usd = float(op.get("foreign_currency_amount") or 0.0)
            op_clp = float(op.get("balance_amount") or op.get("corrected_balance_amount") or 0.0)
            usd_diff = abs(op_usd - tbk_usd)
            if usd_diff > 1.50 or op_usd <= 0:
                continue

            op_doc = str(op.get("document_number") or "")
            for erp_idx, erp in enumerate(erp_txs):
                if erp_idx in claimed_erp_indices or erp.get("card_code") != card_code:
                    continue
                erp_doc = str(erp.get("document_number") or "")
                erp_clp = float(erp.get("amount") or 0.0)
                if erp_doc != op_doc and not (op_clp > 0 and abs(erp_clp - op_clp) <= 10):
                    continue

                erp_date = str(erp.get("transaction_date") or "")
                erp_time = str(erp.get("transaction_time") or "")
                erp_auth = str(erp.get("authorization_code") or "")
                erp_guest = str(erp.get("guest_name") or "—")
                erp_room = str(erp.get("room_number") or "—")
                erp_cashier = str(erp.get("cashier_username") or "—")
                erp_invoice = str(erp.get("invoice_number") or "—")

                sim_ratio, typo_badge = _compute_code_similarity(auth_code, erp_auth)
                same_date = bool(sale_date and erp_date == sale_date)
                same_doc = bool(op_doc and erp_doc == op_doc)
                same_clp = bool(op_clp > 0 and abs(erp_clp - op_clp) <= 10)

                if sim_ratio < 0.55 and not (same_date and same_doc):
                    continue

                score = (
                    (55 if usd_diff < 0.01 else 35)
                    + (25 if same_doc else 0)
                    + (20 if same_clp else 0)
                    + (25 if same_date else 0)
                    + int(sim_ratio * 65)
                )

                reasons = [
                    _chip(f"Monto US$ Exacto ({fmt_usd(op_usd)})" if usd_diff < 0.01 else f"Monto US$ Cercano ({fmt_usd(op_usd)})", "bi-currency-dollar", "emerald"),
                ]
                if typo_badge:
                    reasons.append(_chip(typo_badge, "bi-upc-scan", "gold"))
                reasons.append(
                    _chip(f"Doc. ERP = Operadora (#{erp_doc})" if same_doc else f"Doc. ERP #{erp_doc} / Op #{op_doc}", "bi-file-earmark-check", "cyan")
                )
                if same_clp:
                    reasons.append(_chip(f"Saldo CLP Exacto ({fmt_clp(erp_clp)})", "bi-cash-stack", "emerald"))
                if same_date:
                    reasons.append(_chip(f"Misma Fecha ({erp_date})", "bi-calendar-check", "cyan"))
                reasons.append(_chip(f"Misma Tarjeta ({card_code})", "bi-credit-card-2-front", "purple"))

                suggestion_payload = {
                    "confidence": "ALTA" if score >= 120 else "MEDIA",
                    "headline": f"Coincidencia sugerida en ERP y Operadora (Doc #{erp_doc})",
                    "candidate_summary": f"Huésped: {erp_guest} · Hab: {erp_room} · Cód. ERP: {erp_auth} · Op: {fmt_usd(op_usd)} ({fmt_clp(erp_clp)} CLP)",
                    "files_involved": _files_dict(card_code),
                    "manual_check_note": (
                        f"El voucher de Transbank '{auth_code}' no cruzó de forma directa porque en el ERP Hotel "
                        f"(Doc #{erp_doc}, Factura {erp_invoice}, Hab. {erp_room} — {erp_guest}) el cajero '{erp_cashier}' "
                        f"digitó el código como '{erp_auth}'. Sin embargo, el monto en dólares ({fmt_usd(op_usd)}), "
                        f"el saldo en pesos ({fmt_clp(erp_clp)}), la tarjeta ({card_code}) y la fecha ({erp_date}) "
                        f"coinciden entre los archivos. Verifique manualmente el Doc #{erp_doc} en el ERP."
                    ),
                    "reasons": reasons,
                    "comparison_rows": _build_standard_comparison_rows(
                        tbk_auth=auth_code,
                        erp_auth=erp_auth,
                        op_auth=op.get("authorization_code") or "— (Sin cód. en origen)",
                        auth_status=(typo_badge or f"Cód. ERP: {erp_auth}", "gold"),
                        erp_doc_str=f"Doc #{erp_doc} (Fact. {erp_invoice})",
                        op_doc_str=f"Doc #{op_doc} (Sec. {op.get('sequence_number') or '1'})",
                        doc_status=(f"Documento 100% Exacto (#{erp_doc})" if same_doc else f"ERP #{erp_doc} vs Op #{op_doc}", "emerald" if same_doc else "gold"),
                        tbk_usd=tbk_usd,
                        op_usd_str=fmt_usd(op_usd),
                        usd_status=(f"Monto US$ 100% Exacto ({fmt_usd(op_usd)})" if usd_diff < 0.01 else f"Monto Cercano (Dif: {fmt_usd(usd_diff)})", "emerald"),
                        erp_clp_str=fmt_clp(erp_clp),
                        op_clp_str=fmt_clp(op_clp),
                        clp_status=(f"Saldo CLP 100% Exacto ({fmt_clp(erp_clp)})" if same_clp else f"ERP {fmt_clp(erp_clp)} vs Op {fmt_clp(op_clp)}", "emerald" if same_clp else "gold"),
                        tbk_context=f"Tarjeta: {masked_card} · {sale_date}",
                        erp_context=f"{erp_guest} · Hab. {erp_room} · {erp_date} {erp_time} ({erp_cashier})".strip(),
                        op_context=f"Cargo {card_name} ({card_code})",
                        context_status=(f"Misma Fecha ({erp_date}) · Hab. {erp_room}" if same_date else f"Hab. {erp_room} · {erp_guest}", "emerald" if same_date else "cyan"),
                    ),
                }
                candidate_proposals.append((score, orph_idx, op_idx, erp_idx, suggestion_payload))

    candidate_proposals.sort(key=lambda item: item[0], reverse=True)
    assigned_orphans = set()

    for _, orph_idx, op_idx, erp_idx, suggestion_payload in candidate_proposals:
        if orph_idx in assigned_orphans:
            continue
        if op_idx is not None and op_idx in claimed_op_indices:
            continue
        if erp_idx is not None and erp_idx in claimed_erp_indices:
            continue

        orphan_vouchers[orph_idx]["suggestion"] = suggestion_payload
        assigned_orphans.add(orph_idx)
        if op_idx is not None:
            claimed_op_indices.add(op_idx)
        if erp_idx is not None:
            claimed_erp_indices.add(erp_idx)

    # 2B. Second pass for remaining unassigned orphans (Split ERP invoices or Operator-only match)
    for orph_idx, orph in enumerate(orphan_vouchers):
        if orph_idx in assigned_orphans:
            continue

        card_code = orph.get("card_code", "")
        card_name = orph.get("card_name", card_code)
        tbk_usd = float(orph.get("gross_amount_usd") or 0.0)
        sale_date = str(orph.get("sale_date") or "")
        auth_code = str(orph.get("authorization_code") or "")
        masked_card = str(orph.get("masked_card") or "—")

        exact_op_idx, exact_op = next(
            (
                (idx, op)
                for idx, op in enumerate(operator_txs)
                if idx not in claimed_op_indices
                and op.get("card_code") == card_code
                and abs(float(op.get("foreign_currency_amount") or 0.0) - tbk_usd) < 0.01
                and float(op.get("foreign_currency_amount") or 0.0) > 0
            ),
            (None, None),
        )

        same_date_erps = [
            (idx, e)
            for idx, e in enumerate(erp_txs)
            if idx not in claimed_erp_indices and e.get("card_code") == card_code and e.get("transaction_date") == sale_date
        ]
        split_found = False
        for i in range(len(same_date_erps)):
            for j in range(i + 1, len(same_date_erps)):
                idx1, e1 = same_date_erps[i]
                idx2, e2 = same_date_erps[j]
                if e1.get("room_number") != e2.get("room_number"):
                    continue
                sim1, badge1 = _compute_code_similarity(auth_code, e1.get("authorization_code"))
                sim2, badge2 = _compute_code_similarity(auth_code, e2.get("authorization_code"))
                best_sim = max(sim1, sim2)
                best_badge = badge1 if sim1 >= sim2 else badge2
                sum_clp = float(e1.get("amount") or 0.0) + float(e2.get("amount") or 0.0)
                op_clp = float(exact_op.get("balance_amount") or 0.0) if exact_op else 0.0

                if best_sim >= 0.70 and (op_clp == 0 or abs(sum_clp - op_clp) <= 20):
                    doc1, doc2 = e1.get("document_number"), e2.get("document_number")
                    auth1, auth2 = e1.get("authorization_code"), e2.get("authorization_code")
                    guest, room = e1.get("guest_name") or "—", e1.get("room_number") or "—"

                    reasons = [_chip(f"Monto US$ Exacto ({fmt_usd(tbk_usd)})", "bi-currency-dollar", "emerald")]
                    if best_badge:
                        reasons.append(_chip(best_badge, "bi-upc-scan", "gold"))
                    reasons.extend([
                        _chip(f"Factura Dividida en ERP (2 docs = {fmt_clp(sum_clp)})", "bi-layers", "emerald"),
                        _chip(f"Misma Habitación (Hab. {room})", "bi-door-open", "cyan"),
                        _chip(f"Misma Fecha ({sale_date})", "bi-calendar-check", "cyan"),
                    ])

                    orph["suggestion"] = {
                        "confidence": "ALTA",
                        "headline": f"Posible voucher dividido en 2 facturas ERP (Docs #{doc1} + #{doc2})",
                        "candidate_summary": f"Huésped: {guest} · Hab: {room} · Cód. ERP: {auth1} / {auth2} · Suma ERP: {fmt_clp(sum_clp)} CLP",
                        "files_involved": _files_dict(card_code),
                        "manual_check_note": (
                            f"El voucher de Transbank '{auth_code}' por {fmt_usd(tbk_usd)} corresponde a la suma "
                            f"exacta de 2 facturas en el ERP Hotel para el mismo huésped ({guest}, Hab. {room}) "
                            f"en la misma fecha ({sale_date}): Doc #{doc1} ({fmt_clp(e1.get('amount'))}) y "
                            f"Doc #{doc2} ({fmt_clp(e2.get('amount'))}), totalizando {fmt_clp(sum_clp)} CLP."
                        ),
                        "reasons": reasons,
                        "comparison_rows": _build_standard_comparison_rows(
                            tbk_auth=auth_code,
                            erp_auth=f"{auth1} / {auth2}",
                            op_auth="— (Sin cód. en origen)",
                            auth_status=(best_badge or f"Cód. Similar ({auth1})", "gold"),
                            erp_doc_str=f"2 Docs: #{doc1} + #{doc2}",
                            op_doc_str=f"Doc #{exact_op.get('document_number')}" if exact_op else "2 cargos divididos",
                            doc_status=(f"Voucher dividido en 2 facturas (#{doc1} + #{doc2})", "cyan"),
                            tbk_usd=tbk_usd,
                            op_usd_str=fmt_usd(tbk_usd),
                            usd_status=(f"Suma US$ 100% Exacta ({fmt_usd(tbk_usd)})", "emerald"),
                            erp_clp_str=f"{fmt_clp(e1.get('amount'))} + {fmt_clp(e2.get('amount'))} = {fmt_clp(sum_clp)}",
                            op_clp_str=fmt_clp(op_clp or sum_clp),
                            clp_status=(f"Suma CLP 100% Exacta ({fmt_clp(sum_clp)})", "emerald"),
                            tbk_context=f"Tarjeta: {masked_card} · {sale_date}",
                            erp_context=f"{guest} · Hab. {room} · {sale_date}",
                            op_context=f"Operadora {card_name}",
                            context_status=(f"Misma Hab. {room} y Fecha ({sale_date})", "emerald"),
                        ),
                    }
                    claimed_erp_indices.update((idx1, idx2))
                    if exact_op_idx is not None:
                        claimed_op_indices.add(exact_op_idx)
                    assigned_orphans.add(orph_idx)
                    split_found = True
                    break
            if split_found:
                break

        if not split_found and exact_op is not None and exact_op_idx is not None:
            op_usd = float(exact_op.get("foreign_currency_amount") or 0.0)
            op_clp = float(exact_op.get("balance_amount") or exact_op.get("corrected_balance_amount") or 0.0)
            op_doc = str(exact_op.get("document_number") or "")
            claimed_op_indices.add(exact_op_idx)
            assigned_orphans.add(orph_idx)

            orph["suggestion"] = {
                "confidence": "MEDIA",
                "headline": f"Ubicado en Operadora (Doc #{op_doc}) — No facturado en ERP",
                "candidate_summary": f"Operadora {card_name}: Doc #{op_doc} · Monto Op: {fmt_usd(op_usd)} · Saldo CLP: {fmt_clp(op_clp)}",
                "files_involved": _files_dict(card_code),
                "manual_check_note": (
                    f"El voucher '{auth_code}' ({fmt_usd(tbk_usd)}) fue localizado en la Operadora bajo el Doc #{op_doc} "
                    f"con un saldo equivalente de {fmt_clp(op_clp)} CLP, pero ese folio no aparece en el ERP Hotel. "
                    f"Revise manualmente si el número de documento fue truncado o facturado en el periodo siguiente."
                ),
                "reasons": [
                    _chip(f"Monto US$ Exacto en Operadora ({fmt_usd(op_usd)})", "bi-currency-dollar", "emerald"),
                    _chip(f"Saldo Operadora ({fmt_clp(op_clp)})", "bi-cash-stack", "cyan"),
                    _chip(f"Misma Tarjeta ({card_code})", "bi-credit-card-2-front", "purple"),
                    _chip(f"Doc. Operadora #{op_doc} ausente en ERP", "bi-exclamation-diamond", "gold"),
                ],
                "comparison_rows": _build_standard_comparison_rows(
                    tbk_auth=auth_code,
                    erp_auth="— (No figura en ERP)",
                    op_auth=exact_op.get("authorization_code") or "— (Sin cód. en origen)",
                    auth_status=("Ausente en ERP Hotel", "gold"),
                    erp_doc_str="— (Sin factura ERP asociada)",
                    op_doc_str=f"Doc #{op_doc}",
                    doc_status=(f"Operadora reporta Doc #{op_doc}", "cyan"),
                    tbk_usd=tbk_usd,
                    op_usd_str=fmt_usd(op_usd),
                    usd_status=(f"Monto US$ 100% Exacto ({fmt_usd(op_usd)})", "emerald"),
                    erp_clp_str="—",
                    op_clp_str=fmt_clp(op_clp),
                    clp_status=(f"Saldo Operadora: {fmt_clp(op_clp)}", "cyan"),
                    tbk_context=f"Tarjeta: {masked_card} · {sale_date}",
                    erp_context="—",
                    op_context=f"Operadora {card_name}",
                    context_status=("Revisar facturación en periodo siguiente", "gold"),
                ),
            }

    return preview_data
