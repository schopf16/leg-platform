"""Deregistration ("Abmeldung", D-103): a customer ends whole buildings or single metering points.

* **Notice period:** Gesellschaftsvertrag item 4 (owner document): four months to a month end. ``earliest_termination_date``
  adds four calendar months to the day of the request and takes the end of that month (never earlier than the period).
  This reading of "four months to month end" is the developer's; the staff confirms the real date.
* The customer chooses the earliest date or a later month end (never before the earliest, so never in the past).
* Only active points of the account can be deregistered. A point that is not active yet is withdrawn through its
  application (``applications.withdraw`` / ``cancel``), without a notice period.
* The staff confirms the date (``confirm``), which records the termination with ``metering_points.terminate``; the
  scheduler marks the deregistration effective when the date is reached and sends the mail
  (``application_deadlines.process``).
* ``deletion_blockers`` / ``deletion_released`` tell the account deletion whether the account may go (the hook itself is
  in ``accounts``): every running metering point needs a deregistration that was actively started.
* The customer's confirmation with the second factor or the password (D-062) is the page's job, not the service's.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import mail_queue
from leg_platform.accounts.models import MailJob
from leg_platform.community import history, metering_points, own_iban
from leg_platform.community.application_common import (
    MAX_OPEN_DEREGISTRATIONS_PER_PERSON,
    ApplicationError,
    add_months,
    locked_customer,
    month_end,
    notify,
    staff_actor,
)
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Application,
    Deregistration,
    DeregistrationItem,
    Direction,
    Location,
    MeteringPoint,
    PersonAssignment,
)
from leg_platform.community.status import MeteringPointStatus

NOTICE_MONTHS = 4
logger = logging.getLogger(__name__)
State = Deregistration.State
OPEN_STATES = (State.SUBMITTED, State.CONFIRMED)


def earliest_termination_date(on=None):
    """The earliest month end for a notice given on ``on`` (default today): four months later, to the month end."""
    return month_end(add_months(on or timezone.localdate(), NOTICE_MONTHS))


def _covered():
    """A subquery of the metering points that an open or finished deregistration names (never loaded into Python)."""
    return DeregistrationItem.objects.values("metering_point_id")


def eligible_points(person, today=None):
    """The account's metering points that can be deregistered now: assigned to it today, active, not archived, no
    termination date yet and in no deregistration."""
    today = today or timezone.localdate()
    candidates = MeteringPoint.objects.filter(
        pk__in=PersonAssignment.objects.in_force(today).filter(person=person).values("metering_point_id"),
        is_archived=False, terminated_on__isnull=True,
    ).exclude(pk__in=_covered()).select_related("location")
    return [point for point in candidates if point.status(today) == MeteringPointStatus.ACTIVE]


def request_deregistration(person, *, metering_point_ids=None, location_id=None, desired_date=None, today=None):
    """Request the deregistration of a whole building (``location_id``) or of single points (``metering_point_ids``).

    ``desired_date`` None means the earliest date; otherwise a month end not before it (``date_before_earliest``,
    ``not_month_end``). Points and buildings are found through the account's assignments: an id of anyone else's is
    ``not_found``. ``not_active_yet`` for a point of the account that is not active; ``nothing_to_deregister`` for a
    building without eligible points; ``iban_required`` when a feed-in is among the points and the account has no IBAN
    (KONZEPT 7.2); ``too_many_deregistrations``. Queues the confirmation mail to every address."""
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
        if Deregistration.objects.filter(person=fresh, state__in=OPEN_STATES).count() >= MAX_OPEN_DEREGISTRATIONS_PER_PERSON:
            raise ApplicationError("too_many_deregistrations")
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
            point.terminated_on is not None or point.status(today) != MeteringPointStatus.ACTIVE for point in locked
        ) or DeregistrationItem.objects.filter(metering_point__in=locked).exists():
            raise ApplicationError("not_active_yet")
        if any(point.direction == Direction.FEED_IN for point in locked) and not own_iban.has_iban(fresh):
            # A feed-in is paid out: the bank connection is needed before the deregistration (KONZEPT 7.2).
            raise ApplicationError("iban_required")
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
                metering_points.terminate(item.metering_point, confirmed_date, staff, today)
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


def deletion_blockers(person, today=None):
    """What stops the deletion of the account: ``running_metering_points`` (points of the account that are not
    terminated and in no started deregistration) and ``applications_in_flight`` (submitted, not completed)."""
    today = today or timezone.localdate()
    running = [
        point
        for point in MeteringPoint.objects.filter(
            pk__in=PersonAssignment.objects.not_ended(today).filter(person=person).values("metering_point_id"),
            is_archived=False, terminated_on__isnull=True,
        ).exclude(pk__in=_covered())
        if point.status(today) != MeteringPointStatus.TERMINATED
    ]
    return {
        "running_metering_points": len(running),
        "applications_in_flight": Application.objects.filter(
            person=person, state__in=Application.IN_FLIGHT_STATES
        ).count(),
    }


def deletion_released(person, today=None):
    """Whether the account deletion is released: every running metering point has a deregistration that was actively
    started (requested by the customer; the grid operator need not have confirmed) and no application is in flight."""
    return not any(deletion_blockers(person, today).values())
