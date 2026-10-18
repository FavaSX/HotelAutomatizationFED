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
from django.db.models import Count, Q, Sum

from .models import CrossMatchResult, ReconciliationProcess, ReconciliationSummary
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
        return redirect("dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("dashboard")
        messages.error(request, "Credenciales incorrectas. Verifique su usuario y contraseña.")

    return render(request, "login.html")


def logout_view(request):
    logout(request)
    return redirect("login")


EXCEPTION_LABELS = {
    "AMOUNT_MISMATCH": "Con diferencia de monto",
    "NOT_FOUND_IN_ERP": "No ubicada en ERP",
}

CLP_EXCEPTION_LABELS = {
    "AMOUNT_MISMATCH": "Con diferencia de monto",
    "CARD_TYPE_MISMATCH": "Medio de pago distinto",
    "NOT_FOUND_IN_ERP": "No está en el Borderó",
    "OUT_OF_PERIOD": "Fuera de período",
}

CLP_MODALITY_LABELS = {"CREDIT": "Crédito", "DEBIT": "Débito", "PREPAID": "Prepago"}


def _match_rate(matched: int, total: int) -> float:
    return round(matched / total * 100, 1) if total else 0.0


def _exception_label(labels, code: str) -> str:
    return labels.get(code, code.replace("_", " ").title())


def _closings(currency: str):
    """Saved closings of one currency with voucher totals, newest first."""
    return (
        ReconciliationProcess.objects.filter(process_currency=currency)
        .select_related("user")
        .annotate(
            total_vouchers=Count("cross_match_results"),
            matched_vouchers=Count("cross_match_results", filter=Q(cross_match_results__system_result="MATCHED")),
        )
        .order_by("-execution_date")
    )


def _recent_closings(processes):
    recent = list(processes[:6])
    for proc in recent:
        proc.match_rate = _match_rate(proc.matched_vouchers, proc.total_vouchers)
        proc.exception_count = proc.total_vouchers - proc.matched_vouchers
        proc.bank_list = list(proc.bank_validations.order_by("payment_modality"))
        proc.banks_matched = sum(1 for b in proc.bank_list if b.is_matched)
    return recent


def _pending_exceptions(currency: str):
    """Exceptions of every closing of the currency still pending accountant review."""
    return CrossMatchResult.objects.filter(
        process__process_currency=currency,
        management_status__code="PENDING",
    ).exclude(system_result="MATCHED")


def _pending_by_type(pending, labels):
    return [
        {
            "label": _exception_label(labels, row["system_result"]),
            "code": row["system_result"],
            "count": row["count"],
            "amount": row["amount"] or 0,
        }
        for row in pending.values("system_result").annotate(count=Count("id"), amount=Sum("transbank_amount")).order_by("-count")
    ]


def _result_counts(process):
    """Vouchers of one closing per state, as shown in the "Conciliación automática" card of both panels."""
    results = {
        row["system_result"]: row["count"]
        for row in process.cross_match_results.values("system_result").annotate(count=Count("id"))
    }
    counts = {
        "matched": results.get("MATCHED", 0),
        "discrepancies": results.get("AMOUNT_MISMATCH", 0) + results.get("CARD_TYPE_MISMATCH", 0),
        "orphans": results.get("NOT_FOUND_IN_ERP", 0),
        "out_of_period": results.get("OUT_OF_PERIOD", 0),
    }
    counts["other"] = process.total_vouchers - sum(counts.values())
    return counts


def _rate_delta(recent):
    return round(recent[0].match_rate - recent[1].match_rate, 1) if len(recent) > 1 else None


def _usd_dashboard():
    """Dollar panel: every figure comes from the saved USD closings."""
    processes = _closings("USD")
    recent = _recent_closings(processes)
    if not recent:
        return {"has_data": False}
    last_process = recent[0]

    pending = _pending_exceptions("USD")
    pending_totals = pending.aggregate(count=Count("id"), amount=Sum("transbank_amount"))

    # Cashiers responsible for the open exceptions (vouchers missing in ERP have no cashier)
    cashier_counts = {}
    for row in pending.values("cashier_username").annotate(count=Count("id")):
        name = (row["cashier_username"] or "").strip()
        key = name if name and name != "—" else None
        cashier_counts[key] = cashier_counts.get(key, 0) + row["count"]
    max_cashier = max(cashier_counts.values(), default=0)
    pending_by_cashier = sorted(
        (
            {
                "name": name or "Sin cajero (no está en ERP)",
                "is_unassigned": name is None,
                "count": count,
                "pct": round(count / max_cashier * 100) if max_cashier else 0,
            }
            for name, count in cashier_counts.items()
        ),
        key=lambda c: (c["is_unassigned"], -c["count"]),
    )[:6]

    largest_exceptions = list(pending.select_related("card_type", "process").order_by("-transbank_amount")[:6])
    for item in largest_exceptions:
        item.type_label = _exception_label(EXCEPTION_LABELS, item.system_result)

    # Per-franchise breakdown of the last closing
    card_rows = {
        row["card_type__code"]: row
        for row in last_process.cross_match_results.values("card_type__code", "card_type__name").annotate(
            total=Count("id"),
            matched=Count("id", filter=Q(system_result="MATCHED")),
            amount=Sum("transbank_amount"),
        )
    }
    usd_summaries = {
        s.card_type.code: s
        for s in ReconciliationSummary.objects.filter(process=last_process, currency_table="USD").select_related("card_type")
    }
    max_card_amount = max((r["amount"] or 0 for r in card_rows.values()), default=0)
    franchises = []
    for code in sorted(set(card_rows) | set(usd_summaries)):
        row = card_rows.get(code, {})
        summary = usd_summaries.get(code)
        amount = row.get("amount") or 0
        total = row.get("total", 0)
        franchises.append({
            "code": code,
            "name": row.get("card_type__name") or (summary.card_type.name if summary else code),
            "vouchers": total,
            "matched": row.get("matched", 0),
            "amount": amount,
            "pct": round(amount / max_card_amount * 100) if max_card_amount else 0,
            "difference": summary.difference if summary else 0,
        })
    franchises.sort(key=lambda f: -f["amount"])

    return {
        "has_data": True,
        "last_process": last_process,
        "last_counts": _result_counts(last_process),
        "rate_delta": _rate_delta(recent),
        "bank": last_process.bank_list[0] if last_process.bank_list else None,
        "pending_count": pending_totals["count"] or 0,
        "pending_amount": pending_totals["amount"] or 0,
        "pending_by_type": _pending_by_type(pending, EXCEPTION_LABELS),
        "pending_by_cashier": pending_by_cashier,
        "largest_exceptions": largest_exceptions,
        "franchises": franchises,
        "recent_processes": recent,
        "process_count": processes.count(),
    }


def _split_status_detail(detail: str):
    """CLP closings store 'Modalidad · Tipo de error · Acción sugerida' in system_status_detail."""
    parts = [p.strip() for p in (detail or "").split(" · ", 2)]
    return parts if len(parts) == 3 else ["", "", ""]


def _clp_dashboard():
    """Peso panel: every figure comes from the saved CLP closings (Credit, Debit and Prepaid settlements)."""
    processes = _closings("CLP")
    recent = _recent_closings(processes)
    if not recent:
        return {"has_data": False}
    last_process = recent[0]

    last_counts = _result_counts(last_process)

    pending = _pending_exceptions("CLP")
    pending_totals = pending.aggregate(count=Count("id"), amount=Sum("transbank_amount"))
    out_of_period_amount = pending.filter(system_result="OUT_OF_PERIOD").aggregate(amount=Sum("transbank_amount"))["amount"] or 0

    # Exception report: error type + suggested action, as classified by the CLP engine
    error_groups = {}
    for item in pending.only("system_result", "system_status_detail", "transbank_amount"):
        _, error_type, action = _split_status_detail(item.system_status_detail)
        error_type = error_type or _exception_label(CLP_EXCEPTION_LABELS, item.system_result)
        group = error_groups.setdefault(error_type, {"label": error_type, "count": 0, "amount": 0, "actions": {}})
        group["count"] += 1
        group["amount"] += item.transbank_amount or 0
        if action:
            group["actions"][action] = group["actions"].get(action, 0) + 1
    max_group = max((g["count"] for g in error_groups.values()), default=0)
    pending_by_error = []
    for group in sorted(error_groups.values(), key=lambda g: -g["count"]):
        actions = sorted(group.pop("actions").items(), key=lambda a: -a[1])
        group["action"] = actions[0][0] if actions else ""
        group["more_actions"] = len(actions) - 1
        group["pct"] = round(group["count"] / max_group * 100) if max_group else 0
        pending_by_error.append(group)

    largest_exceptions = list(pending.select_related("card_type", "process").order_by("-transbank_amount")[:6])
    for item in largest_exceptions:
        modality, error_type, action = _split_status_detail(item.system_status_detail)
        item.type_label = error_type or _exception_label(CLP_EXCEPTION_LABELS, item.system_result)
        item.modality_label = modality
        item.action = action

    # Per-modality breakdown of the last closing (Credit / Debit / Prepaid)
    modality_rows = {
        row["transbank_transaction__payment_modality"]: row
        for row in last_process.cross_match_results.values("transbank_transaction__payment_modality").annotate(
            total=Count("id"),
            matched=Count("id", filter=Q(system_result="MATCHED")),
            amount=Sum("transbank_amount"),
        )
    }
    summary_rows = {
        row["payment_modality"].removeprefix("CLP_"): row
        for row in ReconciliationSummary.objects.filter(process=last_process, currency_table="CLP")
        .values("payment_modality")
        .annotate(commissions=Sum("total_commissions"), net_deposit=Sum("net_deposit_total"))
    }
    banks = {b.payment_modality.removeprefix("CLP_"): b for b in last_process.bank_list}
    max_modality_amount = max((r["amount"] or 0 for r in modality_rows.values()), default=0)
    modalities = []
    for code, label in CLP_MODALITY_LABELS.items():
        row = modality_rows.get(code, {})
        summary = summary_rows.get(code, {})
        amount = row.get("amount") or 0
        modalities.append({
            "code": code,
            "label": label,
            "vouchers": row.get("total", 0),
            "matched": row.get("matched", 0),
            "amount": amount,
            "pct": round(amount / max_modality_amount * 100) if max_modality_amount else 0,
            "commissions": summary.get("commissions") or 0,
            "net_deposit": summary.get("net_deposit") or 0,
            "bank": banks.get(code),
        })

    return {
        "has_data": True,
        "last_process": last_process,
        "last_counts": last_counts,
        "rate_delta": _rate_delta(recent),
        "banks": [m for m in modalities if m["bank"]],
        "pending_count": pending_totals["count"] or 0,
        "pending_amount": pending_totals["amount"] or 0,
        "out_of_period_amount": out_of_period_amount,
        "pending_by_type": _pending_by_type(pending, CLP_EXCEPTION_LABELS),
        "pending_by_error": pending_by_error,
        "largest_exceptions": largest_exceptions,
        "modalities": modalities,
        "commissions_total": sum(m["commissions"] for m in modalities),
        "recent_processes": recent,
        "process_count": processes.count(),
    }


@login_required(login_url="login")
def dashboard_view(request):
    """
    Home with two panels (Dólares / Pesos) computed from the saved reconciliation closings.
    Every figure comes from the database; nothing is hardcoded.
    """
    usd = _usd_dashboard()
    clp = _clp_dashboard()
    requested = request.GET.get("moneda", "").lower()
    if requested in ("usd", "clp"):
        active_currency = requested
    else:
        active_currency = "clp" if clp["has_data"] and not usd["has_data"] else "usd"
    return render(request, "dashboard.html", {
        "usd": usd,
        "clp": clp,
        "active_currency": active_currency,
        "currency_from_url": requested in ("usd", "clp"),
    })


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

    needs_enrichment = (
        "suggestion_counts" not in preview
        or any(
            v.get("suggestion") and "action" not in v.get("suggestion", {})
            for v in preview.get("discrepancy_vouchers", []) + preview.get("orphan_vouchers", [])
        )
    )
    if needs_enrichment:
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

    needs_enrichment = (
        "suggestion_counts" not in preview
        or any(
            v.get("suggestion") and (
                "action" not in v.get("suggestion", {})
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
