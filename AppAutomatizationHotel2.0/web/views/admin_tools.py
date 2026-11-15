"""Administration utilities: reconciliation tolerance settings and test database cleanup."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.shortcuts import redirect, render

from ..forms import ToleranceSettingsForm
from ..models import (
    AuditResolutionLog,
    BankDepositValidation,
    BankStatementMovement,
    CardOperatorTransaction,
    CrossMatchResult,
    ErpTransaction,
    ErpTransactionChange,
    ReconciliationProcess,
    ReconciliationSummary,
    SystemConfiguration,
    TransbankTransaction,
)
from .common import CLP_SESSION_KEY, USD_SESSION_KEY


def _clear_reconciliation_tables() -> int:
    """
    Deletes all reconciliation records without triggering Django's giant `IN (...)`
    cascade updates/deletes, which exceed SQLite's bind-variable limit on 20k+ Bordero rows.
    """
    ordered_child_models = (
        AuditResolutionLog,
        ErpTransactionChange,
        CrossMatchResult,
        BankDepositValidation,
        ReconciliationSummary,
        ErpTransaction,
        TransbankTransaction,
        CardOperatorTransaction,
        BankStatementMovement,
    )
    with transaction.atomic():
        process_count = ReconciliationProcess.objects.count()
        qn = connection.ops.quote_name
        with connection.cursor() as cursor:
            for model in ordered_child_models:
                cursor.execute(f"DELETE FROM {qn(model._meta.db_table)}")
        ReconciliationProcess.objects.all().delete()
    return process_count


@login_required(login_url="login")
def clear_test_database_view(request):
    """
    Temporary testing utility: deletes all ReconciliationProcess records and all
    associated transaction/summary/validation tables while keeping catalog tables intact.
    """
    if request.method == "POST":
        process_count = _clear_reconciliation_tables()
        for session_key in (USD_SESSION_KEY, CLP_SESSION_KEY):
            request.session.pop(session_key, None)
        messages.warning(
            request,
            f"Base de datos de pruebas limpiada: se eliminaron {process_count} proceso(s) y todas sus transacciones asociadas."
        )
    return redirect("clp_upload" if request.POST.get("next") == "clp_upload" else "usd_upload")


@login_required(login_url="login")
def settings_view(request):
    """Reconciliation tolerance settings (USD and CLP). Accessible to all authenticated users."""
    config = SystemConfiguration.get_solo()
    form = ToleranceSettingsForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        config = form.save(commit=False)
        config.updated_by = request.user
        config.save()
        messages.success(request, "Tolerancias actualizadas. Se aplicarán a las próximas conciliaciones.")
        return redirect("settings")
    return render(request, "settings.html", {"form": form, "config": config})
