from .models import CrossMatchResult
from .services.erp_editing import OPEN_STATUS_CODES


def pending_exceptions(request):
    """Open exceptions (both currencies) for the "Pendientes" badge in the sidebar."""
    if not request.user.is_authenticated:
        return {}
    count = (
        CrossMatchResult.objects.filter(management_status__code__in=OPEN_STATUS_CODES)
        .exclude(system_result="MATCHED")
        .count()
    )
    return {"sidebar_pending_count": count}
