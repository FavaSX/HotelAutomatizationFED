"""Pendientes inbox: the accountant reviews, resolves and reopens exceptions."""

from urllib.parse import urlencode

from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.db.models import Count, Min, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from ..forms import ExceptionResolutionForm
from ..models import AuditResolutionLog, CardType, CrossMatchResult, ErpTransaction, ErpTransactionChange
from ..services.erp_editing import (
    FIELD_LABELS,
    OPEN_STATUS_CODES,
    CorrectionError,
    add_row,
    candidate_rows,
    candidate_rows_by_match,
    default_payment_code,
    display_value,
    edit_row,
    payment_code_choices,
    soft_delete_row,
)
from ..services.exception_resolution import ResolutionError, reopen_exception, resolve_exception
from ..services.suggestion_actions import accept_suggestion, load_suggestion, open_proposal, reject_suggestion
from ..services.clp_reconciliation import MODALITY_LABELS
from ..services.voucher_recross import RecrossError
from ..services.report_exporter import generate_exceptions_excel_bytes
from .dashboard import CLP_EXCEPTION_LABELS, EXCEPTION_LABELS, _exception_label, _split_status_detail


INBOX_LABELS = {"usd": EXCEPTION_LABELS, "clp": CLP_EXCEPTION_LABELS}
EXCEPTIONS_PAGE_SIZE = 25
EXCEPTION_FILTER_KEYS = ("estado", "moneda", "tipo", "cajero", "tarjeta", "desde", "hasta", "q")


def _pending_count_by_currency():
    rows = (
        CrossMatchResult.objects.filter(management_status__code__in=OPEN_STATUS_CODES).exclude(system_result="MATCHED")
        .values("process__process_currency").annotate(count=Count("id"))
    )
    counts = {"usd": 0, "clp": 0}
    for row in rows:
        counts[row["process__process_currency"].lower()] = row["count"]
    return counts


def _inbox_params(request, pending_counts=None):
    """The inbox always shows one currency: the requested one, else dollars unless only pesos have pending cases."""
    params = request.GET.copy()
    currency = params.get("moneda", "").lower()
    if currency not in INBOX_LABELS:
        pending_counts = pending_counts or _pending_count_by_currency()
        currency = "clp" if pending_counts["clp"] and not pending_counts["usd"] else "usd"
    params["moneda"] = currency
    return params, currency


def _filtered_exceptions(params):
    """
    Exceptions narrowed by the inbox filters, oldest closing first. MATCHED vouchers only appear once the
    accountant fixed the ERP to make them match ("Corregido en ERP"), so the fix stays visible.
    """
    fixed_in_erp = CrossMatchResult.objects.filter(erp_changes__isnull=False).values("id")
    qs = (
        CrossMatchResult.objects.filter(~Q(system_result="MATCHED") | Q(id__in=fixed_in_erp))
        .select_related(
            "process",
            "card_type",
            "management_status",
            "resolved_by",
            "transbank_transaction",
            "erp_transaction",
            "card_operator_transaction",
        )
        .order_by("process__execution_date", "-transbank_amount")
    )
    state = params.get("estado") or "pendientes"
    if state == "pendientes":
        qs = qs.filter(management_status__code__in=OPEN_STATUS_CODES)
    elif state == "resueltas":
        qs = qs.exclude(management_status__code__in=OPEN_STATUS_CODES).exclude(management_status__isnull=True)

    currency = (params.get("moneda") or "").upper()
    if currency in ("USD", "CLP"):
        qs = qs.filter(process__process_currency=currency)
    if params.get("tipo"):
        qs = qs.filter(system_result=params["tipo"])
    cashier = params.get("cajero")
    if cashier == "__none__":
        qs = qs.filter(Q(cashier_username__isnull=True) | Q(cashier_username__in=["", "—"]))
    elif cashier:
        qs = qs.filter(cashier_username=cashier)
    if params.get("tarjeta"):
        qs = qs.filter(card_type__code=params["tarjeta"])
    for key, lookup in (("desde", "sale_date__gte"), ("hasta", "sale_date__lte")):
        value = parse_date(params.get(key) or "")
        if value:
            qs = qs.filter(**{lookup: value})
    query = (params.get("q") or "").strip()
    if query:
        qs = qs.filter(
            Q(authorization_code__icontains=query)
            | Q(account_code__icontains=query)
            | Q(document_number__icontains=query)
            | Q(erp_transaction__account_code__icontains=query)
            | Q(erp_transaction__authorization_code__icontains=query)
            | Q(card_operator_transaction__document_number__icontains=query)
        )
    return qs, state


def _decorate_exception(item, today):
    """Labels, age and Level 2 suggestion for one inbox row."""
    item.is_clp = item.process.process_currency == "CLP"
    item.type_label = _exception_label(CLP_EXCEPTION_LABELS if item.is_clp else EXCEPTION_LABELS, item.system_result)
    item.is_pending = item.management_status is None or item.management_status.code in OPEN_STATUS_CODES
    item.is_rejected = bool(item.management_status and item.management_status.code == "REJECTED")
    item.days_open = (today - timezone.localtime(item.process.execution_date).date()).days
    item.age_level = "ok" if item.days_open < 3 else "warn" if item.days_open < 7 else "late"
    item.suggestion = None
    item.action = ""
    if item.is_clp:
        _, error_type, item.action = _split_status_detail(item.system_status_detail)
        item.type_label = error_type or item.type_label
    item.suggestion = load_suggestion(item)
    item.proposed_change = (item.suggestion or {}).get("proposed_change")
    # Opción A: one-click Aceptar / Rechazar while the case is open and nobody decided on the change yet
    item.proposal = open_proposal(item) if item.is_pending and not item.is_rejected else None
    if item.proposal:
        new = item.proposal["new"]
        # Pesos have no cents: show 67650, not 67650.00
        item.proposal_value = new[:-3] if item.proposal["field"] == "amount" and new.endswith(".00") else new
    return item


@login_required(login_url="login")
def exceptions_view(request):
    """Exceptions of every saved closing of one currency (Dólares / Pesos tabs), filterable, with their history."""
    pending_counts = _pending_count_by_currency()
    request._pending_exceptions_count = pending_counts["usd"] + pending_counts["clp"]
    params, currency = _inbox_params(request, pending_counts)
    qs, state = _filtered_exceptions(params)
    page = Paginator(qs, EXCEPTIONS_PAGE_SIZE).get_page(params.get("page"))
    today = timezone.localdate()
    rows = [_decorate_exception(item, today) for item in page.object_list]
    logs = {}
    for log in AuditResolutionLog.objects.filter(cross_match_result__in=rows).select_related("user", "previous_status", "new_status"):
        logs.setdefault(log.cross_match_result_id, []).append(log)
    changes = {}
    for change in ErpTransactionChange.objects.filter(cross_match_result__in=rows).select_related("user", "erp_transaction"):
        change.field_label = FIELD_LABELS.get(change.field_name, "")
        currency_of = change.erp_transaction.source_currency or "CLP"
        change.old_display = display_value(change.field_name, change.old_value, currency_of)
        change.new_display = display_value(change.field_name, change.new_value, currency_of)
        changes.setdefault(change.cross_match_result_id, []).append(change)
    choices = {cur: payment_code_choices(cur) for cur in ("CLP", "USD")}
    candidates_map = candidate_rows_by_match(
        [item for item in rows if item.is_pending and item.transbank_transaction_id is not None]
    )
    for item in rows:
        item.history = _history_with_reasons(logs.get(item.id, []), changes.get(item.id, []))
        item.erp_changes_list = changes.get(item.id, [])
        _add_correction_context(item, choices[item.process.process_currency], candidates_map.get(item.id))

    suggestion_items, suggestion_total = _suggestion_panel(currency, today)
    totals = qs.aggregate(count=Count("id"), amount=Sum("transbank_amount"), oldest=Min("process__execution_date"))
    totals["oldest_days"] = (today - timezone.localtime(totals["oldest"]).date()).days if totals["oldest"] else None

    currency_exceptions = CrossMatchResult.objects.exclude(system_result="MATCHED").filter(
        process__process_currency=currency.upper()
    )
    cashiers = sorted(
        {c for c in currency_exceptions.values_list("cashier_username", flat=True).distinct() if c and c != "—"}
    )
    filters = {key: params.get(key, "") for key in EXCEPTION_FILTER_KEYS}
    filters["estado"] = state
    query_string = urlencode({k: v for k, v in filters.items() if v})
    # Switching tabs keeps the state filter only: cashiers and types differ between currencies
    tab_query = urlencode({"estado": state}) if state != "pendientes" else ""

    return render(request, "exceptions.html", {
        "currency": currency,
        "pending_counts": pending_counts,
        "tab_query": tab_query,
        "page": page,
        "rows": rows,
        "totals": totals,
        "filters": filters,
        "query_string": query_string,
        "type_choices": sorted(INBOX_LABELS[currency].items(), key=lambda t: t[1]),
        "cashiers": cashiers,
        "card_types": CardType.objects.filter(
            id__in=currency_exceptions.values("card_type_id")
        ).order_by("name"),
        "form": ExceptionResolutionForm(),
        "suggestion_items": suggestion_items,
        "suggestion_cards": {
            "cases": {str(i.id): _suggestion_card(i) for i in [*suggestion_items, *rows] if i.suggestion},
            "return_query": query_string + (f"&page={page.number}" if page.number > 1 else ""),
            "payment_choices": {cur: [[code, label] for code, label, _ in opts] for cur, opts in choices.items()},
        },
        "suggestion_total": suggestion_total,
        "panel_payment_choices": choices[currency.upper()],
    })


SUGGESTION_PANEL_LIMIT = 30


def _suggestion_card(item):
    """The case in the same shape as a step-2 voucher, so suggestion_card.js draws the same component."""
    suggestion = dict(item.suggestion or {})
    proposal = suggestion.get("proposed_change")
    if proposal and not proposal.get("status") and not item.proposal:
        suggestion.pop("proposed_change")  # closed case: show the comparison, no buttons
    tbk = item.transbank_transaction
    card = {
        "case_id": item.id,
        "currency": item.process.process_currency,
        "authorization_code": item.authorization_code,
        "sale_date": item.sale_date.strftime("%d/%m/%Y") if item.sale_date else "",
        "system_result": item.system_result,
        "type_label": item.type_label,
        "action": item.action,
        "suggestion": suggestion,
        "accept_url": reverse("exception_suggestion_accept", args=[item.id]),
        "reject_url": reverse("exception_suggestion_reject", args=[item.id]),
    }
    if item.is_clp:
        modality = tbk.payment_modality if tbk else "CREDIT"
        card.update({
            "tbk_amount": float(item.transbank_amount),
            "payment_modality": modality,
            "modality_label": MODALITY_LABELS.get(modality, ""),
            "card_name": "Redcompra" if modality in ("DEBIT", "PREPAID") else (item.card_type.name if item.card_type else ""),
            "merchant_location": item.merchant_location_name or "",
        })
    else:
        card.update({
            "gross_amount_usd": float(item.transbank_amount),
            "card_name": item.card_type.name if item.card_type else "",
            "card_code": item.card_type.code if item.card_type else "",
            "cashier_username": item.cashier_username or "",
        })
    return card


def _suggestion_panel(currency, today):
    """Open cases of the currency whose suggestion is a concrete ERP change, for the panel above the inbox table."""
    qs = (
        CrossMatchResult.objects.filter(
            process__process_currency=currency.upper(), management_status__code="PENDING",
            auditor_comment__contains='"proposed_change"',
        )
        .exclude(system_result="MATCHED")
        .select_related("process", "card_type", "management_status", "transbank_transaction")
        .order_by("process__execution_date", "-transbank_amount")
    )
    items = []
    for item in qs[:200]:
        _decorate_exception(item, today)
        if item.proposal:
            item.proposal_input = (
                "date" if item.proposal["field"] == "transaction_date"
                else "select" if item.proposal["field"] == "payment_code" else "text"
            )
            items.append(item)
    return items[:SUGGESTION_PANEL_LIMIT], len(items)


def _history_with_reasons(logs, changes):
    """
    One timeline per case, newest first. Every ERP correction already writes a status log whose comment
    summarizes the change, so the ERP change rows only contribute the accountant's reason ("Motivo").
    """
    logs = sorted(logs, key=lambda log: log.created_at, reverse=True)
    for log in logs:
        log.reason = ""
    for change in changes:
        if not change.reason:
            continue
        closest = min(logs, key=lambda log: abs((log.created_at - change.created_at).total_seconds()), default=None)
        if (closest and abs((closest.created_at - change.created_at).total_seconds()) < 10
                and change.reason not in (closest.comment or "")):
            closest.reason = change.reason
    return logs


def _add_correction_context(item, choices, preloaded_candidates=None):
    """ERP rows to fix and the prefilled "add row" values for the "Corregir el ERP" panel of an open case."""
    item.can_correct = item.is_pending and item.transbank_transaction_id is not None
    item.payment_choices = choices
    item.erp_rows = []
    if not item.can_correct:
        return
    item.erp_rows = preloaded_candidates if preloaded_candidates is not None else candidate_rows(item)
    tbk = item.transbank_transaction
    if item.is_clp:
        amount = tbk.original_sale_amount
    else:
        operator = item.card_operator_transaction
        amount = (operator.balance_amount or operator.corrected_balance_amount) if operator else None
    item.new_row = {
        "amount": amount,
        "authorization_code": item.authorization_code,
        "transaction_date": item.sale_date,
        "payment_code": default_payment_code(item, choices=choices),
        "document_number": "",
        # Valor as the Bordero writes a payment: negative, no thousands separator
        "bordero_amount": "" if amount is None else f"-{amount:.2f}".removesuffix(".00"),
    }


def _back_to_inbox(request):
    query = request.POST.get("return_query", "")
    return redirect(f"{reverse('exceptions')}?{query}" if query else reverse("exceptions"))


@login_required(login_url="login")
@require_POST
def exception_resolve_view(request, match_id: int):
    match = get_object_or_404(CrossMatchResult, pk=match_id)
    form = ExceptionResolutionForm(request.POST)
    form.is_valid()
    try:
        resolve_exception(match, form.cleaned_data.get("status", ""), form.cleaned_data.get("comment", ""), request.user)
        messages.success(request, f"Voucher {match.authorization_code} marcado como “{match.management_status.display_name}”.")
    except ResolutionError as exc:
        messages.error(request, f"Voucher {match.authorization_code}: {exc}")
    return _back_to_inbox(request)


@login_required(login_url="login")
@require_POST
def exception_reopen_view(request, match_id: int):
    match = get_object_or_404(CrossMatchResult, pk=match_id)
    form = ExceptionResolutionForm(request.POST)
    form.is_valid()
    try:
        reopen_exception(match, form.cleaned_data.get("comment", ""), request.user)
        messages.success(request, f"Voucher {match.authorization_code} vuelve a quedar pendiente.")
    except ResolutionError as exc:
        messages.error(request, f"Voucher {match.authorization_code}: {exc}")
    return _back_to_inbox(request)


def _correction_text(outcome, done_text):
    """(message level, text) of a finished correction."""
    if outcome.get("matched"):
        return "success", f"{done_text[:1].upper()}{done_text[1:]}. Ahora cuadra y quedó como “Corregido en ERP”."
    if outcome.get("system_result") == "NOT_FOUND_IN_ERP":
        return "warning", f"{done_text[:1].upper()}{done_text[1:]}. Sigue pendiente: no se encuentra en el ERP."
    return "warning", f"{done_text[:1].upper()}{done_text[1:]}. Sigue pendiente: aún hay diferencia de {outcome.get('remaining')}."


def _is_ajax(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _correction_view(request, match_id, operation, done_text):
    """Runs a correction. Plain form posts go back to the inbox; the suggestions modal posts with fetch and gets JSON."""
    match = get_object_or_404(
        CrossMatchResult.objects.select_related("process", "transbank_transaction", "card_type", "management_status"),
        pk=match_id,
    )
    try:
        outcome = operation(match)
    except (CorrectionError, RecrossError) as exc:
        if _is_ajax(request):
            return JsonResponse({"success": False, "error": str(exc)}, status=400)
        messages.error(request, f"Voucher {match.authorization_code}: {exc}")
        return _back_to_inbox(request)
    level, text = _correction_text(outcome, done_text)
    if _is_ajax(request):
        return JsonResponse({"success": True, "matched": bool(outcome.get("matched")), "message": text, "status": "accepted"})
    getattr(messages, level)(request, f"Voucher {match.authorization_code}: {text[:1].lower()}{text[1:]}")
    return _back_to_inbox(request)


def _erp_row(match, row_id):
    return get_object_or_404(ErpTransaction.objects.select_related("card_type"), pk=row_id,
                             source_currency=match.process.process_currency)


@login_required(login_url="login")
@require_POST
def exception_erp_edit_view(request, match_id: int, row_id: int):
    values = {f: request.POST.get(f) for f in FIELD_LABELS}
    return _correction_view(
        request, match_id,
        lambda match: edit_row(_erp_row(match, row_id), values, request.user, match=match, reason=request.POST.get("reason", "")),
        "fila del ERP corregida",
    )


@login_required(login_url="login")
@require_POST
def exception_erp_add_view(request, match_id: int):
    values = {f: request.POST.get(f) for f in FIELD_LABELS}
    return _correction_view(
        request, match_id,
        lambda match: add_row(match, values, request.user, reason=request.POST.get("reason", "")),
        "fila agregada al ERP",
    )


@login_required(login_url="login")
@require_POST
def exception_erp_delete_view(request, match_id: int, row_id: int):
    return _correction_view(
        request, match_id,
        lambda match: soft_delete_row(_erp_row(match, row_id), request.user, request.POST.get("reason", ""), match=match),
        "fila eliminada del ERP",
    )


@login_required(login_url="login")
@require_POST
def exception_suggestion_accept_view(request, match_id: int):
    value = request.POST.get("value")
    return _correction_view(request, match_id, lambda match: accept_suggestion(match, request.user, value), "sugerencia aceptada")


@login_required(login_url="login")
@require_POST
def exception_suggestion_reject_view(request, match_id: int):
    match = get_object_or_404(CrossMatchResult.objects.select_related("management_status"), pk=match_id)
    text = "Sugerencia rechazada. El ERP no se tocó; el caso sigue abierto para corregirlo a mano."
    try:
        reject_suggestion(match, request.user, request.POST.get("comment", ""))
    except CorrectionError as exc:
        if _is_ajax(request):
            return JsonResponse({"success": False, "error": str(exc)}, status=400)
        messages.error(request, f"Voucher {match.authorization_code}: {exc}")
        return _back_to_inbox(request)
    if _is_ajax(request):
        return JsonResponse({"success": True, "matched": False, "message": text, "status": "rejected"})
    messages.info(request, f"Voucher {match.authorization_code}: {text[:1].lower()}{text[1:]}")
    return _back_to_inbox(request)


@login_required(login_url="login")
def exceptions_export_view(request):
    """The filtered inbox as Excel, e.g. to send reception the vouchers of one cashier."""
    params, currency = _inbox_params(request)
    qs, _ = _filtered_exceptions(params)
    today = timezone.localdate()
    rows = [_decorate_exception(item, today) for item in qs]
    excel_stream = generate_exceptions_excel_bytes(rows)
    response = HttpResponse(
        excel_stream.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="excepciones_{currency}_{today:%Y%m%d}.xlsx"'
    return response
