"""Buildings on the customer's side (D-103): identity by EGID, shared buildings, the customer's own names and the lock of
the address.

* **Same EGID, same building, shared.** A building (``Location``) belongs to no account: every customer of a
  multi-family building attaches to it. Choosing a building by EGID that is stored always attaches to it (the customer is
  told), whoever else uses it; the customer sees and edits only the own metering points and drafts there. The neutral
  refusal (``building_unavailable``, with a staff notice) remains only for an archived building and for an EGID that
  contradicts the entered address; ``number_unavailable`` for a metering point number of another account.
* The name and description of a building are per customer (``CustomerBuilding``): one account never reads or changes
  another's. The address is shared and is locked once the staff confirmed it.
* The EGID lookup is ``addresses.buildings.find_buildings`` (imported only when needed); tests replace ``find_candidates``.
  Nothing here trusts an EGID that the lookup did not offer for the typed address.
"""

import logging
import unicodedata

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.community import addressing, application_notices, archived, history
from leg_platform.community.application_common import ApplicationError, locked_customer
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    BUILDING_DESCRIPTION_MAX_LENGTH,
    BUILDING_NAME_MAX_LENGTH,
    EGID_MAX,
    METERING_POINT_NAME_MAX_LENGTH,
    Application,
    ApplicationBuilding,
    CustomerBuilding,
    Location,
    MeteringPoint,
    PersonAssignment,
)

_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
INVALID_TEXT_MESSAGE = _("The text contains control characters.")
logger = logging.getLogger(__name__)


ApplicationNoticeKind = application_notices.Kind


class Refusal(Exception):
    """Inside a transaction: the neutral refusal; the caller records the notice after the rollback."""

    def __init__(self, kind, *, egid=None, numbers=()):
        super().__init__(kind)
        self.kind = kind
        self.egid = egid
        self.numbers = list(numbers)


def refuse(refusal, person):
    """Write the staff notice for a refusal (its own transaction) and raise the neutral error."""
    application_notices.record(person, refusal.kind, egid=refusal.egid, numbers=refusal.numbers)
    if refusal.kind == ApplicationNoticeKind.BUILDING_UNAVAILABLE:
        raise ApplicationError("building_unavailable")
    raise ApplicationError("number_unavailable")


def clean_free_text(text, max_length):
    """A name or description: white space collapsed, no control characters, at most ``max_length`` characters."""
    text = addressing.collapse(text)
    if any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in text):
        raise ValidationError(INVALID_TEXT_MESSAGE, code="invalid_text")
    MaxLengthValidator(max_length)(text)
    return text


def normalise_egid(value):
    """The EGID as a whole number from 1 to nine digits; ``ApplicationError("egid_invalid")`` otherwise."""
    text = str(value).strip() if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= EGID_MAX:
        raise ApplicationError("egid_invalid")
    return int(text)


def find_candidates(street, house_number, postal_code, town, user=None):
    """The buildings the address directory offers for an address: ``addresses.buildings.find_buildings`` (local
    directory, stored answers, then swisstopo for a signed-in ``user``). ``directory_unavailable`` if the module is not
    there or the search could not be made now for a visitor who is not signed in. A plain list of candidates is taken as
    it is (the first interface).

    A search that ``needs_review`` (nothing found after every attempt, or the external search is not possible now because
    of the budget, the circuit breaker or a failure) raises ``ApplicationError("building_not_found", review=True)``: the
    caller may let the person go on with the address as typed, for the review by the LEG (``choose_building``). A search
    that could not be made for the budget, the circuit breaker or a failure also sets the update flag of the directory
    (the existing note for the LEG, D-105), as a search that finds nothing does itself."""
    try:
        from leg_platform.addresses.buildings import UNAVAILABLE, find_buildings
    except ImportError:
        raise ApplicationError("directory_unavailable") from None
    result = find_buildings(street, house_number, postal_code, town, user=user)
    outcome = getattr(result, "outcome", None)
    if outcome is None:
        return list(result)
    if getattr(result, "needs_review", False):
        if outcome == UNAVAILABLE:
            _note_for_review()
        raise ApplicationError("building_not_found", review=True)
    if outcome == UNAVAILABLE:
        raise ApplicationError("directory_unavailable")
    # ``found`` has the buildings; ``invalid`` has none (``building_not_found``).
    return list(result.buildings)


def _note_for_review():
    """Set the update flag of the address directory (the daily run then refreshes it); never fails the customer."""
    try:
        from leg_platform.addresses import jobs

        jobs.set_update_flag()
    except (ImportError, DatabaseError):
        logger.warning("The update flag of the address directory could not be set.")


def suggest_candidates(street, house_number, postal_code, town=""):
    """Buildings of addresses that look like the typed one (typing mistakes), from the local directory only
    (``addresses.buildings.suggest``: no stored answer, no request to swisstopo). Empty if the module is not there."""
    try:
        from leg_platform.addresses.buildings import suggest
    except ImportError:
        return []
    return list(suggest(street, house_number, postal_code, town))


def candidate_value(candidate, name, default=""):
    """A field of a candidate, whether the directory returns objects or mappings."""
    if isinstance(candidate, dict):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def offered_building(typed, egid, user=None):
    """The candidate of the directory for the typed address: the one with ``egid``, or the only one when ``egid`` is
    None. ``ApplicationError``: ``building_not_found`` (none), ``ambiguous`` (several, ``details["egids"]``),
    ``egid_not_offered`` (the EGID does not belong to the address)."""
    candidates = find_candidates(*typed, user=user)
    # Several entrances of one building come back as several candidates with the same EGID: the building is one.
    unique = {}
    for candidate in candidates:
        unique.setdefault(normalise_egid(candidate_value(candidate, "egid")), candidate)
    egids = list(unique)
    candidates = list(unique.values())
    if egid is None:
        if not candidates:
            raise ApplicationError("building_not_found")
        if len(candidates) > 1:
            raise ApplicationError("ambiguous", egids=egids)
        return candidates[0], egids[0]
    if egid not in egids:
        raise ApplicationError("egid_not_offered")
    return candidates[egids.index(egid)], egid


def official_address(candidate, typed):
    """The address of the candidate where the directory gives one, else the typed one (normalised)."""
    street, house_number, postal_code, town = typed
    return addressing.normalise_address(
        candidate_value(candidate, "street", "") or street,
        candidate_value(candidate, "house_number", "") or house_number,
        candidate_value(candidate, "postal_code", "") or postal_code,
        candidate_value(candidate, "town", "") or candidate_value(candidate, "locality", "") or town,
    )


def claimant_ids(egid, location, today=None):
    """The accounts that use the building: a CURRENT person assignment (not ended) on one of its metering points, or an
    application in flight (submitted or later) that names it. Only ``update_own_address`` asks (a shared address is not
    changed by one customer); attaching to a building never asks."""
    today = today or timezone.localdate()
    condition = Q()
    if egid is not None:
        condition |= Q(egid=egid)
    if location is not None:
        condition |= Q(location=location)
    ids = set()
    if condition:
        ids |= set(
            ApplicationBuilding.objects.filter(
                condition, application__state__in=Application.IN_FLIGHT_STATES
            ).values_list("application__person_id", flat=True)
        )
    if location is not None:
        ids |= set(
            PersonAssignment.objects.not_ended(today).filter(metering_point__location=location)
            .values_list("person_id", flat=True)
        )
    return ids


def existing_location(egid, address):
    """The location with this EGID, else the one with this address, else None. Without an EGID (an address that is
    still to be reviewed) only the address is compared: ``egid=None`` must never match every location without an EGID."""
    by_egid = Location.objects.filter(egid=egid).first() if egid is not None else None
    return by_egid or Location.find_by_address(*address)


def resolve(person, egid, address):
    """``(location or None, existing)`` for a building the person wants: a stored location is always attached to (a
    building is shared by all its customers, whoever else uses it); ``None`` for a new building. Raises ``Refusal`` only
    for an archived location and for an EGID that contradicts the address (never without an EGID: the address of a
    building that the LEG still reviews is attached to the stored location with that address)."""
    location = existing_location(egid, address)
    if location is None:
        return None, False
    if location.is_archived or (egid is not None and location.egid is not None and location.egid != egid):
        raise Refusal(ApplicationNoticeKind.BUILDING_UNAVAILABLE, egid=egid)
    return location, True


def owns(person, location, today=None):
    """Whether the account has a CURRENT person assignment (valid now or starting later, not ended) on a metering point
    of the location. A former holder owns nothing: an ended assignment gives no access to anything (D-103)."""
    today = today or timezone.localdate()
    return PersonAssignment.objects.not_ended(today).filter(person=person, metering_point__location=location).exists()


owns_now = owns


def own_building(person, location_id, today=None):
    """The building through the account's current assignments; ``ApplicationError("not_found")`` for any other (a
    stranger's, a former holder's and a missing one alike)."""
    location = Location.objects.filter(pk=location_id).first()
    if location is None or not owns(person, location, today):
        raise ApplicationError("not_found")
    return location


def own_metering_point(person, point_id, today=None):
    """The metering point through the account's current assignment; ``ApplicationError("not_found")`` for any other (a
    point of someone else, a former holder's access and a missing one alike)."""
    today = today or timezone.localdate()
    point = MeteringPoint.objects.filter(
        pk=point_id, person_assignments__in=PersonAssignment.objects.not_ended(today).filter(person=person)
    ).first()
    if point is None:
        raise ApplicationError("not_found")
    return point


def occupied_directions(location, person, today):
    """The directions that the account itself already has at the building: its metering points there with a current
    assignment that are neither archived nor terminated. Other customers' points do not count (one draw and one feed-in
    per customer and building)."""
    mine = PersonAssignment.objects.not_ended(today).filter(person=person).values("metering_point_id")
    live = MeteringPoint.objects.filter(location=location, is_archived=False, pk__in=mine).exclude(
        Q(terminated_on__isnull=False, terminated_on__lte=today)
    )
    return set(live.values_list("direction", flat=True))


def others_use(person, location, today=None):
    """Whether another account has a CURRENT person assignment at the location or an application in flight naming it."""
    return bool(claimant_ids(location.egid, location, today) - {person.pk})


# ---- Names, details and the lock of the address (customer side, existing buildings) ----


def address_locked(location):
    """The address cannot be changed by the customer once the staff confirmed the location (D-085 style).

    A location that the staff entered themselves is not marked "not yet confirmed", so it is locked from the start."""
    return not location.needs_confirmation


class ProfileAddress:
    """What the profile shows next to the account's own address: the EGID of the building and whether the address is
    locked (``PROFIL.md`` section 1)."""

    def __init__(self, egid, locked):
        self.egid, self.locked = egid, locked


def profile_address_building(person):
    """The own building at the address of the account's profile, as a ``ProfileAddress``, or None.

    The account's postal address (profile) is compared with the address of the buildings the account has a metering
    point at (the normalised comparison of ``Location.find_by_address``). None if the account has no such building: the
    EGID and the lock are shown and enforced only where the address is a building of the account. Another account's
    building is never found (``owns``); an archived building does not count. Pure reading."""
    if not (person.street and person.swiss_postal_code and person.town):
        return None
    location = Location.find_by_address(person.street, person.house_number, str(person.swiss_postal_code), person.town)
    if location is None or location.is_archived or not owns(person, location):
        return None
    return ProfileAddress(egid=location.egid, locked=address_locked(location))


def _own_location(person, location_id):
    """The location locked, through the account's current assignments; not found for any other (a former holder
    included; an archived one is refused with ``Archived`` by the callers, D-085)."""
    try:
        location = Location.objects.select_for_update().get(pk=location_id)
    except Location.DoesNotExist:
        raise ApplicationError("not_found") from None
    if not owns(person, location):
        raise ApplicationError("not_found")
    return location


def building_details(person, location):
    """``(name, description)`` that this account gave the building, ``("", "")`` if none. Another account's words are
    never read: the row is found through the account. A former holder (no current assignment) reads nothing, unless an
    application of the own still names the building (a draft keeps its words on its own row, the label is read for it)."""
    mine = owns(person, location) or ApplicationBuilding.objects.filter(
        location=location, application__person=person, application__state__in=Application.OPEN_STATES
    ).exists()
    row = CustomerBuilding.objects.filter(person=person, location=location).first() if mine else None
    return (row.name, row.description) if row else ("", "")


def save_building_details(person, location, name, description):
    """Write the account's own name and description of a building (a row of ``CustomerBuilding``); the caller holds the
    transaction and the account lock. Returns the changed field names."""
    row, _created = CustomerBuilding.objects.select_for_update().get_or_create(person=person, location=location)
    changed = [field for field, value in (("name", name), ("description", description)) if getattr(row, field) != value]
    if changed:
        row.name, row.description = name, description
        row.save(update_fields=[*changed, "updated_at"])
    return changed


def set_building_details(person, location, *, name, description):
    """The account's own name and description of one of its buildings (always changeable; an archived building is
    read-only). Per account: other customers of the building neither see nor change them. Returns the changed field
    names. The words never reach the history."""
    name = clean_free_text(name, BUILDING_NAME_MAX_LENGTH)
    description = clean_free_text(description, BUILDING_DESCRIPTION_MAX_LENGTH)
    with transaction.atomic():
        locked_customer(person)
        fresh = _own_location(person, location.pk)
        archived.ensure_open(fresh, archived.LOCATION)
        changed = save_building_details(person, fresh, name, description)
        if changed:
            history.record(fresh, HistoryEvent.LOCATION_DETAILS_CHANGED, person, {"fields": changed})
    return changed


def set_metering_point_name(person, metering_point, name):
    """The customer's name of one of their metering points (changeable at any time while the account holds it: a CURRENT
    assignment; a former holder gets ``not_found``; an archived point is read-only)."""
    name = clean_free_text(name, METERING_POINT_NAME_MAX_LENGTH)
    with transaction.atomic():
        locked_customer(person)
        own_metering_point(person, metering_point.pk)
        try:
            fresh = MeteringPoint.objects.select_for_update().get(pk=metering_point.pk)
        except MeteringPoint.DoesNotExist:
            raise ApplicationError("not_found") from None
        if fresh.is_archived:
            raise ApplicationError("not_editable")
        if fresh.name == name:
            return False
        fresh.name = name
        fresh.save(update_fields=["name", "updated_at"])
        history.record(fresh, HistoryEvent.METERING_POINT_DATA_CHANGED, person, {"fields": ["name"]})
    return True


def update_own_address(person, location, *, street, house_number, postal_code, town, egid=None):
    """Change the address (and EGID) of an own building that the staff has not confirmed yet.

    ``ApplicationError("address_locked")`` once confirmed. The new EGID must be one the directory offers for the new
    address; an EGID or address that belongs to another location is the neutral refusal (the customer never merges
    locations). Returns the location."""
    typed = addressing.normalise_address(street, house_number, postal_code, town)
    egid = None if egid is None else normalise_egid(egid)
    candidate, new_egid = offered_building(typed, egid, user=person)
    new_address = official_address(candidate, typed)
    try:
        with transaction.atomic():
            locked_customer(person)
            fresh = _own_location(person, location.pk)
            archived.ensure_open(fresh, archived.LOCATION)
            if address_locked(fresh):
                raise ApplicationError("address_locked")
            for other in (Location.objects.filter(egid=new_egid).exclude(pk=fresh.pk).first(),
                          Location.find_by_address(*new_address)):
                if other is not None and other.pk != fresh.pk:
                    raise Refusal(ApplicationNoticeKind.BUILDING_UNAVAILABLE, egid=new_egid)
            if others_use(person, fresh):
                # A building that other customers use has one address for all: one customer does not change it.
                raise ApplicationError("address_locked")
            before = fresh.current_address()
            fresh.street, fresh.house_number, fresh.postal_code, fresh.town = new_address
            fresh.egid = new_egid
            fresh.full_clean()
            fresh.save()
            if before != fresh.current_address():
                history.record(fresh, HistoryEvent.ADDRESS_CHANGED, person)
    except Refusal as refusal:
        refuse(refusal, person)
    return fresh
