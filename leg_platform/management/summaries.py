"""Small summaries of metering points for the pages of the management area (D-079)."""

from django.urls import reverse

from leg_platform.community.models import Direction, LegAssignment


def duplicate_summary(existing):
    """What the dialogs "Create metering point" show when the number exists: the number, direction, status, the
    address, the LEG and the link to the page of the existing metering point (every staff account sees them,
    D-086)."""
    found = (
        LegAssignment.objects.not_ended().filter(metering_point=existing).select_related("leg")
        .order_by("valid_from").first()
    )
    return {
        "number": existing.number,
        "direction": str(Direction(existing.direction).label),
        "location": str(existing.location) if existing.location_id else "",
        "leg": found.leg.name if found else "",
        "status": str(existing.get_status_display()),
        "url": reverse("management:metering_points_detail", args=[existing.pk]),
    }
