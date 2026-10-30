"""Deregistration ("Abmeldung", D-103): a customer ends whole buildings or single metering points.

* **Notice period:** Gesellschaftsvertrag item 4 (owner document): four months to a month end. ``earliest_termination_date``
  adds four calendar months to the day of the request and takes the end of that month (never earlier than the period).
  This reading of "four months to month end" is the developer's; the staff confirms the real date.
* The customer chooses the earliest date or a later month end (never before the earliest, so never in the past).
* Active points and points that are assigned but not active yet (registered, notified, confirmed with a later start) can
  be deregistered, the same way and with the same notice period from the request (owner decision 2026-10-29: the customer
  can always cancel). The staff sees a note on a point that is not active (the grid operator may have to be asked where
  to search it); ``confirm`` terminates it like an active one, so it is free again at the date.
* The staff confirms the date (``confirm``), which records the termination with ``metering_points.terminate``; the
  scheduler marks the deregistration effective when the date is reached and sends the mail
  (``application_deadlines.process``).
* The customer's confirmation with the second factor or the password (D-062) is the page's job, not the service's.
"""

import logging
from dataclasses import dataclass
from datetime import date

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import mail_queue
from leg_platform.accounts.models import MailJob
from leg_platform.community import history, metering_points
from leg_platform.community.application_common import (
    ApplicationError,
    add_months,
    locked_customer,
    month_end,
    notify,
    staff_actor,
)
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Deregistration,
    DeregistrationItem,
    Location,
    MeteringPoint,
    PersonAssignment,
)
from leg_platform.community.status import MeteringPointStatus

NOTICE_MONTHS = 4
logger = logging.getLogger(__name__)
State = Deregistration.State
OPEN_STATES = (State.SUBMITTED, State.CONFIRMED)
PENDING_STATUSES = (MeteringPointStatus.REGISTERED, MeteringPointStatus.NOTIFIED, MeteringPointStatus.CONFIRMED)


def earliest_termination_date(on=None):
    """The earliest month end for a notice given on ``on`` (default today): four months later, to the month end."""
    return month_end(add_months(on or timezone.localdate(), NOTICE_MONTHS))


@dataclass(frozen=True)
class ConfirmationDefaults:
    """What the confirmation dialog of the staff is filled with."""

    desired_date: date  # the date the customer asked for (the earliest one if he took the earliest)
    wants_earliest: bool
    earliest_date: date  # the earliest possible day on the day of the request (notice period)
    suggested_date: date  # the field's value: the desired date, or the end of this month if that has passed


def confirmation_defaults(deregistration, actor, today=None):
    """The defaults for the staff's confirmation dialog of a deregistration that waits for its date: the customer's
    desired date, the earliest date of the notice period and the suggested value (the desired date; if it lies in the
    past, the end of the current month, so the field is always a date ``confirm`` accepts). Staff only (``actor`` is
    re-read: ``not_allowed``); ``not_found`` for a missing one, ``wrong_state`` unless it still waits for the date."""
    staff_actor(actor)
    today = today or timezone.localdate()
    row = Deregistration.objects.filter(pk=getattr(deregistration, "pk", None)).first()
    if row is None:
        raise ApplicationError("not_found")
    if row.state != State.SUBMITTED:
        raise ApplicationError("wrong_state")
    suggested = row.desired_date if row.desired_date >= today else month_end(today)
    return ConfirmationDefaults(
        desired_date=row.desired_date, wants_earliest=row.wants_earliest, earliest_date=row.earliest_date,
        suggested_date=suggested,
    )


def _covered():
    """A subquery of the metering points that an open or finished deregistration names (never loaded into Python)."""
    return DeregistrationItem.objects.values("metering_point_id")


def eligible_points(person, today=None):
    """The account's metering points that can be deregistered now: assigned to it, not archived, no termination date yet
    and in no deregistration, and either active (assigned today) or not active yet (``is_not_active``: registered,
    notified, or confirmed with a later start; the assignment may start later)."""
    today = today or timezone.localdate()
    candidates = MeteringPoint.objects.filter(
        pk__in=PersonAssignment.objects.not_ended(today).filter(person=person).values("metering_point_id"),
        is_archived=False, terminated_on__isnull=True,
    ).exclude(pk__in=_covered()).select_related("location")
    in_force = set(PersonAssignment.objects.in_force(today).filter(person=person).values_list("metering_point_id", flat=True))
    return [
        point for point in candidates
        if (point.status(today) == MeteringPointStatus.ACTIVE and point.pk in in_force) or is_not_active(point, today)
    ]


def is_not_active(point, today=None):
    """Whether the point is assigned but not active yet (the staff gets a note, the notice period is the same)."""
    return point.status(today or timezone.localdate()) in PENDING_STATUSES


def request_deregistration(person, *, metering_point_ids=None, location_id=None, desired_date=None, today=None):
    """Request the deregistration of a whole building (``location_id``) or of single points (``metering_point_ids``).

    ``desired_date`` None means the earliest date; otherwise a month end not before it (``date_before_earliest``,
    ``not_month_end``). Points and buildings are found through the account's assignments: an id of anyone else's is
    ``not_found``. ``not_active_yet`` for a point of the account that cannot be deregistered (ended, archived or in a
    deregistration already); ``nothing_to_deregister`` for a building without such points. A missing IBAN of a feed-in
    and the number of open deregistrations never refuse it (owner decision 2026-10-29: the customer can always cancel; the
    staff sees the IBAN state in the process). Queues the confirmation mail to every address."""
    today = today or timezone.localdate()
    if (metering_point_ids is None) == (location_id is None):
        raise ApplicationError("not_found")
    earliest = earliest_termination_date(today)
    if desired_date is not None:
        if desired_date < earliest:
            raise ApplicationError("date_before_earliest", earliest=earliest)
        if desired_date != month_end(desired_date):
            raise ApplicationError("not_month_end")
    with transaction.atomic():
        fresh = locked_customer(person)
        own = {point.pk: point for point in eligible_points(fresh, today)}
        assigned = set(PersonAssignment.objects.filter(person=fresh).values_list("metering_point_id", flat=True))
        if location_id is not None:
            if not Location.objects.filter(pk=location_id, metering_points__pk__in=assigned).exists():
                raise ApplicationError("not_found")
            chosen = [point for point in own.values() if point.location_id == location_id]
            if not chosen:
                raise ApplicationError("nothing_to_deregister")
        else:
            wanted = set(metering_point_ids)
            if not wanted or not wanted <= assigned:
                raise ApplicationError("not_found")
            if not wanted <= set(own):
                raise ApplicationError("not_active_yet")
            chosen = [own[pk] for pk in sorted(wanted)]
        # The points are locked in id order, then checked once more (a staff step may have changed one).
        locked = list(MeteringPoint.objects.select_for_update().filter(pk__in=[p.pk for p in chosen]).order_by("pk"))
        if len(locked) != len(chosen) or any(
            point.terminated_on is not None
            or not (point.status(today) == MeteringPointStatus.ACTIVE or is_not_active(point, today))
            for point in locked
        ) or DeregistrationItem.objects.filter(metering_point__in=locked).exists():
            raise ApplicationError("not_active_yet")
        deregistration = Deregistration.objects.create(
            person=fresh, earliest_date=earliest, desired_date=desired_date or earliest,
            wants_earliest=desired_date is None,
        )
        DeregistrationItem.objects.bulk_create(
            [DeregistrationItem(deregistration=deregistration, metering_point=point) for point in locked]
        )
        history.record(
            deregistration, HistoryEvent.DEREGISTRATION_SUBMITTED, fresh,
            {"metering_points": len(locked), "desired_date": deregistration.desired_date.isoformat()},
        )
        mail_queue.enqueue_for_account(MailJob.Kind.DEREGISTRATION_SUBMITTED, fresh)
    return deregistration


def own_deregistration(person, pk):
    """The account's deregistration; ``Deregistration.DoesNotExist`` for any other."""
    return Deregistration.objects.get(pk=pk, person=person)


def confirm(deregistration, actor, confirmed_date, today=None):
    """The staff confirms the termination date (a month end, not in the past): records it at every point with
    ``metering_points.terminate`` (all or none; ``cannot_terminate``). The customer's account is locked first."""
    staff = staff_actor(actor)
    today = today or timezone.localdate()
    if confirmed_date != month_end(confirmed_date):
        raise ApplicationError("not_month_end")
    if confirmed_date < today:
        raise ApplicationError("date_before_earliest")
    with transaction.atomic():
        person_id = Deregistration.objects.filter(pk=getattr(deregistration, "pk", None)).values_list(
            "person_id", flat=True
        ).first()
        if person_id is None:
            raise ApplicationError("not_found")
        get_user_model()._base_manager.select_for_update().get(pk=person_id)
        fresh = Deregistration.objects.select_for_update().get(pk=deregistration.pk)
        if fresh.state != State.SUBMITTED:
            raise ApplicationError("wrong_state")
        for item in fresh.items.select_related("metering_point").order_by("metering_point_id"):
            try:
                metering_points.terminate(item.metering_point, confirmed_date, staff, today, allow_not_active=True)
            except metering_points.StatusError:
                raise ApplicationError("cannot_terminate") from None
        fresh.state = State.CONFIRMED
        fresh.confirmed_date = confirmed_date
        fresh.confirmed_at = timezone.now()
        fresh.save(update_fields=["state", "confirmed_date", "confirmed_at", "updated_at"])
        history.record(
            fresh, HistoryEvent.DEREGISTRATION_CONFIRMED, staff, {"confirmed_date": confirmed_date.isoformat()}
        )
    return fresh


def finish_due(today=None, failures=None):
    """Mark every confirmed deregistration whose date is reached as effective and queue the mail (only for an active
    account), once each (the state change and the mail job are one transaction). Returns the number finished. A row that
    fails is logged by the class of the error and appended to ``failures`` (a list); the others go on."""
    today = today or timezone.localdate()
    finished = 0
    for pk in list(
        Deregistration.objects.filter(state=State.CONFIRMED, confirmed_date__lte=today).values_list("pk", flat=True)
    ):
        try:
            with transaction.atomic():
                row = Deregistration.objects.select_for_update().filter(
                    pk=pk, state=State.CONFIRMED, confirmed_date__lte=today
                ).first()
                if row is None:
                    continue
                row.state = State.EFFECTIVE
                row.effective_at = timezone.now()
                row.save(update_fields=["state", "effective_at", "updated_at"])
                history.record(row, HistoryEvent.DEREGISTRATION_EFFECTIVE)
                notify(MailJob.Kind.DEREGISTRATION_EFFECTIVE, row.person)
                finished += 1
        except Exception as error:  # noqa: BLE001 - only its class is logged
            logger.error("A deregistration could not be finished (%s).", type(error).__name__)
            if failures is not None:
                failures.append(pk)
    return finished
