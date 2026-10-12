"""
Excel Report Exporter for Hotel Plaza San Francisco Reconciliation.
Generates styled multi-sheet Excel workbooks directly from persisted database records
(`ReconciliationProcess`, `ReconciliationSummary`, `BankDepositValidation`, `CrossMatchResult`).
"""

import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from ..models import ReconciliationProcess


def generate_committed_usd_excel_bytes(process_id: int) -> io.BytesIO:
    """
    Builds a styled 3-sheet openpyxl Excel report directly from a committed
    ReconciliationProcess in the database.
    """
    from ..templatetags.currency_filters import format_observation

    process = ReconciliationProcess.objects.select_related("user").get(id=process_id)
    usd_summaries = list(process.summaries.filter(currency_table="USD").select_related("card_type"))
    clp_summaries = list(process.summaries.filter(currency_table="CLP").select_related("card_type"))
    bank_val = process.bank_validations.first()
    cross_matches = list(process.cross_match_results.select_related("card_type"))

    wb = openpyxl.Workbook()
    ws_summary = wb.active
    ws_summary.title = "Resumen Dólares"

    navy_fill = PatternFill(start_color="0C0C42", end_color="0C0C42", fill_type="solid")
    gold_fill = PatternFill(start_color="996909", end_color="996909", fill_type="solid")
    light_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_bold = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    navy_bold = Font(name="Calibri", size=11, bold=True, color="0C0C42")
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    # Title & Official DB Metadata
    ws_summary.cell(
        row=1, column=1,
        value=f"HOTEL PLAZA SAN FRANCISCO — REPORTE OFICIAL CONCILIACIÓN DÓLARES (PROCESO #{process.id})"
    ).font = Font(size=14, bold=True, color="0C0C42")

    exec_str = process.execution_date.strftime("%d/%m/%Y %H:%M") if process.execution_date else ""
    bank_status = "Confirmado en Cartola" if (bank_val and bank_val.is_matched) else "Pendiente en Cartola"
    deposit_usd = float(bank_val.transbank_calculated_deposit) if bank_val else 0.0
    ws_summary.cell(
        row=2, column=1,
        value=(
            f"Periodo: {process.accounting_period or 'Cierre USD'} | "
            f"Auditor: {process.user.username} | "
            f"Fecha Registro BD: {exec_str} | "
            f"Abono TBK: US$ {deposit_usd:,.2f} ({bank_status})"
        )
    ).font = Font(size=10, italic=True)

    # Table 1: USD Summary
    ws_summary.cell(row=4, column=1, value="1. CUADRATURA POR OPERADOR EN DÓLARES (US$)").font = navy_bold
    usd_headers = ["Operador", "Total TBK (US$)", "Total Operador (US$)", "Diferencia (US$)"]
    for col_idx, h in enumerate(usd_headers, 1):
        cell = ws_summary.cell(row=5, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    curr_row = 6
    total_usd_tbk = 0.0
    total_usd_op = 0.0
    total_usd_diff = 0.0

    for item in usd_summaries:
        tbk_val = float(item.erp_sales_total)
        op_val = float(item.transbank_sales_total)
        diff_val = float(item.difference)
        total_usd_tbk += tbk_val
        total_usd_op += op_val
        total_usd_diff += diff_val

        ws_summary.cell(row=curr_row, column=1, value=item.card_type.name).border = thin_border
        c2 = ws_summary.cell(row=curr_row, column=2, value=tbk_val)
        c2.number_format = "$#,##0.00"
        c2.border = thin_border
        c3 = ws_summary.cell(row=curr_row, column=3, value=op_val)
        c3.number_format = "$#,##0.00"
        c3.border = thin_border
        c4 = ws_summary.cell(row=curr_row, column=4, value=diff_val)
        c4.number_format = "$#,##0.00"
        c4.border = thin_border
        if diff_val != 0:
            c4.font = Font(bold=True, color="DC2626")
        curr_row += 1

    ws_summary.cell(row=curr_row, column=1, value="TOTAL GENERAL USD").font = navy_bold
    ws_summary.cell(row=curr_row, column=2, value=round(total_usd_tbk, 2)).number_format = "$#,##0.00"
    ws_summary.cell(row=curr_row, column=3, value=round(total_usd_op, 2)).number_format = "$#,##0.00"
    ws_summary.cell(row=curr_row, column=4, value=round(total_usd_diff, 2)).number_format = "$#,##0.00"
    for c in range(1, 5):
        ws_summary.cell(row=curr_row, column=c).fill = light_fill
        ws_summary.cell(row=curr_row, column=c).border = thin_border

    # Table 2: CLP Summary
    curr_row += 3
    ws_summary.cell(row=curr_row, column=1, value="2. CUADRATURA EN MONEDA NACIONAL (PESOS CLP)").font = navy_bold
    curr_row += 1
    clp_headers = ["Operador", "Total ERP (CLP)", "Total Operador (CLP)", "Diferencia (CLP)"]
    for col_idx, h in enumerate(clp_headers, 1):
        cell = ws_summary.cell(row=curr_row, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    curr_row += 1
    total_clp_erp = 0.0
    total_clp_op = 0.0
    total_clp_diff = 0.0

    for item in clp_summaries:
        erp_val = float(item.erp_sales_total)
        op_val = float(item.transbank_sales_total)
        diff_val = float(item.difference)
        total_clp_erp += erp_val
        total_clp_op += op_val
        total_clp_diff += diff_val

        ws_summary.cell(row=curr_row, column=1, value=item.card_type.name).border = thin_border
        c2 = ws_summary.cell(row=curr_row, column=2, value=erp_val)
        c2.number_format = "$#,##0"
        c2.border = thin_border
        c3 = ws_summary.cell(row=curr_row, column=3, value=op_val)
        c3.number_format = "$#,##0"
        c3.border = thin_border
        c4 = ws_summary.cell(row=curr_row, column=4, value=diff_val)
        c4.number_format = "$#,##0"
        c4.border = thin_border
        if diff_val != 0:
            c4.font = Font(bold=True, color="DC2626")
        curr_row += 1

    ws_summary.cell(row=curr_row, column=1, value="TOTAL GENERAL CLP").font = navy_bold
    ws_summary.cell(row=curr_row, column=2, value=round(total_clp_erp, 0)).number_format = "$#,##0"
    ws_summary.cell(row=curr_row, column=3, value=round(total_clp_op, 0)).number_format = "$#,##0"
    ws_summary.cell(row=curr_row, column=4, value=round(total_clp_diff, 0)).number_format = "$#,##0"
    for c in range(1, 5):
        ws_summary.cell(row=curr_row, column=c).fill = light_fill
        ws_summary.cell(row=curr_row, column=c).border = thin_border

    for col_letter in ["A", "B", "C", "D"]:
        ws_summary.column_dimensions[col_letter].width = 28

    # Sheet 2: Vouchers Ubicados Exactos
    ws_matched = wb.create_sheet(title="Vouchers Ubicados")
    matched_headers = [
        "Autorización", "Documento", "Huésped", "Habitación",
        "Cajero", "Tarjeta", "Monto TBK USD", "Monto Operador USD",
        "Diferencia USD", "Monto ERP CLP", "Diagnóstico"
    ]
    for col_idx, h in enumerate(matched_headers, 1):
        cell = ws_matched.cell(row=1, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold

    matched_rows = [m for m in cross_matches if m.system_result == "MATCHED"]
    for r_idx, v in enumerate(matched_rows, 2):
        ws_matched.cell(row=r_idx, column=1, value=v.authorization_code or "")
        ws_matched.cell(row=r_idx, column=2, value=v.document_number or "")
        ws_matched.cell(row=r_idx, column=3, value=v.guest_name or "")
        ws_matched.cell(row=r_idx, column=4, value=v.room_number or "")
        ws_matched.cell(row=r_idx, column=5, value=v.cashier_username or "")
        ws_matched.cell(row=r_idx, column=6, value=v.card_type.name if v.card_type else "")
        ws_matched.cell(row=r_idx, column=7, value=float(v.transbank_amount)).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=8, value=float(v.operator_amount_usd)).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=9, value=float(v.difference_usd)).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=10, value=float(v.erp_amount_clp)).number_format = "$#,##0"
        ws_matched.cell(row=r_idx, column=11, value=format_observation(v.system_observation or ""))

    # Sheet 3: Discrepancias y No Ubicados
    ws_disc = wb.create_sheet(title="Discrepancias y No Ubicados")
    disc_headers = [
        "Autorización", "Documento", "Huésped", "Habitación",
        "Tarjeta", "Monto TBK USD", "Monto Operador USD",
        "Diferencia USD", "Diagnóstico"
    ]
    for col_idx, h in enumerate(disc_headers, 1):
        cell = ws_disc.cell(row=1, column=col_idx, value=h)
        cell.fill = gold_fill
        cell.font = white_bold

    disc_rows = [m for m in cross_matches if m.system_result != "MATCHED"]
    for r_idx, v in enumerate(disc_rows, 2):
        ws_disc.cell(row=r_idx, column=1, value=v.authorization_code or "")
        ws_disc.cell(row=r_idx, column=2, value=v.document_number or "")
        ws_disc.cell(row=r_idx, column=3, value=v.guest_name or "")
        ws_disc.cell(row=r_idx, column=4, value=v.room_number or "")
        ws_disc.cell(row=r_idx, column=5, value=v.card_type.name if v.card_type else "")
        ws_disc.cell(row=r_idx, column=6, value=float(v.transbank_amount)).number_format = "$#,##0.00"
        ws_disc.cell(row=r_idx, column=7, value=float(v.operator_amount_usd)).number_format = "$#,##0.00"
        ws_disc.cell(row=r_idx, column=8, value=float(v.difference_usd)).number_format = "$#,##0.00"
        ws_disc.cell(row=r_idx, column=9, value=format_observation(v.system_observation or ""))

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream


_CLP_RESULT_LABELS = {
    "MATCHED": "Conciliado",
    "AMOUNT_MISMATCH": "Diferencia de Monto",
    "CARD_TYPE_MISMATCH": "Tipo de Tarjeta Incorrecto",
    "NOT_FOUND_IN_ERP": "No Registrada en Bordero",
    "GHOST_IN_ERP": "Cobro en Bordero sin Voucher",
    "TYPO_SUSPECTED": "Posible Error de Tipeo",
    "VOID_OR_CREDIT_NOTE": "Anulación / Nota de Crédito",
}


def generate_committed_clp_excel_bytes(process_id: int) -> io.BytesIO:
    """
    Builds a styled multi-sheet openpyxl Excel report directly from a committed
    CLP ReconciliationProcess in the database (Fase 2).
    """
    process = ReconciliationProcess.objects.select_related("user").get(id=process_id)
    clp_summaries = list(process.summaries.filter(currency_table="CLP").select_related("card_type"))
    bank_validations = list(process.bank_validations.all())
    cross_matches = list(process.cross_match_results.select_related("card_type"))

    wb = openpyxl.Workbook()
    ws_summary = wb.active
    ws_summary.title = "Resumen Pesos"

    navy_fill = PatternFill(start_color="0C0C42", end_color="0C0C42", fill_type="solid")
    gold_fill = PatternFill(start_color="996909", end_color="996909", fill_type="solid")
    light_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_bold = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    navy_bold = Font(name="Calibri", size=11, bold=True, color="0C0C42")
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    ws_summary.cell(
        row=1, column=1,
        value=f"HOTEL PLAZA SAN FRANCISCO — REPORTE OFICIAL CONCILIACIÓN PESOS CLP (PROCESO #{process.id})"
    ).font = Font(size=14, bold=True, color="0C0C42")

    exec_str = process.execution_date.strftime("%d/%m/%Y %H:%M") if process.execution_date else ""
    ws_summary.cell(
        row=2, column=1,
        value=f"Periodo: {process.accounting_period or 'Cierre CLP'} | Auditor: {process.user.username} | Fecha Registro BD: {exec_str}"
    ).font = Font(size=10, italic=True)

    # Table 1: Bank deposit validation by modality (Crédito / Débito / Prepago)
    ws_summary.cell(row=4, column=1, value="1. VALIDACIÓN DE ABONOS EN CARTOLA POR MODALIDAD").font = navy_bold
    deposit_headers = ["Modalidad", "Abono Calculado TBK (CLP)", "Abono en Cartola (CLP)", "Fecha Abono", "Estado"]
    for col_idx, h in enumerate(deposit_headers, 1):
        cell = ws_summary.cell(row=5, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    curr_row = 6
    for bv in bank_validations:
        ws_summary.cell(row=curr_row, column=1, value=bv.payment_modality).border = thin_border
        c2 = ws_summary.cell(row=curr_row, column=2, value=float(bv.transbank_calculated_deposit))
        c2.number_format = "$#,##0"
        c2.border = thin_border
        c3 = ws_summary.cell(row=curr_row, column=3, value=float(bv.bank_statement_deposit or 0))
        c3.number_format = "$#,##0"
        c3.border = thin_border
        ws_summary.cell(row=curr_row, column=4, value=bv.deposit_date.strftime("%d/%m/%Y") if bv.deposit_date else "—").border = thin_border
        status_cell = ws_summary.cell(row=curr_row, column=5, value="Confirmado" if bv.is_matched else "No Encontrado")
        status_cell.border = thin_border
        status_cell.font = Font(bold=True, color="16A34A" if bv.is_matched else "DC2626")
        curr_row += 1

    # Table 2: Brand Summary
    curr_row += 2
    ws_summary.cell(row=curr_row, column=1, value="2. CUADRATURA POR MARCA DE TARJETA (CLP)").font = navy_bold
    curr_row += 1
    brand_headers = ["Operador", "Total Bordero ERP (CLP)", "Total Transbank (CLP)", "Comisiones (CLP)", "Diferencia (CLP)"]
    for col_idx, h in enumerate(brand_headers, 1):
        cell = ws_summary.cell(row=curr_row, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    curr_row += 1
    total_erp, total_tbk, total_comm, total_diff = 0.0, 0.0, 0.0, 0.0
    for item in clp_summaries:
        erp_val = float(item.erp_sales_total)
        tbk_val = float(item.transbank_sales_total)
        comm_val = float(item.total_commissions)
        diff_val = float(item.difference)
        total_erp += erp_val
        total_tbk += tbk_val
        total_comm += comm_val
        total_diff += diff_val

        ws_summary.cell(row=curr_row, column=1, value=item.card_type.name).border = thin_border
        for col_idx, val in [(2, erp_val), (3, tbk_val), (4, comm_val), (5, diff_val)]:
            c = ws_summary.cell(row=curr_row, column=col_idx, value=val)
            c.number_format = "$#,##0"
            c.border = thin_border
            if col_idx == 5 and val != 0:
                c.font = Font(bold=True, color="DC2626")
        curr_row += 1

    ws_summary.cell(row=curr_row, column=1, value="TOTAL GENERAL CLP").font = navy_bold
    for col_idx, val in [(2, total_erp), (3, total_tbk), (4, total_comm), (5, total_diff)]:
        c = ws_summary.cell(row=curr_row, column=col_idx, value=round(val))
        c.number_format = "$#,##0"
        c.border = thin_border
        c.fill = light_fill

    for col_letter in ["A", "B", "C", "D", "E"]:
        ws_summary.column_dimensions[col_letter].width = 26

    # Sheet 2: Vouchers Conciliados
    ws_matched = wb.create_sheet(title="Vouchers Conciliados")
    matched_headers = [
        "Autorización", "Documento", "Huésped", "Habitación",
        "Cajero", "Tarjeta", "Monto TBK CLP", "Monto Bordero CLP", "Diferencia CLP", "Diagnóstico"
    ]
    for col_idx, h in enumerate(matched_headers, 1):
        cell = ws_matched.cell(row=1, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold

    matched_rows = [m for m in cross_matches if m.system_result == "MATCHED"]
    for r_idx, v in enumerate(matched_rows, 2):
        ws_matched.cell(row=r_idx, column=1, value=v.authorization_code or "")
        ws_matched.cell(row=r_idx, column=2, value=v.document_number or "")
        ws_matched.cell(row=r_idx, column=3, value=v.guest_name or "")
        ws_matched.cell(row=r_idx, column=4, value=v.room_number or "")
        ws_matched.cell(row=r_idx, column=5, value=v.cashier_username or "")
        ws_matched.cell(row=r_idx, column=6, value=v.card_type.name if v.card_type else "")
        ws_matched.cell(row=r_idx, column=7, value=float(v.transbank_amount)).number_format = "$#,##0"
        ws_matched.cell(row=r_idx, column=8, value=float(v.erp_amount_clp)).number_format = "$#,##0"
        ws_matched.cell(row=r_idx, column=9, value=float(v.difference_clp)).number_format = "$#,##0"
        ws_matched.cell(row=r_idx, column=10, value=v.system_status_detail or "")

    # Sheet 3: Discrepancias y Excepciones
    ws_disc = wb.create_sheet(title="Discrepancias y Excepciones")
    disc_headers = [
        "Tipo de Error", "Autorización", "Documento", "Huésped", "Tarjeta",
        "Monto TBK CLP", "Monto Bordero CLP", "Diferencia CLP", "Diagnóstico"
    ]
    for col_idx, h in enumerate(disc_headers, 1):
        cell = ws_disc.cell(row=1, column=col_idx, value=h)
        cell.fill = gold_fill
        cell.font = white_bold

    disc_rows = [m for m in cross_matches if m.system_result != "MATCHED"]
    for r_idx, v in enumerate(disc_rows, 2):
        ws_disc.cell(row=r_idx, column=1, value=_CLP_RESULT_LABELS.get(v.system_result, v.system_result))
        ws_disc.cell(row=r_idx, column=2, value=v.authorization_code or "")
        ws_disc.cell(row=r_idx, column=3, value=v.document_number or "")
        ws_disc.cell(row=r_idx, column=4, value=v.guest_name or "")
        ws_disc.cell(row=r_idx, column=5, value=v.card_type.name if v.card_type else "")
        ws_disc.cell(row=r_idx, column=6, value=float(v.transbank_amount)).number_format = "$#,##0"
        ws_disc.cell(row=r_idx, column=7, value=float(v.erp_amount_clp)).number_format = "$#,##0"
        ws_disc.cell(row=r_idx, column=8, value=float(v.difference_clp)).number_format = "$#,##0"
        ws_disc.cell(row=r_idx, column=9, value=v.system_status_detail or "")

    for ws in (ws_matched, ws_disc):
        for col_letter in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]:
            ws.column_dimensions[col_letter].width = 22

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream

