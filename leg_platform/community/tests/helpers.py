"""Helpers of the tests of the community domain and the management area (reserved e-mail domains only)."""

from datetime import date, timedelta

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.community.models import (
    Direction,
    HistoryEntry,
    Leg,
    LegAssignment,
    Location,
    ManagementSettings,
    MeteringPoint,
    PersonAssignment,
    PersonRecord,
    ShareChange,
    TransformerCircuit,
)

NUMBER_PREFIX = "CH1A2B3C4D5E6"  # country and identifier of the examples


def number(last_digits):
    """A complete 33-character metering point number for the examples."""
    return f"{NUMBER_PREFIX}{str(last_digits).zfill(20)}"


def staff_user(email, superuser=False):
    """An active account with staff status (D-086: every staff account may use the management area; the
    ``superuser`` flag makes it a system administrator, who also passes the rules of the Django administration)."""
    user = create_active_user(email)
    user.set_privileges(is_staff=True, is_superuser=superuser)
    return user


def make_location(street="Examplestrasse", house_number="1", postal_code="3000", town="Bern", **extra):
    return Location.objects.create(
        street=street, house_number=house_number, postal_code=postal_code, town=town, **extra
    )


def make_metering_point(last_digits=1, direction=Direction.CONSUMPTION, location=None, **extra):
    return MeteringPoint.objects.create(
        number=number(last_digits), direction=direction, location=location, **extra
    )


def make_circuit(grid_number="TRA1000", name=""):
    return TransformerCircuit.objects.create(grid_number=grid_number, name=name)


def make_leg(name="LEG Example", **extra):
    return Leg.objects.create(name=name, **extra)


def assign_leg(metering_point, leg, valid_from=date(2026, 1, 1), valid_to=None):
    return LegAssignment.objects.create(
        metering_point=metering_point, leg=leg, valid_from=valid_from, valid_to=valid_to
    )


def assign_person(metering_point, person, valid_from=date(2026, 1, 1), valid_to=None):
    return PersonAssignment.objects.create(
        metering_point=metering_point, person=person, valid_from=valid_from, valid_to=valid_to
    )


def create_table_rows(cls, customer, administrator, *, note, remark, iban):
    """Fill ``cls`` with one row of every table of the management data; ``cls.objects`` maps each model to its row."""
    cls.circuit = make_circuit("TRA4711", "Circuit name")
    cls.location = make_location(transformer_circuit=cls.circuit, remark=remark)
    cls.point = make_metering_point(5, location=cls.location, remark=remark)
    cls.leg = make_leg("LEG Table", remark=remark)
    cls.leg_assignment = assign_leg(cls.point, cls.leg)
    cls.person_assignment = assign_person(cls.point, customer)
    cls.record = PersonRecord(person=customer, staff_note=note, operator_customer_number="OP-1")
    cls.record.iban = iban
    cls.record.save()
    cls.shares = ShareChange.objects.create(
        record=cls.record, quantity=4, valid_from=date(2026, 1, 1), actor=administrator
    )
    cls.history = HistoryEntry.objects.create(
        subject_type="location", subject_id=cls.location.pk, event="created", actor=administrator,
        detail={"fields": ["street"]},
    )
    cls.settings_row = ManagementSettings.load()
    cls.objects = {
        HistoryEntry: cls.history, Leg: cls.leg, LegAssignment: cls.leg_assignment, Location: cls.location,
        ManagementSettings: cls.settings_row, MeteringPoint: cls.point,
        PersonAssignment: cls.person_assignment, PersonRecord: cls.record, ShareChange: cls.shares,
        TransformerCircuit: cls.circuit,
    }


def day(offset=0, base=None):
    """A date relative to a base (default: a fixed day), for rules that need an explicit "today"."""
    return (base or date(2026, 6, 15)) + timedelta(days=offset)
