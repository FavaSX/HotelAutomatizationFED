"""
Excel Report Exporter for Hotel Plaza San Francisco Reconciliation.
Provides two Excel generation engines:
  1. generate_usd_preview_excel_bytes: Multi-sheet styled draft workbook directly from the in-memory USD preview.
  2. generate_reconciliation_excel_bytes: Styled workbook generated from persisted CrossMatchResult records.
"""

import io
from io import BytesIO
from typing import Any, Dict
import openpyxl
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from ..models import ReconciliationProcess, CrossMatchResult


def generate_usd_preview_excel_bytes(preview: Dict[str, Any]) -> io.BytesIO:
    """
    Builds a styled openpyxl Excel report directly from the in-memory USD preview dictionary.
    """
    from ..templatetags.currency_filters import format_observation

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

    # Title
    ws_summary.cell(row=1, column=1, value="HOTEL PLAZA SAN FRANCISCO — REPORTE PRELIMINAR DÓLARES (USD)").font = Font(size=14, bold=True, color="0C0C42")
    ws_summary.cell(row=2, column=1, value=f"Periodo: {preview.get('accounting_period', '')} | Estado: Borrador en Revisión").font = Font(size=10, italic=True)

    # Table 1: USD
    ws_summary.cell(row=4, column=1, value="1. CUADRATURA POR OPERADOR EN DÓLARES (US$)").font = navy_bold
    usd_headers = ["Operador", "Total TBK (US$)", "Total Operador (US$)", "Diferencia (US$)"]
    for col_idx, h in enumerate(usd_headers, 1):
        cell = ws_summary.cell(row=5, column=col_idx, value=h)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    curr_row = 6
    for item in preview.get("usd_table", []):
        ws_summary.cell(row=curr_row, column=1, value=item["card_name"]).border = thin_border
        c2 = ws_summary.cell(row=curr_row, column=2, value=item["erp_sales"])
        c2.number_format = "$#,##0.00"
        c2.border = thin_border
        c3 = ws_summary.cell(row=curr_row, column=3, value=item["tbk_sales"])
        c3.number_format = "$#,##0.00"
        c3.border = thin_border
        c4 = ws_summary.cell(row=curr_row, column=4, value=item["difference"])
        c4.number_format = "$#,##0.00"
        c4.border = thin_border
        if item["difference"] != 0:
            c4.font = Font(bold=True, color="DC2626")
        curr_row += 1

    # Totals row
    t = preview.get("usd_totals", {})
    ws_summary.cell(row=curr_row, column=1, value="TOTAL USD").font = navy_bold
    ws_summary.cell(row=curr_row, column=2, value=t.get("erp", 0.0)).number_format = "$#,##0.00"
    ws_summary.cell(row=curr_row, column=3, value=t.get("tbk", 0.0)).number_format = "$#,##0.00"
    ws_summary.cell(row=curr_row, column=4, value=t.get("diff", 0.0)).number_format = "$#,##0.00"
    for c in range(1, 5):
        ws_summary.cell(row=curr_row, column=c).fill = light_fill
        ws_summary.cell(row=curr_row, column=c).border = thin_border

    # Tab 2: Vouchers Ubicados
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

    for r_idx, v in enumerate(preview.get("matched_vouchers", []), 2):
        ws_matched.cell(row=r_idx, column=1, value=v["authorization_code"])
        ws_matched.cell(row=r_idx, column=2, value=v["document_number"])
        ws_matched.cell(row=r_idx, column=3, value=v["guest_name"])
        ws_matched.cell(row=r_idx, column=4, value=v["room_number"])
        ws_matched.cell(row=r_idx, column=5, value=v["cashier_username"])
        ws_matched.cell(row=r_idx, column=6, value=v.get("card_name", v["card_code"]))
        ws_matched.cell(row=r_idx, column=7, value=v["gross_amount_usd"]).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=8, value=v["operator_amount_usd"]).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=9, value=v["difference_usd"]).number_format = "$#,##0.00"
        ws_matched.cell(row=r_idx, column=10, value=v["erp_amount_clp"]).number_format = "$#,##0"
        ws_matched.cell(row=r_idx, column=11, value=format_observation(v["observation"]))

    # Tab 3: Discrepancias y No Ubicados
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

    r_idx = 2
    for v in preview.get("discrepancy_vouchers", []) + preview.get("orphan_vouchers", []):
        ws_disc.cell(row=r_idx, column=1, value=v["authorization_code"])
        ws_disc.cell(row=r_idx, column=2, value=v["document_number"])
        ws_disc.cell(row=r_idx, column=3, value=v["guest_name"])
        ws_disc.cell(row=r_idx, column=4, value=v["room_number"])
        ws_disc.cell(row=r_idx, column=5, value=v.get("card_name", v["card_code"]))
        ws_disc.cell(row=r_idx, column=6, value=v["gross_amount_usd"]).number_format = "$#,##0.00"
        ws_disc.cell(row=r_idx, column=7, value=v["operator_amount_usd"]).number_format = "$#,##0.00"
        ws_disc.cell(row=r_idx, column=8, value=v["difference_usd"]).number_format = "$#,##0.00"
        ws_disc.cell(row=r_idx, column=9, value=format_observation(v["observation"]))
        r_idx += 1

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream


def generate_reconciliation_excel_bytes(process_id: int, report_type: str = "MATCHED") -> BytesIO:
    """
    Generates a styled Excel report in memory from CrossMatchResult records.
    report_type:
      - 'MATCHED': Matched transactions and amount/card-type discrepancies.
      - 'UNMATCHED': Orphan transactions (missing in ERP) and Ghost charges (missing in TBK).
    """
    process = ReconciliationProcess.objects.get(id=process_id)
    queryset = CrossMatchResult.objects.filter(process=process).select_related(
        "card_type", "management_status"
    )

    if report_type == "MATCHED":
        records = queryset.filter(system_result__in=["MATCHED", "AMOUNT_MISMATCH", "CARD_TYPE_MISMATCH"])
        report_title = f"Reporte de Transacciones Ubicadas ({process.process_currency})"
        sheet_name = "Ubicadas"
        header_color = "1F4E78"
    else:
        records = queryset.filter(system_result__in=["NOT_FOUND_IN_ERP", "GHOST_IN_ERP"])
        report_title = f"Reporte de Transacciones Huérfanas y Fantasmas ({process.process_currency})"
        sheet_name = "No_Ubicadas"
        header_color = "C00000"

    rows_data = []
    for item in records:
        rows_data.append({
            "Código Autorización": item.authorization_code or "",
            "N° Documento / Boleta": item.document_number or "",
            "Fecha Venta": item.sale_date.strftime("%d/%m/%Y") if item.sale_date else "",
            "Tarjeta": item.card_type.name if item.card_type else "",
            "Local": item.merchant_location_name or "HOTEL PLAZA SAN FRANCISCO",
            "Habitación": item.room_number or "",
            "Huésped": item.guest_name or "",
            "Cajero ERP": item.cashier_username or "",
            "Monto TBK": float(item.transbank_amount),
            "Monto Operadora (USD)": float(item.operator_amount_usd),
            "Monto ERP (CLP)": float(item.erp_amount_clp),
            "Diferencia (USD)": float(item.difference_usd),
            "Diferencia (CLP)": float(item.difference_clp),
            "Resultado Sistema": item.system_result,
            "Detalle Diagnóstico": item.system_status_detail or "",
            "Observación": item.system_observation or "",
            "Estado Gestión": item.management_status.display_name if item.management_status else "",
            "Comentario Auditor": item.auditor_comment or "",
        })

    dataframe = pd.DataFrame(rows_data)
    output_buffer = BytesIO()

    with pd.ExcelWriter(output_buffer, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False, sheet_name=sheet_name, startrow=2)
        worksheet = writer.sheets[sheet_name]

        worksheet["A1"] = report_title
        worksheet["A1"].font = Font(size=14, bold=True, color=header_color)

        header_fill = PatternFill(start_color=header_color, end_color=header_color, fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)

        for cell in worksheet[3]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for column_cells in worksheet.columns:
            max_length = max(len(str(cell.value or "")) for cell in column_cells)
            column_letter = column_cells[0].column_letter
            worksheet.column_dimensions[column_letter].width = min(max(max_length + 3, 12), 45)

    output_buffer.seek(0)
    return output_buffer
