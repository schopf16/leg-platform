"""The participation status line(s) for the start page of a customer (D-103; design walkthrough W1-18).

Once the LEG has confirmed or completed an application, or while a deregistration runs, nothing on the start page told
the customer where he stands. ``status_lines`` answers with at most two plain lines **as data** (a kind and a date; the
page chooses the words). Nothing is mailed for a completion or a confirmed date (the owner wants fewer mails), so this
line is where the customer finds it.

Read through the account only; no names, addresses or numbers are part of a line.
"""

from dataclasses import dataclass
from datetime import date

from django.db.models import Q
from django.utils import timezone

from leg_platform.community.models import Application, Deregistration, PersonAssignment

# Kinds, in the order of importance (the first two that apply are returned).
DEREGISTRATION_RUNNING = "deregistration_running"  # the LEG confirmed the date; ``date`` is the last day
DEREGISTRATION_REQUESTED = "deregistration_requested"  # waits for the LEG's date; ``date`` is None
APPLICATION_AT_LEG = "application_at_leg"  # submitted or in review at the LEG; ``date`` is None
APPLICATION_AT_OPERATOR = "application_at_operator"  # registration at the grid operator started or confirmed
PARTICIPATION_STARTS = "participation_starts"  # a metering point starts later; ``date`` is the first start
PARTICIPATION_STARTED = "participation_started"  # taking part; ``date`` is the first start day
MAX_LINES = 2


@dataclass(frozen=True)
class StatusLine:
    kind: str
    date: date | None = None


def status_lines(person, today=None):
    """At most two ``StatusLine`` for the account, most important first; ``[]`` if there is nothing to say.

    1. A deregistration: confirmed (the last day) before waiting for the date.
    2. An application in flight: at the LEG, or at the grid operator.
    3. The own participation: the first start day if a metering point starts later, else since when it runs.
    A draft is not a status (the start page shows it on its own)."""
    today = today or timezone.localdate()
    lines = []

    confirmed = (
        Deregistration.objects.filter(person=person, state=Deregistration.State.CONFIRMED)
        .order_by("confirmed_date")
        .values_list("confirmed_date", flat=True)
        .first()
    )
    if confirmed is not None:
        lines.append(StatusLine(DEREGISTRATION_RUNNING, confirmed))
    elif Deregistration.objects.filter(person=person, state=Deregistration.State.SUBMITTED).exists():
        lines.append(StatusLine(DEREGISTRATION_REQUESTED))

    states = set(Application.objects.filter(person=person, state__in=Application.IN_FLIGHT_STATES).values_list("state", flat=True))
    if states & {Application.State.OPERATOR_STARTED, Application.State.OPERATOR_CONFIRMED}:
        lines.append(StatusLine(APPLICATION_AT_OPERATOR))
    elif states:
        lines.append(StatusLine(APPLICATION_AT_LEG))

    if len(lines) < MAX_LINES:
        mine = PersonAssignment.objects.not_ended(today).filter(
            Q(metering_point__terminated_on__isnull=True) | Q(metering_point__terminated_on__gt=today),
            person=person, metering_point__is_archived=False,
        )
        starts = mine.filter(valid_from__gt=today).order_by("valid_from").values_list("valid_from", flat=True).first()
        if starts is not None:
            lines.append(StatusLine(PARTICIPATION_STARTS, starts))
        else:
            since = mine.filter(valid_from__lte=today).order_by("valid_from").values_list("valid_from", flat=True).first()
            if since is not None:
                lines.append(StatusLine(PARTICIPATION_STARTED, since))
    return lines[:MAX_LINES]
