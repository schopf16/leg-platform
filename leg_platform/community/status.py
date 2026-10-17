"""The status of a metering point, derived from its dates (D-077; ``standorte/VORGABEN.md`` section 8,
``messpunkte/detail/VORGABEN.md``).

Nothing is set by hand and no scheduler runs: the status follows from four dates and the day.

* registered: created, not yet announced to the grid operator (no date of announcement).
* notified ("angemeldet"): announced to the grid operator; the person has to confirm.
* confirmed: the grid operator confirmed; there is no start date yet or it is not reached.
* active: confirmed and the start date is reached (automatic).
* terminated: the date of the termination is reached. A termination that is recorded but lies in the future
  does not change the status until that day (D-086: the page says "Kündigung zum DD.MM.YYYY" meanwhile).
"""

from django.db import models
from django.db.models import Q
from django.utils.translation import pgettext_lazy


class MeteringPointStatus(models.TextChoices):
    # With a context: the words are written in lower case in the status lists of the pages
    # ("aktiv"), unlike other texts with the same English source.
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
    """The database condition for a status on ``today``: the same rules as ``derive_status``, so
    that a list can filter by status in the database. The statuses exclude each other."""
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
