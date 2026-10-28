"""The transformer circuits around one building for the page "Nachbarschaft" (D-120; NACHBARSCHAFT.md).

Read only, from the address directory file: the published buildings with their LV95 position, the transformer station of
each building (``TransformerStation``, D-118) and the plants of the published plant register (D-117). Nothing about a
participant is read here; the page shows public building and plant data only.

A circuit is the set of buildings with the same station name in one postal code. A building without a station name
belongs to the circuit of its nearest building with one (implicitly the same circuit). A circuit's outline is the convex
hull of its building positions, so every building of the circuit lies inside its outline.
"""

from collections import defaultdict
from dataclasses import dataclass

from .geo import lv95_to_wgs84
from .models import Address, Directory, Plant, PlantRegister, TransformerStation

# Lookups of many buildings in one query stay below SQLite's limit of variables per statement.
CHUNK = 500


@dataclass(frozen=True)
class Circuit:
    """One transformer circuit of a postal code: public counts and the outline and PV points as (longitude, latitude)."""

    name: str
    building_count: int
    pv_count: int
    outline: tuple
    pv_points: tuple


@dataclass(frozen=True)
class Neighbourhood:
    """The circuits of the postal code of a building; ``own_circuit`` is the name of the own circuit (None if unknown)."""

    postal_code: str
    own_point: tuple | None
    own_circuit: str | None
    circuits: tuple


def for_building(egid):
    """The neighbourhood of the building ``egid``, or None when the address directory has no such building."""
    generation = _generation()
    if generation is None:
        return None
    own = Address.objects.filter(generation=generation, egid=egid).values_list("postal_code", flat=True).first()
    if own is None:
        return None
    return _area(generation, own, egid)


def for_postal_code(postal_code):
    """The circuits of a postal code without an own building (no own point, no own circuit), or None when the
    directory shows no circuit there."""
    generation = _generation()
    if generation is None:
        return None
    area = _area(generation, postal_code, None)
    return area if area.circuits else None


def _generation():
    return Directory.objects.filter(pk=1).values_list("generation", flat=True).first()


def _area(generation, postal_code, egid):
    """The circuits of a postal code; ``egid`` is the own building (None for a whole area)."""
    positions = _positions(generation, postal_code)
    names = _station_names(postal_code, positions)
    assigned = _assign(positions, names)
    pv = _pv_buildings(set(positions))
    circuits = []
    for name, members in _groups(assigned).items():
        outline = _hull([positions[member] for member in members])
        pv_members = [member for member in members if member in pv]
        circuits.append(Circuit(
            name=name,
            building_count=len(members),
            pv_count=len(pv_members),
            outline=tuple(_to_wgs84(point) for point in outline),
            pv_points=tuple(_to_wgs84(positions[member]) for member in pv_members),
        ))
    own_point = _to_wgs84(positions[egid]) if egid in positions else None
    return Neighbourhood(
        postal_code=postal_code,
        own_point=own_point,
        own_circuit=assigned.get(egid),
        circuits=tuple(sorted(circuits, key=lambda circuit: circuit.name)),
    )


def _positions(generation, postal_code):
    """LV95 position (easting, northing) of every building of the postal code with a position: the mean of its rows."""
    sums = defaultdict(lambda: [0.0, 0.0, 0])
    rows = Address.objects.filter(
        generation=generation, postal_code=postal_code, egid__isnull=False,
        easting__isnull=False, northing__isnull=False,
    ).values_list("egid", "easting", "northing")
    for egid, easting, northing in rows:
        total = sums[egid]
        total[0] += float(easting)
        total[1] += float(northing)
        total[2] += 1
    return {egid: (e / count, n / count) for egid, (e, n, count) in sums.items()}


def _station_names(postal_code, positions):
    """The station name of each building of the postal code (``""`` when the map shows none), for known positions."""
    names = {}
    rows = TransformerStation.objects.filter(postal_code=postal_code).values_list("egid", "name")
    for egid, name in rows:
        if egid in positions:
            names[egid] = name
    return names


def _assign(positions, names):
    """Circuit name of every building: its station name, or the name of its nearest named building."""
    named = {egid: name for egid, name in names.items() if name}
    if not named:
        return {}
    assigned = dict(named)
    for egid, (easting, northing) in positions.items():
        if egid in assigned:
            continue
        nearest = min(
            named,
            key=lambda other: (positions[other][0] - easting) ** 2 + (positions[other][1] - northing) ** 2,
        )
        assigned[egid] = named[nearest]
    return assigned


def _groups(assigned):
    groups = defaultdict(list)
    for egid, name in assigned.items():
        groups[name].append(egid)
    return groups


def _pv_buildings(egids):
    """The buildings of ``egids`` with at least one plant in the published plant register (D-117)."""
    register = PlantRegister.objects.filter(pk=1).values("generation", "row_count").first()
    if register is None or register["row_count"] == 0:
        return set()
    found = set()
    ordered = sorted(egids)
    for start in range(0, len(ordered), CHUNK):
        found.update(
            Plant.objects.filter(generation=register["generation"], egid__in=ordered[start:start + CHUNK])
            .values_list("egid", flat=True).distinct()
        )
    return found


def _hull(points):
    """Convex hull of the points (monotone chain), counter-clockwise, without a repeated first point."""
    unique = sorted(set(points))
    if len(unique) <= 2:
        return unique

    def turn(origin, first, second):
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (first[1] - origin[1]) * (second[0] - origin[0])

    lower, upper = [], []
    for point in unique:
        while len(lower) >= 2 and turn(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(unique):
        while len(upper) >= 2 and turn(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _to_wgs84(point):
    longitude, latitude = lv95_to_wgs84(point[0], point[1])
    return round(longitude, 6), round(latitude, 6)
