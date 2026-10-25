"""
US Dollars (USD) reconciliation workflow:
  1. File Upload (usd_upload_view)
  2. In-Memory Preview & Audit (usd_preview_view)
  3. Decision: Commit to DB (usd_commit_view) or Discard (usd_discard_view)
  4. Official Excel export from the DB (usd_export_committed_view)
"""

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages

from ..models import ReconciliationProcess, SystemConfiguration
from ..services import (
    calculate_usd_preview,
    commit_usd_preview_to_database,
    generate_committed_usd_excel_bytes,
    detect_and_align_usd_files,
    enrich_preview_with_suggestions,
)
from .common import (
    USD_SESSION_KEY,
    extract_unique_cashiers,
    handle_commit_preview,
    handle_detect_files_api,
    handle_discard_preview,
    handle_export_committed,
)


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

    all_cashiers = extract_unique_cashiers(
        preview.get("matched_vouchers", []),
        preview.get("discrepancy_vouchers", []),
        preview.get("orphan_vouchers", []),
    )

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
        "cashiers": all_cashiers,
        "all_cashiers": all_cashiers,
        "tolerance": preview.get("tolerance", 0.0),
        "suggestion_counts": preview.get("suggestion_counts", {}),
    }
    return render(request, "usd_preview.html", context)


@login_required(login_url="login")
def usd_discard_view(request):
    """Step 3A: Discard the in-memory reconciliation and return to upload."""
    return handle_discard_preview(
        request,
        session_key=USD_SESSION_KEY,
        upload_url_name="usd_upload",
        info_message="Proceso descartado de la memoria. Puede cargar un nuevo lote de archivos.",
        require_post=True,
    )


@login_required(login_url="login")
def usd_commit_view(request):
    """
    Step 3B: Confirm and persist the previewed reconciliation into the database.
    Returns JSON confirmation (process ID and Excel download URL) when called via AJAX.
    """
    return handle_commit_preview(
        request,
        session_key=USD_SESSION_KEY,
        commit_fn=commit_usd_preview_to_database,
        upload_url_name="usd_upload",
        preview_url_name="usd_preview",
        export_url_name="usd_export_committed",
        default_period_label="Cierre Dólares (USD)",
        currency_label="Dólares",
    )


@login_required(login_url="login")
def usd_export_committed_view(request, process_id: int):
    """Step 4: Download the official styled Excel report generated from persisted DB records."""
    return handle_export_committed(
        request,
        process_id=process_id,
        export_fn=generate_committed_usd_excel_bytes,
        upload_url_name="usd_upload",
        filename_prefix="reporte_oficial_dolares_proceso",
        not_found_label="proceso",
    )


@login_required(login_url="login")
def usd_detect_files_view(request):
    """Inspects the internal cell structure of uploaded Excel files and aligns them to the 7 USD roles."""
    return handle_detect_files_api(request, detect_and_align_usd_files)
