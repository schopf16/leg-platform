"""Creating a metering point (D-078; ``standorte/VORGABEN.md``, "Messpunkt anlegen").

At a location: the direction is required, the number and the LEG are optional, no person and no
date. A metering point without a number is known here but not yet at the BKW. The LEG is assigned
from the day of the entry (the dialog asks for no date).
"""

from django.db import IntegrityError, transaction
from django.utils import timezone

from leg_platform.community import assignments, history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import MeteringPoint


class DuplicateNumber(Exception):
    """A metering point with this number exists: ``existing``."""

    def __init__(self, existing):
        super().__init__("duplicate_number")
        self.existing = existing


def find_by_number(number):
    return MeteringPoint.objects.filter(number=number).select_related("location").first() if number else None


def create_metering_point(*, direction, location=None, number=None, leg=None, actor=None, **details):
    """Create the metering point; ``details`` may hold ``pv_power_kwp``, ``battery_kwh``,
    ``wallbox_kw`` and ``remark``. Raises ``DuplicateNumber`` or ``ValidationError``."""
    metering_point = MeteringPoint(direction=direction, location=location, number=number or None, **details)
    # The uniqueness is checked below (a clear answer with the existing metering point), not by the form check.
    metering_point.full_clean(validate_unique=False)
    try:
        with transaction.atomic():
            if number:
                existing = find_by_number(metering_point.number)
                if existing is not None:
                    raise DuplicateNumber(existing)
            metering_point.save()
            history.record(metering_point, HistoryEvent.CREATED, actor)
            if leg is not None:
                assignments.assign(assignments.LEG, metering_point, leg, timezone.localdate(), actor)
    except IntegrityError:
        # Two requests with the same number at the same time: the database constraint decides.
        existing = find_by_number(metering_point.number)
        if existing is None:
            raise
        raise DuplicateNumber(existing) from None
    return metering_point
