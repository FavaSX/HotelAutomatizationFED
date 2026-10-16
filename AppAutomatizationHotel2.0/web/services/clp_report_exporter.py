"""
Excel Report Exporter for the CLP (Pesos) Card Reconciliation.
Generates the official styled workbook from a committed ReconciliationProcess:
  1. Resumen Pesos: deposits validated against the bank statement and totals by medium / brand.
  2. Vouchers Cuadrados: every Transbank voucher reconciled with the Bordero.
  3. Reporte de Excepciones: the single exception report requested by the hotel
     (Tipo de error, Fecha de origen, Documento / Código, Monto discrepante, Acción sugerida).
"""

import io
from datetime import date
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from ..models import ReconciliationProcess

NAVY_FILL = PatternFill(start_color="0C0C42", end_color="0C0C42", fill_type="solid")
GOLD_FILL = PatternFill(start_color="996909", end_color="996909", fill_type="solid")
LIGHT_FILL = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
WHITE_BOLD = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
NAVY_BOLD = Font(name="Calibri", size=11, bold=True, color="0C0C42")
RED_BOLD = Font(bold=True, color="DC2626")
THIN_BORDER = Border(*(Side(style="thin", color="CBD5E1"),) * 4)
CLP_FORMAT = "$#,##0"

# Most actionable exceptions first; out-of-period sales are only pending another Bordero.
EXCEPTION_ORDER = {
    "NOT_FOUND_IN_ERP": 0,
    "AMOUNT_MISMATCH": 1,
    "CARD_TYPE_MISMATCH": 2,
    "OUT_OF_PERIOD": 3,
}

MODALITY_LABELS = {
    "CLP_CREDIT": "Crédito",
    "CLP_DEBIT": "Débito",
    "CLP_PREPAID": "Prepago",
    "CREDIT": "Crédito",
    "DEBIT": "Débito",
    "PREPAID": "Prepago",
}


def _write_header(ws, row: int, headers, fill=NAVY_FILL):
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=row, column=col_idx, value=header)
        cell.fill = fill
        cell.font = WHITE_BOLD
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _write_row(ws, row: int, values, money_columns=(), red_columns=()):
    for col_idx, value in enumerate(values, 1):
        cell = ws.cell(row=row, column=col_idx, value=value)
        cell.border = THIN_BORDER
        if col_idx in money_columns:
            cell.number_format = CLP_FORMAT
        if col_idx in red_columns and value:
            cell.font = RED_BOLD


def _split_status_detail(detail: str):
    """system_status_detail is stored as 'Medio · Tipo de error · Acción sugerida'."""
    parts = (detail or "").split(" · ", 2)
    return (parts + ["", "", ""])[:3]


def generate_committed_clp_excel_bytes(process_id: int) -> io.BytesIO:
    """Builds the styled 3-sheet Excel report from a committed CLP ReconciliationProcess."""
    process = ReconciliationProcess.objects.select_related("user").get(id=process_id, process_currency="CLP")
    validations = list(process.bank_validations.select_related("bank_movement").order_by("payment_modality"))
    summaries = list(process.summaries.filter(currency_table="CLP").select_related("card_type"))
    cross_matches = list(
        process.cross_match_results.select_related("card_type", "transbank_transaction").order_by("sale_date", "authorization_code")
    )

    wb = openpyxl.Workbook()

    # Sheet 1: Resumen Pesos
    ws = wb.active
    ws.title = "Resumen Pesos"
    ws.cell(row=1, column=1, value=f"HOTEL PLAZA SAN FRANCISCO — REPORTE OFICIAL CONCILIACIÓN PESOS (PROCESO #{process.id})").font = Font(size=14, bold=True, color="0C0C42")
    exec_str = process.execution_date.strftime("%d/%m/%Y %H:%M") if process.execution_date else ""
    ws.cell(row=2, column=1, value=f"Periodo: {process.accounting_period or 'Cierre Pesos'} | Auditor: {process.user.username} | Fecha Registro BD: {exec_str}").font = Font(size=10, italic=True)

    ws.cell(row=4, column=1, value="1. VALIDACIÓN DE ABONOS TRANSBANK EN CARTOLA").font = NAVY_BOLD
    _write_header(ws, 5, ["Medio de pago", "Abono TBK (CLP)", "Abono Cartola (CLP)", "Diferencia (CLP)", "Fecha Cartola", "Estado"])
    row = 6
    for validation in validations:
        difference = float(validation.transbank_calculated_deposit - (validation.bank_statement_deposit or 0))
        _write_row(
            ws, row,
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
    _write_header(ws, row, ["Medio · Marca", "Ventas TBK (CLP)", "Ventas Borderó (CLP)", "Diferencia (CLP)", "Comisión + IVA (CLP)", "Abono Neto (CLP)"])
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
        _write_row(ws, row, [label, *values], money_columns=(2, 3, 4, 5, 6), red_columns=(4,))
        row += 1
    _write_row(ws, row, ["TOTAL GENERAL CLP", *totals], money_columns=(2, 3, 4, 5, 6))
    for col in range(1, 7):
        ws.cell(row=row, column=col).fill = LIGHT_FILL
        ws.cell(row=row, column=col).font = NAVY_BOLD
    for col_letter in "ABCDEF":
        ws.column_dimensions[col_letter].width = 26

    # Sheet 2: Vouchers Cuadrados
    ws_matched = wb.create_sheet(title="Vouchers Cuadrados")
    _write_header(ws_matched, 1, [
        "Autorización TBK", "Código en Borderó", "Medio", "Tarjeta", "Fecha Venta", "Huésped", "Hab.",
        "Cajero", "Monto TBK", "Monto Borderó", "Diferencia", "Diagnóstico",
    ])
    row = 2
    for match in (m for m in cross_matches if m.system_result == "MATCHED"):
        modality = match.transbank_transaction.payment_modality if match.transbank_transaction else ""
        _write_row(ws_matched, row, [
            match.authorization_code or "", match.document_number or "", MODALITY_LABELS.get(modality, modality),
            match.card_type.name if match.card_type else "", match.sale_date.strftime("%d/%m/%Y") if match.sale_date else "",
            match.guest_name or "", match.room_number or "", match.cashier_username or "",
            float(match.transbank_amount), float(match.erp_amount_clp), float(match.difference_clp),
            match.system_observation or "",
        ], money_columns=(9, 10, 11))
        row += 1

    # Sheet 3: Reporte de Excepciones
    ws_exc = wb.create_sheet(title="Reporte de Excepciones")
    _write_header(ws_exc, 1, [
        "Tipo de Error", "Fecha de Origen", "Documento / Código", "Monto Discrepante", "Acción Sugerida",
        "Medio", "Tarjeta", "Huésped", "Hab.", "Cajero", "Monto TBK", "Monto Borderó", "Diagnóstico",
    ], fill=GOLD_FILL)
    row = 2
    exceptions = sorted(
        (m for m in cross_matches if m.system_result != "MATCHED"),
        key=lambda m: (EXCEPTION_ORDER.get(m.system_result, 9), m.sale_date or date.min),
    )
    for match in exceptions:
        modality_label, error_type, action = _split_status_detail(match.system_status_detail)
        code = match.authorization_code or ""
        if match.document_number and match.document_number != code:
            code = f"{code} / {match.document_number}"
        discrepant_amount = (
            float(match.difference_clp)
            if match.system_result in ("AMOUNT_MISMATCH", "CARD_TYPE_MISMATCH")
            else float(match.transbank_amount)
        )
        _write_row(ws_exc, row, [
            error_type, match.sale_date.strftime("%d/%m/%Y") if match.sale_date else "", code,
            discrepant_amount, action, modality_label, match.card_type.name if match.card_type else "",
            match.guest_name or "", match.room_number or "", match.cashier_username or "",
            float(match.transbank_amount), float(match.erp_amount_clp), match.system_observation or "",
        ], money_columns=(4, 11, 12))
        row += 1

    for sheet in (ws_matched, ws_exc):
        for column_cells in sheet.columns:
            width = max(len(str(cell.value or "")) for cell in column_cells[:200])
            sheet.column_dimensions[column_cells[0].column_letter].width = min(max(12, width + 2), 60)
        sheet.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream
