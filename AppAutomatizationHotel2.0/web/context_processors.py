from .models import CrossMatchResult


def pending_exceptions(request):
    """Open exceptions (both currencies) for the "Pendientes" badge in the sidebar."""
    if not request.user.is_authenticated:
        return {}
    count = (
        CrossMatchResult.objects.filter(management_status__code="PENDING")
        .exclude(system_result="MATCHED")
        .count()
    )
    return {"sidebar_pending_count": count}
