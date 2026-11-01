"""Find the building (EGID) of an address: local directory first, then stored answers, then one search at swisstopo.

KONZEPT 3a (owner, binding): a building is identified by its EGID, not by the text of its address.

Order, each step only when the one before found nothing:

1. The local directory (the SQLite file, current generation; no internet).
2. A stored answer of an earlier external search (``models.BuildingLookup``): positive answers for
   ``POSITIVE_CACHE_DAYS``, negative ones for ``NEGATIVE_CACHE_DAYS``. It lives in the application database, so
   an import of the directory does not replace it.
3. One request to swisstopo (``swisstopo.search``), only for a signed-in account and within the rate limits
   (``limits.allow_external_lookup``). Only the address is sent, only to determine the EGID (owner approval
   2026-10-19). The answer is stored.

Only a definite "swisstopo does not know it" is a negative result: it sets the update flag (``jobs.set_update_flag``,
at most once) and tells the caller that every attempt was made. A search that could not be made (not signed in, rate
limit, no answer) is "unavailable": nothing is stored and no flag is set. No address is logged or stored in
clear text by this module (the stored answer is found by a keyed digest).
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import DatabaseError
from django.utils import timezone
from django.utils.connection import ConnectionDoesNotExist

from leg_platform.accounts.digests import keyed_digest

from . import jobs, limits, services, swisstopo
from .keys import key
from .limits import allow_external_lookup
from .models import Address, BuildingLookup, Directory, TransformerStation

logger = logging.getLogger(__name__)

# How long a stored answer is used. A negative one is short: the official directory is updated daily and the
# person may have corrected the entry; a positive one follows the cycle of the directory itself.
NEGATIVE_CACHE_DAYS = 7
POSITIVE_CACHE_DAYS = 90
RESULT_LIMIT = 10
# "Did you mean one of these addresses?" (``suggest``): at most this many candidates.
SUGGESTION_LIMIT = 5
PURPOSE = b"LEG-Platform building lookup v1"

FOUND = "found"
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"
INVALID = "invalid"

# Why a search is "unavailable" (``BuildingSearch.reason``).
REASON_NOT_SIGNED_IN = "not_signed_in"
REASON_LIMIT = "limit"
REASON_CIRCUIT_OPEN = "circuit_open"
REASON_FAILED = "failed"
SOURCE_DIRECTORY = "directory"
SOURCE_STORED = "stored"
SOURCE_SWISSTOPO = "swisstopo"


@dataclass(frozen=True)
class Building:
    """One candidate: a building (EGID) at an entrance (``entrance_id``, None if unknown) with the address it is listed under.

    The wizard shows "house number and entrance, EGID"; a person chooses when several candidates come back.
    """

    egid: int
    entrance_id: object  # int or None
    street: str
    house_number: str
    postal_code: str
    locality: str


@dataclass(frozen=True)
class BuildingSearch:
    """The result of ``find_buildings``.

    ``outcome``: ``found`` (``buildings`` has one or more candidates, one per EGID and entrance; several mean the
    person must choose),
    ``not_found`` (every attempt was made, the update flag is set or was set before: tell the person and note the
    address for the review by the LEG), ``unavailable`` (the external search could not be made now: ask again
    later; ``reason`` says why) or ``invalid`` (the input cannot be a complete address). ``source`` says where the
    buildings came from.

    ``needs_review``: the caller notes the address for the review by the LEG and lets the person go on. True for
    ``not_found`` and for an ``unavailable`` search that a signed-in person cannot repeat usefully now (budget used up,
    circuit breaker open, the service failed): nobody is stuck because swisstopo or the budget is. It is False for
    ``invalid`` and for a visitor who is not signed in.
    """

    buildings: tuple
    outcome: str
    source: str = ""
    reason: str = ""

    @property
    def needs_review(self):
        return self.outcome == NOT_FOUND or (
            self.outcome == UNAVAILABLE and self.reason in {REASON_LIMIT, REASON_CIRCUIT_OPEN, REASON_FAILED}
        )


def clean(street, house_number, postal_code, town):
    """The normalised input, or None if it cannot be a complete address of the official directory."""
    values = []
    for value, limit in (
        (street, swisstopo.STREET_LIMIT), (house_number, swisstopo.HOUSE_NUMBER_LIMIT),
        (postal_code, 4), (town, swisstopo.TOWN_LIMIT),
    ):
        if not isinstance(value, str) or len(value) > limit * 2:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            return None
        value = " ".join(value.split())
        if len(value) > limit:
            return None
        values.append(value)
    street, house_number, postal_code, town = values
    if not street or not swisstopo.HOUSE_NUMBER.fullmatch(house_number) or not swisstopo.POSTAL_CODE.fullmatch(postal_code):
        return None
    return street, house_number, postal_code, town


def _digest(street, house_number, postal_code, town):
    return keyed_digest(
        "\n".join((key(street), key(house_number), postal_code, key(town))), PURPOSE
    )


def _local(street, house_number, postal_code, town):
    """The buildings of the current generation of the directory (empty if it has none, or cannot be read)."""
    try:
        directory = Directory.objects.filter(pk=1).first()
        if directory is None or directory.row_count == 0 or not directory.has_egid:
            return []
        query = Address.objects.filter(
            generation=directory.generation, postal_code=postal_code, street_key=key(street),
            house_number_key=key(house_number), egid__isnull=False,
        )
        if town:
            query = query.filter(locality_key=key(town))
        rows = list(
            query.order_by("egid", "entrance_id")
            .values("egid", "entrance_id", "street", "house_number", "postal_code", "locality")
            .distinct()[:RESULT_LIMIT * 5]
        )
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("The building search in the address directory is unavailable.")
        return []
    return _unique(rows)


def _unique(rows):
    """One candidate per EGID and entrance, in that order, at most ``RESULT_LIMIT``."""
    found = {}
    for row in rows:
        entrance = row.get("entrance_id")
        found.setdefault((row["egid"], -1 if entrance is None else entrance), Building(
            egid=row["egid"], entrance_id=entrance, street=row["street"], house_number=row["house_number"],
            postal_code=row["postal_code"], locality=row["locality"],
        ))
    return tuple(found[identity] for identity in sorted(found))[:RESULT_LIMIT]


def _directory_answers():
    """Whether the local directory can answer a building question: published, with building identifiers, readable."""
    try:
        directory = Directory.objects.filter(pk=1).first()
    except (DatabaseError, ConnectionDoesNotExist):
        return False
    return directory is not None and directory.row_count > 0 and directory.has_egid


def local_egids(street, house_number, postal_code):
    """The EGIDs the local directory lists at this address: a set (empty when it lists none), or None when the directory
    cannot answer now (missing, empty, without identifiers, unreadable) or the input cannot be an address.

    The postal code fixes the town, so the town is not asked. Never an outbound request (D-105)."""
    cleaned = clean(street, house_number, postal_code, "")
    if cleaned is None or not _directory_answers():
        return None
    return {building.egid for building in _local(*cleaned)}


def station_name(street, house_number, postal_code):
    """The transformer station of the building(s) at this address, or "": one name only when every building the directory
    lists there has the same station (an address with two buildings of one station is answered, one with two stations or
    a building without a scanned station is not)."""
    egids = local_egids(street, house_number, postal_code)
    if not egids:
        return ""
    names = dict(TransformerStation.objects.filter(egid__in=egids).values_list("egid", "name"))
    found = {names.get(egid, "") for egid in egids}
    return found.pop() if len(found) == 1 else ""


def _stored(digest):
    """``(found, buildings)`` of a fresh stored answer, or ``(False, ())`` if there is none; ``found`` is True also for
    a stored negative answer."""
    try:
        stored = BuildingLookup.objects.filter(digest=digest).first()
    except DatabaseError:
        logger.warning("The stored building searches could not be read.")
        return False, ()
    if stored is None or stored.expires_at <= timezone.now():
        return False, ()
    try:
        return True, _unique(stored.buildings)
    except (KeyError, TypeError, AttributeError):
        # Not what this module wrote: treated as none.
        return False, ()


def _store(digest, buildings):
    days = POSITIVE_CACHE_DAYS if buildings else NEGATIVE_CACHE_DAYS
    try:
        BuildingLookup.objects.update_or_create(
            digest=digest, defaults={
                "expires_at": timezone.now() + timedelta(days=days),
                "buildings": [
                    {"egid": b.egid, "entrance_id": b.entrance_id, "street": b.street,
                     "house_number": b.house_number, "postal_code": b.postal_code, "locality": b.locality}
                    for b in buildings
                ],
            },
        )
    except DatabaseError:
        # The answer is still returned; it is only asked again next time.
        logger.warning("The result of a building search could not be stored.")


def delete_expired(now=None):
    """Delete the stored answers that are no longer used; returns the number deleted."""
    return BuildingLookup.objects.filter(expires_at__lte=now or timezone.now()).delete()[0]


def suggest(street, house_number, postal_code, town=""):
    """Buildings of addresses that look like the typed one (typing mistakes), at most ``SUGGESTION_LIMIT``.

    LOCAL DIRECTORY ONLY: never a stored answer lookup, never a request to swisstopo (KONZEPT 3a: the person confirms a
    suggestion first, the external search comes only after that). Empty when the typed address itself is in the
    directory, when the input is no complete address, when the directory has no identifiers or cannot be read. The
    approximate matching and its bounds are those of the address suggestions (``services.approximate_matches``)."""
    cleaned = clean(street, house_number, postal_code, town)
    if cleaned is None or _local(*cleaned):
        return ()
    street, house_number, postal_code, town = cleaned
    try:
        directory = Directory.objects.filter(pk=1).first()
        if directory is None or directory.row_count == 0 or not directory.has_egid:
            return ()
        rows, _kind = services.approximate_matches(
            Address.objects.filter(generation=directory.generation),
            {"street": street, "house_number": house_number, "postal_code": postal_code, "locality": town},
        )
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("The address suggestions of the building search are unavailable.")
        return ()
    found = {}
    for row in rows:
        for building in _local(row["street"], row["house_number"], row["postal_code"], row["locality"]):
            found.setdefault((building.egid, -1 if building.entrance_id is None else building.entrance_id), building)
        if len(found) >= SUGGESTION_LIMIT:
            break
    return tuple(found[identity] for identity in sorted(found))[:SUGGESTION_LIMIT]


def find_buildings(street, house_number, postal_code, town="", *, user=None):
    """The buildings (EGIDs) of an address, see the module text. ``user`` allows the external search when signed in."""
    cleaned = clean(street, house_number, postal_code, town)
    if cleaned is None:
        return BuildingSearch((), INVALID)
    buildings = _local(*cleaned)
    if buildings:
        return BuildingSearch(buildings, FOUND, SOURCE_DIRECTORY)
    digest = _digest(*cleaned)
    known, buildings = _stored(digest)
    if known:
        return BuildingSearch(buildings, FOUND if buildings else NOT_FOUND, SOURCE_STORED)
    # Only a signed-in, active account may cause an outbound request, and only within the budgets. While the
    # circuit breaker is open (the service failed repeatedly) nobody asks, and no budget is used up.
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return BuildingSearch((), UNAVAILABLE, reason=REASON_NOT_SIGNED_IN)
    if limits.circuit_open():
        return BuildingSearch((), UNAVAILABLE, reason=REASON_CIRCUIT_OPEN)
    if not allow_external_lookup(user):
        return BuildingSearch((), UNAVAILABLE, reason=REASON_LIMIT)
    try:
        found = swisstopo.search(*cleaned)
    except swisstopo.SearchFailure as failure:
        # Fixed sentence only, never the address or the answer.
        logger.warning("The building search at swisstopo failed: %s", failure)
        if failure.counts:
            limits.record_failure()
        return BuildingSearch((), UNAVAILABLE, reason=REASON_FAILED)
    limits.record_success()
    buildings = _unique(found)
    _store(digest, buildings)
    if buildings:
        return BuildingSearch(buildings, FOUND, SOURCE_SWISSTOPO)
    try:
        jobs.set_update_flag()
    except DatabaseError:
        logger.warning("The update flag of the address directory could not be set.")
    return BuildingSearch((), NOT_FOUND, SOURCE_SWISSTOPO)
