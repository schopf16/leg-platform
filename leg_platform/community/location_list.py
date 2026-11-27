"""Queries of the list of locations (D-078): search, filters, sorting and the metering point rows.

Pure query code, no request. The search finds what the list and detail page show (address, circuit,
metering point numbers, LEG names, person names), never the remarks.
"""

import re
from dataclasses import dataclass, field

from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from leg_platform.community import buildings, numbers
from leg_platform.community.models import (
    Application,
    ApplicationBuilding,
    ApplicationMeteringPoint,
    LegAssignment,
    Location,
    MeteringPoint,
    PersonAssignment,
)

OCCUPANCY_ALL = "all"
OCCUPANCY_OCCUPIED = "occupied"
OCCUPANCY_FREE = "free"
OCCUPANCY_EMPTY = "empty"
OCCUPANCY_ARCHIVED = "archived"

DEFAULT_SORT = "address"
MAX_WORDS = 5  # every word adds non-indexed LIKE conditions: the number of words is bounded, as in person_search


def pending_points():
    """The metering points of applications in flight (``Application.CORRECTABLE_STATES``) that are not stored yet."""
    return ApplicationMeteringPoint.objects.filter(
        building__application__state__in=Application.CORRECTABLE_STATES, metering_point__isnull=True
    )


def folded_buildings():
    """``{building id: location id}``: the buildings of applications in flight without a location whose EGID or address
    names a stored location they may be attached to (``buildings.existing_locations`` and ``buildings.usable``, the rule
    of the completion). The list shows their metering points under that location, not as a row of their own; an
    archived location or a contradicting EGID keeps the building apart. Two queries."""
    rows = ApplicationBuilding.objects.filter(
        application__state__in=Application.CORRECTABLE_STATES, location__isnull=True
    ).values_list("pk", "egid", "street", "house_number", "postal_code", "town")
    wanted = {pk: (egid, (street, number, postal_code, town)) for pk, egid, street, number, postal_code, town in rows}
    found = buildings.existing_locations(wanted)
    return {pk: location.pk for pk, location in found.items() if buildings.usable(location, wanted[pk][0])}


def _at_locations(points, folded):
    """``(location id, applicant id)`` of the pending ``points`` at a stored location, linked or folded."""
    rows = points.filter(Q(building__location__isnull=False) | Q(building_id__in=list(folded))).values_list(
        "building_id", "building__location_id", "building__application__person_id"
    )
    return [(location_id or folded[building_id], person_id) for building_id, location_id, person_id in rows]


@dataclass
class PendingHere:
    """The metering points in registration at a stored location: their number and their applicants (person ids)."""

    points: int = 0
    applicants: set = field(default_factory=set)


def pending_at_locations(folded):
    """``{location id: PendingHere}`` of the metering points in registration at a stored location (one query)."""
    found = {}
    for location_id, person_id in _at_locations(pending_points(), folded):
        here = found.setdefault(location_id, PendingHere())
        here.points += 1
        here.applicants.add(person_id)
    return found


def _occupants(pending, today):
    """``{location id: count}`` for the locations with points in registration: the persons with an assignment valid now
    or later and the applicants, each person once."""
    persons = {location_id: set(here.applicants) for location_id, here in pending.items()}
    assigned = PersonAssignment.objects.not_ended(today).filter(
        metering_point__location_id__in=list(pending), metering_point__is_archived=False
    ).values_list("metering_point__location_id", "person_id")
    for location_id, person_id in assigned:
        persons[location_id].add(person_id)
    return {location_id: len(ids) for location_id, ids in persons.items()}


def base_queryset(today=None, pending=None):
    """All locations with ``mp_count`` (metering points not archived), ``person_count`` (persons with an assignment
    valid now or later, each once), and from ``pending`` (``pending_at_locations``): ``pending_count`` (the metering
    points in registration there), ``point_count`` (``mp_count`` plus ``pending_count``: the column "Metering points"
    and its sort) and ``occupant_count`` (``person_count`` plus the applicants, each person once: the column
    "Occupancy", its filter and its sort)."""
    today = today or timezone.localdate()
    points = (
        MeteringPoint.objects.filter(location=OuterRef("pk"), is_archived=False)
        .order_by()
        .values("location")
        .annotate(n=Count("pk"))
        .values("n")
    )
    persons = (
        PersonAssignment.objects.not_ended(today)
        .filter(metering_point__location=OuterRef("pk"), metering_point__is_archived=False)
        .order_by()
        .values("metering_point__location")
        .annotate(n=Count("person", distinct=True))
        .values("n")
    )
    queryset = Location.objects.select_related("transformer_circuit").annotate(
        mp_count=Coalesce(Subquery(points, output_field=IntegerField()), Value(0)),
        person_count=Coalesce(Subquery(persons, output_field=IntegerField()), Value(0)),
    )
    pending = pending or {}
    occupants = _occupants(pending, today) if pending else {}
    return queryset.annotate(
        occupant_count=Case(
            *(When(pk=location_id, then=Value(count)) for location_id, count in occupants.items()),
            default=F("person_count"), output_field=IntegerField(),
        ),
        pending_count=Case(
            *(When(pk=location_id, then=Value(here.points)) for location_id, here in pending.items()),
            default=Value(0), output_field=IntegerField(),
        ),
    ).annotate(point_count=F("mp_count") + F("pending_count"))


def apply_occupancy(queryset, value):
    """The filter "Occupancy": only "archived" shows archived locations, every other value hides them."""
    if value == OCCUPANCY_ARCHIVED:
        return queryset.filter(is_archived=True)
    queryset = queryset.filter(is_archived=False)
    if value == OCCUPANCY_OCCUPIED:
        return queryset.filter(occupant_count__gt=0)
    if value == OCCUPANCY_FREE:
        return queryset.filter(point_count__gt=0, occupant_count=0)
    if value == OCCUPANCY_EMPTY:
        return queryset.filter(point_count=0, occupant_count=0)
    return queryset


def apply_circuit(queryset, grid_number):
    return queryset.filter(transformer_circuit__grid_number=grid_number) if grid_number else queryset


def apply_problems(queryset):
    return queryset.filter(address_correction_open=True)


def _pending_matches(condition, folded):
    """The ids of the stored locations with a metering point in registration that matches ``condition``."""
    return {location_id for location_id, _person_id in _at_locations(pending_points().filter(condition), folded)}


def apply_search(queryset, text, *, include_assignments=True, today=None, folded=None):
    """Every word (at most ``MAX_WORDS``) must match the address parts, the circuit (number, name), a metering point
    number and, with ``include_assignments``, a LEG name or a person name. Identifiers are compared in capitals. With
    ``folded`` (``folded_buildings``) the number and the applicant of a metering point in registration there match too."""
    today = today or timezone.localdate()
    pasted = numbers.pasted_number(text)
    if pasted is not None:
        # A number copied from a page (its blocks stand apart, D-109): the locations that have it.
        pending = _pending_matches(Q(number__contains=pasted), folded) if folded is not None else set()
        if pending or MeteringPoint.objects.filter(number__contains=pasted).exists():
            return queryset.filter(
                Exists(MeteringPoint.objects.filter(location=OuterRef("pk"), number__contains=pasted)) | Q(pk__in=pending)
            )
    for word in (text or "").split()[:MAX_WORDS]:
        upper = word.upper()
        condition = (
            Q(street__icontains=word) | Q(house_number__icontains=word) | Q(postal_code__icontains=word)
            | Q(town__icontains=word) | Q(address_addition__icontains=word)
            | Q(transformer_circuit__grid_number__contains=upper) | Q(transformer_circuit__name__icontains=word)
            | Exists(MeteringPoint.objects.filter(location=OuterRef("pk"), number__contains=upper))
        )
        if include_assignments:
            condition |= Exists(
                LegAssignment.objects.filter(
                    metering_point__location=OuterRef("pk"), leg__name__icontains=word
                )
            )
            condition |= Exists(
                PersonAssignment.objects.not_ended(today).filter(
                    metering_point__location=OuterRef("pk")
                ).filter(
                    Q(person__first_name__icontains=word) | Q(person__last_name__icontains=word)
                    | Q(person__organisation_name__icontains=word)
                )
            )
        if folded is not None:
            applicant = "building__application__person__"
            condition |= Q(
                pk__in=_pending_matches(
                    Q(number__contains=upper) | Q(**{f"{applicant}first_name__icontains": word})
                    | Q(**{f"{applicant}last_name__icontains": word})
                    | Q(**{f"{applicant}organisation_name__icontains": word}),
                    folded,
                )
            )
        queryset = queryset.filter(condition)
    return queryset


def apply_sort(queryset, key, descending=False):
    """Sort by a column (address: street, house number numerically, then as text; other columns tie-break by
    address). "occupancy": no metering points first, then unused, then by number of persons."""
    address = [F("street"), Cast("house_number", IntegerField()), F("house_number"), F("pk")]
    first = []
    if key == "circuit":
        first = [F("transformer_circuit__grid_number")]
    elif key == "metering_points":
        first = [F("point_count")]
    elif key == "occupancy":
        queryset = queryset.annotate(
            occupancy_rank=Case(
                When(point_count=0, occupant_count=0, then=Value(-1)), default=F("occupant_count"),
                output_field=IntegerField(),
            )
        )
        first = [F("occupancy_rank")]
    return queryset.order_by(
        *(
            item.desc(nulls_last=True) if descending else item.asc(nulls_last=True)
            for item in first + address
        )
    )


@dataclass(frozen=True)
class MeteringPointRow:
    """One metering point under a location, as the tables show it."""

    metering_point: MeteringPoint
    status: str
    leg_id: int | None
    leg_name: str
    person_id: int | None
    person_name: str


def _pick(assignments, today):
    """The assignment to show: the one valid today, else the next one that starts later."""
    current = [a for a in assignments if a.valid_from <= today]
    if current:
        return max(current, key=lambda a: a.valid_from)
    return min(assignments, key=lambda a: a.valid_from) if assignments else None


def person_name(person):
    return f"{person.first_name} {person.last_name}".strip() or person.organisation_name


def details(locations, today=None):
    """``(rows, persons)``: ``rows[location_id]`` the ``MeteringPointRow`` list (not archived, by number),
    ``persons[location_id]`` the number of distinct persons with an assignment valid now or later. Three queries."""
    today = today or timezone.localdate()
    ids = [location.pk for location in locations]
    points = list(
        MeteringPoint.objects.filter(location_id__in=ids, is_archived=False).order_by(
            F("number").asc(nulls_last=True), "pk"
        )
    )
    point_ids = [point.pk for point in points]
    legs = {}
    for assignment in LegAssignment.objects.not_ended(today).filter(metering_point_id__in=point_ids).select_related("leg"):
        legs.setdefault(assignment.metering_point_id, []).append(assignment)
    people = {}
    for assignment in PersonAssignment.objects.not_ended(today).filter(metering_point_id__in=point_ids).select_related("person"):
        people.setdefault(assignment.metering_point_id, []).append(assignment)
    rows = {location_id: [] for location_id in ids}
    persons = {location_id: set() for location_id in ids}
    for point in points:
        leg = _pick(legs.get(point.pk, []), today)
        person = _pick(people.get(point.pk, []), today)
        persons[point.location_id].update(a.person_id for a in people.get(point.pk, []))
        rows[point.location_id].append(
            MeteringPointRow(
                metering_point=point,
                status=point.status(today),
                leg_id=leg.leg_id if leg else None,
                leg_name=leg.leg.name if leg else "",
                person_id=person.person_id if person else None,
                person_name=person_name(person.person) if person else "",
            )
        )
    return rows, {location_id: len(found) for location_id, found in persons.items()}


def locations_for_choice():
    """All locations not archived, by address."""
    return Location.objects.filter(is_archived=False).order_by("street", "house_number")


LOCATION_CHOICE_LIMIT = 10
_LOCATION_TEXT = re.compile(r"(?P<street>.+) (?P<number>\S+), (?P<postal>\d{4}) (?P<town>.+)")


def location_suggestions(text, limit=LOCATION_CHOICE_LIMIT):
    """At most ``limit`` locations (not archived) matching address parts or a metering point number."""
    return list(apply_search(locations_for_choice(), text, include_assignments=False)[:limit])


@dataclass(frozen=True)
class LocationFound:
    """``location`` is the location or None; ``code`` is "" for one match, else ``none`` or ``several``."""

    location: Location | None = None
    code: str = ""


def resolve(text):
    """The one location the text names (``str(location)`` or a part matched by exactly one location)."""
    text = (text or "").strip()
    match = _LOCATION_TEXT.fullmatch(text)
    if match:
        found = Location.find_by_address(match["street"], match["number"], match["postal"], match["town"])
        if found is not None and not found.is_archived:
            return LocationFound(found)
    found = location_suggestions(text, limit=2)
    if not found:
        return LocationFound(code="none")
    if len(found) > 1:
        return LocationFound(code="several")
    return LocationFound(found[0])
