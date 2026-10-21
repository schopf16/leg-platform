"""Small summaries of metering points for the management pages."""

from django.urls import reverse

from leg_platform.community import numbers
from leg_platform.community.models import Direction, LegAssignment


def duplicate_summary(existing):
    """What "Create metering point" shows when the number exists (the number as every page groups it, D-109)."""
    found = (
        LegAssignment.objects.not_ended().filter(metering_point=existing).select_related("leg")
        .order_by("valid_from").first()
    )
    return {
        "number": numbers.format_metering_point_number(existing.number),
        "direction": str(Direction(existing.direction).label),
        "location": str(existing.location) if existing.location_id else "",
        "leg": found.leg.name if found else "",
        "status": str(existing.get_status_display()),
        "url": reverse("management:metering_points_detail", args=[existing.pk]),
    }
