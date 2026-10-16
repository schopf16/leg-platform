"""Small summaries of metering points for the pages of the management area (D-079)."""

from django.urls import reverse

from leg_platform.community.models import Direction, LegAssignment


def duplicate_summary(existing):
    """What the dialogs "Create metering point" show when the number exists: the number, the direction,
    the location, the LEG and the status of the existing metering point, and the address of its page."""
    leg = LegAssignment.objects.not_ended().filter(metering_point=existing).select_related("leg").order_by("valid_from").first()
    return {
        "number": existing.number,
        "direction": str(Direction(existing.direction).label),
        "location": str(existing.location) if existing.location_id else "",
        "leg": leg.leg.name if leg else "",
        "status": str(existing.get_status_display()),
        "url": reverse("management:metering_points_detail", args=[existing.pk]),
    }
