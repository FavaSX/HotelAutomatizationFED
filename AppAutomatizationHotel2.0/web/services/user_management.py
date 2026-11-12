"""Superuser-only user lifecycle management (create, edit, password reset, soft delete & restore)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import UserAccountStatus, UserAuditLog

ROLE_STANDARD = "STANDARD"
ROLE_SUPERADMIN = "SUPERADMIN"
ROLE_CHOICES: List[Tuple[str, str]] = [
    (ROLE_STANDARD, "Usuario estándar"),
    (ROLE_SUPERADMIN, "Superadmin"),
]


def user_role(user: User) -> str:
    """Returns canonical application role code for a Django User."""
    return ROLE_SUPERADMIN if user.is_superuser else ROLE_STANDARD


def active_superadmin_count(*, exclude_user_id: Optional[int] = None) -> int:
    """Counts active superadmins, optionally excluding one user id."""
    qs = User.objects.filter(is_superuser=True, is_active=True)
    if exclude_user_id is not None:
        qs = qs.exclude(pk=exclude_user_id)
    return qs.count()


def _ensure_status_record(target: User, *, actor: Optional[User] = None) -> UserAccountStatus:
    """Returns or creates the UserAccountStatus row for `target` (safe for legacy accounts)."""
    status_obj, created = UserAccountStatus.objects.get_or_create(
        user=target,
        defaults={
            "created_by": actor,
            "is_soft_deleted": not target.is_active,
        },
    )
    if not created and status_obj.is_soft_deleted != (not target.is_active):
        status_obj.is_soft_deleted = not target.is_active
        status_obj.save(update_fields=["is_soft_deleted", "updated_at"])
    return status_obj


def _invalidate_user_sessions(user_id: int) -> None:
    """Deletes any active Django sessions belonging to `user_id` upon soft deletion."""
    now = timezone.now()
    target_str = str(user_id)
    to_delete: List[str] = []
    for sess in Session.objects.filter(expire_date__gte=now).only("session_key", "session_data"):
        try:
            data = sess.get_decoded()
        except Exception:
            continue
        if str(data.get("_auth_user_id") or "") == target_str:
            to_delete.append(sess.session_key)
    if to_delete:
        Session.objects.filter(session_key__in=to_delete).delete()


@transaction.atomic
def create_managed_user(
    actor: User,
    *,
    username: str,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    role: str = ROLE_STANDARD,
    password: str,
) -> User:
    """Creates an active User account with lifecycle tracking and immutable audit log."""
    clean_username = (username or "").strip()
    if not clean_username:
        raise ValueError("El nombre de usuario es obligatorio.")
    if User.objects.filter(username__iexact=clean_username).exists():
        raise ValueError(f"Ya existe un usuario con el nombre '{clean_username}'.")
    if role not in (ROLE_STANDARD, ROLE_SUPERADMIN):
        raise ValueError("Rol inválido.")

    is_super = role == ROLE_SUPERADMIN
    new_user = User(
        username=clean_username,
        first_name=(first_name or "").strip(),
        last_name=(last_name or "").strip(),
        email=(email or "").strip(),
        is_superuser=is_super,
        is_staff=is_super,
        is_active=True,
    )
    validate_password(password, user=new_user)
    new_user.set_password(password)
    new_user.save()

    UserAccountStatus.objects.create(
        user=new_user,
        created_by=actor,
        is_soft_deleted=False,
    )
    role_label = "Superadmin" if is_super else "Usuario estándar"
    UserAuditLog.objects.create(
        target_user=new_user,
        actor=actor,
        action=UserAuditLog.ACTION_CREATED,
        details=f"Cuenta creada con rol {role_label}.",
    )
    return new_user


@transaction.atomic
def update_managed_user(
    actor: User,
    target: User,
    *,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    role: str = ROLE_STANDARD,
    new_password: str = "",
) -> Tuple[bool, bool]:
    """
    Updates profile details, role and optionally resets password for `target`.
    Returns `(profile_or_role_changed, password_changed)`.
    """
    if role not in (ROLE_STANDARD, ROLE_SUPERADMIN):
        raise ValueError("Rol inválido.")

    want_super = role == ROLE_SUPERADMIN
    if target.pk == actor.pk and target.is_superuser and not want_super:
        raise ValueError("No puedes quitarte el rol de superadmin a ti mismo.")
    if target.is_superuser and target.is_active and not want_super:
        if active_superadmin_count(exclude_user_id=target.pk) == 0:
            raise ValueError("Debe quedar al menos un superadmin activo en el sistema.")

    clean_first = (first_name or "").strip()
    clean_last = (last_name or "").strip()
    clean_email = (email or "").strip()

    changes: List[str] = []
    if (target.first_name or "") != clean_first or (target.last_name or "") != clean_last:
        changes.append("nombre actualizado")
    if (target.email or "") != clean_email:
        changes.append(f"correo: {clean_email or '—'}")
    if bool(target.is_superuser) != want_super or bool(target.is_staff) != want_super:
        changes.append(f"rol → {'Superadmin' if want_super else 'Usuario estándar'}")

    target.first_name = clean_first
    target.last_name = clean_last
    target.email = clean_email
    target.is_superuser = want_super
    target.is_staff = want_super

    password_changed = bool(new_password)
    if password_changed:
        validate_password(new_password, user=target)
        target.set_password(new_password)

    profile_changed = bool(changes)
    if profile_changed or password_changed:
        target.save()
        _ensure_status_record(target, actor=actor)

    if profile_changed:
        UserAuditLog.objects.create(
            target_user=target,
            actor=actor,
            action=UserAuditLog.ACTION_UPDATED,
            details=", ".join(changes).capitalize() + ".",
        )
    if password_changed:
        UserAuditLog.objects.create(
            target_user=target,
            actor=actor,
            action=UserAuditLog.ACTION_PASSWORD_RESET,
            details="Contraseña actualizada desde Gestión de Usuarios.",
        )

    return profile_changed, password_changed


@transaction.atomic
def soft_delete_user(actor: User, target: User, *, reason: str = "") -> UserAccountStatus:
    """Deactivates `target` without removing its database row or breaking audit history."""
    if target.pk == actor.pk:
        raise ValueError("No puedes eliminar tu propio usuario mientras tienes sesión iniciada.")
    if not target.is_active:
        raise ValueError("Este usuario ya se encuentra eliminado (soft delete).")
    if target.is_superuser and active_superadmin_count(exclude_user_id=target.pk) == 0:
        raise ValueError("No puedes eliminar al último superadmin activo del sistema.")

    clean_reason = (reason or "").strip()
    now = timezone.now()

    target.is_active = False
    target.save(update_fields=["is_active"])

    status_obj = _ensure_status_record(target, actor=actor)
    status_obj.is_soft_deleted = True
    status_obj.deleted_at = now
    status_obj.deleted_by = actor
    status_obj.delete_reason = clean_reason[:255]
    status_obj.save(update_fields=["is_soft_deleted", "deleted_at", "deleted_by", "delete_reason", "updated_at"])

    _invalidate_user_sessions(target.pk)

    log_detail = f"Motivo: {clean_reason}" if clean_reason else "Sin motivo especificado."
    UserAuditLog.objects.create(
        target_user=target,
        actor=actor,
        action=UserAuditLog.ACTION_SOFT_DELETED,
        details=log_detail[:500],
    )
    return status_obj


@transaction.atomic
def restore_user(actor: User, target: User, *, reason: str = "") -> UserAccountStatus:
    """Restores a previously soft-deleted user back to active login access."""
    if target.is_active:
        raise ValueError("Este usuario ya se encuentra activo.")

    clean_note = (reason or "").strip()
    now = timezone.now()

    target.is_active = True
    target.save(update_fields=["is_active"])

    status_obj = _ensure_status_record(target, actor=actor)
    status_obj.is_soft_deleted = False
    status_obj.restored_at = now
    status_obj.restored_by = actor
    status_obj.save(update_fields=["is_soft_deleted", "restored_at", "restored_by", "updated_at"])

    log_detail = f"Nota: {clean_note}" if clean_note else "Usuario reactivado para volver a iniciar sesión."
    UserAuditLog.objects.create(
        target_user=target,
        actor=actor,
        action=UserAuditLog.ACTION_RESTORED,
        details=log_detail[:500],
    )
    return status_obj


def build_users_manage_context(*, estado: str = "ALL", rol: str = "ALL", q: str = "") -> Dict[str, Any]:
    """Builds filtered user list, KPI metrics, and recent user-management audit entries."""
    base_qs = User.objects.select_related(
        "account_status",
        "account_status__created_by",
        "account_status__deleted_by",
        "account_status__restored_by",
    )
    all_users = list(base_qs)
    kpis = {
        "total": len(all_users),
        "active": sum(1 for u in all_users if u.is_active),
        "deleted": sum(1 for u in all_users if not u.is_active),
        "superadmins": sum(1 for u in all_users if u.is_active and u.is_superuser),
        "standard": sum(1 for u in all_users if u.is_active and not u.is_superuser),
    }

    qs = base_qs.order_by("-is_active", "-is_superuser", "username")
    if estado == "ACTIVE":
        qs = qs.filter(is_active=True)
    elif estado == "DELETED":
        qs = qs.filter(is_active=False)

    if rol == ROLE_SUPERADMIN:
        qs = qs.filter(is_superuser=True)
    elif rol == ROLE_STANDARD:
        qs = qs.filter(is_superuser=False)

    clean_q = (q or "").strip()
    if clean_q:
        qs = qs.filter(
            Q(username__icontains=clean_q)
            | Q(first_name__icontains=clean_q)
            | Q(last_name__icontains=clean_q)
            | Q(email__icontains=clean_q)
        )

    rows: List[User] = list(qs)
    for u in rows:
        u.managed_role = user_role(u)
        u.managed_role_label = "Superadmin" if u.is_superuser else "Usuario estándar"
        full = f"{(u.first_name or '').strip()} {(u.last_name or '').strip()}".strip()
        u.full_display_name = full or "—"
        try:
            u.lifecycle_status = u.account_status
        except UserAccountStatus.DoesNotExist:
            u.lifecycle_status = None
        u.is_deleted_state = not u.is_active

    recent_logs = list(
        UserAuditLog.objects.select_related("target_user", "actor").order_by("-created_at", "-id")[:25]
    )

    return {
        "user_rows": rows,
        "kpis": kpis,
        "recent_logs": recent_logs,
        "filters": {
            "estado": estado if estado in ("ALL", "ACTIVE", "DELETED") else "ALL",
            "rol": rol if rol in ("ALL", ROLE_STANDARD, ROLE_SUPERADMIN) else "ALL",
            "q": clean_q,
        },
        "role_choices": ROLE_CHOICES,
    }
