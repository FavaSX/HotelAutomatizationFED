"""
Views for US Dollars (USD) Financial Reconciliation Workflow.
Implements the 3-step lifecycle:
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
    generate_committed_clp_excel_bytes,
    detect_and_align_usd_files,
    execute_clp_reconciliation,
)


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
    """
    Step 1: Upload the 7 USD Excel files and process them purely in memory.
    """
    if request.method == "POST":
        erp_file = request.FILES.get("usd_erp_file")
        transbank_file = request.FILES.get("usd_transbank_file")
        bank_file = request.FILES.get("usd_bank_file")
        amex_file = request.FILES.get("usd_amex_file")
        diners_file = request.FILES.get("usd_diners_file")
        visa_file = request.FILES.get("usd_visa_file")
        mastercard_file = request.FILES.get("usd_mastercard_file")

        required_files = [erp_file, transbank_file, bank_file, amex_file, diners_file, visa_file, mastercard_file]
        if not all(required_files):
            messages.error(request, "Por favor seleccione los 7 archivos requeridos para la conciliación en Dólares.")
            return redirect("usd_upload")

        try:
            preview_data = calculate_usd_preview(
                erp_file=erp_file,
                transbank_file=transbank_file,
                bank_statement_file=bank_file,
                amex_file=amex_file,
                diners_file=diners_file,
                visa_file=visa_file,
                mastercard_file=mastercard_file,
            )
            # Store in session for review
            request.session["usd_reconciliation_preview"] = preview_data
            messages.success(request, "Archivos analizados con éxito en memoria. Revise los resultados antes de guardar.")
            return redirect("usd_preview")

        except Exception as error:
            messages.error(request, f"Error al procesar los archivos en memoria: {error}")
            return redirect("usd_upload")

    context = {
        "saved_processes_count": ReconciliationProcess.objects.count(),
    }
    return render(request, "usd_upload.html", context)


@login_required(login_url="login")
def usd_preview_view(request):
    """
    Step 2: Display Excel-like financial summary tables, bank validation, and voucher tabs.
    """
    preview = request.session.get("usd_reconciliation_preview")
    if not preview:
        messages.warning(request, "No hay ningún proceso preliminar en memoria. Por favor cargue los archivos.")
        return redirect("usd_upload")

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
        "counts": preview.get("counts", {}),
    }
    return render(request, "usd_preview.html", context)


@login_required(login_url="login")
def usd_discard_view(request):
    """
    Step 3A: Discard preview without touching the database.
    """
    if "usd_reconciliation_preview" in request.session:
        del request.session["usd_reconciliation_preview"]
    messages.info(request, "La previsualización fue descartada. La base de datos no fue modificada.")
    return redirect("usd_upload")


@login_required(login_url="login")
def usd_commit_view(request):
    """
    Step 3B: Confirm and persist the previewed reconciliation into the database.
    Returns real backend JSON confirmation (including DB process ID and Excel download URL)
    when called via AJAX from the commit modal.
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if request.method == "POST":
        preview = request.session.get("usd_reconciliation_preview")
        if not preview:
            if is_ajax:
                return JsonResponse({"error": "No hay datos en memoria para guardar."}, status=400)
            messages.error(request, "No hay datos en memoria para guardar.")
            return redirect("usd_upload")

        try:
            process = commit_usd_preview_to_database(request.user, preview)
            del request.session["usd_reconciliation_preview"]

            if is_ajax:
                total_vouchers = process.cross_match_results.count()
                matched_count = process.cross_match_results.filter(system_result="MATCHED").count()
                return JsonResponse({
                    "success": True,
                    "process_id": process.id,
                    "accounting_period": process.accounting_period or "Cierre Dólares (USD)",
                    "execution_date": process.execution_date.strftime("%d/%m/%Y %H:%M:%S") if process.execution_date else "",
                    "auditor": process.user.username,
                    "total_vouchers": total_vouchers,
                    "matched_count": matched_count,
                    "export_url": reverse("usd_export_committed", args=[process.id]),
                    "upload_url": reverse("usd_upload"),
                })

            messages.success(request, f"¡Cierre en Dólares #{process.id} guardado formalmente con éxito en la base de datos!")
            return redirect("usd_upload")
        except Exception as error:
            if is_ajax:
                return JsonResponse({"error": f"Error al guardar en base de datos: {error}"}, status=500)
            messages.error(request, f"Error al guardar el proceso en base de datos: {error}")
            return redirect("usd_preview")

    return redirect("usd_preview")


@login_required(login_url="login")
def usd_export_committed_view(request, process_id: int):
    """
    Step 4: Download the official styled Excel report generated directly from the
    persisted database records of a committed ReconciliationProcess.
    """
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
    """
    Temporary testing utility: deletes all ReconciliationProcess records (and cascades
    to all 7 child transaction/summary/validation tables) while keeping catalog tables intact.
    """
    if request.method == "POST":
        process_count = ReconciliationProcess.objects.count()
        ReconciliationProcess.objects.all().delete()
        if "usd_reconciliation_preview" in request.session:
            del request.session["usd_reconciliation_preview"]
        messages.warning(
            request,
            f"Base de datos de pruebas limpiada: se eliminaron {process_count} proceso(s) y todas sus transacciones asociadas."
        )
    return redirect("usd_upload")


@login_required(login_url="login")
def usd_detect_files_view(request):
    """
    Experimental Auto-Detection Endpoint:
    Inspects the internal cell structure of uploaded Excel files and returns
    the alignment table mapping each file to one of the 7 required USD slots.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Método no permitido."}, status=405)

    uploaded_files = request.FILES.getlist("bulk_files")
    if not uploaded_files:
        return JsonResponse({"error": "No se recibieron archivos para analizar."}, status=400)

    alignment_report = detect_and_align_usd_files(uploaded_files)
    return JsonResponse(alignment_report)


@login_required(login_url="login")
def clp_upload_view(request):
    """
    Phase 2: Upload the 5 CLP files (Transbank Crédito/Débito/Prepago, Bordero ERP,
    Cartola Bancaria) and run the cross-match directly against the database.
    """
    if request.method == "POST":
        credit_file = request.FILES.get("clp_credito")
        debit_file = request.FILES.get("clp_debito")
        prepaid_file = request.FILES.get("clp_prepago")
        bordero_file = request.FILES.get("clp_bordero")
        bank_file = request.FILES.get("clp_cartola")

        required_files = [credit_file, debit_file, prepaid_file, bordero_file, bank_file]
        if not all(required_files):
            messages.error(request, "Por favor seleccione los 5 archivos requeridos para la conciliación en Pesos (CLP).")
            return redirect("clp_upload")

        try:
            process, summary = execute_clp_reconciliation(
                user=request.user,
                erp_bordero_file=bordero_file,
                transbank_credit_file=credit_file,
                transbank_debit_file=debit_file,
                transbank_prepaid_file=prepaid_file,
                bank_statement_file=bank_file,
            )
            messages.success(request, f"¡Cierre en Pesos #{process.id} procesado y guardado con éxito en la base de datos!")
            return redirect("clp_result", process_id=process.id)
        except Exception as error:
            messages.error(request, f"Error al procesar los archivos: {error}")
            return redirect("clp_upload")

    context = {
        "recent_clp_processes": ReconciliationProcess.objects.filter(process_currency="CLP")[:5],
    }
    return render(request, "clp_upload.html", context)


@login_required(login_url="login")
def clp_result_view(request, process_id: int):
    """
    Phase 2: Displays the persisted results of a committed CLP reconciliation process.
    """
    try:
        process = ReconciliationProcess.objects.get(id=process_id, process_currency="CLP")
    except ReconciliationProcess.DoesNotExist:
        messages.error(request, f"El proceso CLP #{process_id} no existe en la base de datos.")
        return redirect("clp_upload")

    cross_matches = process.cross_match_results.select_related("card_type", "management_status")
    counts = {
        "matched": cross_matches.filter(system_result="MATCHED").count(),
        "amount_mismatch": cross_matches.filter(system_result="AMOUNT_MISMATCH").count(),
        "card_mismatch": cross_matches.filter(system_result="CARD_TYPE_MISMATCH").count(),
        "typo_suspected": cross_matches.filter(system_result="TYPO_SUSPECTED").count(),
        "not_found": cross_matches.filter(system_result="NOT_FOUND_IN_ERP").count(),
        "ghost": cross_matches.filter(system_result="GHOST_IN_ERP").count(),
        "void": cross_matches.filter(system_result="VOID_OR_CREDIT_NOTE").count(),
    }

    result_labels = {
        "AMOUNT_MISMATCH": "Diferencia de Monto",
        "CARD_TYPE_MISMATCH": "Tipo de Tarjeta Incorrecto",
        "NOT_FOUND_IN_ERP": "No Registrada en Bordero",
        "GHOST_IN_ERP": "Cobro en Bordero sin Voucher",
        "TYPO_SUSPECTED": "Posible Error de Tipeo",
        "VOID_OR_CREDIT_NOTE": "Anulación / Nota de Crédito",
    }
    discrepancies = [
        {
            "label": result_labels.get(d.system_result, d.system_result),
            "authorization_code": d.authorization_code,
            "document_number": d.document_number,
            "guest_name": d.guest_name,
            "card_type": d.card_type,
            "transbank_amount": d.transbank_amount,
            "erp_amount_clp": d.erp_amount_clp,
            "system_status_detail": d.system_status_detail,
        }
        for d in cross_matches.exclude(system_result="MATCHED").order_by("-id")[:300]
    ]

    context = {
        "process": process,
        "bank_validations": process.bank_validations.all(),
        "summaries": process.summaries.filter(currency_table="CLP").select_related("card_type"),
        "counts": counts,
        "discrepancies": discrepancies,
    }
    return render(request, "clp_result.html", context)


@login_required(login_url="login")
def clp_export_view(request, process_id: int):
    """
    Phase 2: Download the official styled Excel report generated from the
    persisted database records of a committed CLP ReconciliationProcess.
    """
    try:
        excel_stream = generate_committed_clp_excel_bytes(process_id)
    except ReconciliationProcess.DoesNotExist:
        messages.error(request, f"El proceso CLP #{process_id} no existe en la base de datos.")
        return redirect("clp_upload")

    response = HttpResponse(
        excel_stream.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="reporte_oficial_pesos_proceso_{process_id}.xlsx"'
    return response

