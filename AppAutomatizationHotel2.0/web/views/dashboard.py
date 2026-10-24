"""Home with the Dólares / Pesos panels, computed from the saved reconciliation closings."""

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum

from ..models import CrossMatchResult, ReconciliationProcess, ReconciliationSummary


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
