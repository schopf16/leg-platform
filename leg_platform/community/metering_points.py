"""Metering points: creating, the steps towards the grid operator, archiving and deleting (D-078, D-079).

Creating (``standorte/VORGABEN.md``, ``messpunkte/VORGABEN.md``): the direction is required, the
number, the location and the LEG are optional, no person and no date. A metering point without a
number is known here but not yet at the grid operator. The LEG is assigned from the day of the entry (the
dialog asks for no date).

The steps (``messpunkte/detail/VORGABEN.md``): registered -> "notified" (announced to the grid operator) ->
confirmation (with or without a start date) -> a start date -> active (automatic, when the date is
reached; no scheduler) -> termination to a month end. Each step is one transaction that locks the
metering point, checks the status again on the server (a stale page cannot do a step twice or out of
order) and writes the history.
"""

import calendar
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from leg_platform.community import archived, assignments, history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import HistoryEntry, Leg, Location, MeteringPoint
from leg_platform.community.status import MeteringPointStatus


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
            if leg is not None:
                # Locked first, as everywhere (``assignments.assign``); an archived LEG is refused there.
                Leg.objects.select_for_update().get(pk=leg.pk)
            if location is not None:
                # A location that is archived is read-only: no metering point is created at it (D-085).
                archived.ensure_open(Location.objects.select_for_update().get(pk=location.pk), archived.LOCATION)
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


# The steps ------------------------------------------------------------------------------------

NOTIFY = "notify"
CONFIRM = "confirm"
START = "start"
LEG_MISSING = "leg_missing"


class StatusError(Exception):
    """A step that the rules refuse. ``code`` is one of: ``archived``, ``wrong_status``,
    ``date_in_future``, ``date_before_previous``, ``not_month_end``."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def next_step(metering_point, has_leg, today=None):
    """The next step of a metering point: ``NOTIFY``, ``CONFIRM``, ``START``, ``LEG_MISSING`` (a
    metering point needs a LEG before it is announced to the grid operator, so there is no step yet) or None.
    The list shows it for the quick filters (``messpunkte/VORGABEN.md``)."""
    if metering_point.is_archived:
        return None
    status = metering_point.status(today)
    if status == MeteringPointStatus.REGISTERED:
        return NOTIFY if has_leg else LEG_MISSING
    if status == MeteringPointStatus.NOTIFIED:
        return CONFIRM
    if status == MeteringPointStatus.CONFIRMED and metering_point.start_date is None:
        return START
    return None


def _step(metering_point, expected_status, today):
    """Lock the metering point and check that it is still in the status the step needs."""
    fresh = MeteringPoint.objects.select_for_update().get(pk=metering_point.pk)
    if fresh.is_archived:
        raise StatusError("archived")
    if fresh.status(today) != expected_status:
        raise StatusError("wrong_status")
    return fresh


def _not_after_today(day, today):
    if day > today:
        raise StatusError("date_in_future")


def mark_notified(metering_point, registered_on, actor=None, today=None):
    """"Als angemeldet setzen": the registration at the grid operator has happened on ``registered_on``
    (not in the future)."""
    today = today or timezone.localdate()
    with transaction.atomic():
        fresh = _step(metering_point, MeteringPointStatus.REGISTERED, today)
        _not_after_today(registered_on, today)
        fresh.registered_with_operator_on = registered_on
        fresh.save(update_fields=["registered_with_operator_on", "updated_at"])
        history.record(fresh, HistoryEvent.REGISTERED_WITH_OPERATOR, actor, {"registered_on": registered_on.isoformat()})
    return fresh


def record_confirmation(metering_point, confirmed_on, start_date=None, actor=None, today=None):
    """"Bestätigung erfassen": the grid operator confirmed on ``confirmed_on`` (not before the registration and
    not in the future). Without a start date the registration stays on standby; with one the status
    stays "confirmed" until the date is reached."""
    today = today or timezone.localdate()
    with transaction.atomic():
        fresh = _step(metering_point, MeteringPointStatus.NOTIFIED, today)
        _not_after_today(confirmed_on, today)
        if confirmed_on < fresh.registered_with_operator_on:
            raise StatusError("date_before_previous")
        fresh.confirmed_on = confirmed_on
        fresh.start_date = start_date
        fresh.save(update_fields=["confirmed_on", "start_date", "updated_at"])
        detail = {"confirmed_on": confirmed_on.isoformat()}
        if start_date is not None:
            detail["start_date"] = start_date.isoformat()
        history.record(fresh, HistoryEvent.CONFIRMATION_RECORDED, actor, detail)
    return fresh


def record_start_date(metering_point, start_date, actor=None, today=None):
    """"Startdatum erfassen": the start date of a confirmed registration that is on standby."""
    today = today or timezone.localdate()
    with transaction.atomic():
        fresh = _step(metering_point, MeteringPointStatus.CONFIRMED, today)
        if fresh.start_date is not None:
            raise StatusError("wrong_status")
        fresh.start_date = start_date
        fresh.save(update_fields=["start_date", "updated_at"])
        history.record(fresh, HistoryEvent.START_DATE_RECORDED, actor, {"start_date": start_date.isoformat()})
    return fresh


def is_month_end(day):
    return day.day == calendar.monthrange(day.year, day.month)[1]


def terminate(metering_point, terminated_on, actor=None, today=None):
    """"Kündigung erfassen": the date that the grid operator gives, always the end of a month and not before the
    start. A date in the future keeps the status "active" until it is reached."""
    today = today or timezone.localdate()
    with transaction.atomic():
        fresh = _step(metering_point, MeteringPointStatus.ACTIVE, today)
        if fresh.terminated_on is not None:
            raise StatusError("wrong_status")
        if not is_month_end(terminated_on):
            raise StatusError("not_month_end")
        if terminated_on < fresh.start_date:
            raise StatusError("date_before_previous")
        fresh.terminated_on = terminated_on
        fresh.save(update_fields=["terminated_on", "updated_at"])
        history.record(fresh, HistoryEvent.TERMINATION_RECORDED, actor, {"terminated_on": terminated_on.isoformat()})
    return fresh


# Archiving and deleting ---------------------------------------------------------------------------


class NotDeletable(Exception):
    """The metering point has a history: it is archived, not deleted."""


def set_archived(metering_point, archived, actor=None):
    with transaction.atomic():
        fresh = MeteringPoint.objects.select_for_update().get(pk=metering_point.pk)
        if fresh.is_archived != archived:
            fresh.is_archived = archived
            fresh.save(update_fields=["is_archived", "updated_at"])
            history.record(fresh, HistoryEvent.ARCHIVED if archived else HistoryEvent.UNARCHIVED, actor)
    return fresh


def can_delete(metering_point):
    """A metering point is deleted only without a history (``messpunkte/detail/VORGABEN.md``: at
    least one entry other than "Created" means archive instead)."""
    return not history.has_history(metering_point)


def delete(metering_point):
    """Delete a metering point that has no history. Raises ``NotDeletable``."""
    with transaction.atomic():
        fresh = MeteringPoint.objects.select_for_update().get(pk=metering_point.pk)
        if not can_delete(fresh):
            raise NotDeletable
        # Only the entry "Created" exists; it goes with the metering point.
        HistoryEntry.objects.filter(subject_type=history.METERING_POINT, subject_id=fresh.pk).delete()
        fresh.delete()


@dataclass(frozen=True)
class Assigned:
    """One assignment of a chain as the detail page shows it."""

    assignment: object
    current: bool  # valid today
    pending: bool  # starts later


def chain(kind, metering_point, today=None):
    """The assignments of a kind that are not ended (valid now or starting later), oldest first."""
    today = today or timezone.localdate()
    model = assignments.model_of(kind)
    found = list(
        model.objects.not_ended(today)
        .filter(metering_point=metering_point)
        .select_related("leg" if kind == assignments.LEG else "person")
        .order_by("valid_from")
    )
    return [Assigned(a, a.valid_from <= today, a.valid_from > today) for a in found]
