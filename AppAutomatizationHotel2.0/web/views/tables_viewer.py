"""
Read-only (GET-only) views for visualizing all reconciliation tables and audit trails:
  1. Consolidated global views across ALL saved reconciliations (/tablas/erp/, /tablas/transbank/, etc.)
  2. Saved closings directory (`closings_list_view` -> /tablas/)
  3. Saved closing multi-tab detail (`closing_detail_view` -> /tablas/cierre/<id>/):
     - Resumen y Cuadratura (`resumen`)
     - Resultados del Cruce (`cruce` - CrossMatchResult)
     - Borderó ERP (`erp` - ErpTransaction, including rows absent from Transbank)
     - Cartola Bancaria (`cartola` - BankStatementMovement & BankDepositValidation)
     - Posición por Clientes (`operadoras` - CardOperatorTransaction for USD)
     - Transbank Detallado (`transbank` - TransbankTransaction)
  4. Audit Trail (`audit_trail_view` -> /auditoria/):
     - Correcciones ERP (`ErpTransactionChange`)
     - Resoluciones de Excepciones (`AuditResolutionLog`)
"""

from decimal import Decimal
from typing import Any, Callable, Dict, Iterable
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from ..models import (
    AuditResolutionLog,
    CrossMatchResult,
    ErpTransactionChange,
    ReconciliationProcess,
    ReconciliationSummary,
)
from ..services.erp_editing import OPEN_STATUS_CODES
from ..services.tables_viewer import (
    MODALITY_LABELS,
    _paginate,
    build_bank_view_data,
    build_cruce_view_data,
    build_erp_view_data,
    build_operator_view_data,
    build_transbank_view_data,
    format_erp_change_row,
    get_consultas_nav_counts,
    parse_iso_date,
    process_bank_movements_queryset,
    process_erp_queryset,
    process_operator_queryset,
    process_transbank_queryset,
    resolve_per_page,
)


PAGE_SIZE_CLOSINGS = 25
PAGE_SIZE_ROWS = 25


def _clean_query_string(request, exclude: Iterable[str] = ("page",)) -> str:
    params = request.GET.copy()
    for key in exclude:
        params.pop(key, None)
    return params.urlencode()


def _render_consolidated(
    request,
    *,
    tab_key: str,
    title: str,
    subtitle: str,
    badge_label: str,
    badge_icon: str,
    reset_url_name: str,
    builder: Callable[[Any], Dict[str, Any]],
):
    """Shared renderer for all consolidated global table views (strictly GET-only)."""
    data = builder(request)
    context = {
        "consolidated": True,
        "tab_key": tab_key,
        "active_nav": tab_key,
        "page_title": title,
        "page_subtitle": subtitle,
        "badge_label": badge_label,
        "badge_icon": badge_icon,
        "reset_url_name": reset_url_name,
        "nav_counts": get_consultas_nav_counts(),
        "query_string": _clean_query_string(request),
        **data,
    }
    return render(request, "tables/consolidated.html", context)


@login_required(login_url="login")
@require_GET
def consolidated_erp_view(request):
    """Global consolidated view of all active Borderó ERP rows across reconciliations."""
    return _render_consolidated(
        request,
        tab_key="erp",
        title="Borderó ERP Consolidado",
        subtitle="Todos los movimientos cargados en el Borderó ERP a lo largo de las conciliaciones guardadas, incluyendo registros sin voucher Transbank.",
        badge_label="Borderó ERP",
        badge_icon="bi-building",
        reset_url_name="consolidated_erp",
        builder=lambda req: build_erp_view_data(req, process=None, per_page=PAGE_SIZE_ROWS),
    )


@login_required(login_url="login")
@require_GET
def consolidated_transbank_view(request):
    """Global consolidated view of all Transbank transactions across reconciliations."""
    return _render_consolidated(
        request,
        tab_key="transbank",
        title="Transbank Detallado Consolidado",
        subtitle="Historial unificado de ventas, comisiones, IVA y abonos netos de Transbank en USD y CLP.",
        badge_label="Transbank Detallado",
        badge_icon="bi-receipt",
        reset_url_name="consolidated_transbank",
        builder=lambda req: build_transbank_view_data(req, process=None, per_page=PAGE_SIZE_ROWS),
    )


@login_required(login_url="login")
@require_GET
def consolidated_bank_view(request):
    """Global consolidated view of all Bank Statement movements across reconciliations."""
    return _render_consolidated(
        request,
        tab_key="cartola",
        title="Cartola Bancaria Consolidada",
        subtitle="Movimientos bancarios históricos cargados en USD y CLP junto con su clasificación de abono Transbank.",
        badge_label="Cartola Bancaria",
        badge_icon="bi-bank",
        reset_url_name="consolidated_bank",
        builder=lambda req: build_bank_view_data(req, process=None, per_page=PAGE_SIZE_ROWS),
    )


@login_required(login_url="login")
@require_GET
def consolidated_operator_view(request):
    """Global consolidated view of all USD Card Operator (Posición por Clientes) rows."""
    return _render_consolidated(
        request,
        tab_key="operadoras",
        title="Posición por Clientes Consolidada (USD)",
        subtitle="Registros históricos de American Express, Diners Club, Visa y MasterCard en dólares.",
        badge_label="Posición por Clientes",
        badge_icon="bi-credit-card",
        reset_url_name="consolidated_operator",
        builder=lambda req: build_operator_view_data(req, process=None, per_page=PAGE_SIZE_ROWS),
    )


@login_required(login_url="login")
@require_GET
def consolidated_matches_view(request):
    """Global consolidated view of all CrossMatchResult records across reconciliations."""
    return _render_consolidated(
        request,
        tab_key="cruces",
        title="Resultados del Cruce Consolidados",
        subtitle="Detalle unificado de conciliaciones, diferencias y excepciones por voucher.",
        badge_label="Resultados del Cruce",
        badge_icon="bi-arrow-left-right",
        reset_url_name="consolidated_matches",
        builder=lambda req: build_cruce_view_data(req, process=None, per_page=PAGE_SIZE_ROWS),
    )


@login_required(login_url="login")
@require_GET
def closings_list_view(request):
    """Directory of all saved USD & CLP closings."""
    moneda = (request.GET.get("moneda") or "ALL").upper()
    if moneda not in ("ALL", "USD", "CLP"):
        moneda = "ALL"
    q = (request.GET.get("q") or "").strip()
    date_from = parse_iso_date(request.GET.get("desde"))
    date_to = parse_iso_date(request.GET.get("hasta"))

    processes = (
        ReconciliationProcess.objects.select_related("user")
        .prefetch_related("bank_validations")
        .annotate(
            total_vouchers=Count("cross_match_results"),
            matched_vouchers=Count(
                "cross_match_results",
                filter=Q(cross_match_results__system_result="MATCHED"),
            ),
        )
        .order_by("-execution_date", "-id")
    )

    if moneda in ("USD", "CLP"):
        processes = processes.filter(process_currency=moneda)
    if date_from:
        processes = processes.filter(execution_date__date__gte=date_from)
    if date_to:
        processes = processes.filter(execution_date__date__lte=date_to)
    if q:
        q_filter = (
            Q(accounting_period__icontains=q)
            | Q(user__username__icontains=q)
            | Q(erp_file_name__icontains=q)
            | Q(bank_statement_file_name__icontains=q)
        )
        if q.isdigit():
            q_filter |= Q(id=int(q))
        processes = processes.filter(q_filter)

    closings_per_page = resolve_per_page(request, default=PAGE_SIZE_CLOSINGS)
    page = _paginate(
        processes,
        request.GET.get("page"),
        per_page=closings_per_page,
        base_default=PAGE_SIZE_CLOSINGS,
    )
    page_ids = [p.id for p in page.object_list]

    pending_by_proc = {
        row["process_id"]: row["c"]
        for row in CrossMatchResult.objects.filter(
            process_id__in=page_ids,
            management_status__code__in=OPEN_STATUS_CODES,
        )
        .exclude(system_result="MATCHED")
        .values("process_id")
        .annotate(c=Count("id"))
    } if page_ids else {}

    for proc in page.object_list:
        total = proc.total_vouchers or 0
        matched = proc.matched_vouchers or 0
        proc.match_rate = round(matched / total * 100, 1) if total else 0.0
        proc.pending_count = pending_by_proc.get(proc.id, 0)
        bank_list = list(proc.bank_validations.all())
        proc.bank_total = len(bank_list)
        proc.bank_matched = sum(1 for b in bank_list if b.is_matched)
        proc.uploaded_files = [
            name
            for name in (
                proc.erp_file_name,
                proc.bank_statement_file_name,
                proc.transbank_general_file_name,
                proc.transbank_credit_file_name,
                proc.transbank_debit_file_name,
                proc.transbank_prepaid_file_name,
            )
            if name
        ]

    all_totals = ReconciliationProcess.objects.aggregate(
        total=Count("id"),
        usd=Count("id", filter=Q(process_currency="USD")),
        clp=Count("id", filter=Q(process_currency="CLP")),
    )
    nav_counts = get_consultas_nav_counts()

    context = {
        "active_nav": "cierres",
        "nav_counts": nav_counts,
        "page": page,
        "processes": page.object_list,
        "query_string": _clean_query_string(request),
        "filters": {
            "moneda": moneda,
            "q": q,
            "desde": request.GET.get("desde", ""),
            "hasta": request.GET.get("hasta", ""),
        },
        "kpis": {
            "total": all_totals["total"] or 0,
            "usd": all_totals["usd"] or 0,
            "clp": all_totals["clp"] or 0,
            "vouchers": nav_counts["cruces"],
        },
    }
    return render(request, "tables/closings_list.html", context)


def _closing_resumen_context(process: ReconciliationProcess) -> Dict[str, Any]:
    validations = list(process.bank_validations.select_related("bank_movement").order_by("payment_modality"))
    for v in validations:
        v.modality_label = MODALITY_LABELS.get(v.payment_modality, v.payment_modality)
        v.difference = (v.bank_statement_deposit or Decimal("0")) - (v.transbank_calculated_deposit or Decimal("0"))

    summaries_qs = ReconciliationSummary.objects.filter(process=process).select_related("card_type")
    if process.process_currency == "USD":
        usd_rows = list(summaries_qs.filter(currency_table="USD").order_by("card_type__code"))
        clp_by_code = {s.card_type.code: s for s in summaries_qs.filter(currency_table="CLP")}
        summary_rows = [
            {"card_name": r.card_type.name, "usd": r, "clp": clp_by_code.get(r.card_type.code)}
            for r in usd_rows
        ]
    else:
        summary_rows = [
            {
                "modality_label": MODALITY_LABELS.get(r.payment_modality or "", r.payment_modality or "—"),
                "card_name": r.card_type.name,
                "row": r,
            }
            for r in summaries_qs.order_by("payment_modality", "card_type__code")
        ]

    source_files = [
        ("Borderó ERP", process.erp_file_name),
        ("Cartola Bancaria", process.bank_statement_file_name),
        ("Transbank General (USD)", process.transbank_general_file_name),
        ("Transbank Crédito", process.transbank_credit_file_name),
        ("Transbank Débito", process.transbank_debit_file_name),
        ("Transbank Prepago", process.transbank_prepaid_file_name),
        ("Posición por Clientes AX", process.american_express_file_name),
        ("Posición por Clientes DI", process.diners_file_name),
        ("Posición por Clientes VI", process.visa_file_name),
        ("Posición por Clientes MC", process.mastercard_file_name),
    ]
    return {
        "validations": validations,
        "summary_rows": summary_rows,
        "source_files": [(lbl, fname) for lbl, fname in source_files if fname],
    }


@login_required(login_url="login")
@require_GET
def closing_detail_view(request, process_id: int):
    """Multi-tab viewer for a saved ReconciliationProcess."""
    process = get_object_or_404(ReconciliationProcess.objects.select_related("user"), id=process_id)
    is_usd = process.process_currency == "USD"
    valid_tabs = ["resumen", "cruce", "erp", "cartola", "transbank"]
    if is_usd:
        valid_tabs.insert(4, "operadoras")

    active_tab = (request.GET.get("vista") or "resumen").lower()
    if active_tab not in valid_tabs:
        active_tab = "resumen"

    cruce_agg = process.cross_match_results.aggregate(
        total=Count("id"),
        matched=Count("id", filter=Q(system_result="MATCHED")),
    )
    cruce_total = cruce_agg["total"] or 0
    matched_total = cruce_agg["matched"] or 0
    erp_qs_base = process_erp_queryset(process)
    linked_erp_ids = set(
        process.cross_match_results.exclude(erp_transaction__isnull=True).values_list("erp_transaction_id", flat=True)
    )
    erp_agg = erp_qs_base.aggregate(
        total=Count("id"),
        unlinked=Count("id", filter=Q(reconciled=False) & ~Q(id__in=linked_erp_ids)),
    )
    erp_total = erp_agg["total"] or 0
    erp_unlinked_count = erp_agg["unlinked"] or 0
    bank_total = process_bank_movements_queryset(process).count()
    op_total = process_operator_queryset(process).count() if is_usd else 0
    tbk_total = process_transbank_queryset(process).count()

    tab_builders = {
        "resumen": lambda: _closing_resumen_context(process),
        "cruce": lambda: build_cruce_view_data(request, process=process, per_page=PAGE_SIZE_ROWS),
        "erp": lambda: build_erp_view_data(request, process=process, per_page=PAGE_SIZE_ROWS),
        "cartola": lambda: build_bank_view_data(request, process=process, per_page=PAGE_SIZE_ROWS),
        "operadoras": lambda: build_operator_view_data(request, process=process, per_page=PAGE_SIZE_ROWS),
        "transbank": lambda: build_transbank_view_data(request, process=process, per_page=PAGE_SIZE_ROWS),
    }
    tab_data = tab_builders[active_tab]()

    context = {
        "consolidated": False,
        "process": process,
        "is_usd": is_usd,
        "active_tab": active_tab,
        "match_rate": round(matched_total / cruce_total * 100, 1) if cruce_total else 0.0,
        "tab_counts": {
            "cruce": cruce_total,
            "matched": matched_total,
            "erp": erp_total,
            "erp_unlinked": erp_unlinked_count,
            "bank": bank_total,
            "operadoras": op_total,
            "transbank": tbk_total,
        },
        "query_string": _clean_query_string(request),
        **tab_data,
    }
    return render(request, "tables/closing_detail.html", context)


@login_required(login_url="login")
@require_GET
def audit_trail_view(request):
    """
    Audit Trail (/auditoria/) covering:
      - `erp`: ErpTransactionChange (manual edits, accepted suggestions, added/deleted rows)
      - `resoluciones`: AuditResolutionLog (exception status transitions and comments)
    """
    vista = (request.GET.get("vista") or "erp").lower()
    if vista not in ("erp", "resoluciones"):
        vista = "erp"

    moneda = (request.GET.get("moneda") or "ALL").upper()
    if moneda not in ("ALL", "USD", "CLP"):
        moneda = "ALL"
    fuente = (request.GET.get("fuente") or "").upper()
    q = (request.GET.get("q") or "").strip()
    date_from = parse_iso_date(request.GET.get("desde"))
    date_to = parse_iso_date(request.GET.get("hasta"))

    # Never expose legacy guest_name/room_number field changes
    erp_changes_base = ErpTransactionChange.objects.exclude(
        field_name__in=("guest_name", "room_number")
    ).select_related(
        "user",
        "erp_transaction",
        "erp_transaction__process",
        "cross_match_result",
        "cross_match_result__process",
    )
    resolutions_base = AuditResolutionLog.objects.select_related(
        "user",
        "previous_status",
        "new_status",
        "cross_match_result",
        "cross_match_result__process",
        "cross_match_result__card_type",
        "cross_match_result__erp_transaction",
        "cross_match_result__card_operator_transaction",
    )

    if moneda in ("USD", "CLP"):
        erp_changes_base = erp_changes_base.filter(
            Q(erp_transaction__source_currency=moneda)
            | Q(cross_match_result__process__process_currency=moneda)
        )
        resolutions_base = resolutions_base.filter(cross_match_result__process__process_currency=moneda)

    if date_from:
        erp_changes_base = erp_changes_base.filter(created_at__date__gte=date_from)
        resolutions_base = resolutions_base.filter(created_at__date__gte=date_from)
    if date_to:
        erp_changes_base = erp_changes_base.filter(created_at__date__lte=date_to)
        resolutions_base = resolutions_base.filter(created_at__date__lte=date_to)

    if q:
        erp_changes_base = erp_changes_base.filter(
            Q(user__username__icontains=q)
            | Q(user__first_name__icontains=q)
            | Q(reason__icontains=q)
            | Q(old_value__icontains=q)
            | Q(new_value__icontains=q)
            | Q(erp_transaction__account_code__icontains=q)
            | Q(erp_transaction__document_number__icontains=q)
            | Q(erp_transaction__authorization_code__icontains=q)
            | Q(cross_match_result__authorization_code__icontains=q)
        )
        resolutions_base = resolutions_base.filter(
            Q(user__username__icontains=q)
            | Q(user__first_name__icontains=q)
            | Q(comment__icontains=q)
            | Q(cross_match_result__authorization_code__icontains=q)
            | Q(cross_match_result__account_code__icontains=q)
            | Q(cross_match_result__document_number__icontains=q)
            | Q(cross_match_result__erp_transaction__account_code__icontains=q)
        )

    audit_per_page = resolve_per_page(request, default=PAGE_SIZE_ROWS)
    if vista == "erp":
        if fuente in (ErpTransactionChange.SOURCE_SUGGESTION, ErpTransactionChange.SOURCE_MANUAL):
            erp_changes_qs = erp_changes_base.filter(source=fuente)
        else:
            erp_changes_qs = erp_changes_base
        page = _paginate(erp_changes_qs, request.GET.get("page"), per_page=audit_per_page, base_default=PAGE_SIZE_ROWS)
        rows = [format_erp_change_row(c) for c in page.object_list]
    else:
        page = _paginate(resolutions_base, request.GET.get("page"), per_page=audit_per_page, base_default=PAGE_SIZE_ROWS)
        rows = list(page.object_list)

    context = {
        "vista": vista,
        "page": page,
        "rows": rows,
        "counts": {
            "erp": erp_changes_base.count(),
            "resoluciones": resolutions_base.count(),
        },
        "filters": {
            "moneda": moneda,
            "fuente": fuente,
            "q": q,
            "desde": request.GET.get("desde", ""),
            "hasta": request.GET.get("hasta", ""),
        },
        "query_string": _clean_query_string(request),
    }
    return render(request, "tables/audit_trail.html", context)
