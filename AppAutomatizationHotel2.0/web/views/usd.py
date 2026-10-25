"""
US Dollars (USD) reconciliation workflow:
  1. File Upload (usd_upload_view)
  2. In-Memory Preview & Audit (usd_preview_view)
  3. Decision: Commit to DB (usd_commit_view) or Discard (usd_discard_view)
  4. Official Excel export from the DB (usd_export_committed_view)
"""

from django.shortcuts import render, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse

from ..models import ReconciliationProcess, SystemConfiguration
from ..services import (
    calculate_usd_preview,
    commit_usd_preview_to_database,
    generate_committed_usd_excel_bytes,
    detect_and_align_usd_files,
    enrich_preview_with_suggestions,
)
from .common import USD_SESSION_KEY


USD_FILE_INPUTS = {
    "erp_file": "usd_erp_file",
    "transbank_file": "usd_transbank_file",
    "bank_statement_file": "usd_bank_file",
    "amex_file": "usd_amex_file",
    "diners_file": "usd_diners_file",
    "visa_file": "usd_visa_file",
    "mastercard_file": "usd_mastercard_file",
}


@login_required(login_url="login")
def usd_upload_view(request):
    """Step 1: Upload the 7 USD Excel files and process them purely in memory."""
    if request.method == "POST":
        files = {arg: request.FILES.get(field) for arg, field in USD_FILE_INPUTS.items()}
        if not all(files.values()):
            messages.error(request, "Por favor seleccione los 7 archivos requeridos para la conciliación en Dólares.")
            return redirect("usd_upload")

        extra_erp_files = request.FILES.getlist("usd_extra_erp_files")
        try:
            request.session[USD_SESSION_KEY] = calculate_usd_preview(**files, extra_erp_files=extra_erp_files)
            messages.success(request, "Archivos analizados con éxito en memoria. Revise los resultados antes de guardar.")
            return redirect("usd_preview")
        except Exception as error:
            messages.error(request, f"Error al procesar los archivos en memoria: {error}")
            return redirect("usd_upload")

    return render(request, "usd_upload.html", {
        "saved_processes_count": ReconciliationProcess.objects.count(),
    })


@login_required(login_url="login")
def usd_preview_view(request):
    """Step 2: Display Excel-like financial summary tables, bank validation, and voucher tabs."""
    preview = request.session.get(USD_SESSION_KEY)
    if not preview:
        messages.warning(request, "No hay ningún proceso preliminar en memoria. Por favor cargue los archivos.")
        return redirect("usd_upload")

    needs_enrichment = (
        "suggestion_counts" not in preview
        or any(
            v.get("suggestion") and (
                "action" not in v.get("suggestion", {})
                or "Doc #" in v.get("suggestion", {}).get("headline", "")
            )
            for v in preview.get("discrepancy_vouchers", []) + preview.get("orphan_vouchers", [])
        )
    )
    if needs_enrichment:
        enrich_preview_with_suggestions(preview)
        request.session[USD_SESSION_KEY] = preview

    if "tolerance" not in preview:
        preview["tolerance"] = float(SystemConfiguration.get_solo().usd_rounding_tolerance)
        request.session[USD_SESSION_KEY] = preview

    all_cashiers = sorted({
        v.get("cashier_username", "").strip()
        for v in (
            preview.get("matched_vouchers", [])
            + preview.get("discrepancy_vouchers", [])
            + preview.get("orphan_vouchers", [])
        )
        if v.get("cashier_username") and v.get("cashier_username") != "—"
    })

    # One row per franchise with both the USD and CLP squaring side by side
    clp_by_code = {row["card_code"]: row for row in preview.get("clp_table", [])}
    franchise_rows = []
    for usd_row in preview.get("usd_table", []):
        clp_row = clp_by_code.get(usd_row["card_code"], {})
        franchise_rows.append({
            "card_code": usd_row["card_code"],
            "card_name": usd_row["card_name"].removesuffix(" US$"),
            "usd": usd_row,
            "clp": clp_row,
            "has_activity": bool(usd_row["erp_sales"] or usd_row["tbk_sales"] or clp_row.get("erp_sales")),
        })

    counts = preview.get("counts", {})
    total = counts.get("total", 0)
    match_rate = round(counts.get("matched", 0) / total * 100, 1) if total else 0.0

    context = {
        "preview": preview,
        "franchise_rows": franchise_rows,
        "match_rate": match_rate,
        "bank": preview.get("bank_validation", {}),
        "usd_table": preview.get("usd_table", []),
        "clp_table": preview.get("clp_table", []),
        "usd_totals": preview.get("usd_totals", {}),
        "clp_totals": preview.get("clp_totals", {}),
        "matched_vouchers": preview.get("matched_vouchers", []),
        "discrepancy_vouchers": preview.get("discrepancy_vouchers", []),
        "orphan_vouchers": preview.get("orphan_vouchers", []),
        "counts": counts,
        "all_cashiers": all_cashiers,
        "tolerance": preview.get("tolerance", 0.0),
        "suggestion_counts": preview.get("suggestion_counts", {}),
    }
    return render(request, "usd_preview.html", context)


@login_required(login_url="login")
def usd_discard_view(request):
    """Step 3A: Discard the in-memory reconciliation and return to upload."""
    if request.method == "POST":
        request.session.pop(USD_SESSION_KEY, None)
        messages.info(request, "Proceso descartado de la memoria. Puede cargar un nuevo lote de archivos.")
    return redirect("usd_upload")


@login_required(login_url="login")
def usd_commit_view(request):
    """
    Step 3B: Confirm and persist the previewed reconciliation into the database.
    Returns JSON confirmation (process ID and Excel download URL) when called via AJAX.
    """
    if request.method != "POST":
        return redirect("usd_preview")

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    preview = request.session.get(USD_SESSION_KEY)
    if not preview:
        if is_ajax:
            return JsonResponse({"error": "No hay datos en memoria para guardar."}, status=400)
        messages.error(request, "No hay datos en memoria para guardar.")
        return redirect("usd_upload")

    try:
        process = commit_usd_preview_to_database(request.user, preview)
    except Exception as error:
        if is_ajax:
            return JsonResponse({"error": f"Error al guardar en base de datos: {error}"}, status=500)
        messages.error(request, f"Error al guardar el proceso en base de datos: {error}")
        return redirect("usd_preview")

    request.session.pop(USD_SESSION_KEY, None)
    if is_ajax:
        return JsonResponse({
            "success": True,
            "process_id": process.id,
            "accounting_period": process.accounting_period or "Cierre Dólares (USD)",
            "execution_date": process.execution_date.strftime("%d/%m/%Y %H:%M:%S") if process.execution_date else "",
            "auditor": process.user.username,
            "total_vouchers": process.cross_match_results.count(),
            "matched_count": process.cross_match_results.filter(system_result="MATCHED").count(),
            "export_url": reverse("usd_export_committed", args=[process.id]),
            "upload_url": reverse("usd_upload"),
        })

    messages.success(request, f"¡Cierre en Dólares #{process.id} guardado formalmente con éxito en la base de datos!")
    return redirect("usd_upload")


@login_required(login_url="login")
def usd_export_committed_view(request, process_id: int):
    """Step 4: Download the official styled Excel report generated from persisted DB records."""
    try:
        excel_stream = generate_committed_usd_excel_bytes(process_id)
    except ReconciliationProcess.DoesNotExist:
        messages.error(request, f"El proceso #{process_id} no existe en la base de datos.")
        return redirect("usd_upload")

    response = HttpResponse(
        excel_stream.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="reporte_oficial_dolares_proceso_{process_id}.xlsx"'
    return response


@login_required(login_url="login")
def usd_detect_files_view(request):
    """Inspects the internal cell structure of uploaded Excel files and aligns them to the 7 USD roles."""
    if request.method != "POST":
        return JsonResponse({"error": "Método no permitido."}, status=405)

    uploaded_files = request.FILES.getlist("bulk_files")
    if not uploaded_files:
        return JsonResponse({"error": "No se recibieron archivos para analizar."}, status=400)

    return JsonResponse(detect_and_align_usd_files(uploaded_files))
