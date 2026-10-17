"""Queries of the LEG list and detail page (D-080).

Figures come from ``community.legs`` in one query, so the list works on ``(leg, summary)`` rows sorted in Python.
Search (name, grid numbers of the members' transformer circuits; never the remark) and display filter are queries.
The detail page lists the members with the state of the last column: already in the own LEG of the circuit,
a dedicated LEG exists (move recommended), or none.
"""

from dataclasses import dataclass

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from leg_platform.community import legs
from leg_platform.community.location_list import person_name
from leg_platform.community.models import Leg, LegAssignment, MeteringPoint, PersonAssignment
from leg_platform.community.status import MeteringPointStatus

DISPLAY_ALL = "all"
DISPLAY_ARCHIVED = "archived"
DISPLAYS = (DISPLAY_ALL, DISPLAY_ARCHIVED)

DEFAULT_SORT = "name"

# "Price optimised" first; a LEG without metering points has no tier and comes last in both directions.
_TIER_ORDER = {legs.DiscountTier.OPTIMISED: 0, legs.DiscountTier.BASIC: 1}


@dataclass(frozen=True)
class LegRow:
    leg: Leg
    summary: legs.LegSummary

    @property
    def problems(self):
        return self.summary.problems


def base_queryset(display=DISPLAY_ALL):
    """"all" shows the live LEGs, "archived" only the archived ones."""
    return Leg.objects.filter(is_archived=(display == DISPLAY_ARCHIVED))


def apply_search(queryset, text, today=None):
    """Every word must match the LEG name or a member's transformer circuit grid number (case/accent-insensitive)."""
    today = today or timezone.localdate()
    for word in (text or "").split():
        in_circuit = Exists(
            LegAssignment.objects.in_force(today).filter(
                leg=OuterRef("pk"), metering_point__is_archived=False,
                metering_point__location__transformer_circuit__grid_number__contains=word.upper(),
            )
        )
        queryset = queryset.filter(Q(name__icontains=word) | in_circuit)
    return queryset


def rows_for(queryset, threshold_percent=None, today=None):
    """``[LegRow]`` for the queryset's LEGs."""
    found = list(queryset)
    figures = legs.summaries(found, threshold_percent, today)
    return [LegRow(leg, figures[leg.pk]) for leg in found]


def apply_problems(rows):
    return [row for row in rows if row.problems]


def _nulls_last(items, key, descending):
    """Sorted by ``key``; None keys come last in both directions. Stable, so the caller's order breaks ties."""
    present = [item for item in items if key(item) is not None]
    missing = [item for item in items if key(item) is None]
    present.sort(key=key, reverse=descending)
    return present + missing


def sort_rows(rows, key, descending=False):
    """Sort by a column; the name breaks ties, a LEG without a value comes last. Participants sort by consumers, then producers."""
    ordered = sorted(rows, key=lambda row: row.leg.name.casefold())
    if key == "quota":
        return _nulls_last(ordered, lambda row: row.leg.production_quota_percent, descending)
    if key == "participants":
        return _nulls_last(
            ordered,
            lambda row: (row.summary.consumers, row.summary.producers) if row.summary.metering_points else None,
            descending,
        )
    if key == "tier":
        return _nulls_last(ordered, lambda row: _TIER_ORDER.get(row.summary.tier), descending)
    if key == "points":
        return sorted(ordered, key=lambda row: row.summary.metering_points, reverse=descending)
    return sorted(ordered, key=lambda row: row.leg.name.casefold(), reverse=descending)


ACTION_OWN = "own"
ACTION_RECOMMEND = "recommend"
ACTION_NONE = "none"

DEFAULT_MEMBER_SORT = "number"

_STATUS_ORDER = {
    status: rank
    for rank, status in enumerate(
        (
            MeteringPointStatus.REGISTERED, MeteringPointStatus.NOTIFIED, MeteringPointStatus.CONFIRMED,
            MeteringPointStatus.ACTIVE, MeteringPointStatus.TERMINATED,
        )
    )
}


@dataclass
class Member:
    """A metering point of the LEG on the detail page. ``action``: ``ACTION_OWN``, ``ACTION_RECOMMEND`` (with
    ``other_leg``), ``ACTION_NONE``, or None if its circuit is unknown."""

    point: MeteringPoint
    status: str
    circuit: str
    action: str | None = None
    other_leg: Leg | None = None


def members_of(leg, today=None):
    """The metering points of the LEG on the day (not archived), by number."""
    today = today or timezone.localdate()
    ids = LegAssignment.objects.in_force(today).filter(leg=leg).values("metering_point_id")
    return list(
        MeteringPoint.objects.filter(pk__in=ids, is_archived=False)
        .select_related("location", "location__transformer_circuit")
        .order_by("number", "pk")
    )


def dedicated_legs(exclude_leg, threshold_percent=None, today=None):
    """``{circuit_id: Leg}``: the first LEG by name (not archived, not ``exclude_leg``) that covers only that circuit."""
    candidates = list(Leg.objects.filter(is_archived=False).exclude(pk=exclude_leg.pk).order_by("name"))
    found = legs.summaries(candidates, threshold_percent, today)
    dedicated = {}
    for candidate in candidates:
        summary = found[candidate.pk]
        if summary.tier == legs.DiscountTier.OPTIMISED and len(summary.circuit_ids) == 1:
            dedicated.setdefault(next(iter(summary.circuit_ids)), candidate)
    return dedicated


def members(leg, summary, threshold_percent=None, today=None):
    """The ``Member`` rows with the last-column state: a price-optimised LEG (one circuit) is "own" for every
    point; otherwise a point gets the dedicated LEG of its circuit as a recommendation, else "none"; a point
    without a known circuit gets no state."""
    today = today or timezone.localdate()
    points = members_of(leg, today)
    rows = []
    for point in points:
        circuit = point.location.transformer_circuit if point.location_id else None
        rows.append(
            Member(
                point=point, status=point.status(today), circuit=circuit.grid_number if circuit else "",
            )
        )
    if summary.tier == legs.DiscountTier.OPTIMISED:
        for row in rows:
            row.action = ACTION_OWN
    elif rows:
        dedicated = dedicated_legs(leg, threshold_percent, today)
        for row in rows:
            circuit = row.point.location.transformer_circuit_id if row.point.location_id else None
            if circuit is None:
                continue
            other = dedicated.get(circuit)
            row.action, row.other_leg = (ACTION_RECOMMEND, other) if other else (ACTION_NONE, None)
    return rows


def occupants(points, today=None):
    """``{metering point id: name}`` of the person valid now, else the next one. One query."""
    today = today or timezone.localdate()
    found = {}
    queryset = PersonAssignment.objects.not_ended(today).filter(
        metering_point__in=[p.pk for p in points]
    ).select_related("person")
    for assignment in queryset.order_by("valid_from"):
        found.setdefault(assignment.metering_point_id, person_name(assignment.person))
    return found


def _house_key(house_number):
    digits = ""
    for char in house_number:
        if not (char.isascii() and char.isdigit()):
            break
        digits += char
    return (int(digits) if digits else 0, house_number.casefold())


def sort_members(rows, key, descending=False, occupants=None):
    """Sort the members table by a column; the id breaks ties, a row without a value comes last.
    ``occupants`` maps metering point id to person name."""
    occupants = occupants or {}
    ordered = sorted(rows, key=lambda row: row.point.pk)
    if key == "address":
        return _nulls_last(
            ordered,
            lambda row: (row.point.location.street.casefold(), *_house_key(row.point.location.house_number))
            if row.point.location_id else None,
            descending,
        )
    if key == "circuit":
        return _nulls_last(ordered, lambda row: row.circuit.upper() or None, descending)
    if key == "status":
        return sorted(ordered, key=lambda row: _STATUS_ORDER[row.status], reverse=descending)
    if key == "occupant":
        return _nulls_last(ordered, lambda row: (occupants.get(row.point.pk) or "").casefold() or None, descending)
    return _nulls_last(ordered, lambda row: row.point.number, descending)
