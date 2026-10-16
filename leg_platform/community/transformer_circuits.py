"""Circuits of the grid: creating and changing them, and the figures of the card "Netz" (D-078).

A transformer circuit is identified by the number the BKW assigns (``TRA`` and digits, unique after
conversion to capital letters and trimming); the name is free and not unique
(``standorte/VORGABEN.md``, "Nachtrag: Trafokreis"). A circuit has no page of its own: it is chosen
or created in the card "Netz" of a location.
"""

from django.db import transaction

from leg_platform.community import numbers
from leg_platform.community.models import Direction, MeteringPoint, Location, TransformerCircuit

SEPARATOR = " · "


def label(circuit):
    """"TRA21359 · Ittigen Nord", or the number alone for a circuit without a name."""
    if circuit is None:
        return ""
    return f"{circuit.bkw_number}{SEPARATOR}{circuit.name}" if circuit.name else circuit.bkw_number


def number_of_choice(text):
    """The BKW number in what a person typed or chose in the field ("TRA21359 · Ittigen Nord" or just
    "tra21359"): the part before the separator, converted as the numbers are stored; "" if empty."""
    return numbers.normalise_transformer_circuit_number((text or "").split(SEPARATOR.strip())[0])


def find(number):
    """The circuit with this number (normalised comparison), or None."""
    number = numbers.normalise_transformer_circuit_number(number)
    return TransformerCircuit.objects.filter(bkw_number=number).first() if number else None


def create(number, name=""):
    """A new circuit. Raises ``ValidationError`` for a wrong number or a number that exists."""
    circuit = TransformerCircuit(
        bkw_number=numbers.normalise_transformer_circuit_number(number), name=name
    )
    circuit.full_clean()
    circuit.save()
    return circuit


def affected_locations(circuit):
    """The number of locations a change of the circuit reaches (name or number)."""
    return circuit.locations.count()


def update(circuit, number, name):
    """Change number and name of a circuit; the change reaches every location with it. Raises
    ``ValidationError`` (wrong number, or another circuit has it)."""
    with transaction.atomic():
        fresh = TransformerCircuit.objects.select_for_update().get(pk=circuit.pk)
        fresh.bkw_number = numbers.normalise_transformer_circuit_number(number)
        fresh.name = name
        fresh.full_clean()
        fresh.save()
    return fresh


def stats(circuit):
    """The figures of the card "Netz": locations in the circuit and its metering points by
    direction (archived locations and metering points do not count)."""
    locations = Location.objects.filter(transformer_circuit=circuit, is_archived=False)
    points = MeteringPoint.objects.filter(location__in=locations, is_archived=False)
    return {
        "locations": locations.count(),
        "consumption": points.filter(direction=Direction.CONSUMPTION).count(),
        "feed_in": points.filter(direction=Direction.FEED_IN).count(),
    }


def choices():
    """All circuits for a field with suggestions, each number once."""
    return list(TransformerCircuit.objects.order_by("bkw_number"))
