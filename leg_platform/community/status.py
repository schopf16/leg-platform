"""Metering point status, derived from four dates and the day (D-077, D-086); nothing is set by hand.

registered (no announcement date) -> notified -> confirmed (no start date yet) -> active (start date
reached); terminated once the termination date is reached (a future one changes nothing yet).
"""

from django.db import models
from django.db.models import Q
from django.utils.translation import pgettext_lazy


class MeteringPointStatus(models.TextChoices):
    # Context: lower case in the status lists, unlike other texts with the same English source.
    REGISTERED = "registered", pgettext_lazy("metering point status", "registered")
    NOTIFIED = "notified", pgettext_lazy("metering point status", "notified")
    CONFIRMED = "confirmed", pgettext_lazy("metering point status", "confirmed")
    ACTIVE = "active", pgettext_lazy("metering point status", "active")
    TERMINATED = "terminated", pgettext_lazy("metering point status", "terminated")


def derive_status(*, registered_with_operator_on, confirmed_on, start_date, terminated_on, today):
    """The status on ``today``. Every date may be None."""
    if terminated_on is not None and terminated_on <= today:
        return MeteringPointStatus.TERMINATED
    if confirmed_on is not None:
        if start_date is not None and start_date <= today:
            return MeteringPointStatus.ACTIVE
        return MeteringPointStatus.CONFIRMED
    if registered_with_operator_on is not None:
        return MeteringPointStatus.NOTIFIED
    return MeteringPointStatus.REGISTERED


def status_filter(status, today):
    """The database condition for a status on ``today``: same rules as ``derive_status``; the statuses exclude each other."""
    terminated = Q(terminated_on__isnull=False, terminated_on__lte=today)
    if status == MeteringPointStatus.TERMINATED:
        return terminated
    running = ~terminated
    confirmed = Q(confirmed_on__isnull=False)
    started = Q(start_date__isnull=False, start_date__lte=today)
    if status == MeteringPointStatus.ACTIVE:
        return running & confirmed & started
    if status == MeteringPointStatus.CONFIRMED:
        return running & confirmed & ~started
    if status == MeteringPointStatus.NOTIFIED:
        return running & Q(confirmed_on__isnull=True, registered_with_operator_on__isnull=False)
    if status == MeteringPointStatus.REGISTERED:
        return running & Q(confirmed_on__isnull=True, registered_with_operator_on__isnull=True)
    raise ValueError(f"Unknown status: {status!r}")
