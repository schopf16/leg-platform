"""Small summaries of metering points for the pages of the management area (D-079)."""

from django.urls import reverse

from leg_platform.community.models import Direction, LegAssignment

VIEW_POINTS = "community.view_meteringpoint"
VIEW_LOCATION = "community.view_location"
VIEW_LEG = "community.view_leg"


def duplicate_summary(existing, user):
    """What the dialogs "Create metering point" show when the number exists. The number is the one that the
    account typed; everything else of the existing metering point is shown only with the right to see it
    (D-084, M-4): direction, status and the link to its page with ``view_meteringpoint``, the address with
    ``view_location`` (and that one), the LEG with ``view_leg``. Without any of them the notice only says
    that the number exists (the value that is not allowed stays empty)."""
    can_see_point = user.has_perm(VIEW_POINTS)
    leg = ""
    if can_see_point and user.has_perm(VIEW_LEG):
        found = (
            LegAssignment.objects.not_ended().filter(metering_point=existing).select_related("leg")
            .order_by("valid_from").first()
        )
        leg = found.leg.name if found else ""
    return {
        "number": existing.number,
        "direction": str(Direction(existing.direction).label) if can_see_point else "",
        "location": str(existing.location)
        if can_see_point and existing.location_id and user.has_perm(VIEW_LOCATION) else "",
        "leg": leg,
        "status": str(existing.get_status_display()) if can_see_point else "",
        "url": reverse("management:metering_points_detail", args=[existing.pk]) if can_see_point else "",
    }
