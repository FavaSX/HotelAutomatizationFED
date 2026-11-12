"""Superuser-only views for managing application users (create, edit, soft delete & restore)."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..forms import ManagedUserCreateForm, ManagedUserEditForm
from ..services.user_management import (
    build_users_manage_context,
    create_managed_user,
    restore_user,
    soft_delete_user,
    update_managed_user,
    user_role,
)


def _require_superuser(request):
    """Blocks non-superusers cleanly; returns a redirect response when denied."""
    if not request.user.is_superuser:
        messages.error(request, "Solo un superadmin puede gestionar usuarios.")
        return redirect("dashboard")
    return None


def _manage_redirect_url(request) -> str:
    """Preserves active list filters when returning to the user-management view."""
    params = {}
    for key in ("estado", "rol", "q"):
        val = (request.POST.get(f"filter_{key}") or request.GET.get(key) or "").strip()
        if val and not (key in ("estado", "rol") and val == "ALL"):
            params[key] = val
    base = reverse("users_manage")
    return f"{base}?{urlencode(params)}" if params else base


def _render_users_manage(
    request,
    *,
    create_form=None,
    open_create_modal: bool = False,
    edit_form=None,
    edit_target: User = None,
):
    estado = (request.GET.get("estado") or request.POST.get("filter_estado") or "ALL").strip().upper()
    rol = (request.GET.get("rol") or request.POST.get("filter_rol") or "ALL").strip().upper()
    q = (request.GET.get("q") or request.POST.get("filter_q") or "").strip()

    ctx = build_users_manage_context(estado=estado, rol=rol, q=q)
    ctx["create_form"] = create_form or ManagedUserCreateForm()
    ctx["open_create_modal"] = open_create_modal
    ctx["edit_form"] = edit_form or ManagedUserEditForm(auto_id="id_edit_%s")
    ctx["edit_target"] = edit_target
    return render(request, "users_manage.html", ctx)


@login_required(login_url="login")
def users_manage_view(request):
    """Lists all users (active and soft-deleted) and handles creating new accounts."""
    denied = _require_superuser(request)
    if denied is not None:
        return denied

    if request.method == "POST":
        form = ManagedUserCreateForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                created = create_managed_user(
                    request.user,
                    username=cd["username"],
                    first_name=cd.get("first_name", ""),
                    last_name=cd.get("last_name", ""),
                    email=cd.get("email", ""),
                    role=cd["role"],
                    password=cd["password"],
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, f"Usuario '{created.username}' creado correctamente.")
                return redirect(_manage_redirect_url(request))
        return _render_users_manage(request, create_form=form, open_create_modal=True)

    return _render_users_manage(request)


@login_required(login_url="login")
@require_POST
def user_edit_view(request, user_id: int):
    """Updates user details/role and optionally resets password."""
    denied = _require_superuser(request)
    if denied is not None:
        return denied

    target = get_object_or_404(User, pk=user_id)
    form = ManagedUserEditForm(request.POST, actor=request.user, target_user=target, auto_id="id_edit_%s")
    if form.is_valid():
        cd = form.cleaned_data
        try:
            profile_changed, password_changed = update_managed_user(
                request.user,
                target,
                first_name=cd.get("first_name", ""),
                last_name=cd.get("last_name", ""),
                email=cd.get("email", ""),
                role=cd["role"],
                new_password=cd.get("new_password", ""),
            )
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            if target.pk == request.user.pk and password_changed:
                update_session_auth_hash(request, target)
            if profile_changed or password_changed:
                messages.success(request, f"Usuario '{target.username}' actualizado correctamente.")
            else:
                messages.info(request, f"No hubo cambios para el usuario '{target.username}'.")
            return redirect(_manage_redirect_url(request))

    target.managed_role = user_role(target)
    return _render_users_manage(request, edit_form=form, edit_target=target)


@login_required(login_url="login")
@require_POST
def user_soft_delete_view(request, user_id: int):
    """Soft-deletes a user (marks inactive with metadata + invalidates active sessions)."""
    denied = _require_superuser(request)
    if denied is not None:
        return denied

    target = get_object_or_404(User, pk=user_id)
    reason = request.POST.get("reason", "")
    try:
        soft_delete_user(request.user, target, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.warning(
            request,
            f"Usuario '{target.username}' eliminado (soft delete). Su historial contable y de auditoría se conserva intacto.",
        )
    return redirect(_manage_redirect_url(request))


@login_required(login_url="login")
@require_POST
def user_restore_view(request, user_id: int):
    """Restores a soft-deleted user back to active status."""
    denied = _require_superuser(request)
    if denied is not None:
        return denied

    target = get_object_or_404(User, pk=user_id)
    reason = request.POST.get("reason", "")
    try:
        restore_user(request.user, target, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Usuario '{target.username}' reactivado correctamente.")
    return redirect(_manage_redirect_url(request))
