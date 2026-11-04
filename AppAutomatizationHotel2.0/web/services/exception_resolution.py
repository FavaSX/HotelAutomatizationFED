"""
Manual resolution of reconciliation exceptions (the "Pendientes" inbox).

The accountant closes a pending CrossMatchResult with a reason (a RESOLVED_* ReconciliationStatus)
or reopens it. Every change is written to AuditResolutionLog, which is also where the accountant's
comment lives: CrossMatchResult.auditor_comment holds the Level 2 suggestion JSON and must not be touched.
"""

from django.db import transaction
from django.utils import timezone

from ..models import AuditResolutionLog, CrossMatchResult, ReconciliationStatus

RESOLVED_PREFIX = "RESOLVED_"
# Reasons that close an exception without fixing its source, so they need a written justification
COMMENT_REQUIRED_CODES = {"RESOLVED_ACCEPTED"}


class ResolutionError(ValueError):
    pass


def resolution_statuses():
    return ReconciliationStatus.objects.filter(code__startswith=RESOLVED_PREFIX).order_by("id")


def _log(match: CrossMatchResult, user, previous_status, new_status, comment: str) -> None:
    AuditResolutionLog.objects.create(
        cross_match_result=match, user=user,
        previous_status=previous_status, new_status=new_status, comment=comment,
    )


@transaction.atomic
def resolve_exception(match: CrossMatchResult, status_code: str, comment: str, user) -> None:
    if match.system_result == "MATCHED":
        raise ResolutionError("Este voucher ya está cuadrado; no hay nada que resolver.")
    new_status = resolution_statuses().filter(code=status_code).first()
    if new_status is None:
        raise ResolutionError("Selecciona un motivo de resolución válido.")
    comment = (comment or "").strip()
    if status_code in COMMENT_REQUIRED_CODES and not comment:
        raise ResolutionError(f"“{new_status.display_name}” requiere un comentario que lo justifique.")

    previous_status = match.management_status
    match.management_status = new_status
    match.resolved_by = user
    match.resolution_date = timezone.now()
    match.save(update_fields=["management_status", "resolved_by", "resolution_date"])
    _log(match, user, previous_status, new_status, comment)


@transaction.atomic
def reopen_exception(match: CrossMatchResult, comment: str, user) -> None:
    comment = (comment or "").strip()
    if not comment:
        raise ResolutionError("Indica por qué se reabre el caso.")
    pending_status = ReconciliationStatus.objects.get(code="PENDING")
    if match.management_status_id == pending_status.id:
        raise ResolutionError("El caso ya está pendiente.")

    previous_status = match.management_status
    match.management_status = pending_status
    match.resolved_by = None
    match.resolution_date = None
    match.save(update_fields=["management_status", "resolved_by", "resolution_date"])
    _log(match, user, previous_status, pending_status, comment)
