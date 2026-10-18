"""
Views for the US Dollars (USD) and Chilean Pesos (CLP) Financial Reconciliation Workflows.
Both implement the same lifecycle (USD shown):
  1. File Upload (usd_upload_view)
  2. In-Memory Preview & Audit (usd_preview_view)
  3. Decision: Commit to DB (usd_commit_view) or Discard (usd_discard_view)
  4. In-Memory Excel Draft Export (usd_export_draft_view)
"""

from django.shortcuts import render, redirect
from django.urls import reverse
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse

from .models import ReconciliationProcess
from .services import (
    calculate_usd_preview,
    commit_usd_preview_to_database,
    generate_committed_usd_excel_bytes,
    detect_and_align_usd_files,
    enrich_preview_with_suggestions,
    calculate_clp_preview,
    enrich_clp_preview_with_suggestions,
    commit_clp_preview_to_database,
    generate_committed_clp_excel_bytes,
    detect_and_align_clp_files,
)


USD_SESSION_KEY = "usd_reconciliation_preview"
CLP_SESSION_KEY = "clp_reconciliation_preview"

USD_FILE_INPUTS = {
    "erp_file": "usd_erp_file",
    "transbank_file": "usd_transbank_file",
    "bank_statement_file": "usd_bank_file",
    "amex_file": "usd_amex_file",
    "diners_file": "usd_diners_file",
    "visa_file": "usd_visa_file",
    "mastercard_file": "usd_mastercard_file",
}


def login_view(request):
    if request.user.is_authenticated:
        return redirect("usd_upload")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("usd_upload")
        messages.error(request, "Credenciales incorrectas. Verifique su usuario y contraseña.")

    return render(request, "login.html")


def logout_view(request):
    logout(request)
    return redirect("login")


@login_required(login_url="login")
def usd_upload_view(request):
    """Step 1: Upload the 7 USD Excel files and process them purely in memory."""
    if request.method == "POST":
        files = {arg: request.FILES.get(field) for arg, field in USD_FILE_INPUTS.items()}
        if not all(files.values()):
            messages.error(request, "Por favor seleccione los 7 archivos requeridos para la conciliación en Dólares.")
            return redirect("usd_upload")

        try:
            request.session[USD_SESSION_KEY] = calculate_usd_preview(**files)
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

    if "suggestion_counts" not in preview:
        enrich_preview_with_suggestions(preview)
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

    context = {
        "preview": preview,
        "bank": preview.get("bank_validation", {}),
        "usd_table": preview.get("usd_table", []),
        "clp_table": preview.get("clp_table", []),
        "usd_totals": preview.get("usd_totals", {}),
        "clp_totals": preview.get("clp_totals", {}),
        "matched_vouchers": preview.get("matched_vouchers", []),
        "discrepancy_vouchers": preview.get("discrepancy_vouchers", []),
        "orphan_vouchers": preview.get("orphan_vouchers", []),
        "cashiers": all_cashiers,
        "counts": preview.get("counts", {}),
        "suggestion_counts": preview.get("suggestion_counts", {}),
    }
    return render(request, "usd_preview.html", context)


@login_required(login_url="login")
def usd_discard_view(request):
    """Step 3A: Discard preview without touching the database."""
    request.session.pop(USD_SESSION_KEY, None)
    messages.info(request, "La previsualización fue descartada. La base de datos no fue modificada.")
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
def clear_test_database_view(request):
    """Deletes all ReconciliationProcess records while keeping catalog tables intact."""
    if request.method == "POST":
        process_count = ReconciliationProcess.objects.count()
        ReconciliationProcess.objects.all().delete()
        for session_key in (USD_SESSION_KEY, CLP_SESSION_KEY):
            request.session.pop(session_key, None)
        messages.warning(
            request,
            f"Base de datos de pruebas limpiada: se eliminaron {process_count} proceso(s) y todas sus transacciones asociadas."
        )
    return redirect("clp_upload" if request.POST.get("next") == "clp_upload" else "usd_upload")


@login_required(login_url="login")
def usd_detect_files_view(request):
    """Inspects the internal cell structure of uploaded Excel files and aligns them to the 7 USD roles."""
    if request.method != "POST":
        return JsonResponse({"error": "Método no permitido."}, status=405)

    uploaded_files = request.FILES.getlist("bulk_files")
    if not uploaded_files:
        return JsonResponse({"error": "No se recibieron archivos para analizar."}, status=400)

    return JsonResponse(detect_and_align_usd_files(uploaded_files))



# ==============================================================================
# CHILEAN PESOS (CLP) RECONCILIATION — Phase 2
# ==============================================================================

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

    if "suggestion_counts" not in preview:
        enrich_clp_preview_with_suggestions(preview)
        request.session[CLP_SESSION_KEY] = preview

    # Same order as the official Excel report: most actionable exceptions first.
    exception_vouchers = (
        preview.get("orphan_vouchers", [])
        + preview.get("discrepancy_vouchers", [])
        + preview.get("out_of_period_vouchers", [])
    )
    all_cashiers = sorted({
        r.get("cashier_username", "").strip()
        for r in (
            preview.get("matched_vouchers", [])
            + preview.get("discrepancy_vouchers", [])
            + preview.get("orphan_vouchers", [])
            + preview.get("out_of_period_vouchers", [])
            + preview.get("unmatched_erp_rows", [])
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
        "unmatched_erp_rows": preview.get("unmatched_erp_rows", []),
        "exception_vouchers": exception_vouchers,
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
