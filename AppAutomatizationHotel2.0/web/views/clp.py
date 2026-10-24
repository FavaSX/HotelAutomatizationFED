"""
Chilean Pesos (CLP) reconciliation workflow — Phase 2.
Same lifecycle as USD: upload, in-memory preview, commit or discard, official Excel export.
"""

from django.shortcuts import render, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse

from ..models import ReconciliationProcess
from ..services import (
    calculate_clp_preview,
    enrich_clp_preview_with_suggestions,
    commit_clp_preview_to_database,
    generate_committed_clp_excel_bytes,
    detect_and_align_clp_files,
)
from .common import CLP_SESSION_KEY


CLP_FILE_INPUTS = {
    "erp_file": "clp_erp_file",
    "bank_statement_file": "clp_bank_file",
    "transbank_credit_file": "clp_tbk_credit_file",
    "transbank_debit_file": "clp_tbk_debit_file",
    "transbank_prepaid_file": "clp_tbk_prepaid_file",
}


@login_required(login_url="login")
def clp_upload_view(request):
    """
    Step 1: Upload the 5 CLP Excel files (Bordero, cartola and the 3 Transbank settlements)
    and process them purely in memory.
    """
    if request.method == "POST":
        files = {arg: request.FILES.get(input_name) for arg, input_name in CLP_FILE_INPUTS.items()}
        if not all(files.values()):
            messages.error(request, "Por favor seleccione los 5 archivos requeridos para la conciliación en Pesos.")
            return redirect("clp_upload")

        extra_erp_files = request.FILES.getlist("clp_extra_erp_files")
        try:
            request.session[CLP_SESSION_KEY] = calculate_clp_preview(**files, extra_erp_files=extra_erp_files)
            messages.success(request, "Archivos analizados con éxito en memoria. Revise los resultados antes de guardar.")
            return redirect("clp_preview")
        except Exception as error:
            messages.error(request, f"Error al procesar los archivos en memoria: {error}")
            return redirect("clp_upload")

    context = {
        "saved_processes_count": ReconciliationProcess.objects.count(),
        "saved_clp_processes_count": ReconciliationProcess.objects.filter(process_currency="CLP").count(),
    }
    return render(request, "clp_upload.html", context)


@login_required(login_url="login")
def clp_detect_files_view(request):
    """Inspects the uploaded Excel files and aligns them against the 5 required CLP roles."""
    if request.method != "POST":
        return JsonResponse({"error": "Método no permitido."}, status=405)

    uploaded_files = request.FILES.getlist("bulk_files")
    if not uploaded_files:
        return JsonResponse({"error": "No se recibieron archivos para analizar."}, status=400)

    return JsonResponse(detect_and_align_clp_files(uploaded_files))


@login_required(login_url="login")
def clp_preview_view(request):
    """
    Step 2: Bank deposit validations, summary by payment medium, voucher tabs and exception report.
    """
    preview = request.session.get(CLP_SESSION_KEY)
    if not preview:
        messages.warning(request, "No hay ningún proceso preliminar en memoria. Por favor cargue los archivos.")
        return redirect("clp_upload")

    needs_enrichment = (
        "suggestion_counts" not in preview
        or any(
            v.get("suggestion") and (
                "action" not in v.get("suggestion", {})
                or "Doc #" in v.get("suggestion", {}).get("headline", "")
                or v.get("suggestion", {}).get("headline", "").startswith("Posible registro")
            )
            for v in preview.get("discrepancy_vouchers", []) + preview.get("orphan_vouchers", []) + preview.get("out_of_period_vouchers", [])
        )
        or any(
            "no encontrado en el Borderó" in v.get("observation", "")
            for v in preview.get("orphan_vouchers", []) + preview.get("out_of_period_vouchers", [])
        )
    )
    if needs_enrichment:
        enrich_clp_preview_with_suggestions(preview)
        request.session[CLP_SESSION_KEY] = preview

    all_cashiers = sorted({
        r.get("cashier_username", "").strip()
        for r in (
            preview.get("matched_vouchers", [])
            + preview.get("discrepancy_vouchers", [])
            + preview.get("orphan_vouchers", [])
            + preview.get("out_of_period_vouchers", [])
        )
        if r.get("cashier_username") and r.get("cashier_username") != "—"
    })

    context = {
        "preview": preview,
        "bank_validations": preview.get("bank_validations", []),
        "modality_table": preview.get("modality_table", []),
        "clp_totals": preview.get("clp_totals", {}),
        "matched_vouchers": preview.get("matched_vouchers", []),
        "discrepancy_vouchers": preview.get("discrepancy_vouchers", []),
        "orphan_vouchers": preview.get("orphan_vouchers", []),
        "out_of_period_vouchers": preview.get("out_of_period_vouchers", []),
        "cashiers": all_cashiers,
        "counts": preview.get("counts", {}),
        "suggestion_counts": preview.get("suggestion_counts", {}),
        "all_banks_matched": all(b.get("is_matched") for b in preview.get("bank_validations", [])),
    }
    return render(request, "clp_preview.html", context)


@login_required(login_url="login")
def clp_discard_view(request):
    """Step 3A: Discard the CLP preview without touching the database."""
    request.session.pop(CLP_SESSION_KEY, None)
    messages.info(request, "La previsualización fue descartada. La base de datos no fue modificada.")
    return redirect("clp_upload")


@login_required(login_url="login")
def clp_commit_view(request):
    """
    Step 3B: Confirm and persist the previewed CLP reconciliation into the database.
    Returns JSON (process ID and Excel download URL) when called via AJAX from the commit modal.
    """
    if request.method != "POST":
        return redirect("clp_preview")

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    preview = request.session.get(CLP_SESSION_KEY)
    if not preview:
        if is_ajax:
            return JsonResponse({"error": "No hay datos en memoria para guardar."}, status=400)
        messages.error(request, "No hay datos en memoria para guardar.")
        return redirect("clp_upload")

    try:
        process = commit_clp_preview_to_database(request.user, preview)
    except Exception as error:
        if is_ajax:
            return JsonResponse({"error": f"Error al guardar en base de datos: {error}"}, status=500)
        messages.error(request, f"Error al guardar el proceso en base de datos: {error}")
        return redirect("clp_preview")

    request.session.pop(CLP_SESSION_KEY, None)

    if is_ajax:
        return JsonResponse({
            "success": True,
            "process_id": process.id,
            "accounting_period": process.accounting_period or "Cierre Pesos (CLP)",
            "execution_date": process.execution_date.strftime("%d/%m/%Y %H:%M:%S") if process.execution_date else "",
            "auditor": process.user.username,
            "total_vouchers": process.cross_match_results.count(),
            "matched_count": process.cross_match_results.filter(system_result="MATCHED").count(),
            "export_url": reverse("clp_export_committed", args=[process.id]),
            "upload_url": reverse("clp_upload"),
        })

    messages.success(request, f"¡Cierre en Pesos #{process.id} guardado formalmente con éxito en la base de datos!")
    return redirect("clp_upload")


@login_required(login_url="login")
def clp_export_committed_view(request, process_id: int):
    """Step 4: Download the official CLP Excel report (with the exception report) from the database."""
    try:
        excel_stream = generate_committed_clp_excel_bytes(process_id)
    except ReconciliationProcess.DoesNotExist:
        messages.error(request, f"El proceso en pesos #{process_id} no existe en la base de datos.")
        return redirect("clp_upload")

    response = HttpResponse(
        excel_stream.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="reporte_oficial_pesos_proceso_{process_id}.xlsx"'
    return response
