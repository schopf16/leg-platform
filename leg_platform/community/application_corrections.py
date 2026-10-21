"""The staff's corrections of an application in flight (D-109): the address of a building and the metering points.

The LEG checks what the customer entered and often has to correct a typing mistake: a wrong address, a wrong number,
a wrong direction or plant figure. These functions do that for the draft rows of an application while the LEG works on
it (``Application.CORRECTABLE_STATES``: submitted, in review, registration at the grid operator started, confirmed by the
grid operator). A draft is the customer's (and is refused: ``wrong_state``), a completed application has real rows (the
pages of the locations and metering points change those).

Every function takes the ``actor`` (re-read from the database: an active staff account, ``not_allowed`` otherwise),
the application and the id of one of its rows; the row is found through the application (``not_found`` for any other).

* **The same rules as the customer's wizard** (``applications``, ``buildings``): the address is checked like
  ``applications.choose_building`` (the four parts, the EGID that the directory offers for the address, a stored
  building attached to, the duplicates of the application and of the account's other applications); a metering point
  like ``add_metering_point`` / ``replace_metering_point`` (the number rule of D-102, the number free for this account,
  one draw and one feed-in per building, plant data only for a feed-in) plus what the submission requires (a feed-in
  needs a bank connection of the account, a building keeps its draw).
* **One transaction, the account row and then the application row locked** (the order of the customer's and the staff's
  services: ``application_staff._lock``), the state read under the lock. The customer may change a submitted
  application (D-107, ``applications.reopen_for_change``): exactly one of the two clicks wins; the staff's loses with
  ``wrong_state`` when the customer got there first, and the customer's reads the corrected rows when the staff got there
  first. The dialog carries the **token** of the row it showed (``building_token`` / ``point_token``): if the row is not
  the same any more under the lock (the customer changed and submitted it again, or another staff member corrected it) the
  correction is refused with ``changed`` and nothing is saved.
* **The audit trail**: one history entry naming the row and the changed fields (never a value: D-077, D-103, the owner's
  design "Vorgänge" section 5) and one security log event (``ACCOUNT_DATA_CHANGED_BY_STAFF``: application number and
  field names only, no personal data, D-018). Nothing is written when nothing changed (``unchanged``).
* No mass assignment: the fields that can change are the named keyword arguments; no id, state or owner is taken from
  the caller.
"""

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent
from leg_platform.community import addressing, application_staff, applications, buildings, history, numbers, own_iban
from leg_platform.community.application_common import ApplicationError, staff_actor
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Application,
    ApplicationBuilding,
    ApplicationMeteringPoint,
    Direction,
    Location,
    ManagementSettings,
)

BUILDING_FIELDS = ("street", "house_number", "postal_code", "town", "egid")
FIGURE_FIELDS = ("pv_power_kwp", "battery_kwh", "wallbox_kw")
POINT_FIELDS = ("number", "direction", *FIGURE_FIELDS)


@dataclass(frozen=True)
class Corrected:
    """The result of a correction: the row, the names of the changed fields and the warnings about odd figures."""

    row: object
    fields: tuple
    warnings: tuple = ()


def _token(values):
    """A short digest of the values a dialog showed; a decimal counts by its value, not by its scale."""
    parts = [
        format(value.normalize(), "f") if isinstance(value, Decimal) else ("" if value is None else str(value))
        for value in values
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]


def building_token(row):
    """What the dialog of a building shows, as a short token (no secret: it only detects a change in between)."""
    return _token((row.location_id, row.egid, row.street, row.house_number, row.postal_code, row.town))


def point_token(row):
    """What the dialog of a metering point shows, as a short token."""
    return _token((row.number, row.direction, row.pv_power_kwp, row.battery_kwh, row.wallbox_kw))


def correctable(application):
    """Whether the staff may correct this application now (the state; the service checks again under the lock)."""
    return application.state in Application.CORRECTABLE_STATES


def _find(model, row_id, application):
    """The row of this application, locked; ``not_found`` for any other (never reveals one)."""
    lookup = {"building__application": application} if model is ApplicationMeteringPoint else {"application": application}
    try:
        return model.objects.select_for_update().get(pk=row_id, **lookup)
    except (model.DoesNotExist, TypeError, ValueError):
        raise ApplicationError("not_found") from None


def _check_state(application):
    """A cheap look at the state before the directory is asked (a request to the outside is not made for a draft or a
    completed application); the decision is made again under the lock."""
    state = Application.objects.filter(pk=getattr(application, "pk", None)).values_list("state", flat=True).first()
    if state is None:
        raise ApplicationError("not_found")
    if state not in Application.CORRECTABLE_STATES:
        raise ApplicationError("wrong_state")


def _record(application, event, row_key, row, fields, staff, person):
    """The history entry and the security log event of one correction, in the transaction of the change."""
    history.record(application, event, staff, {row_key: row.pk, "fields": list(fields)})
    security_log.record(
        SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF, account=person, actor=staff,
        detail=f"application {application.pk}, fields {', '.join(fields)}",
    )
    application.save(update_fields=["updated_at"])


# ---- The address of a building ----


def correct_building(
    application, actor, building_id, *, street, house_number, postal_code, town, egid=None, expected,
    allow_unlisted=False,
):
    """Correct the address of a new building of the application; returns ``Corrected``.

    Checked like ``applications.choose_building``: the four parts must be a valid address (``ValidationError``), the EGID
    must be one the address directory offers for the typed address (``egid`` None takes the only candidate; the answer
    carries the directory's own spelling of the address). ``ambiguous`` (``details["egids"]``) asks for the EGID;
    ``egid_not_offered``; ``directory_unavailable``. An address the directory does not know (``building_not_found``) is
    refused unless ``allow_unlisted``: then it is kept as typed, **without an EGID**, for the review (what the customer
    gets without being asked). A stored building with the same EGID or address is attached to, as in the wizard
    (``already_added`` / ``already_in_progress`` / ``direction_taken`` for the clashes); the address of a building that is
    stored already is the location's (``building_stored``: change it on the page of the location). ``expected`` is the
    ``building_token`` the dialog showed (``changed`` if the row differs now); ``unchanged`` if nothing differs.
    Also ``wrong_state``, ``not_found``, ``not_allowed``, ``building_unavailable`` (an archived building, an EGID that
    contradicts the address)."""
    staff = staff_actor(actor)
    _check_state(application)
    typed = addressing.normalise_address(street, house_number, postal_code, town)
    probe = Location(street=typed[0], house_number=typed[1], postal_code=typed[2], town=typed[3])
    probe.clean_fields(exclude={"egid", "address_digest"})
    egid = None if egid in (None, "") else buildings.normalise_egid(egid)
    # The directory is read before the transaction (no lock is held while it answers), as in the wizard.
    try:
        candidate, new_egid = buildings.offered_building(typed, egid, user=staff)
    except ApplicationError as error:
        if not (allow_unlisted and egid is None and error.code == "building_not_found"):
            raise
        candidate, new_egid = None, None
    address = buildings.official_address(candidate, typed)
    today = timezone.localdate()
    with transaction.atomic():
        fresh = application_staff._lock(application, Application.CORRECTABLE_STATES)
        person = get_user_model()._base_manager.get(pk=fresh.person_id)
        row = _find(ApplicationBuilding, building_id, fresh)
        if building_token(row) != expected:
            raise ApplicationError("changed")
        if row.location_id is not None:
            raise ApplicationError("building_stored")
        before = {name: getattr(row, name) for name in (*BUILDING_FIELDS, "location_id")}
        siblings = ApplicationBuilding.objects.filter(application=fresh).exclude(pk=row.pk)
        mine_elsewhere = ApplicationBuilding.objects.filter(
            application__person=person, application__state__in=Application.OPEN_STATES
        ).exclude(application=fresh)
        if new_egid is not None and siblings.filter(egid=new_egid).exists():
            raise ApplicationError("already_added")
        if new_egid is None and siblings.filter(
            egid__isnull=True, street=address[0], house_number=address[1], postal_code=address[2], town=address[3]
        ).exists():
            raise ApplicationError("already_added")
        try:
            location, _existing = buildings.resolve(person, new_egid, address)
        except buildings.Refusal:
            raise ApplicationError("building_unavailable") from None
        if location is not None:
            if siblings.filter(location=location).exists():
                raise ApplicationError("already_added")
            if mine_elsewhere.filter(location=location).exists():
                raise ApplicationError("already_in_progress")
            taken = buildings.occupied_directions(location, person, today)
            if any(point.direction in taken for point in row.metering_points.all()):
                raise ApplicationError("direction_taken")
            # A stored building is attached with its own EGID (its address is known here).
            if new_egid is None:
                new_egid = location.egid
            row.location, row.egid = location, new_egid
            row.street = row.house_number = row.postal_code = row.town = ""
        else:
            if new_egid is not None and mine_elsewhere.filter(egid=new_egid).exists():
                raise ApplicationError("already_in_progress")
            row.egid = new_egid
            row.street, row.house_number, row.postal_code, row.town = address
        after = {name: getattr(row, name) for name in (*BUILDING_FIELDS, "location_id")}
        changed = [name for name in (*BUILDING_FIELDS, "location_id") if before[name] != after[name]]
        if not changed:
            raise ApplicationError("unchanged")
        row.save()
        fields = tuple("location" if name == "location_id" else name for name in changed)
        _record(fresh, HistoryEvent.APPLICATION_BUILDING_CORRECTED, "building_id", row, fields, staff, person)
    return Corrected(row, fields)


# ---- A metering point ----


def correct_metering_point(
    application, actor, point_id, *, entered_number, direction, pv_power_kwp=None, battery_kwh=None,
    wallbox_kw=None, expected,
):
    """Correct the number, the direction and the plant figures of a metering point of the application; returns
    ``Corrected`` (with the warnings about unusually large figures).

    The same checks as the wizard: ``entered_number`` is the typed end of the number (D-102: white space is ignored, zeros
    are added or leading zeros removed, a complete pasted number loses the prefix; country and identifier come from the
    settings, so they cannot be changed here, as in the wizard); the number must be free (``number_in_application``,
    ``number_own``, ``number_taken``: a stored metering point or another account's application; the staff may be told,
    no notice is written); one draw and one feed-in per building (``direction_taken``; where the building has both and the
    customer entered them the wrong way round, choosing the direction of the other point **exchanges** the two directions:
    both rows are corrected and recorded, ``plant_data_swap`` if the other point has plant details that a draw cannot
    have: clear them there first); plant figures only for a feed-in
    (``plant_data_feed_in_only``); and what the submission requires: a feed-in needs the bank connection of the account
    (``iban_required``) and a building keeps its draw (``draw_required``). ``ValidationError`` for a number or a figure
    that fails the rules, ``prefix_not_configured``. The number is left as it is when the typed end is its own end (a
    stored prefix that differs from the settings stays). ``expected`` is the ``point_token`` the dialog showed (``changed``
    if the row differs now); ``unchanged`` if nothing differs."""
    staff = staff_actor(actor)
    if direction not in Direction.values:
        raise ApplicationError("direction_invalid")
    figures = applications._figures(direction, pv_power_kwp, battery_kwh, wallbox_kw)
    tail = numbers.normalise_entered_number(entered_number, ManagementSettings.load().metering_point_prefix)
    today = timezone.localdate()
    with transaction.atomic():
        fresh = application_staff._lock(application, Application.CORRECTABLE_STATES)
        person = get_user_model()._base_manager.get(pk=fresh.person_id)
        row = _find(ApplicationMeteringPoint, point_id, fresh)
        if point_token(row) != expected:
            raise ApplicationError("changed")
        before = {name: getattr(row, name) for name in POINT_FIELDS}
        if len(row.number) == numbers.METERING_POINT_LENGTH and row.number[numbers.HEAD_LENGTH :] == tail:
            number = row.number
        else:
            number = applications._full_number(tail)
            try:
                applications._check_number(person, fresh, number, ignore=row)
            except buildings.Refusal:
                raise ApplicationError("number_taken") from None
        swapped = None
        if direction != row.direction:
            building = row.building
            siblings = list(building.metering_points.exclude(pk=row.pk))
            occupied = (
                buildings.occupied_directions(building.location, person, today) if building.location_id else set()
            )
            holder = next((sibling for sibling in siblings if sibling.direction == direction), None)
            if holder is not None:
                # The building has a draw and a feed-in and the customer entered them the wrong way round: the two
                # exchange their directions (the set of directions of the building does not change, so the bank
                # connection and the draw that the submission required stay as they were).
                if row.direction == Direction.CONSUMPTION and any(
                    getattr(holder, name) is not None for name in FIGURE_FIELDS
                ):
                    raise ApplicationError("plant_data_swap")
                swapped = holder
            else:
                if direction in occupied:
                    raise ApplicationError("direction_taken")
                if direction == Direction.FEED_IN and not own_iban.has_iban(person):
                    raise ApplicationError("iban_required")
                if row.direction == Direction.CONSUMPTION and Direction.CONSUMPTION not in occupied:
                    raise ApplicationError("draw_required")
        old_direction = row.direction
        row.number, row.direction = number, direction
        for name, value in figures.items():
            setattr(row, name, value)
        row.clean_fields(exclude={"building", "metering_point"})
        changed = tuple(name for name in POINT_FIELDS if before[name] != getattr(row, name))
        if not changed:
            raise ApplicationError("unchanged")
        if swapped is not None:
            # (building, direction) is unique: the row steps aside, the other takes its direction, then the row follows.
            moment = timezone.now()
            ApplicationMeteringPoint.objects.filter(pk=row.pk).update(direction="", updated_at=moment)
            ApplicationMeteringPoint.objects.filter(pk=swapped.pk).update(direction=old_direction, updated_at=moment)
        row.save()
        _record(fresh, HistoryEvent.APPLICATION_POINT_CORRECTED, "point_id", row, changed, staff, person)
        if swapped is not None:
            history.record(
                fresh, HistoryEvent.APPLICATION_POINT_CORRECTED, staff, {"point_id": swapped.pk, "fields": ["direction"]}
            )
    return Corrected(row, changed, tuple(applications.plant_warnings(**figures)))
