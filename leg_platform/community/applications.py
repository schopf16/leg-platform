"""The customer's application ("Anmeldung") from the draft to the submission (D-103).

Every function takes the **account** and reads its objects through it: an ``Application`` of another account is
``ApplicationError("not_found")``, an id of a building or point that is not in the application is ``not_found``. One
transaction per call, the account row locked first and then the application row (the order everywhere), history
entries with ids and field names only.

Draft rows, not real rows: the buildings and metering points of an application are ``ApplicationBuilding`` and
``ApplicationMeteringPoint`` (``models.Application``). A cancelled or rolled-back application therefore deletes only
its own rows and can never touch a ``Location`` or ``MeteringPoint``, another account's building, the account or the
IBAN.

Steps (``STEPS``): building, metering points, bank connection (only with a feed-in), contract, start. A step is
``DONE``, ``KNOWN`` ("already on file": a preselected building, a bank connection or contract stored before the
application started) or ``OPEN``; the start date is chosen only after the contract (never prefilled).
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from leg_platform.accounts import mail_queue
from leg_platform.accounts.models import MailJob
from leg_platform.community import addressing, buildings, contracts, history, numbers, own_iban
from leg_platform.community.application_common import (
    MAX_BUILDINGS_PER_APPLICATION,
    MAX_DRAFTS_PER_PERSON,
    MAX_OPEN_APPLICATIONS_PER_PERSON,
    ApplicationError,
    add_days,
    add_months,
    locked_customer,
)
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    BUILDING_DESCRIPTION_MAX_LENGTH,
    BUILDING_NAME_MAX_LENGTH,
    METERING_POINT_NAME_MAX_LENGTH,
    Application,
    ApplicationBuilding,
    ApplicationMeteringPoint,
    ContractAcceptance,
    Direction,
    Location,
    ManagementSettings,
    MeteringPoint,
    PersonAssignment,
)

BUILDING = "building"
METERING_POINTS = "metering_points"
BANK_CONNECTION = "bank_connection"
CONTRACT = "contract"
START = "start"
STEPS = (BUILDING, METERING_POINTS, BANK_CONNECTION, CONTRACT, START)

DONE = "done"
KNOWN = "known"
OPEN = "open"

# The start date is not before this many months after today (the registration at the grid operator happens later, so
# this is the best the portal knows; ``application_staff.complete`` checks against the real registration date).
START_AFTER_MONTHS = 3

# Figures above these values only give a warning (proposals; they never block).
PLAUSIBLE_MAXIMUM = {"pv_power_kwp": Decimal(1000), "battery_kwh": Decimal(2000), "wallbox_kw": Decimal(1000)}

UNSET = object()


@dataclass(frozen=True)
class Step:
    key: str
    applicable: bool
    state: str
    available: bool = True


@dataclass(frozen=True)
class Progress:
    steps: tuple
    done: int
    total: int
    next_step: str | None

    def step(self, key):
        return next(step for step in self.steps if step.key == key)


@dataclass(frozen=True)
class BuildingChoice:
    building: ApplicationBuilding
    existing: bool  # attached to a building that is already stored: tell the customer
    review: bool = False  # the address is not in the directory (yet): kept as typed, without an EGID, for the LEG


@dataclass(frozen=True)
class AddedPoint:
    point: ApplicationMeteringPoint
    warnings: tuple  # [(field, "unusually_large")]


def earliest_start_date(today=None):
    """The earliest start date the customer may choose."""
    return add_months(today or timezone.localdate(), START_AFTER_MONTHS)


def plant_warnings(**figures):
    """``[(field, "unusually_large")]`` for figures above the plausible maximum (warnings only)."""
    return [
        (name, "unusually_large")
        for name, value in figures.items()
        if value is not None and name in PLAUSIBLE_MAXIMUM and Decimal(value) > PLAUSIBLE_MAXIMUM[name]
    ]


# ---- Reading ----


def open_applications(person):
    """The account's applications that are not completed, oldest first."""
    return Application.objects.filter(person=person, state__in=Application.OPEN_STATES)


def own_application(person, pk):
    """The application of this account; ``Application.DoesNotExist`` for any other (never reveals one)."""
    return Application.objects.get(pk=pk, person=person)


def returned_reason(application):
    """The reason the LEG gave when it returned the application for correction, for the banner on the draft; ``""`` if
    the draft was not returned (or was submitted again: the reason is cleared then). The caller got the application
    through ``own_application``; the value is read from the database, not from the object."""
    reason = (
        Application.objects.filter(
            pk=getattr(application, "pk", None), state__in=Application.DRAFT_STATES, returned_at__isnull=False
        )
        .values_list("returned_reason", flat=True)
        .first()
    )
    return reason or ""


def rollback_date(application, now=None):
    """The day on which the scheduler deletes the draft if the customer does nothing: the reminder day plus
    ``rollback_after_days`` for a paused draft, and the day the contract deadline lapses for a draft that waits only for
    the contract (the earlier of the two if both run); ``None`` for any other state or a day beyond the range of
    dates. For "continue, otherwise it is deleted on <date>"."""
    values = ManagementSettings.load()
    row = (
        Application.objects.filter(pk=getattr(application, "pk", None), state__in=Application.DRAFT_STATES)
        .values("state", "reminded_at", "contract_pending_since")
        .first()
    )
    if row is None:
        return None
    moments = []
    if row["state"] == Application.State.PAUSED and row["reminded_at"] is not None:
        moments.append(add_days(row["reminded_at"], values.rollback_after_days))
    if row["contract_pending_since"] is not None:
        moments.append(add_days(row["contract_pending_since"], values.contract_deadline_days))
    moments = [moment for moment in moments if moment is not None]
    return timezone.localdate(min(moments)) if moments else None


def next_deletion_date(person_id):
    """The earliest ``rollback_date`` of the account's paused drafts, or ``None`` (no paused draft, or no date). The
    mail worker asks for it when the reminder "not completed yet" is sent: the job carries no payload."""
    dates = [
        rollback_date(application)
        for application in Application.objects.filter(person_id=person_id, state=Application.State.PAUSED)
    ]
    dates = [day for day in dates if day is not None]
    return min(dates) if dates else None


def _draw_present(building, occupied):
    """A draw is entered in the building, or the account has one at the stored building already. ``occupied(location)``
    gives the directions the account has there."""
    rows = list(building.metering_points.all())
    if any(row.direction == Direction.CONSUMPTION for row in rows):
        return True
    return building.location is not None and Direction.CONSUMPTION in occupied(building.location)


def _start_valid(application, today):
    if application.start_choice == Application.StartChoice.ASAP:
        return application.desired_start_date is None
    if application.start_choice == Application.StartChoice.DATE:
        return application.desired_start_date is not None and application.desired_start_date >= earliest_start_date(today)
    return False


def progress(application, today=None):
    """The steps, how many are done and the next open one (wizard state computed from the stored rows)."""
    today = today or timezone.localdate()
    listed = list(application.buildings.select_related("location").prefetch_related("metering_points"))
    acceptance = contracts.acceptance_of(application.person, contracts.current_version())
    return _progress(
        application, listed, today,
        accepted_at=acceptance.accepted_at if acceptance is not None else None,
        has_iban=lambda: own_iban.has_iban(application.person),
        occupied=lambda location: buildings.occupied_directions(location, application.person_id, today),
    )


def progress_many(drafts, today=None):
    """``{application id: Progress}`` for many applications with a fixed number of queries (the list of the staff shows
    the progress of every draft; one ``progress`` per draft would be several queries each). The same computation as
    ``progress``; the buildings and their points must be prefetched on each application (``buildings`` with
    ``select_related("location")`` and ``prefetch_related("metering_points")``, ``select_related("person")``)."""
    today = today or timezone.localdate()
    drafts = list(drafts)
    if not drafts:
        return {}
    version = contracts.current_version()
    accepted = {}
    if version is not None:
        accepted = dict(
            ContractAcceptance.objects.filter(version=version, person_id__in={row.person_id for row in drafts})
            .values_list("person_id", "accepted_at")
        )
    # The directions each account has at the stored buildings its drafts name: one query for all of them.
    locations = {
        building.location_id for row in drafts for building in row.buildings.all() if building.location_id is not None
    }
    held = {}
    if locations:
        live = (
            PersonAssignment.objects.not_ended(today)
            .filter(
                person_id__in={row.person_id for row in drafts}, metering_point__location_id__in=locations,
                metering_point__is_archived=False,
            )
            .exclude(Q(metering_point__terminated_on__isnull=False, metering_point__terminated_on__lte=today))
            .values_list("person_id", "metering_point__location_id", "metering_point__direction")
        )
        for person_id, location_id, direction in live:
            held.setdefault((person_id, location_id), set()).add(direction)
    with_feed_in = {
        row.person_id for row in drafts
        if any(point.direction == Direction.FEED_IN for building in row.buildings.all()
               for point in building.metering_points.all())
    }
    with_iban = own_iban.accounts_with_iban(with_feed_in) if with_feed_in else set()
    result = {}
    for row in drafts:
        result[row.pk] = _progress(
            row, list(row.buildings.all()), today, accepted_at=accepted.get(row.person_id),
            has_iban=lambda row=row: row.person_id in with_iban,
            occupied=lambda location, row=row: held.get((row.person_id, location.pk), set()),
        )
    return result


def _progress(application, listed, today, *, accepted_at, has_iban, occupied):
    """The one computation behind ``progress`` and ``progress_many``. ``has_iban()`` is asked only for a feed-in;
    ``occupied(location)`` gives the directions the account has at a stored building."""
    rows = [row for building in listed for row in building.metering_points.all()]
    has_feed_in = any(row.direction == Direction.FEED_IN for row in rows)

    if not listed:
        building_state = OPEN
    elif any(building.preselected for building in listed):
        building_state = KNOWN
    else:
        building_state = DONE
    points_state = DONE if rows and all(_draw_present(building, occupied) for building in listed) else OPEN
    if has_feed_in and has_iban():
        bank_state = KNOWN if application.bank_known_at_start else DONE
    else:
        bank_state = OPEN
    if accepted_at is None:
        contract_state = OPEN
    else:
        contract_state = KNOWN if accepted_at < application.created_at else DONE
    start_state = DONE if _start_valid(application, today) else OPEN

    steps = (
        Step(BUILDING, True, building_state),
        Step(METERING_POINTS, True, points_state, available=building_state != OPEN),
        Step(BANK_CONNECTION, has_feed_in, bank_state, available=points_state != OPEN),
        Step(CONTRACT, True, contract_state, available=points_state != OPEN),
        Step(START, True, start_state, available=contract_state != OPEN),
    )
    applicable = [step for step in steps if step.applicable]
    next_step = next((step.key for step in applicable if step.state == OPEN), None)
    return Progress(steps, sum(step.state != OPEN for step in applicable), len(applicable), next_step)


def post_submit_progress(application):
    """The five steps of "Your application was submitted": ``[(key, state)]`` with state ``done``, ``next`` or ``open``.

    Keys: ``submitted``, ``review``, ``operator_registration``, ``operator_confirmation``, ``start``."""
    order = (
        Application.State.SUBMITTED, Application.State.IN_REVIEW, Application.State.OPERATOR_STARTED,
        Application.State.OPERATOR_CONFIRMED, Application.State.COMPLETED,
    )
    keys = ("submitted", "review", "operator_registration", "operator_confirmation", "start")
    if application.state in Application.DRAFT_STATES:
        reached = -1
    else:
        reached = order.index(application.state)
    result = []
    for index, key in enumerate(keys):
        if key == "submitted":
            state = DONE if reached >= 0 else OPEN
        elif index <= reached:
            state = DONE
        else:
            state = "next" if index == reached + 1 and reached >= 0 else OPEN
        result.append((key, state))
    return result


# ---- Internal helpers ----


def _lock_draft(person, application, states=Application.DRAFT_STATES):
    """The application locked, through the account; ``not_found`` for another account's, ``not_editable`` in a state
    the customer cannot edit."""
    try:
        fresh = Application.objects.select_for_update().get(pk=getattr(application, "pk", None), person=person)
    except Application.DoesNotExist:
        raise ApplicationError("not_found") from None
    if fresh.state not in states:
        raise ApplicationError("not_editable")
    return fresh


def _changed(application, now=None):
    """The customer changed the draft: the idle clock restarts, a pause ends, the contract clock and the wizard step
    follow the stored rows."""
    now = now or timezone.now()
    application.last_activity_at = now
    application.reminded_at = None
    if application.state == Application.State.PAUSED:
        application.state = Application.State.DRAFT
    current = progress(application)
    pending = (
        current.step(BUILDING).state != OPEN
        and current.step(METERING_POINTS).state != OPEN
        and (not current.step(BANK_CONNECTION).applicable or current.step(BANK_CONNECTION).state != OPEN)
        and current.step(CONTRACT).state == OPEN
    )
    application.contract_pending_since = (application.contract_pending_since or now) if pending else None
    application.wizard_step = current.next_step or START
    application.save(
        update_fields=["last_activity_at", "reminded_at", "state", "contract_pending_since", "wizard_step", "updated_at"]
    )


def _find_row(_draft, model, row_id, **lookup):
    try:
        return model.objects.select_for_update().get(pk=row_id, **lookup)
    except (model.DoesNotExist, TypeError, ValueError):
        raise ApplicationError("not_found") from None


def delete_application(application, reason, actor=None):
    """Roll the application back: one history entry (``reason`` is a fixed key, never a value) and the rows of the
    application are deleted. Only the application and its draft rows: never a ``Location``, ``MeteringPoint``, the
    account or the IBAN. A completed application is refused. Call inside a transaction that holds the row lock."""
    if application.state == Application.State.COMPLETED:
        raise ApplicationError("wrong_state")
    history.record(application, HistoryEvent.APPLICATION_ROLLED_BACK, actor, {"reason": reason})
    application.delete()


# ---- Draft ----


def start_draft(person, *, location=None, today=None):
    """Start a draft; with ``location`` (an own building: "add a metering point") the building is chosen and fixed.

    ``too_many_applications``, ``too_many_drafts``; for ``location``: ``not_found`` (not the account's building, the
    same answer for every foreign id), ``already_in_progress`` (an own application already names it),
    ``building_complete`` (draw and feed-in exist)."""
    today = today or timezone.localdate()
    now = timezone.now()
    with transaction.atomic():
        fresh = locked_customer(person)
        open_rows = Application.objects.filter(person=fresh, state__in=Application.OPEN_STATES)
        if open_rows.count() >= MAX_OPEN_APPLICATIONS_PER_PERSON:
            raise ApplicationError("too_many_applications")
        if open_rows.filter(state__in=Application.DRAFT_STATES).count() >= MAX_DRAFTS_PER_PERSON:
            raise ApplicationError("too_many_drafts")
        chosen = None
        if location is not None:
            try:
                chosen = Location.objects.select_for_update().get(pk=getattr(location, "pk", None))
            except Location.DoesNotExist:
                raise ApplicationError("not_found") from None
            if chosen.is_archived or not buildings.owns(fresh, chosen, today):
                raise ApplicationError("not_found")
            if ApplicationBuilding.objects.filter(
                location=chosen, application__person=fresh, application__state__in=Application.OPEN_STATES
            ).exists():
                raise ApplicationError("already_in_progress")
            if buildings.occupied_directions(chosen, fresh, today) >= {Direction.CONSUMPTION, Direction.FEED_IN}:
                raise ApplicationError("building_complete")
        application = Application.objects.create(
            person=fresh, last_activity_at=now, bank_known_at_start=own_iban.has_iban(fresh)
        )
        if chosen is not None:
            ApplicationBuilding.objects.create(
                application=application, location=chosen, egid=chosen.egid, preselected=True
            )
        history.record(application, HistoryEvent.APPLICATION_STARTED, fresh, {"preselected": chosen is not None})
        _changed(application, now)
    return application


def cancel(person, application):
    """The customer abandons the draft: it is rolled back (its rows deleted, nothing else). No mail: the customer did it."""
    with transaction.atomic():
        fresh = locked_customer(person)
        draft = _lock_draft(fresh, application)
        delete_application(draft, "cancelled", fresh)


def _take_back(person, application, states, detail=None):
    """Submitted application -> draft again with all its data, under the account's and the application's row lock (the
    staff's steps lock the same rows, so exactly one of the two wins). The cap of drafts applies; the staff's
    ``return_for_correction`` is exempt."""
    with transaction.atomic():
        fresh = locked_customer(person)
        locked = _lock_draft(fresh, application, states=states)
        if Application.objects.filter(person=fresh, state__in=Application.DRAFT_STATES).count() >= MAX_DRAFTS_PER_PERSON:
            raise ApplicationError("too_many_drafts")
        locked.state = Application.State.DRAFT
        locked.submitted_at = None
        history.record(locked, HistoryEvent.APPLICATION_WITHDRAWN, fresh, detail)
        _changed(locked)
        locked.save(update_fields=["submitted_at", "updated_at"])
    return locked


def withdraw(person, application):
    """Take a submitted application back, as long as the registration at the grid operator has not started: it becomes a
    draft again with all its data (``wrong_state`` later: only the staff can act then). A draft is created by this, so the
    cap of drafts per account applies (``too_many_drafts``; the staff's ``return_for_correction`` is exempt)."""
    return _take_back(person, application, Application.WITHDRAWABLE_STATES)


def reopen_for_change(person, application):
    """Change a submitted application that the LEG has not picked up yet (D-107): it becomes a draft again with all its
    data, the customer edits it in the wizard and submits it again (the queue position is the new submission's). Only
    state ``submitted`` qualifies; once the staff took it into review (or started the registration at the grid operator)
    the customer cannot change it any more (``review_started`` / ``not_editable``). The check is made under the row lock
    that ``application_staff.start_review`` takes too, so a click that races a review ends in exactly one of the two. There
        is no cap of changes (owner decision 2026-10-22); the wizard rate limit applies to the view."""
    try:
        return _take_back(person, application, (Application.State.SUBMITTED,), {"purpose": "change"})
    except ApplicationError as error:
        if error.code == "not_editable" and Application.objects.filter(
            pk=getattr(application, "pk", None), person=person, state=Application.State.IN_REVIEW
        ).exists():
            raise ApplicationError("review_started") from None
        raise


def go_to_step(person, application, step):
    """Move the wizard to an earlier or the next open step; later steps are ``step_not_available``."""
    with transaction.atomic():
        fresh = locked_customer(person)
        locked = _lock_draft(fresh, application)
        current = progress(locked)
        reachable = [s.key for s in current.steps if s.applicable]
        if step not in reachable:
            raise ApplicationError("step_not_available")
        limit = reachable.index(current.next_step) if current.next_step in reachable else len(reachable) - 1
        if reachable.index(step) > limit or not current.step(step).available:
            raise ApplicationError("step_not_available")
        locked.wizard_step = step
        locked.save(update_fields=["wizard_step", "updated_at"])
    return locked


# ---- Buildings ----


def choose_building(
    person, application, *, street, house_number, postal_code, town, egid=None, name="", description=""
):
    """Add the building of an address to the draft; returns ``BuildingChoice``.

    The EGID must be one the address directory offers for the typed address (``egid`` None takes the only candidate).
    A stored building with the same EGID is always attached to, whoever else uses it (``existing``, tell the customer;
    the customer sees only the own points there). ``building_unavailable`` is the neutral refusal for an archived
    building and for an EGID that contradicts the address (with a staff notice). Also: ``building_not_found``,
    ``ambiguous`` (``details["egids"]``), ``egid_not_offered``, ``directory_unavailable``, ``already_added``,
    ``already_in_progress`` (an own application names it), ``building_complete`` (the account has a draw and a feed-in
    there), ``too_many_buildings``; ``ValidationError`` for an address or text that fails the rules. ``name`` and
    ``description`` are the account's own words for the building."""
    typed = addressing.normalise_address(street, house_number, postal_code, town)
    probe = Location(street=typed[0], house_number=typed[1], postal_code=typed[2], town=typed[3])
    probe.clean_fields(exclude={"egid", "address_digest"})
    name = buildings.clean_free_text(name, BUILDING_NAME_MAX_LENGTH)
    description = buildings.clean_free_text(description, BUILDING_DESCRIPTION_MAX_LENGTH)
    egid = None if egid is None else buildings.normalise_egid(egid)
    # The directory is read before the transaction starts (no lock is held while it answers). An address that the
    # directory does not know (or cannot be asked about now: budget, circuit breaker, failure) is kept as typed, without
    # an EGID, for the review by the LEG; the customer goes on (``review``). Only without an EGID of the person's choice.
    review = False
    try:
        candidate, new_egid = buildings.offered_building(typed, egid, user=person)
    except ApplicationError as error:
        if egid is not None or error.code != "building_not_found" or not error.details.get("review"):
            raise
        candidate, new_egid, review = None, None, True
    address = buildings.official_address(candidate, typed)
    today = timezone.localdate()
    try:
        with transaction.atomic():
            fresh = locked_customer(person)
            draft = _lock_draft(fresh, application)
            if draft.buildings.count() >= MAX_BUILDINGS_PER_APPLICATION:
                raise ApplicationError("too_many_buildings")
            if new_egid is not None and draft.buildings.filter(egid=new_egid).exists():
                raise ApplicationError("already_added")
            if review and draft.buildings.filter(
                egid__isnull=True, street=address[0], house_number=address[1], postal_code=address[2], town=address[3]
            ).exists():
                raise ApplicationError("already_added")
            location, existing = buildings.resolve(fresh, new_egid, address)
            others = ApplicationBuilding.objects.filter(
                application__person=fresh, application__state__in=Application.OPEN_STATES
            ).exclude(application=draft)
            if location is not None:
                if draft.buildings.filter(location=location).exists():
                    raise ApplicationError("already_added")
                if others.filter(location=location).exists():
                    raise ApplicationError("already_in_progress")
                if buildings.occupied_directions(location, fresh, today) >= {Direction.CONSUMPTION, Direction.FEED_IN}:
                    raise ApplicationError("building_complete")
                # A stored building is attached with its own EGID (its address is known here, no directory needed).
                if new_egid is None:
                    new_egid = location.egid
                review = review and location.egid is None
            elif new_egid is not None and others.filter(egid=new_egid).exists():
                raise ApplicationError("already_in_progress")
            if location is None:
                row = ApplicationBuilding(
                    application=draft, egid=new_egid, name=name, description=description,
                    street=address[0], house_number=address[1], postal_code=address[2], town=address[3],
                )
            else:
                row = ApplicationBuilding(
                    application=draft, location=location, egid=new_egid, name=name, description=description
                )
            row.save()
            detail = {"building_id": row.pk, "existing": existing}
            if review:
                detail["needs_review"] = True
            history.record(draft, HistoryEvent.APPLICATION_BUILDING_SET, fresh, detail)
            _changed(draft)
    except buildings.Refusal as refusal:
        buildings.refuse(refusal, person)
    return BuildingChoice(row, existing, review)


def remove_building(person, application, building_id):
    """Remove a building (and its points) from the draft; a preselected building is fixed (``building_fixed``)."""
    with transaction.atomic():
        fresh = locked_customer(person)
        draft = _lock_draft(fresh, application)
        row = _find_row(draft, ApplicationBuilding, building_id, application=draft)
        if row.preselected:
            raise ApplicationError("building_fixed")
        row.delete()
        _changed(draft)


def set_draft_building_details(person, application, building_id, *, name, description=""):
    """The account's own name and description of a building in the draft (becomes its ``CustomerBuilding`` at
    completion; for a stored building of the account later: ``buildings.set_building_details``)."""
    name = buildings.clean_free_text(name, BUILDING_NAME_MAX_LENGTH)
    description = buildings.clean_free_text(description, BUILDING_DESCRIPTION_MAX_LENGTH)
    with transaction.atomic():
        fresh = locked_customer(person)
        draft = _lock_draft(fresh, application)
        row = _find_row(draft, ApplicationBuilding, building_id, application=draft)
        row.name, row.description = name, description
        row.save(update_fields=["name", "description", "updated_at"])
        _changed(draft)


# ---- Metering points ----


def _full_number(entered):
    values = ManagementSettings.load()
    if not values.identifier:
        raise ApplicationError("prefix_not_configured")
    return numbers.compose_metering_point_number(values.country_code, values.identifier, entered)


def _check_number(person, draft, number, ignore=None):
    """Raise for a number that cannot be used: ``number_in_application`` / ``number_own`` (the account's own: the page
    may say so) or the neutral ``Refusal`` for a number of another account."""
    in_application = ApplicationMeteringPoint.objects.filter(building__application=draft, number=number)
    if ignore is not None:
        in_application = in_application.exclude(pk=ignore.pk)
    if in_application.exists():
        raise ApplicationError("number_in_application")
    real = MeteringPoint.objects.filter(number=number).first()
    if real is not None:
        if PersonAssignment.objects.filter(person=person, metering_point=real).exists():
            raise ApplicationError("number_own")
        raise buildings.Refusal(buildings.ApplicationNoticeKind.NUMBER_UNAVAILABLE, numbers=[number])
    # Other accounts' drafts block nobody; their applications in flight do.
    in_flight = ApplicationMeteringPoint.objects.filter(
        number=number, building__application__state__in=Application.IN_FLIGHT_STATES
    ).exclude(building__application=draft)
    owners = set(in_flight.values_list("building__application__person_id", flat=True))
    if person.pk in owners:
        raise ApplicationError("number_own")
    if owners:
        raise buildings.Refusal(buildings.ApplicationNoticeKind.NUMBER_UNAVAILABLE, numbers=[number])


def _figures(direction, pv_power_kwp, battery_kwh, wallbox_kw):
    figures = {"pv_power_kwp": pv_power_kwp, "battery_kwh": battery_kwh, "wallbox_kw": wallbox_kw}
    if direction != Direction.FEED_IN and any(value is not None for value in figures.values()):
        raise ApplicationError("plant_data_feed_in_only")
    return figures


def add_metering_point(
    person, application, building_id, *, direction, entered_number, name="", pv_power_kwp=None, battery_kwh=None,
    wallbox_kw=None,
):
    """Add a draw or a feed-in to a building of the draft; returns ``AddedPoint`` (with warnings for odd figures).

    ``entered_number`` is the typed end of the number (D-102: zeros are added or leading zeros removed; the country and
    identifier come from the settings). One draw and at most one feed-in per building, counting the points a stored
    building already has (``direction_taken``). ``number_in_application`` / ``number_own`` for the account's own number,
    the neutral ``number_unavailable`` (and a staff notice) for any other account's. ``ValidationError`` for an invalid
    number or figure; ``prefix_not_configured``; ``plant_data_feed_in_only``."""
    if direction not in Direction.values:
        raise ApplicationError("direction_invalid")
    name = buildings.clean_free_text(name, METERING_POINT_NAME_MAX_LENGTH)
    figures = _figures(direction, pv_power_kwp, battery_kwh, wallbox_kw)
    number = _full_number(entered_number)
    today = timezone.localdate()
    try:
        with transaction.atomic():
            fresh = locked_customer(person)
            draft = _lock_draft(fresh, application)
            building = _find_row(draft, ApplicationBuilding, building_id, application=draft)
            taken = set(building.metering_points.values_list("direction", flat=True))
            if building.location is not None:
                taken |= buildings.occupied_directions(building.location, fresh, today)
            if direction in taken:
                raise ApplicationError("direction_taken")
            _check_number(fresh, draft, number)
            row = ApplicationMeteringPoint(building=building, direction=direction, number=number, name=name, **figures)
            row.clean_fields(exclude={"building", "metering_point"})
            row.save()
            _changed(draft)
    except buildings.Refusal as refusal:
        buildings.refuse(refusal, person)
    return AddedPoint(row, tuple(plant_warnings(**figures)))


def replace_metering_point(
    person, application, point_id, *, entered_number, name=UNSET, pv_power_kwp=UNSET, battery_kwh=UNSET,
    wallbox_kw=UNSET,
):
    """Change the number (and, when given, the name and figures) of a point of the draft; same checks as adding."""
    number = _full_number(entered_number)
    try:
        with transaction.atomic():
            fresh = locked_customer(person)
            draft = _lock_draft(fresh, application)
            row = _find_row(draft, ApplicationMeteringPoint, point_id, building__application=draft)
            _check_number(fresh, draft, number, ignore=row)
            row.number = number
            if name is not UNSET:
                row.name = buildings.clean_free_text(name, METERING_POINT_NAME_MAX_LENGTH)
            given = {"pv_power_kwp": pv_power_kwp, "battery_kwh": battery_kwh, "wallbox_kw": wallbox_kw}
            for field, value in given.items():
                if value is not UNSET:
                    setattr(row, field, value)
            _figures(
                row.direction, row.pv_power_kwp, row.battery_kwh, row.wallbox_kw
            )
            row.clean_fields(exclude={"building", "metering_point"})
            row.save()
            _changed(draft)
    except buildings.Refusal as refusal:
        buildings.refuse(refusal, person)
    return row


def remove_metering_point(person, application, point_id):
    """Remove a point from the draft (a draft point is not active, so no notice period applies)."""
    with transaction.atomic():
        fresh = locked_customer(person)
        draft = _lock_draft(fresh, application)
        row = _find_row(draft, ApplicationMeteringPoint, point_id, building__application=draft)
        row.delete()
        _changed(draft)


# ---- Bank connection, contract, start ----


def save_bank_connection(person, application, value):
    """The bank connection step: stores the account's IBAN (``own_iban.set_own_iban``: full validation, security event,
    mail to every address). Only with a feed-in in the draft (``step_not_available``). Returns whether it changed."""
    with transaction.atomic():
        fresh = locked_customer(person)
        draft = _lock_draft(fresh, application)
        if not ApplicationMeteringPoint.objects.filter(
            building__application=draft, direction=Direction.FEED_IN
        ).exists():
            raise ApplicationError("step_not_available")
        changed = own_iban.set_own_iban(fresh, value)
        _changed(draft)
    return changed


def accept_contract_step(person, application, version_id, *, agrees_contract, agrees_data_exchange):
    """The contract step: both ticks for the current version (``contracts.accept_contract``); returns the acceptance."""
    with transaction.atomic():
        fresh = locked_customer(person)
        draft = _lock_draft(fresh, application)
        acceptance = contracts.accept_contract(
            fresh, version_id, agrees_contract=agrees_contract, agrees_data_exchange=agrees_data_exchange
        )
        _changed(draft)
    return acceptance


def set_start(person, application, *, asap=False, desired_date=None, today=None):
    """The start: "as soon as possible" or a desired date not before ``earliest_start_date``. Only after the contract
    (``contract_missing``); nothing is prefilled. ``start_invalid`` for both or neither or a date that is too early."""
    today = today or timezone.localdate()
    if bool(asap) == (desired_date is not None):
        raise ApplicationError("start_invalid")
    if desired_date is not None and desired_date < earliest_start_date(today):
        raise ApplicationError("start_invalid", earliest=earliest_start_date(today))
    with transaction.atomic():
        fresh = locked_customer(person)
        draft = _lock_draft(fresh, application)
        if not contracts.has_accepted_current(fresh):
            raise ApplicationError("contract_missing")
        draft.start_choice = Application.StartChoice.ASAP if asap else Application.StartChoice.DATE
        draft.desired_start_date = None if asap else desired_date
        draft.save(update_fields=["start_choice", "desired_start_date", "updated_at"])
        _changed(draft)
    return draft


# ---- Submit ----


def missing_items(application, today=None):
    """What stops the submission: ``building``, ``metering_points``, ``bank_connection``, ``contract``, ``start``."""
    current = progress(application, today)
    return [step.key for step in current.steps if step.applicable and step.state == OPEN]


def submit(person, application, *, today=None):
    """Submit the draft: building with a draw, IBAN if there is a feed-in, the contract accepted and a start date.

    ``ApplicationError("incomplete", missing=[...])`` lists the open steps. Metering point numbers and EGIDs are checked
    again; another account's is the neutral refusal. The confirmation mail goes to every address of the account, but only
    at the first submission of the application (no history entry ``application_submitted`` yet); every later submission
    sends none."""
    today = today or timezone.localdate()
    try:
        with transaction.atomic():
            fresh = locked_customer(person)
            draft = _lock_draft(fresh, application)
            missing = missing_items(draft, today)
            if missing:
                raise ApplicationError("incomplete", missing=missing)
            for building in draft.buildings.select_related("location"):
                if building.egid is not None:
                    # Archived meanwhile, or an EGID that contradicts the address now: the neutral refusal.
                    buildings.resolve(fresh, building.egid, building.current_address())
                for row in building.metering_points.all():
                    _check_number(fresh, draft, row.number, ignore=row)
            now = timezone.now()
            draft.state = Application.State.SUBMITTED
            draft.submitted_at = now
            draft.reminded_at = None
            draft.contract_pending_since = None
            draft.iban_reminded_at = None
            draft.returned_at = None
            draft.returned_reason = ""
            draft.save(
                update_fields=[
                    "state", "submitted_at", "reminded_at", "contract_pending_since", "iban_reminded_at",
                    "returned_at", "returned_reason", "updated_at",
                ]
            )
            # The confirmation mail is sent once per application, at its first submission (D-107): a later submission
            # (after a change, a withdrawal or a return for correction) is a resubmission and sends no mail. The history
            # entry of every submission is kept (audit trail). Read before the new entry is written, under the row lock.
            first_submission = not history.entries_for(draft).filter(event=HistoryEvent.APPLICATION_SUBMITTED).exists()
            history.record(draft, HistoryEvent.APPLICATION_SUBMITTED, fresh)
            if first_submission:
                mail_queue.enqueue_for_account(MailJob.Kind.APPLICATION_SUBMITTED, fresh)
    except buildings.Refusal as refusal:
        buildings.refuse(refusal, person)
    return draft
