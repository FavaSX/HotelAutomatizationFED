"""Superuser-only tools: tolerance settings and the test database cleanup."""

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages

from ..forms import ToleranceSettingsForm
from ..models import ReconciliationProcess, SystemConfiguration
from .common import USD_SESSION_KEY, CLP_SESSION_KEY


@login_required(login_url="login")
def clear_test_database_view(request):
    """
    Temporary testing utility: deletes all ReconciliationProcess records (and cascades
    to all 7 child transaction/summary/validation tables) while keeping catalog tables intact.
    Restricted to superusers so demo accounts on the public deployment cannot wipe data.
    """
    if not request.user.is_superuser:
        messages.error(request, "Solo un administrador puede vaciar la base de datos.")
    elif request.method == "POST":
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
def settings_view(request):
    """Reconciliation tolerance settings (USD and CLP). Restricted to superusers."""
    if not request.user.is_superuser:
        messages.error(request, "Solo un administrador puede modificar la configuración.")
        return redirect("dashboard")

    config = SystemConfiguration.get_solo()
    form = ToleranceSettingsForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        config = form.save(commit=False)
        config.updated_by = request.user
        config.save()
        messages.success(request, "Tolerancias actualizadas. Se aplicarán a las próximas conciliaciones.")
        return redirect("settings")
    return render(request, "settings.html", {"form": form, "config": config})
