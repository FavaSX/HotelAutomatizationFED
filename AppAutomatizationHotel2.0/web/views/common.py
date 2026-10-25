"""
Shared Session Keys and HTTP / Session Controller Helpers for Reconciliation Views (USD & CLP).
Eliminates duplicated boilerplate across `views/usd.py` and `views/clp.py` for:
  - Extracting unique cashiers from voucher buckets
  - Inspecting and aligning uploaded files via AJAX
  - Discarding an in-memory preview
  - Committing an in-memory preview to the database (with AJAX JSON response)
  - Streaming the official Excel report from a committed ReconciliationProcess
"""

from typing import Any, Callable, Dict, Iterable, List
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from ..models import ReconciliationProcess

USD_SESSION_KEY = "usd_reconciliation_preview"
CLP_SESSION_KEY = "clp_reconciliation_preview"


def extract_unique_cashiers(*voucher_lists: Iterable[Dict[str, Any]]) -> List[str]:
    """Returns a sorted list of distinct non-empty cashier usernames across voucher buckets."""
    return sorted({
        v.get("cashier_username", "").strip()
        for bucket in voucher_lists
        for v in (bucket or [])
        if v.get("cashier_username") and v.get("cashier_username") != "—"
    })


def handle_detect_files_api(request, detect_fn: Callable[[List[Any]], Dict[str, Any]]) -> JsonResponse:
    """Shared controller for `usd_detect_files_view` and `clp_detect_files_view`."""
    if request.method != "POST":
        return JsonResponse({"error": "Método no permitido."}, status=405)

    uploaded_files = request.FILES.getlist("bulk_files")
    if not uploaded_files:
        return JsonResponse({"error": "No se recibieron archivos para analizar."}, status=400)

    return JsonResponse(detect_fn(uploaded_files))


def handle_discard_preview(
    request,
    session_key: str,
    upload_url_name: str,
    info_message: str,
    require_post: bool = False,
):
    """Shared controller for discarding an in-memory reconciliation preview."""
    if not require_post or request.method == "POST":
        request.session.pop(session_key, None)
        messages.info(request, info_message)
    return redirect(upload_url_name)


def handle_commit_preview(
    request,
    session_key: str,
    commit_fn: Callable[[Any, Dict[str, Any]], ReconciliationProcess],
    upload_url_name: str,
    preview_url_name: str,
    export_url_name: str,
    default_period_label: str,
    currency_label: str,
):
    """Shared controller for persisting an in-memory preview and returning JSON or redirect."""
    if request.method != "POST":
        return redirect(preview_url_name)

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    preview = request.session.get(session_key)
    if not preview:
        if is_ajax:
            return JsonResponse({"error": "No hay datos en memoria para guardar."}, status=400)
        messages.error(request, "No hay datos en memoria para guardar.")
        return redirect(upload_url_name)

    try:
        process = commit_fn(request.user, preview)
    except Exception as error:
        if is_ajax:
            return JsonResponse({"error": f"Error al guardar en base de datos: {error}"}, status=500)
        messages.error(request, f"Error al guardar el proceso en base de datos: {error}")
        return redirect(preview_url_name)

    request.session.pop(session_key, None)
    if is_ajax:
        return JsonResponse({
            "success": True,
            "process_id": process.id,
            "accounting_period": process.accounting_period or default_period_label,
            "execution_date": process.execution_date.strftime("%d/%m/%Y %H:%M:%S") if process.execution_date else "",
            "auditor": process.user.username,
            "total_vouchers": process.cross_match_results.count(),
            "matched_count": process.cross_match_results.filter(system_result="MATCHED").count(),
            "export_url": reverse(export_url_name, args=[process.id]),
            "upload_url": reverse(upload_url_name),
        })

    messages.success(request, f"¡Cierre en {currency_label} #{process.id} guardado formalmente con éxito en la base de datos!")
    return redirect(upload_url_name)


def handle_export_committed(
    request,
    process_id: int,
    export_fn: Callable[[int], Any],
    upload_url_name: str,
    filename_prefix: str,
    not_found_label: str,
):
    """Shared controller for generating and streaming the official Excel report."""
    try:
        excel_stream = export_fn(process_id)
    except ReconciliationProcess.DoesNotExist:
        messages.error(request, f"El {not_found_label} #{process_id} no existe en la base de datos.")
        return redirect(upload_url_name)

    response = HttpResponse(
        excel_stream.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename_prefix}_{process_id}.xlsx"'
    return response
