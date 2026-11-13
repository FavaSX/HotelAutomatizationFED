"""
Chilean Pesos (CLP) reconciliation workflow — Phase 2.
Same lifecycle as USD: upload, in-memory preview, commit or discard, official Excel export.
"""

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages

from ..models import ReconciliationProcess
from ..services import (
    calculate_clp_preview,
    enrich_clp_preview_with_suggestions,
    commit_clp_preview_to_database,
    generate_committed_clp_excel_bytes,
    detect_and_align_clp_files,
)
from ..services.erp_coverage import get_erp_coverage_summary
from ..services.tables_viewer import build_preview_source_tables_context
from .common import (
    handle_preview_correction,
    CLP_SESSION_KEY,
    extract_unique_cashiers,
    handle_commit_preview,
    handle_detect_files_api,
    handle_discard_preview,
    handle_export_committed,
)


@login_required(login_url="login")
def clp_upload_view(request):
    """
    Step 1: Upload the CLP Excel files (Bordero, cartola and the Transbank settlement(s))
    and process them purely in memory.
    """
    if request.method == "POST":
        erp_file = request.FILES.get("clp_erp_file")
        bank_file = request.FILES.get("clp_bank_file")
        extra_erp_files = request.FILES.getlist("clp_extra_erp_files")

        # Unified Transbank inputs + optional extra TBK files
        tbk_primary = request.FILES.get("clp_tbk_file")
        extra_tbk_files = request.FILES.getlist("clp_extra_tbk_files")

        # Legacy separate inputs fallback
        legacy_credit = request.FILES.get("clp_tbk_credit_file")
        legacy_debit = request.FILES.get("clp_tbk_debit_file")
        legacy_prepaid = request.FILES.get("clp_tbk_prepaid_file")

        tbk_files = []
        if tbk_primary:
            tbk_files.append(tbk_primary)
        if extra_tbk_files:
            tbk_files.extend(extra_tbk_files)
        for leg in [legacy_credit, legacy_debit, legacy_prepaid]:
            if leg and leg not in tbk_files:
                tbk_files.append(leg)

        if not erp_file or not bank_file or not tbk_files:
            messages.error(
                request,
                "Por favor asegúrese de cargar los 3 componentes requeridos: Borderó ERP, Cartola Bancaria y Abono(s) Transbank en Pesos."
            )
            return redirect("clp_upload")

        try:
            preview = calculate_clp_preview(
                erp_file=erp_file,
                bank_statement_file=bank_file,
                extra_erp_files=extra_erp_files,
                tbk_files=tbk_files,
            )
            request.session[CLP_SESSION_KEY] = preview
            failed_extras = preview.get("failed_extra_erp_files") or []
            if failed_extras:
                messages.warning(
                    request,
                    f"No se pudieron leer algunos archivos ERP adicionales: {', '.join(failed_extras)}.",
                )
            messages.success(request, "Archivos analizados con éxito en memoria. Revise los resultados antes de guardar.")
            return redirect("clp_preview")
        except Exception as error:
            messages.error(request, f"Error al procesar los archivos en memoria: {error}")
            return redirect("clp_upload")

    context = {
        "saved_processes_count": ReconciliationProcess.objects.count(),
        "saved_clp_processes_count": ReconciliationProcess.objects.filter(process_currency="CLP").count(),
        "erp_coverage": get_erp_coverage_summary(),
    }
    return render(request, "clp_upload.html", context)


@login_required(login_url="login")
def clp_detect_files_view(request):
    """Inspects the uploaded Excel files and aligns them against the 3 required CLP roles."""
    return handle_detect_files_api(request, detect_and_align_clp_files)


@login_required(login_url="login")
def clp_preview_view(request):
    """
    Step 2: Bank deposit validations, summary by payment medium, voucher tabs and exception report.
    """
    preview = request.session.get(CLP_SESSION_KEY)
    if not preview:
        messages.warning(request, "No hay ningún proceso preliminar en memoria. Por favor cargue los archivos.")
        return redirect("clp_upload")

    stale_tokens = ("doc #", "hab.", "habitación", "huésped")
    needs_enrichment = (
        "suggestion_counts" not in preview
        or any(
            v.get("suggestion") and (
                "action" not in v.get("suggestion", {})
                or v.get("suggestion", {}).get("headline", "").startswith("Posible registro")
                or any(
                    tok in " ".join(
                        str(v.get("suggestion", {}).get(k, "") or "").lower()
                        for k in ("headline", "action", "candidate_summary", "manual_check_note")
                    )
                    for tok in stale_tokens
                )
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

    all_cashiers = extract_unique_cashiers(
        preview.get("matched_vouchers", []),
        preview.get("discrepancy_vouchers", []),
        preview.get("orphan_vouchers", []),
        preview.get("out_of_period_vouchers", []),
    )

    counts = preview.get("counts", {})
    total = counts.get("total", 0)
    match_rate = round(counts.get("matched", 0) / total * 100, 1) if total else 0.0

    # Summary of the active Transbank deposits for the bank KPI card (same layout as the USD preview)
    bank_validations = preview.get("bank_validations", [])
    active_banks = [
        b for b in bank_validations
        if b.get("calculated_deposit") or b.get("total_sales") or b.get("bank_deposit_amount")
    ] or bank_validations
    bank_summary = {
        "matched": sum(1 for b in active_banks if b.get("is_matched")),
        "total": len(active_banks),
        "calculated": sum(b.get("calculated_deposit") or 0 for b in bank_validations),
        "deposited": sum(b.get("bank_deposit_amount") or 0 for b in bank_validations),
    }

    context = {
        "preview": preview,
        "match_rate": match_rate,
        "bank_summary": bank_summary,
        "bank_validations": bank_validations,
        "modality_table": preview.get("modality_table", []),
        "clp_totals": preview.get("clp_totals", {}),
        "matched_vouchers": preview.get("matched_vouchers", []),
        "discrepancy_vouchers": preview.get("discrepancy_vouchers", []),
        "orphan_vouchers": preview.get("orphan_vouchers", []),
        "out_of_period_vouchers": preview.get("out_of_period_vouchers", []),
        "cashiers": all_cashiers,
        "counts": preview.get("counts", {}),
        "suggestion_counts": preview.get("suggestion_counts", {}),
        "all_banks_matched": all(b.get("is_matched") for b in active_banks),
        "source_tables": build_preview_source_tables_context(preview, "CLP"),
    }
    return render(request, "clp_preview.html", context)


@login_required(login_url="login")
def clp_discard_view(request):
    """Step 3A: Discard the CLP preview without touching the database."""
    return handle_discard_preview(
        request,
        session_key=CLP_SESSION_KEY,
        upload_url_name="clp_upload",
        info_message="La previsualización fue descartada. La base de datos no fue modificada.",
        require_post=True,
    )


@login_required(login_url="login")
def clp_commit_view(request):
    """
    Step 3B: Confirm and persist the previewed CLP reconciliation into the database.
    Returns JSON (process ID and Excel download URL) when called via AJAX from the commit modal.
    """
    return handle_commit_preview(
        request,
        session_key=CLP_SESSION_KEY,
        commit_fn=commit_clp_preview_to_database,
        upload_url_name="clp_upload",
        preview_url_name="clp_preview",
        export_url_name="clp_export_committed",
        default_period_label="Cierre Pesos (CLP)",
        currency_label="Pesos",
    )


@login_required(login_url="login")
def clp_export_committed_view(request, process_id: int):
    """Step 4: Download the official CLP Excel report (with the exception report) from the database."""
    return handle_export_committed(
        request,
        process_id=process_id,
        export_fn=generate_committed_clp_excel_bytes,
        upload_url_name="clp_upload",
        filename_prefix="reporte_oficial_pesos_proceso",
        not_found_label="proceso en pesos",
    )


@login_required(login_url="login")
def clp_preview_correction_view(request):
    """Step 2: apply the suggested ERP correction of one voucher before saving the closing."""
    return handle_preview_correction(request, CLP_SESSION_KEY, "CLP")
