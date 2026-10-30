"""The staff's side of an application (D-103): review, the registration at the grid operator, completion and
rejection. Every function takes an ``actor`` that is re-read from the database: an active staff account.

One transaction each, the account row locked first and then the application row (the order of the customer's side),
the state rechecked under the lock (no step twice or out of order), history entries with ids, dates and field names only.

Nothing real exists before ``complete``: the buildings and points are draft rows. ``complete`` creates the ``Location``
(or attaches to the stored one), the ``MeteringPoint`` rows with the dates of the registration, and the person
assignments from the start date. The registration at the grid operator itself happens outside the portal.
"""

from dataclasses import dataclass
from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from leg_platform.accounts.models import MailJob
from leg_platform.community import (
    application_notices,
    archived,
    assignments,
    buildings,
    customer_messages,
    history,
    metering_points,
)
from leg_platform.community.application_common import ApplicationError, add_months, notify, staff_actor
from leg_platform.community.applications import START_AFTER_MONTHS, delete_application
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    REJECTION_NOTE_MAX_LENGTH,
    Application,
    ApplicationMeteringPoint,
    CustomerBuilding,
    Location,
    MeteringPoint,
)

State = Application.State
REJECTION_REASONS = ("rejected", "operator_rejected")


def _lock(application, states):
    """The application locked after its account, in one of ``states`` (else ``wrong_state``)."""
    user_model = get_user_model()
    person_id = Application.objects.filter(pk=getattr(application, "pk", None)).values_list("person_id", flat=True).first()
    if person_id is None:
        raise ApplicationError("not_found")
    user_model._base_manager.select_for_update().get(pk=person_id)
    fresh = Application.objects.select_for_update().get(pk=application.pk)
    if fresh.state not in states:
        raise ApplicationError("wrong_state")
    return fresh


class _NumberConflict(Exception):
    def __init__(self, person, numbers):
        super().__init__("number_conflict")
        self.person, self.numbers = person, numbers


def conflicting_numbers(application):
    """The numbers of the draft rows that cannot go to the grid operator: stored for a metering point already, or named by
    an application in flight that was submitted earlier (the later one yields, so exactly one of two simultaneous
    submissions goes on, whichever the staff handles first). The conflict is found before the registration, not only
    at ``complete``."""
    mine = (application.submitted_at, application.pk)
    found = []
    for row in ApplicationMeteringPoint.objects.filter(building__application=application):
        if MeteringPoint.objects.filter(number=row.number).exists():
            found.append(row.number)
            continue
        others = ApplicationMeteringPoint.objects.filter(
            number=row.number, building__application__state__in=Application.IN_FLIGHT_STATES
        ).exclude(building__application=application).values_list(
            "building__application__submitted_at", "building__application_id"
        )
        if any(submitted is None or (submitted, pk) < mine for submitted, pk in others):
            found.append(row.number)
    return found


def _check_numbers(application):
    numbers = conflicting_numbers(application)
    if numbers:
        raise _NumberConflict(application.person_id, numbers)


def _refuse_numbers(conflict):
    """After the rollback: the staff notice (it names the numbers), then ``number_exists``."""
    person = get_user_model()._base_manager.filter(pk=conflict.person).first()
    if person is not None:
        application_notices.record(person, application_notices.Kind.NUMBER_UNAVAILABLE, numbers=conflict.numbers)
    raise ApplicationError("number_exists", numbers=conflict.numbers)


def start_review(application, actor):
    """Submitted -> in review. ``number_exists`` if a number of the application conflicts (see ``conflicting_numbers``;
    a notice names the numbers)."""
    staff = staff_actor(actor)
    try:
        with transaction.atomic():
            fresh = _lock(application, (State.SUBMITTED,))
            _check_numbers(fresh)
            fresh.state = State.IN_REVIEW
            fresh.save(update_fields=["state", "updated_at"])
            history.record(fresh, HistoryEvent.APPLICATION_IN_REVIEW, staff)
    except _NumberConflict as conflict:
        _refuse_numbers(conflict)
    return fresh


def mark_operator_started(application, actor, started_on=None, today=None):
    """The registration at the grid operator was started on ``started_on`` (not in the future; default today). From then
    on only the staff can change the application and the customer cannot withdraw it."""
    staff = staff_actor(actor)
    today = today or timezone.localdate()
    started_on = started_on or today
    if started_on > today:
        raise ApplicationError("date_in_future")
    try:
        with transaction.atomic():
            fresh = _lock(application, Application.WITHDRAWABLE_STATES)
            for building in fresh.buildings.select_related("location"):
                if building.egid is not None:
                    try:
                        buildings.resolve(fresh.person_id, building.egid, building.current_address())
                    except buildings.Refusal:
                        raise ApplicationError("building_unavailable") from None
            _check_numbers(fresh)
            fresh.state = State.OPERATOR_STARTED
            fresh.operator_started_on = started_on
            fresh.operator_started_at = timezone.now()
            fresh.save(update_fields=["state", "operator_started_on", "operator_started_at", "updated_at"])
            history.record(
                fresh, HistoryEvent.APPLICATION_OPERATOR_STARTED, staff, {"started_on": started_on.isoformat()}
            )
    except _NumberConflict as conflict:
        _refuse_numbers(conflict)
    return fresh


def record_operator_confirmation(application, actor, confirmed_on, today=None):
    """The grid operator's confirmation arrived on ``confirmed_on`` (not in the future, not before the registration)."""
    staff = staff_actor(actor)
    today = today or timezone.localdate()
    if confirmed_on > today:
        raise ApplicationError("date_in_future")
    with transaction.atomic():
        fresh = _lock(application, (State.OPERATOR_STARTED,))
        if confirmed_on < fresh.operator_started_on:
            raise ApplicationError("date_before_start")
        fresh.state = State.OPERATOR_CONFIRMED
        fresh.operator_confirmed_on = confirmed_on
        fresh.save(update_fields=["state", "operator_confirmed_on", "updated_at"])
        history.record(
            fresh, HistoryEvent.APPLICATION_OPERATOR_CONFIRMED, staff, {"confirmed_on": confirmed_on.isoformat()}
        )
    return fresh


def earliest_completion_start(application):
    """The earliest start date the staff may set: three months after the registration at the grid operator."""
    return add_months(application.operator_started_on, START_AFTER_MONTHS)


@dataclass(frozen=True)
class CompletionDefaults:
    """What the completion dialog of the staff is filled with."""

    start_choice: str  # the customer's wish: "asap", "date" or "" (none recorded)
    desired_start_date: date | None  # the customer's date when ``start_choice`` is "date"
    earliest_start_date: date  # registration + 3 months, or (before the registration) today + 3 months
    registered: bool  # whether the earliest date rests on the real registration date
    suggested_start_date: date  # the field's value: the wish, never before the earliest date
    unconfirmed_addresses: int  # buildings whose address is not confirmed yet (new ones count)


def completion_defaults(application, actor, today=None):
    """The defaults for the staff's completion dialog: the customer's start wish (as soon as possible, or a date), the
    earliest allowed date (three months after the registration at the grid operator; today + 3 months while no
    registration date is recorded), the suggested field value (the wish, but never before the earliest date) and how
    many of the buildings are not confirmed yet (a new building is created "not yet confirmed" unless the staff confirms
    the addresses). Staff only (``not_allowed``); ``not_found``; ``wrong_state`` unless the grid operator's
    confirmation arrived (the state ``complete`` takes). Pure reading."""
    staff_actor(actor)
    today = today or timezone.localdate()
    row = Application.objects.filter(pk=getattr(application, "pk", None)).first()
    if row is None:
        raise ApplicationError("not_found")
    if row.state != State.OPERATOR_CONFIRMED:
        raise ApplicationError("wrong_state")
    registered = row.operator_started_on is not None
    earliest = add_months(row.operator_started_on if registered else today, START_AFTER_MONTHS)
    wish = row.desired_start_date if row.start_choice == Application.StartChoice.DATE else None
    suggested = max(wish, earliest) if wish is not None else earliest
    unconfirmed = 0
    for building in row.buildings.select_related("location"):
        location = building.location or buildings.existing_location(building.egid, building.current_address())
        if location is None or location.needs_confirmation:
            unconfirmed += 1
    return CompletionDefaults(
        start_choice=row.start_choice, desired_start_date=wish, earliest_start_date=earliest, registered=registered,
        suggested_start_date=suggested, unconfirmed_addresses=unconfirmed,
    )


def _new_location(building, confirm):
    location = Location(
        street=building.street, house_number=building.house_number, postal_code=building.postal_code,
        town=building.town, egid=building.egid, needs_confirmation=not confirm,
    )
    try:
        location.full_clean()
    except ValidationError:
        raise ApplicationError("building_unavailable") from None
    return location


def _insert(location):
    """Save a new location in a savepoint (so a lost race on the unique (EGID, address) does not poison the outer
    transaction)."""
    with transaction.atomic():
        location.save()


def _existing_for(building):
    """The stored location for the draft building (locked), or None; ``building_unavailable`` if it cannot be used."""
    address = (building.street, building.house_number, building.postal_code, building.town)
    existing = buildings.existing_location(building.egid, address)
    if existing is None:
        return None
    existing = Location.objects.select_for_update().get(pk=existing.pk)
    if existing.is_archived or (
        building.egid is not None and existing.egid is not None and existing.egid != building.egid
    ):
        raise ApplicationError("building_unavailable")
    # A building is shared by every customer of it: the one that exists is used, whoever else is there.
    if existing.egid is None and building.egid is not None:
        existing.egid = building.egid
        existing.save(update_fields=["egid", "updated_at"])
    return existing


def _location_for(building, staff, confirm):
    if building.location_id is not None:
        location = Location.objects.select_for_update().get(pk=building.location_id)
        try:
            archived.ensure_open(location, archived.LOCATION)
        except archived.Archived:
            raise ApplicationError("building_unavailable") from None
        return location
    existing = _existing_for(building)
    if existing is not None:
        return existing
    location = _new_location(building, confirm)
    try:
        _insert(location)
    except IntegrityError:
        # Two completions created the same new building at the same moment: the other one won; use its building
        # (a locking read sees the committed row also under the repeatable-read snapshot).
        existing = _existing_for(building)
        if existing is None:
            raise ApplicationError("building_unavailable") from None
        return existing
    history.record(location, HistoryEvent.CREATED, staff)
    if confirm:
        history.record(location, HistoryEvent.LOCATION_CONFIRMED, staff)
    return location


def complete(application, actor, start_date, *, today=None, confirm_addresses=False):
    """Operator confirmed -> completed: create the real rows and assign the points to the person from ``start_date``.

    ``start_date`` is not before three months after the registration at the grid operator (``start_too_early``).
    New locations are marked "not yet confirmed" unless ``confirm_addresses`` (then the address is locked at once).
    The points get the dates of the registration and the confirmation. ``number_exists`` if a number was stored
    meanwhile, ``building_unavailable`` for an archived building or one whose EGID contradicts the address; nothing is
    kept then. An existing building with the same EGID is used (buildings are shared by their customers)."""
    staff = staff_actor(actor)
    today = today or timezone.localdate()
    with transaction.atomic():
        fresh = _lock(application, (State.OPERATOR_CONFIRMED,))
        if start_date < earliest_completion_start(fresh):
            raise ApplicationError("start_too_early", earliest=earliest_completion_start(fresh))
        person = get_user_model()._base_manager.get(pk=fresh.person_id)
        created = 0
        # A fixed order (EGID, then id) so two completions at shared buildings take their locks in the same order.
        ordered = sorted(fresh.buildings.select_related("location"), key=lambda b: (b.egid or 0, b.pk))
        for building in ordered:
            location = _location_for(building, staff, confirm_addresses)
            if building.location_id is None:
                building.location = location
                building.save(update_fields=["location", "updated_at"])
            if building.name or building.description:
                # The customer's own words for the building; an empty field keeps what the account wrote before.
                stored = CustomerBuilding.objects.filter(person=person, location=location).first()
                old_name, old_description = (stored.name, stored.description) if stored else ("", "")
                buildings.save_building_details(
                    person, location, building.name or old_name, building.description or old_description
                )
            for row in building.metering_points.order_by("pk"):
                try:
                    point = metering_points.create_metering_point(
                        direction=row.direction, location=location, number=row.number, actor=staff, name=row.name,
                        pv_power_kwp=row.pv_power_kwp, battery_kwh=row.battery_kwh, wallbox_kw=row.wallbox_kw,
                    )
                except metering_points.DuplicateNumber:
                    raise ApplicationError("number_exists") from None
                point.registered_with_operator_on = fresh.operator_started_on
                point.confirmed_on = fresh.operator_confirmed_on
                point.start_date = start_date
                point.save(update_fields=["registered_with_operator_on", "confirmed_on", "start_date", "updated_at"])
                history.record(
                    point, HistoryEvent.REGISTERED_WITH_OPERATOR, staff,
                    {"registered_on": fresh.operator_started_on.isoformat()},
                )
                history.record(
                    point, HistoryEvent.CONFIRMATION_RECORDED, staff,
                    {"confirmed_on": fresh.operator_confirmed_on.isoformat(), "start_date": start_date.isoformat()},
                )
                try:
                    assignments.assign(assignments.PERSON, point, person, start_date, staff, today)
                except assignments.AssignmentError as error:
                    raise ApplicationError(error.code) from None
                row.metering_point = point
                row.save(update_fields=["metering_point", "updated_at"])
                created += 1
        fresh.state = State.COMPLETED
        fresh.completed_at = timezone.now()
        fresh.save(update_fields=["state", "completed_at", "updated_at"])
        history.record(
            fresh, HistoryEvent.APPLICATION_COMPLETED, staff,
            {"start_date": start_date.isoformat(), "metering_points": created},
        )
    return fresh


def reject(application, actor, reason="rejected", note=""):
    """Roll the application back (not completed): the staff rejects it (``reason`` ``rejected``: the LEG did not accept)
    or the grid operator did (``operator_rejected``). The draft rows are deleted; the account, its IBAN and every stored
    building stay. ``note`` is an optional free text of at most 300 characters for the customer (``ValidationError`` if
    longer or invalid); it is kept only in the one-time message, never in the history or the mail.

    The customer gets the mail ``application_rejected`` (or ``application_rejected_operator``: the reason-specific
    sentence) and a one-time message of the same kind (``customer_messages``)."""
    staff = staff_actor(actor)
    if reason not in REJECTION_REASONS:
        raise ApplicationError("wrong_state")
    note = buildings.clean_free_text(note if isinstance(note, str) else "", REJECTION_NOTE_MAX_LENGTH)
    operator = reason == "operator_rejected"
    with transaction.atomic():
        fresh = _lock(application, Application.IN_FLIGHT_STATES)
        person = get_user_model()._base_manager.get(pk=fresh.person_id)
        delete_application(fresh, reason, staff)
        customer_messages.record(
            person,
            customer_messages.Kind.APPLICATION_REJECTED_OPERATOR if operator else customer_messages.Kind.APPLICATION_REJECTED,
            note,
        )
        notify(
            MailJob.Kind.APPLICATION_REJECTED_OPERATOR if operator else MailJob.Kind.APPLICATION_REJECTED, person
        )
