"""Home with the Dólares / Pesos panels, computed from the saved reconciliation closings."""

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum

from ..services.clp_reconciliation import MODALITY_LABELS as CLP_MODALITY_LABELS
from ..services.clp_report_exporter import _split_status_detail
from ..services.erp_editing import OPEN_STATUS_CODES
from ..services.suggestion_actions import load_suggestion
from ..templatetags.currency_filters import _normalize_bordero_text
from ..models import CrossMatchResult, ReconciliationProcess


EXCEPTION_LABELS = {
    "AMOUNT_MISMATCH": "Con diferencia de monto",
    "NOT_FOUND_IN_ERP": "No ubicada en Borderó",
}

USD_DEFAULT_ACTIONS = {
    "AMOUNT_MISMATCH": "Revisar diferencia entre Transbank, Borderó y Posición por Clientes",
    "NOT_FOUND_IN_ERP": "Buscar la venta en el Borderó y registrarla o verificar su código",
}

CLP_EXCEPTION_LABELS = {
    "AMOUNT_MISMATCH": "Con diferencia de monto",
    "CARD_TYPE_MISMATCH": "Medio de pago distinto",
    "NOT_FOUND_IN_ERP": "No está en el Borderó",
    "OUT_OF_PERIOD": "Fuera de período",
}


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
        management_status__code__in=OPEN_STATUS_CODES,
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


def _pending_by_cashier(pending):
    """Top 6 cashiers responsible for open exceptions; ERP-missing rows stay grouped at the bottom."""
    cashier_counts = {}
    for row in pending.values("cashier_username").annotate(count=Count("id")):
        name = (row["cashier_username"] or "").strip()
        key = name if name and name != "—" else None
        cashier_counts[key] = cashier_counts.get(key, 0) + row["count"]
    max_cashier = max(cashier_counts.values(), default=0)
    return sorted(
        (
            {
                "name": name or "Sin cajero (no está en Borderó)",
                "is_unassigned": name is None,
                "count": count,
                "pct": round(count / max_cashier * 100) if max_cashier else 0,
            }
            for name, count in cashier_counts.items()
        ),
        key=lambda c: (c["is_unassigned"], -c["count"]),
    )[:6]


def _classify_usd_error(item):
    """Maps a pending USD CrossMatchResult to a granular error type label and suggested action."""
    suggestion = load_suggestion(item)
    headline = (suggestion.get("headline") or "") if suggestion else ""
    action = _normalize_bordero_text((suggestion.get("action") or "").strip()) if suggestion else ""
    headline_lower = headline.lower()

    if item.system_result == "NOT_FOUND_IN_ERP":
        if suggestion:
            proposed_old = (suggestion.get("proposed_change") or {}).get("old")
            if suggestion.get("db_match") or "venta pendiente en bd" in headline_lower:
                label = "Venta previa en BD"
            elif "dividido" in headline_lower:
                label = "Factura dividida en Borderó"
            elif "no facturado en erp" in headline_lower or "no facturado en borderó" in headline_lower:
                label = "En Pos. Clientes (falta en Borderó)"
            elif "sin código" in headline_lower or proposed_old in ("", "—"):
                label = "Falta código en Borderó"
            else:
                label = "Error de tipeo"
        else:
            label = _exception_label(EXCEPTION_LABELS, item.system_result)
            action = USD_DEFAULT_ACTIONS["NOT_FOUND_IN_ERP"]
    elif item.system_result == "AMOUNT_MISMATCH":
        obs_lower = (item.system_observation or "").lower()
        if "col. usd en 0,00" in headline_lower:
            label = "Sin monto US$ en Pos. Clientes"
        elif "posición por clientes" in headline_lower and "hallado" in headline_lower:
            label = "Doc. distinto en Pos. Clientes"
        elif "cruce parcial" in headline_lower or "cobro parcial" in headline_lower:
            label = "Cobro parcial / propina"
        elif "variación cambiaria" in headline_lower or "dif. cambiaria" in headline_lower:
            label = "Diferencia de monto"
        elif "no encontrado en posición por clientes" in obs_lower:
            label = "Falta en Pos. Clientes"
        else:
            label = _exception_label(EXCEPTION_LABELS, item.system_result)
        if not action:
            action = (
                "Verificar registro en Posición por Clientes de la franquicia"
                if label == "Falta en Pos. Clientes"
                else USD_DEFAULT_ACTIONS["AMOUNT_MISMATCH"]
            )
    else:
        label = _exception_label(EXCEPTION_LABELS, item.system_result)
        if not action:
            action = USD_DEFAULT_ACTIONS.get(item.system_result, "")

    return label, action


def _classify_clp_error(item):
    _, error_type, action = _split_status_detail(item.system_status_detail)
    label = _normalize_bordero_text(error_type) if error_type else _exception_label(CLP_EXCEPTION_LABELS, item.system_result)
    return label, _normalize_bordero_text(action)


def classify_exception_error(item, currency=None):
    """Returns `(error_type_label, suggested_action)` for a pending exception item."""
    resolved_currency = (currency or item.process.process_currency or "").upper()
    if resolved_currency == "CLP":
        return _classify_clp_error(item)
    return _classify_usd_error(item)


def _build_pending_error_groups(pending_items, classify_fn):
    """Groups pending exceptions by error type with counts, amounts, and top suggested action."""
    error_groups = {}
    for item in pending_items:
        error_type, action = classify_fn(item)
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
    return pending_by_error


def _usd_dashboard():
    """Dollar panel: every figure comes from the saved USD closings."""
    processes = _closings("USD")
    recent = _recent_closings(processes)
    if not recent:
        return {"has_data": False}
    last_process = recent[0]

    pending = _pending_exceptions("USD")
    pending_totals = pending.aggregate(count=Count("id"), amount=Sum("transbank_amount"))

    pending_by_error = _build_pending_error_groups(
        pending.only("system_result", "system_status_detail", "system_observation", "auditor_comment", "transbank_amount"),
        _classify_usd_error,
    )

    largest_exceptions = list(
        pending.select_related("card_type", "process", "erp_transaction", "card_operator_transaction")
        .order_by("-transbank_amount")[:6]
    )
    for item in largest_exceptions:
        item.type_label, item.action = _classify_usd_error(item)

    return {
        "has_data": True,
        "last_process": last_process,
        "last_counts": _result_counts(last_process),
        "rate_delta": _rate_delta(recent),
        "bank": last_process.bank_list[0] if last_process.bank_list else None,
        "pending_count": pending_totals["count"] or 0,
        "pending_amount": pending_totals["amount"] or 0,
        "pending_by_type": _pending_by_type(pending, EXCEPTION_LABELS),
        "pending_by_cashier": _pending_by_cashier(pending),
        "pending_by_error": pending_by_error,
        "largest_exceptions": largest_exceptions,
        "recent_processes": recent,
        "process_count": processes.count(),
    }


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

    pending_by_error = _build_pending_error_groups(
        pending.only("system_result", "system_status_detail", "transbank_amount"),
        _classify_clp_error,
    )

    largest_exceptions = list(
        pending.select_related("card_type", "process", "erp_transaction", "card_operator_transaction")
        .order_by("-transbank_amount")[:6]
    )
    for item in largest_exceptions:
        modality, error_type, action = _split_status_detail(item.system_status_detail)
        item.type_label = error_type or _exception_label(CLP_EXCEPTION_LABELS, item.system_result)
        item.modality_label = modality
        item.action = action

    banks_by_code = {b.payment_modality.removeprefix("CLP_"): b for b in last_process.bank_list}
    banks = [
        {"label": label, "bank": banks_by_code[code]}
        for code, label in CLP_MODALITY_LABELS.items()
        if code in banks_by_code
    ]

    return {
        "has_data": True,
        "last_process": last_process,
        "last_counts": last_counts,
        "rate_delta": _rate_delta(recent),
        "banks": banks,
        "pending_count": pending_totals["count"] or 0,
        "pending_amount": pending_totals["amount"] or 0,
        "out_of_period_amount": out_of_period_amount,
        "pending_by_type": _pending_by_type(pending, CLP_EXCEPTION_LABELS),
        "pending_by_cashier": _pending_by_cashier(pending),
        "pending_by_error": pending_by_error,
        "largest_exceptions": largest_exceptions,
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
    request._pending_exceptions_count = (usd.get("pending_count") or 0) + (clp.get("pending_count") or 0)
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
