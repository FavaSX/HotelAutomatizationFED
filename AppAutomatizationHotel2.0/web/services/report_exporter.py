"""
Excel Report Exporter for Hotel Plaza San Francisco Reconciliation.
Generates styled Excel workbooks directly from database CrossMatchResult records,
including full guest, room, cashier, USD, and CLP columns.
"""

from io import BytesIO
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment

from ..models import ReconciliationProcess, CrossMatchResult


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

