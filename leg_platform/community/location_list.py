"""Queries of the list of locations (D-078; ``standorte/VORGABEN.md``, ``LISTEN-STANDARD.md``).

Search, filters, sorting and the rows of the metering point tables under the locations. Pure query
code, no request: the page passes the values it parsed. The search finds what the list and the
detail page show: the address, the circuit, the numbers of the metering points, the names of their
LEGs and, only for an account that may see persons, the names of the persons; never the remarks
(``LISTEN-STANDARD.md``, section 4).
"""

from dataclasses import dataclass

from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from leg_platform.community.models import LegAssignment, Location, MeteringPoint, PersonAssignment

OCCUPANCY_ALL = "all"
OCCUPANCY_OCCUPIED = "occupied"
OCCUPANCY_FREE = "free"
OCCUPANCY_EMPTY = "empty"
OCCUPANCY_ARCHIVED = "archived"
OCCUPANCIES = (OCCUPANCY_ALL, OCCUPANCY_OCCUPIED, OCCUPANCY_FREE, OCCUPANCY_EMPTY, OCCUPANCY_ARCHIVED)

SORT_KEYS = ("address", "circuit", "metering_points", "occupancy")
DEFAULT_SORT = "address"


def base_queryset(today=None):
    """All locations with ``mp_count`` (metering points that are not archived) and ``person_count``
    (the persons with an assignment that is valid now or later, each once)."""
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
    return Location.objects.select_related("transformer_circuit").annotate(
        mp_count=Coalesce(Subquery(points, output_field=IntegerField()), Value(0)),
        person_count=Coalesce(Subquery(persons, output_field=IntegerField()), Value(0)),
    )


def apply_occupancy(queryset, value):
    """The filter "Occupancy". Archived locations appear only with "archived", and then only they
    do; every other value hides them (``LISTEN-STANDARD.md``, section 4)."""
    if value == OCCUPANCY_ARCHIVED:
        return queryset.filter(is_archived=True)
    queryset = queryset.filter(is_archived=False)
    if value == OCCUPANCY_OCCUPIED:
        return queryset.filter(person_count__gt=0)
    if value == OCCUPANCY_FREE:
        return queryset.filter(mp_count__gt=0, person_count=0)
    if value == OCCUPANCY_EMPTY:
        return queryset.filter(mp_count=0)
    return queryset


def apply_circuit(queryset, bkw_number):
    return queryset.filter(transformer_circuit__bkw_number=bkw_number) if bkw_number else queryset


def apply_problems(queryset):
    return queryset.filter(address_correction_open=True)


def apply_search(queryset, text, *, see_persons=False, today=None):
    """Every word of the text must match something visible: the parts of the address, the circuit
    (number and name), a metering point number, the name of a LEG of a metering point and, with
    ``see_persons``, the name of a person who uses one. Letters match without regard to case or
    accents (the collation of the columns); the identifiers of the exact collation are compared
    in capitals."""
    today = today or timezone.localdate()
    for word in (text or "").split():
        upper = word.upper()
        condition = (
            Q(street__icontains=word) | Q(house_number__icontains=word) | Q(postal_code__icontains=word)
            | Q(town__icontains=word) | Q(address_addition__icontains=word)
            | Q(transformer_circuit__bkw_number__contains=upper) | Q(transformer_circuit__name__icontains=word)
            | Exists(MeteringPoint.objects.filter(location=OuterRef("pk"), number__contains=upper))
            | Exists(
                LegAssignment.objects.filter(
                    metering_point__location=OuterRef("pk"), leg__name__icontains=word
                )
            )
        )
        if see_persons:
            condition |= Exists(
                PersonAssignment.objects.not_ended(today).filter(
                    metering_point__location=OuterRef("pk")
                ).filter(
                    Q(person__first_name__icontains=word) | Q(person__last_name__icontains=word)
                    | Q(person__organisation_name__icontains=word)
                )
            )
        queryset = queryset.filter(condition)
    return queryset


def apply_sort(queryset, key, descending=False):
    """Sort by a column. The address is sorted by street, then the house number as a number, then as
    text; the other columns end with the address as the tie breaker. "Occupancy" puts the locations
    without a metering point first, then those that nobody uses, then by the number of persons."""
    address = [F("street"), Cast("house_number", IntegerField()), F("house_number"), F("pk")]
    first = []
    if key == "circuit":
        first = [F("transformer_circuit__bkw_number")]
    elif key == "metering_points":
        first = [F("mp_count")]
    elif key == "occupancy":
        queryset = queryset.annotate(
            occupancy_rank=Case(
                When(mp_count=0, then=Value(-1)), default=F("person_count"), output_field=IntegerField()
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


def details(locations, today=None, *, see_persons=False):
    """For the locations of one page: ``(rows, persons)`` with ``rows[location_id]`` the list of
    ``MeteringPointRow`` (metering points that are not archived, by number) and
    ``persons[location_id]`` the number of different persons with an assignment that is valid now
    or later (the figure that "Occupancy" shows). The name of a person is given only with
    ``see_persons``; the id always, so that "no person" can be told from a person. Three queries."""
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
                person_name=person_name(person.person) if person and see_persons else "",
            )
        )
    return rows, {location_id: len(found) for location_id, found in persons.items()}


def locations_for_choice():
    """All locations for a field with suggestions (not archived), by address."""
    return Location.objects.filter(is_archived=False).order_by("street", "house_number")
