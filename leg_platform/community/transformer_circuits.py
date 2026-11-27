"""Transformer circuits (unique by normalised grid number, free name) and their figures (D-078, D-142).

A circuit is created by the staff in the menu "Transformer circuits" (``create``), by the derivation from the station of
a building (``derive``) or when the staff chooses a station of the address directory that is not stored yet
(``locations.set_circuit``). Its source is derived, never stored: a circuit whose number is a station of the current
directory comes from the operator, any other one was entered by hand (``source``)."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from leg_platform.community import history, numbers
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Direction, Location, MeteringPoint, TransformerCircuit

SEPARATOR = " · "

# The source of a circuit (D-142): a station of the address directory, entered by hand, or unknown while the directory
# cannot answer.
SOURCE_OPERATOR = "operator"
SOURCE_MANUAL = "manual"
SOURCE_UNKNOWN = "unknown"


def label(circuit):
    """"TRA21359 · Ittigen Nord", or the number alone for a circuit without a name."""
    if circuit is None:
        return ""
    return f"{circuit.grid_number}{SEPARATOR}{circuit.name}" if circuit.name else circuit.grid_number


def find(number):
    """The circuit with this number (normalised comparison), or None."""
    number = numbers.normalise_transformer_circuit_number(number)
    return TransformerCircuit.objects.filter(grid_number=number).first() if number else None


def create(number, name="", actor=None, *, derived=False):
    """A new circuit with its History entry "created" (``derived``: made by the derivation, D-142). Raises
    ``ValidationError`` for a wrong number or a number that exists (also when a parallel request created it between
    the check and the save: the database constraint decides, L-7)."""
    circuit = TransformerCircuit(grid_number=numbers.normalise_transformer_circuit_number(number), name=name)
    circuit.full_clean()
    try:
        with transaction.atomic():
            circuit.save()
            history.record(circuit, HistoryEvent.CREATED, actor, {"derived": True} if derived else None)
    except IntegrityError:
        circuit.validate_unique()  # now finds the other one and raises the same message as the check above
        raise
    return circuit


def find_or_create(number, actor=None, *, derived=False):
    """The stored circuit with this number, else a new one; a parallel creation of the same number finds that one."""
    found = find(number)
    if found is not None:
        return found
    try:
        return create(number, actor=actor, derived=derived)
    except ValidationError:
        found = find(number)
        if found is None:
            raise
        return found


def affected_locations(circuit):
    """The number of locations a change of the circuit reaches."""
    return circuit.locations.count()


def update(circuit, number, name, actor=None):
    """Change number and name; a change is one History entry ``master_data_changed`` (field names, old and new number).
    Raises ``ValidationError`` (wrong or taken number, also when a parallel request took the number between the check
    and the save: the database constraint decides, as in ``create``)."""
    try:
        with transaction.atomic():
            fresh = TransformerCircuit.objects.select_for_update().get(pk=circuit.pk)
            before = (fresh.grid_number, fresh.name)
            fresh.grid_number = numbers.normalise_transformer_circuit_number(number)
            fresh.name = name
            fresh.full_clean()
            fresh.save()
            fields = [
                field
                for field, old, new in (("grid_number", before[0], fresh.grid_number), ("name", before[1], fresh.name))
                if old != new
            ]
            if fields:
                history.record(
                    fresh, HistoryEvent.MASTER_DATA_CHANGED, actor,
                    {"fields": fields, "old": before[0], "new": fresh.grid_number},
                )
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


def number_key(number):
    """Sort key of a grid number: "TRA9" before "TRA10"."""
    digits = number[3:]
    return (int(digits) if digits.isascii() and digits.isdigit() else 0, number)


def _number_of(name):
    match = numbers.TRANSFORMER_CIRCUIT_NUMBER.search((name or "").upper())
    return match.group(0) if match else ""


def stations_answer():
    """Whether the address directory can answer a station question: it answers building questions
    (``directory_answers``) and holds the rows of a completed transformer scan. A scan replaces its rows in one
    transaction (``addresses.transformers.store``), so a reader never sees a scan half written; but the table is empty
    on a new instance and after a reset of the directory file until the next weekly scan, and in that window no answer
    may remove a derived circuit or call an operator circuit "entered by hand"."""
    from leg_platform.addresses.buildings import directory_answers
    from leg_platform.addresses.models import TransformerStation

    return directory_answers() and TransformerStation.objects.exists()


def directory_numbers():
    """The grid numbers of the stations the address directory holds for the buildings of its current generation (a
    set), or None while the directory cannot answer (the rule of ``station_number``).

    Runs on every circuit suggestion and page, so it starts from the few station rows and asks the index
    ``addr_generation_egid`` once per row (a few milliseconds with the full directory); filtering the addresses of the
    generation first took a second."""
    from django.db.models import Exists, OuterRef

    from leg_platform.addresses.models import Address, Directory, TransformerStation

    try:
        if not stations_answer():
            return None
        generation = Directory.objects.filter(pk=1).values_list("generation", flat=True).first()
        in_generation = Address.objects.filter(generation=generation, egid=OuterRef("egid"))
        names = (
            TransformerStation.objects.exclude(name="")
            .filter(Exists(in_generation))
            .values_list("name", flat=True)
            .distinct()
        )
        found = {_number_of(name) for name in names}
    except Exception:  # noqa: BLE001  (the directory cannot be read now: the source is unknown)
        return None
    found.discard("")
    return found


def source(number, stations):
    """``SOURCE_OPERATOR`` when the number is a station of the directory, ``SOURCE_MANUAL`` otherwise,
    ``SOURCE_UNKNOWN`` when ``stations`` (``directory_numbers``) is None."""
    if stations is None:
        return SOURCE_UNKNOWN
    return SOURCE_OPERATOR if number in stations else SOURCE_MANUAL


def station_number(egid):
    """The grid number ("TRA1024") of the transformer station the address directory holds for a building: "" when the
    directory answered and holds none (or there is no EGID), None when the directory cannot answer now (missing, empty,
    without building identifiers, without a completed scan, unreadable, e.g. during an import; ``stations_answer``).
    None must change nothing."""
    if not egid:
        return ""
    from leg_platform.addresses.transformers import station_of

    try:
        if not stations_answer():
            return None
        station = station_of(egid)
    except Exception:  # noqa: BLE001  (the directory cannot be read now: nothing is derived or removed)
        return None
    if not station or not station.get("name"):
        return ""
    return _number_of(station["name"])


def derive(location, actor=None, *, clear_when_unavailable=False):
    """Set the circuit of a location from its EGID (the station of the directory) unless it was set by hand.

    Unknown station (the directory answered): a derived circuit is removed (the list shows "–"). The directory cannot
    answer: nothing changes, unless ``clear_when_unavailable`` (the staff removed a hand choice: it goes, the scheduled
    ``fill_transformer_circuits`` derives the circuit later). The caller holds the row lock. Returns True when the
    circuit changed (saved, one History entry with ``derived``; a circuit made for it has no actor)."""
    if location.circuit_manual or location.is_archived:
        return False
    number = station_number(location.egid)
    if number is None and not clear_when_unavailable:
        return False
    circuit = find_or_create(number, derived=True) if number else None
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
    try:
        if not stations_answer():
            return 0
    except Exception:  # noqa: BLE001  (the directory cannot be read now: nothing is derived or removed)
        return 0
    changed = 0
    for pk in Location.objects.filter(circuit_manual=False, is_archived=False).order_by("pk").values_list("pk", flat=True):
        with transaction.atomic():
            location = Location.objects.select_for_update().select_related("transformer_circuit").filter(pk=pk).first()
            if location is not None and derive(location):
                changed += 1
    return changed
