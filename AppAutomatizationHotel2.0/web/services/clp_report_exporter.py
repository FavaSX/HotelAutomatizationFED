"""
Excel Report Exporter for the CLP (Pesos) Card Reconciliation (Phase 2).
Generates the official styled workbook from a committed ReconciliationProcess,
using shared styling helpers from `excel_styles.py`.
"""

import io
from datetime import date
from typing import Any, Dict, List
import openpyxl
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet

from ..models import CrossMatchResult, ReconciliationProcess
from .clp_reconciliation import MODALITY_LABELS as BASE_MODALITY_LABELS
from .clp_suggestion_engine import EXCEPTION_ERROR_TYPES
from .excel_styles import (
    CLP_FORMAT,
    GOLD_FILL,
    LIGHT_FILL,
    NAVY_BOLD,
    NAVY_FILL,
    auto_adjust_columns,
    write_data_row,
    write_header_row,
)
from .suggestion_actions import load_suggestion

# Most actionable exceptions first; out-of-period sales are only pending another Bordero.
EXCEPTION_ORDER = {
    "NOT_FOUND_IN_ERP": 0,
    "AMOUNT_MISMATCH": 1,
    "CARD_TYPE_MISMATCH": 2,
    "OUT_OF_PERIOD": 3,
}

MODALITY_LABELS = {
    **BASE_MODALITY_LABELS,
    **{f"CLP_{k}": v for k, v in BASE_MODALITY_LABELS.items()},
}

PROPOSAL_STATUS_LABELS = {
    "accepted": "Aceptada",
    "rejected": "Rechazada",
    "applied": "Aplicada",
}


def _split_status_detail(detail: str):
    """system_status_detail is stored as 'Medio · Tipo de error · Acción sugerida'."""
    parts = [p.strip() for p in (detail or "").split(" · ", 2)]
    return (parts + ["", "", ""])[:3]


def _to_amount(value: Any, fallback: Any = 0) -> float:
    candidate = fallback if value in (None, "", "—") else value
    try:
        return float(candidate or 0)
    except (TypeError, ValueError):
        return float(fallback or 0)


def _suggestion_origin(sug: Dict[str, Any], erp_row: Dict[str, Any]) -> str:
    db_match = sug.get("db_match") if isinstance(sug.get("db_match"), dict) else {}
    if db_match.get("process_id"):
        pid = db_match["process_id"]
        period = db_match.get("accounting_period") or ""
        return f"BD Proceso #{pid}" + (f" ({period})" if period and period != f"Proceso #{pid}" else "")
    files_involved = sug.get("files_involved") if isinstance(sug.get("files_involved"), dict) else {}
    erp_file = erp_row.get("source_file") or files_involved.get("erp") or "Borderó"
    excel_row = erp_row.get("excel_row")
    if isinstance(excel_row, int):
        return f"{erp_file} (Fila {excel_row})"
    return str(erp_file)


def _suggestion_proposal_text(sug: Dict[str, Any]) -> str:
    proposal = sug.get("proposed_change")
    if isinstance(proposal, dict):
        state = PROPOSAL_STATUS_LABELS.get(proposal.get("status"), "Pendiente")
        label = proposal.get("label") or ""
        return f"[{state}] {label}" if label else state
    return sug.get("action") or "Revisión manual"


def _suggestion_case_label(match: CrossMatchResult) -> str:
    if match.system_result == "MATCHED":
        return "Cuadrado (Sugerencia aplicada)"
    _, error_type, _ = _split_status_detail(match.system_status_detail)
    return error_type or EXCEPTION_ERROR_TYPES.get(match.system_result, match.system_result)


def _populate_clp_suggestions_sheet(ws: Worksheet, cross_matches: List[CrossMatchResult]) -> None:
    """Writes Level 2 assisted suggestions persisted in `CrossMatchResult.auditor_comment` for CLP."""
    headers = [
        "#",
        "Voucher TBK",
        "Tipo Excepción",
        "Tarjeta / Medio",
        "Fecha POS",
        "Monto TBK (CLP)",
        "Doc. ERP Sugerido",
        "Cuenta ERP Sugerida",
        "Medio ERP",
        "Fecha ERP",
        "Monto ERP (CLP)",
        "Cajero ERP",
        "Origen Sugerencia",
        "Confianza",
        "Cambio Propuesto / Estado",
        "Diagnóstico Sugerido",
        "Motivos Coincidencia",
        "Nota de Auditoría",
    ]
    write_header_row(ws, 1, headers, fill=NAVY_FILL, center=True, wrap_text=True)

    center_align = Alignment(horizontal="center")
    ordered_matches = sorted(
        cross_matches,
        key=lambda m: (EXCEPTION_ORDER.get(m.system_result, 9), m.sale_date or date.min, m.authorization_code or ""),
    )

    sug_count, curr_row = 0, 2
    for match in ordered_matches:
        sug = load_suggestion(match)
        if not sug:
            continue

        sug_count += 1
        erp_row = sug.get("erp_row") if isinstance(sug.get("erp_row"), dict) else {}
        modality_label, _, _ = _split_status_detail(match.system_status_detail)
        if not modality_label and match.transbank_transaction_id:
            modality_label = MODALITY_LABELS.get(match.transbank_transaction.payment_modality, "")
        card_name = match.card_type.name if match.card_type else ""
        if modality_label and card_name and modality_label.lower() not in card_name.lower():
            card_display = f"{modality_label} · {card_name}"
        else:
            card_display = card_name or modality_label or "—"

        cashier_erp = erp_row.get("cashier_username")
        if not cashier_erp or cashier_erp == "—":
            cashier_erp = match.cashier_username if match.cashier_username and match.cashier_username != "—" else "—"

        confidence = sug.get("confidence", "MEDIA")
        reasons_str = ", ".join(
            r.get("label", "")
            for r in sug.get("reasons", [])
            if isinstance(r, dict) and r.get("label")
        )

        write_data_row(
            ws,
            curr_row,
            [
                sug_count,
                match.authorization_code or "",
                _suggestion_case_label(match),
                card_display,
                match.sale_date.strftime("%d/%m/%Y") if match.sale_date else "—",
                float(match.transbank_amount),
                erp_row.get("authorization_code") or match.display_erp_document or "—",
                erp_row.get("account_code") or match.display_account_code or "—",
                erp_row.get("payment_code") or "—",
                erp_row.get("transaction_date") or "—",
                _to_amount(erp_row.get("amount"), fallback=match.erp_amount_clp),
                cashier_erp,
                _suggestion_origin(sug, erp_row),
                confidence,
                _suggestion_proposal_text(sug),
                sug.get("headline", ""),
                reasons_str,
                sug.get("manual_check_note", ""),
            ],
            money_columns=(6, 11),
            number_format=CLP_FORMAT,
        )
        for col_idx in (1, 2, 5, 7, 9, 10, 14):
            ws.cell(row=curr_row, column=col_idx).alignment = center_align
        ws.cell(row=curr_row, column=14).font = Font(
            bold=True, color="059669" if confidence == "ALTA" else "D97706"
        )
        curr_row += 1

    if sug_count == 0:
        c_empty = ws.cell(
            row=2,
            column=1,
            value="No se detectaron sugerencias asistidas secundarias para los vouchers con excepción.",
        )
        c_empty.font = Font(italic=True, color="64748B")


def generate_committed_clp_excel_bytes(process_id: int) -> io.BytesIO:
    """Builds the styled 4-sheet Excel report from a committed CLP ReconciliationProcess."""
    process = ReconciliationProcess.objects.select_related("user").get(id=process_id, process_currency="CLP")
    validations = list(process.bank_validations.select_related("bank_movement").order_by("payment_modality"))
    summaries = list(process.summaries.filter(currency_table="CLP").select_related("card_type"))
    cross_matches = list(
        process.cross_match_results.select_related(
            "card_type", "transbank_transaction", "erp_transaction"
        ).order_by("sale_date", "authorization_code")
    )

    wb = openpyxl.Workbook()

    # Sheet 1: Resumen Pesos
    ws = wb.active
    ws.title = "Resumen Pesos"
    ws.cell(
        row=1,
        column=1,
        value=f"HOTEL PLAZA SAN FRANCISCO — REPORTE OFICIAL CONCILIACIÓN PESOS (PROCESO #{process.id})",
    ).font = Font(size=14, bold=True, color="0C0C42")
    exec_str = process.execution_date.strftime("%d/%m/%Y %H:%M") if process.execution_date else ""
    ws.cell(
        row=2,
        column=1,
        value=f"Periodo: {process.accounting_period or 'Cierre Pesos'} | Auditor: {process.user.username} | Fecha Registro BD: {exec_str}",
    ).font = Font(size=10, italic=True)

    ws.cell(row=4, column=1, value="1. VALIDACIÓN DE ABONOS TRANSBANK EN CARTOLA").font = NAVY_BOLD
    write_header_row(ws, 5, ["Medio de pago", "Abono TBK (CLP)", "Abono Cartola (CLP)", "Diferencia (CLP)", "Fecha Cartola", "Estado"], fill=NAVY_FILL, center=True, wrap_text=True)
    row = 6
    for validation in validations:
        difference = float(validation.transbank_calculated_deposit - (validation.bank_statement_deposit or 0))
        write_data_row(
            ws,
            row,
            [
                MODALITY_LABELS.get(validation.payment_modality, validation.payment_modality),
                float(validation.transbank_calculated_deposit),
                float(validation.bank_statement_deposit or 0),
                difference,
                validation.deposit_date.strftime("%d/%m/%Y") if validation.deposit_date else "No detectado",
                "Confirmado en Cartola" if validation.is_matched else "Pendiente en Cartola",
            ],
            money_columns=(2, 3, 4),
            red_columns=(4,),
        )
        row += 1

    row += 2
    ws.cell(row=row, column=1, value="2. CUADRATURA POR MEDIO DE PAGO Y MARCA (TRANSBANK VS BORDERÓ)").font = NAVY_BOLD
    row += 1
    write_header_row(ws, row, ["Medio · Marca", "Ventas TBK (CLP)", "Ventas Borderó (CLP)", "Diferencia (CLP)", "Comisión + IVA (CLP)", "Abono Neto (CLP)"], fill=NAVY_FILL, center=True, wrap_text=True)
    row += 1
    totals = [0.0] * 5
    for summary in sorted(summaries, key=lambda s: (s.payment_modality or "", s.card_type.name)):
        values = [
            float(summary.transbank_sales_total),
            float(summary.erp_sales_total),
            float(summary.difference),
            float(summary.total_commissions),
            float(summary.net_deposit_total),
        ]
        totals = [t + v for t, v in zip(totals, values)]
        label = f"{MODALITY_LABELS.get(summary.payment_modality, summary.payment_modality)} · {summary.card_type.name}"
        write_data_row(ws, row, [label, *values], money_columns=(2, 3, 4, 5, 6), red_columns=(4,))
        row += 1
    write_data_row(ws, row, ["TOTAL GENERAL CLP", *totals], money_columns=(2, 3, 4, 5, 6))
    for col in range(1, 7):
        ws.cell(row=row, column=col).fill = LIGHT_FILL
        ws.cell(row=row, column=col).font = NAVY_BOLD
    for col_letter in "ABCDEF":
        ws.column_dimensions[col_letter].width = 26

    # Sheet 2: Vouchers Cuadrados
    ws_matched = wb.create_sheet(title="Vouchers Cuadrados")
    write_header_row(ws_matched, 1, [
        "Autorización TBK", "Cuenta", "Documento Borderó", "Medio", "Tarjeta", "Fecha Venta",
        "Cajero", "Monto TBK", "Monto Borderó", "Diferencia", "Diagnóstico",
    ], fill=NAVY_FILL, center=True, wrap_text=True)
    row = 2
    for match in (m for m in cross_matches if m.system_result == "MATCHED"):
        modality = match.transbank_transaction.payment_modality if match.transbank_transaction else ""
        write_data_row(
            ws_matched,
            row,
            [
                match.authorization_code or "",
                match.display_account_code or "",
                match.display_erp_document or match.authorization_code or "",
                MODALITY_LABELS.get(modality, modality),
                match.card_type.name if match.card_type else "",
                match.sale_date.strftime("%d/%m/%Y") if match.sale_date else "—",
                match.cashier_username or "",
                float(match.transbank_amount),
                float(match.erp_amount_clp),
                float(match.difference_clp),
                match.system_observation or "",
            ],
            money_columns=(8, 9, 10),
        )
        row += 1

    # Sheet 3: Reporte de Excepciones (sorted by severity, then date)
    ws_exc = wb.create_sheet(title="Reporte de Excepciones")
    write_header_row(ws_exc, 1, [
        "Tipo de error",
        "Fecha de origen",
        "Medio de pago",
        "Código en Transbank",
        "Cuenta",
        "Documento Borderó",
        "Monto Transbank (CLP)",
        "Monto Borderó (CLP)",
        "Monto discrepante (CLP)",
        "Cajero",
        "Acción sugerida",
        "Detalle",
    ], fill=GOLD_FILL, center=True, wrap_text=True)
    exceptions = sorted(
        (m for m in cross_matches if m.system_result != "MATCHED"),
        key=lambda m: (EXCEPTION_ORDER.get(m.system_result, 9), m.sale_date or date.min, m.authorization_code or ""),
    )
    row = 2
    for match in exceptions:
        modality_label, error_type, action = _split_status_detail(match.system_status_detail)
        sug = load_suggestion(match)
        if sug and sug.get("action"):
            action = sug["action"]
        cashier = match.cashier_username if match.cashier_username and match.cashier_username != "—" else "—"
        write_data_row(
            ws_exc,
            row,
            [
                error_type or match.system_result,
                match.sale_date.strftime("%d/%m/%Y") if match.sale_date else "—",
                modality_label,
                match.authorization_code or "",
                match.display_account_code or "—",
                match.display_erp_document or "—",
                float(match.transbank_amount),
                float(match.erp_amount_clp),
                float(match.difference_clp),
                cashier,
                action,
                match.system_observation or "",
            ],
            money_columns=(7, 8, 9),
            red_columns=(1, 9),
        )
        row += 1

    # Sheet 4: Sugerencias Asistidas (Nivel 2 desde auditor_comment)
    ws_sug = wb.create_sheet(title="Sugerencias Asistidas")
    _populate_clp_suggestions_sheet(ws_sug, cross_matches)

    auto_adjust_columns((ws_matched, ws_exc, ws_sug), min_width=14, max_width=45)

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream
