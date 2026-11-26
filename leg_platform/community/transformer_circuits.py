"""Transformer circuits (unique by normalised grid number, free name) and the figures of the card "Netz" (D-078)."""

from django.db import IntegrityError, transaction

from leg_platform.community import numbers
from leg_platform.community.models import Direction, MeteringPoint, Location, TransformerCircuit

SEPARATOR = " · "


def label(circuit):
    """"TRA21359 · Ittigen Nord", or the number alone for a circuit without a name."""
    if circuit is None:
        return ""
    return f"{circuit.grid_number}{SEPARATOR}{circuit.name}" if circuit.name else circuit.grid_number


def find(number):
    """The circuit with this number (normalised comparison), or None."""
    number = numbers.normalise_transformer_circuit_number(number)
    return TransformerCircuit.objects.filter(grid_number=number).first() if number else None


def create(number, name=""):
    """A new circuit. Raises ``ValidationError`` for a wrong number or a number that exists (also when a parallel
    request created it between the check and the save: the database constraint decides, L-7)."""
    circuit = TransformerCircuit(
        grid_number=numbers.normalise_transformer_circuit_number(number), name=name
    )
    circuit.full_clean()
    try:
        with transaction.atomic():
            circuit.save()
    except IntegrityError:
        circuit.validate_unique()  # now finds the other one and raises the same message as the check above
        raise
    return circuit


def affected_locations(circuit):
    """The number of locations a change of the circuit reaches."""
    return circuit.locations.count()


def update(circuit, number, name):
    """Change number and name; raises ``ValidationError`` (wrong or taken number, also when a parallel request took the
    number between the check and the save: the database constraint decides, as in ``create``)."""
    try:
        with transaction.atomic():
            fresh = TransformerCircuit.objects.select_for_update().get(pk=circuit.pk)
            fresh.grid_number = numbers.normalise_transformer_circuit_number(number)
            fresh.name = name
            fresh.full_clean()
            fresh.save()
    except IntegrityError:
        fresh.validate_unique()  # now finds the other one and raises the same message as full_clean
        raise
    return fresh


def stats(circuit):
    """Locations and metering points by direction of the circuit, archived ones not counted."""
    locations = Location.objects.filter(transformer_circuit=circuit, is_archived=False)
    points = MeteringPoint.objects.filter(location__in=locations, is_archived=False)
    return {
        "locations": locations.count(),
        "consumption": points.filter(direction=Direction.CONSUMPTION).count(),
        "feed_in": points.filter(direction=Direction.FEED_IN).count(),
    }


def choices():
    """All circuits, ordered by number."""
    return list(TransformerCircuit.objects.order_by("grid_number"))


def station_number(egid):
    """The grid number ("TRA1024") of the transformer station the address directory holds for a building: "" when the
    directory answered and holds none (or there is no EGID), None when the directory cannot answer now (missing, empty,
    without building identifiers, unreadable, e.g. during an import). None must change nothing."""
    if not egid:
        return ""
    from leg_platform.addresses.buildings import directory_answers
    from leg_platform.addresses.transformers import station_of

    try:
        if not directory_answers():
            return None
        station = station_of(egid)
    except Exception:  # noqa: BLE001  (the directory cannot be read now: nothing is derived or removed)
        return None
    if not station or not station.get("name"):
        return ""
    match = numbers.TRANSFORMER_CIRCUIT_NUMBER.search(station["name"].upper())
    return match.group(0) if match else ""


def derive(location, actor=None, *, clear_when_unavailable=False):
    """Set the circuit of a location from its EGID (the station of the directory) unless it was set by hand.

    Unknown station (the directory answered): a derived circuit is removed (the list shows "–"). The directory cannot
    answer: nothing changes, unless ``clear_when_unavailable`` (the staff removed a hand choice: it goes, the scheduled
    ``fill_transformer_circuits`` derives the circuit later). The caller holds the row lock. Returns True when the
    circuit changed (saved, one History entry)."""
    from . import history
    from .history import HistoryEvent

    if location.circuit_manual or location.is_archived:
        return False
    number = station_number(location.egid)
    if number is None and not clear_when_unavailable:
        return False
    circuit = (find(number) or create(number)) if number else None
    before = location.transformer_circuit
    if before == circuit:
        return False
    location.transformer_circuit = circuit
    location.save(update_fields=["transformer_circuit", "updated_at"])
    history.record(
        location, HistoryEvent.TRANSFORMER_CIRCUIT_CHANGED, actor,
        {"old": before.grid_number if before else "", "new": circuit.grid_number if circuit else "", "derived": True},
    )
    return True


def fill_missing():
    """``derive`` for every open location that has no manual circuit, one transaction each; returns the number changed.

    Stops at once, changing nothing, while the address directory cannot answer (command ``fill_transformer_circuits``,
    a task of ``run_scheduled_tasks``)."""
    from leg_platform.addresses.buildings import directory_answers

    if not directory_answers():
        return 0
    changed = 0
    for pk in Location.objects.filter(circuit_manual=False, is_archived=False).order_by("pk").values_list("pk", flat=True):
        with transaction.atomic():
            location = Location.objects.select_for_update().select_related("transformer_circuit").filter(pk=pk).first()
            if location is not None and derive(location):
                changed += 1
    return changed
