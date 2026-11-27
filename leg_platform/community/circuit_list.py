"""Queries of the list and the detail page of the transformer circuits (D-142).

Pure query code, no request. The figures of all circuits of a list come from three grouped queries (never one query per
circuit), so the list works on ``CircuitRow`` objects sorted in Python, like the LEG list. Archived locations and
metering points are not counted (as ``transformer_circuits.stats``); "in a LEG" is a LEG assignment valid that day.
"""

from dataclasses import dataclass, field

from django.db.models import Count, Exists, F, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from leg_platform.community import transformer_circuits
from leg_platform.community.models import Direction, Leg, LegAssignment, Location, MeteringPoint, TransformerCircuit

DEFAULT_SORT = "number"
MAX_WORDS = 5

# The order of the sources when the list is sorted by source.
_SOURCE_ORDER = {
    transformer_circuits.SOURCE_OPERATOR: 0, transformer_circuits.SOURCE_MANUAL: 1, transformer_circuits.SOURCE_UNKNOWN: 2,
}


@dataclass
class CircuitRow:
    """A circuit with its figures; ``legs`` are ``(id, name)`` pairs of the LEGs with a member in the circuit."""

    circuit: TransformerCircuit
    source: str
    locations: int = 0
    consumption: int = 0
    consumption_in_leg: int = 0
    feed_in: int = 0
    feed_in_in_leg: int = 0
    legs: list = field(default_factory=list)

    @property
    def points(self):
        return self.consumption + self.feed_in

    @property
    def points_in_leg(self):
        return self.consumption_in_leg + self.feed_in_in_leg


def apply_search(queryset, text, today=None):
    """Every word (at most ``MAX_WORDS``) must match the grid number, the name or the name of a LEG with a member in the
    circuit (case-insensitive)."""
    today = today or timezone.localdate()
    for word in (text or "").split()[:MAX_WORDS]:
        in_leg = Exists(
            LegAssignment.objects.in_force(today).filter(
                metering_point__location__transformer_circuit=OuterRef("pk"), metering_point__is_archived=False,
                metering_point__location__is_archived=False, leg__name__icontains=word,
            )
        )
        queryset = queryset.filter(Q(grid_number__contains=word.upper()) | Q(name__icontains=word) | in_leg)
    return queryset


def _in_leg(today):
    return Exists(LegAssignment.objects.in_force(today).filter(metering_point=OuterRef("pk")))


def _counted_points(today):
    """The metering points that count: not archived, at a location that is not archived."""
    return MeteringPoint.objects.filter(is_archived=False, location__is_archived=False).annotate(in_leg=_in_leg(today))


def rows_for(queryset, stations, today=None):
    """``[CircuitRow]`` for the circuits of the queryset; ``stations`` is ``transformer_circuits.directory_numbers()``.
    Four queries whatever the number of circuits."""
    today = today or timezone.localdate()
    circuits = list(queryset)
    rows = {circuit.pk: CircuitRow(circuit, transformer_circuits.source(circuit.grid_number, stations)) for circuit in circuits}
    ids = list(rows)
    if not ids:
        return []
    for circuit_id, count in (
        Location.objects.filter(transformer_circuit_id__in=ids, is_archived=False).order_by()
        .values("transformer_circuit_id").annotate(n=Count("pk")).values_list("transformer_circuit_id", "n")
    ):
        rows[circuit_id].locations = count
    grouped = (
        _counted_points(today).filter(location__transformer_circuit_id__in=ids).order_by()
        .values("location__transformer_circuit_id", "direction", "in_leg").annotate(n=Count("pk"))
        .values_list("location__transformer_circuit_id", "direction", "in_leg", "n")
    )
    for circuit_id, direction, in_leg, count in grouped:
        row = rows[circuit_id]
        if direction == Direction.CONSUMPTION:
            row.consumption += count
            row.consumption_in_leg += count if in_leg else 0
        else:
            row.feed_in += count
            row.feed_in_in_leg += count if in_leg else 0
    members = (
        LegAssignment.objects.in_force(today).order_by()
        .filter(
            metering_point__is_archived=False, metering_point__location__is_archived=False,
            metering_point__location__transformer_circuit_id__in=ids,
        )
        .values_list("metering_point__location__transformer_circuit_id", "leg_id", "leg__name")
        .distinct()
    )
    for circuit_id, leg_id, leg_name in members:
        rows[circuit_id].legs.append((leg_id, leg_name))
    for row in rows.values():
        row.legs.sort(key=lambda pair: pair[1].casefold())
    return [rows[circuit.pk] for circuit in circuits]


def _nulls_last(items, key, descending):
    present = [item for item in items if key(item) is not None]
    missing = [item for item in items if key(item) is None]
    present.sort(key=key, reverse=descending)
    return present + missing


def sort_rows(rows, key, descending=False):
    """Sort by a column; the number breaks ties ("TRA9" before "TRA10"), a circuit without a name comes last."""
    ordered = sorted(rows, key=lambda row: transformer_circuits.number_key(row.circuit.grid_number))
    if key == "name":
        return _nulls_last(ordered, lambda row: row.circuit.name.casefold() or None, descending)
    if key == "source":
        return sorted(ordered, key=lambda row: _SOURCE_ORDER[row.source], reverse=descending)
    if key == "locations":
        return sorted(ordered, key=lambda row: row.locations, reverse=descending)
    if key == "consumption":
        return sorted(ordered, key=lambda row: (row.consumption, row.consumption_in_leg), reverse=descending)
    if key == "feed_in":
        return sorted(ordered, key=lambda row: (row.feed_in, row.feed_in_in_leg), reverse=descending)
    if key == "legs":
        return sorted(ordered, key=lambda row: len(row.legs), reverse=descending)
    return ordered[::-1] if descending else ordered


def locations_of(circuit):
    """The locations of the circuit that are not archived, by address, with ``mp_count`` (metering points not
    archived)."""
    points = (
        MeteringPoint.objects.filter(location=OuterRef("pk"), is_archived=False)
        .order_by().values("location").annotate(n=Count("pk")).values("n")
    )
    return list(
        Location.objects.filter(transformer_circuit=circuit, is_archived=False)
        .annotate(mp_count=Coalesce(Subquery(points, output_field=IntegerField()), Value(0)))
        .order_by(F("street"), Cast("house_number", IntegerField()), F("house_number"), F("pk"))
    )


@dataclass(frozen=True)
class CircuitLeg:
    """A LEG with members in the circuit: ``here`` of its metering points lie in the circuit, ``total`` in all."""

    leg: Leg
    here: int
    total: int


def legs_of(circuit, today=None):
    """The LEGs with a member in the circuit, by name. Two queries."""
    today = today or timezone.localdate()
    members = LegAssignment.objects.in_force(today).filter(metering_point__is_archived=False).order_by()
    here = dict(
        members.filter(
            metering_point__location__transformer_circuit=circuit, metering_point__location__is_archived=False
        ).values("leg_id").annotate(n=Count("metering_point", distinct=True)).values_list("leg_id", "n")
    )
    if not here:
        return []
    total = dict(
        members.filter(leg_id__in=list(here)).values("leg_id").annotate(n=Count("metering_point", distinct=True))
        .values_list("leg_id", "n")
    )
    found = Leg.objects.filter(pk__in=list(here)).order_by("name")
    return [CircuitLeg(leg, here[leg.pk], total.get(leg.pk, 0)) for leg in found]
