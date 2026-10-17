"""Transformer circuits (unique by normalised grid number, free name) and the figures of the card "Netz" (D-078)."""

from django.db import transaction

from leg_platform.community import numbers
from leg_platform.community.models import Direction, MeteringPoint, Location, TransformerCircuit

SEPARATOR = " · "


def label(circuit):
    """"TRA21359 · Ittigen Nord", or the number alone for a circuit without a name."""
    if circuit is None:
        return ""
    return f"{circuit.grid_number}{SEPARATOR}{circuit.name}" if circuit.name else circuit.grid_number


def number_of_choice(text):
    """The normalised grid number of a typed or chosen value ("TRA21359 · Ittigen Nord", "tra21359"); "" if empty."""
    return numbers.normalise_transformer_circuit_number((text or "").split(SEPARATOR.strip())[0])


def find(number):
    """The circuit with this number (normalised comparison), or None."""
    number = numbers.normalise_transformer_circuit_number(number)
    return TransformerCircuit.objects.filter(grid_number=number).first() if number else None


def create(number, name=""):
    """A new circuit. Raises ``ValidationError`` for a wrong number or a number that exists."""
    circuit = TransformerCircuit(
        grid_number=numbers.normalise_transformer_circuit_number(number), name=name
    )
    circuit.full_clean()
    circuit.save()
    return circuit


def affected_locations(circuit):
    """The number of locations a change of the circuit reaches."""
    return circuit.locations.count()


def update(circuit, number, name):
    """Change number and name; raises ``ValidationError`` (wrong or taken number)."""
    with transaction.atomic():
        fresh = TransformerCircuit.objects.select_for_update().get(pk=circuit.pk)
        fresh.grid_number = numbers.normalise_transformer_circuit_number(number)
        fresh.name = name
        fresh.full_clean()
        fresh.save()
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
