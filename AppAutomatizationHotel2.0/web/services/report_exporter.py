"""
Excel Report Exporter for Hotel Plaza San Francisco Reconciliation.
Generates styled multi-sheet Excel workbooks directly from persisted database records
(`ReconciliationProcess`, `ReconciliationSummary`, `BankDepositValidation`, `CrossMatchResult`).
"""

import io
import json
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
        "Fecha", "Cruce Voucher (TBK = ERP)", "Cruce Documento (ERP = OP)",
        "Cajero", "Tarjeta", "Monto TBK USD", "Monto Operador USD",
        "Diferencia USD", "Monto ERP CLP", "Diagnóstico"
    ]
    for col_idx, h in enumerate(matched_headers, 1):
        cell = ws_matched.cell(row=1, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold

    matched_rows = [m for m in cross_matches if m.system_result == "MATCHED"]
    for r_idx, v in enumerate(matched_rows, 2):
        sale_dt = v.sale_date.strftime("%d/%m/%Y") if v.sale_date else "—"
        ws_matched.cell(row=r_idx, column=1, value=sale_dt)
        ws_matched.cell(row=r_idx, column=2, value=f"TBK: {v.authorization_code} = ERP: {v.authorization_code}" if v.authorization_code else "")
        ws_matched.cell(row=r_idx, column=3, value=f"ERP: {v.document_number} = OP: {v.document_number}" if v.document_number else "")
        ws_matched.cell(row=r_idx, column=4, value=v.cashier_username or "")
        ws_matched.cell(row=r_idx, column=5, value=v.card_type.name if v.card_type else "")
        ws_matched.cell(row=r_idx, column=6, value=float(v.transbank_amount)).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=7, value=float(v.operator_amount_usd)).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=8, value=float(v.difference_usd)).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=9, value=float(v.erp_amount_clp)).number_format = "$#,##0"
        ws_matched.cell(row=r_idx, column=10, value=format_observation(v.system_observation or ""))

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

    # Sheet 4: Sugerencias Asistidas (Nivel 2)
    ws_sug = wb.create_sheet(title="Sugerencias Asistidas")
    sug_headers = [
        "#",
        "Voucher TBK",
        "Tipo Discrepancia",
        "Tarjeta",
        "Monto TBK (US$)",
        "Fecha POS",
        "Cód. ERP Sugerido",
        "Doc. ERP",
        "Monto ERP (CLP)",
        "Cajero ERP",
        "Huésped / Habitación",
        "Doc. Operadora",
        "Monto Op. (US$)",
        "Saldo Op. (CLP)",
        "Confianza",
        "Diagnóstico Sugerido",
        "Motivos Coincidencia",
        "Nota de Auditoría",
    ]
    for col_idx, h in enumerate(sug_headers, 1):
        cell = ws_sug.cell(row=1, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center", vertical="center")

    sug_count = 0
    curr_sug_row = 2
    for v in cross_matches:
        if not v.auditor_comment:
            continue
        try:
            sug = json.loads(v.auditor_comment)
            if not isinstance(sug, dict):
                continue
        except Exception:
            continue

        sug_count += 1
        case_type = "Con Diferencia de Monto" if v.system_result == "AMOUNT_MISMATCH" else "En TBK pero NO en ERP"
        reasons_str = ", ".join(r.get("label", "") for r in sug.get("reasons", []) if isinstance(r, dict))
        headline = sug.get("headline", "")
        confidence = sug.get("confidence", "MEDIA")
        manual_note = sug.get("manual_check_note", "")

        rows_comp = sug.get("comparison_rows", [])
        r_auth = rows_comp[0] if len(rows_comp) > 0 else {}
        r_doc = rows_comp[1] if len(rows_comp) > 1 else {}
        r_usd = rows_comp[2] if len(rows_comp) > 2 else {}
        r_clp = rows_comp[3] if len(rows_comp) > 3 else {}

        erp_auth = r_auth.get("erp_value", "—")
        erp_doc = r_doc.get("erp_value", "—")
        op_doc = r_doc.get("op_value", "—")
        op_usd_val = float(v.operator_amount_usd)
        erp_clp_val = float(v.erp_amount_clp)
        op_clp_str = r_clp.get("op_value", "")
        erp_cashier = sug.get("erp_cashier", v.cashier_username or "—")
        guest_str = f"{v.guest_name} (Hab. {v.room_number})" if v.guest_name and v.guest_name != "—" else "—"
        sale_date_str = v.sale_date.strftime("%d/%m/%Y") if v.sale_date else "—"

        ws_sug.cell(row=curr_sug_row, column=1, value=sug_count).alignment = Alignment(horizontal="center")
        ws_sug.cell(row=curr_sug_row, column=2, value=v.authorization_code or "").alignment = Alignment(horizontal="center")
        ws_sug.cell(row=curr_sug_row, column=3, value=case_type)
        ws_sug.cell(row=curr_sug_row, column=4, value=v.card_type.name if v.card_type else "")
        c_usd = ws_sug.cell(row=curr_sug_row, column=5, value=float(v.transbank_amount))
        c_usd.number_format = "$#,##0.00"
        ws_sug.cell(row=curr_sug_row, column=6, value=sale_date_str).alignment = Alignment(horizontal="center")
        ws_sug.cell(row=curr_sug_row, column=7, value=erp_auth).alignment = Alignment(horizontal="center")
        ws_sug.cell(row=curr_sug_row, column=8, value=erp_doc)
        c_clp = ws_sug.cell(row=curr_sug_row, column=9, value=erp_clp_val)
        c_clp.number_format = "$#,##0"
        ws_sug.cell(row=curr_sug_row, column=10, value=erp_cashier)
        ws_sug.cell(row=curr_sug_row, column=11, value=guest_str)
        ws_sug.cell(row=curr_sug_row, column=12, value=op_doc)
        c_op_usd = ws_sug.cell(row=curr_sug_row, column=13, value=op_usd_val)
        c_op_usd.number_format = "$#,##0.00"
        ws_sug.cell(row=curr_sug_row, column=14, value=op_clp_str)
        c_conf = ws_sug.cell(row=curr_sug_row, column=15, value=confidence)
        c_conf.alignment = Alignment(horizontal="center")
        c_conf.font = Font(bold=True, color="059669" if confidence == "ALTA" else "D97706")
        ws_sug.cell(row=curr_sug_row, column=16, value=headline)
        ws_sug.cell(row=curr_sug_row, column=17, value=reasons_str)
        ws_sug.cell(row=curr_sug_row, column=18, value=manual_note)

        for col_i in range(1, 19):
            ws_sug.cell(row=curr_sug_row, column=col_i).border = thin_border
        curr_sug_row += 1

    if sug_count == 0:
        c_empty = ws_sug.cell(row=2, column=1, value="No se detectaron sugerencias asistidas secundarias para los vouchers con diferencia o no ubicados.")
        c_empty.font = Font(italic=True, color="64748B")

    # Auto-adjust column widths for all sheets
    for ws in [ws_matched, ws_disc, ws_sug]:
        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col[:40]:
                val_str = str(cell.value or "")
                if len(val_str) > max_len:
                    max_len = len(val_str)
            ws.column_dimensions[col_letter].width = max(12, min(max_len + 3, 45))

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream

