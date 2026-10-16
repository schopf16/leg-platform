"""Helpers of the tests of the community domain and the management area (reserved e-mail domains only)."""

from datetime import date, timedelta

from django.contrib.auth.models import Permission

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.community.models import (
    Direction,
    Leg,
    LegAssignment,
    Location,
    MeteringPoint,
    PersonAssignment,
    TransformerCircuit,
)

NUMBER_PREFIX = "CH1A2B3C4D5E6"  # country and identifier of the examples


def number(last_digits):
    """A complete 33-character metering point number for the examples."""
    return f"{NUMBER_PREFIX}{str(last_digits).zfill(20)}"


def staff_user(email, *codenames, superuser=False):
    """An account with staff status and the given rights of the community app (by codename)."""
    user = create_active_user(email)
    user.set_privileges(is_staff=True, is_superuser=superuser)
    if codenames:
        user.user_permissions.set(
            Permission.objects.filter(content_type__app_label="community", codename__in=codenames)
        )
    return user


def make_location(street="Examplestrasse", house_number="1", postal_code="3000", town="Bern", **extra):
    return Location.objects.create(
        street=street, house_number=house_number, postal_code=postal_code, town=town, **extra
    )


def make_metering_point(last_digits=1, direction=Direction.CONSUMPTION, location=None, **extra):
    return MeteringPoint.objects.create(
        number=number(last_digits), direction=direction, location=location, **extra
    )


def make_circuit(bkw_number="TRA1000", name=""):
    return TransformerCircuit.objects.create(bkw_number=bkw_number, name=name)


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


def day(offset=0, base=None):
    """A date relative to a base (default: a fixed day), for rules that need an explicit "today"."""
    return (base or date(2026, 6, 15)) + timedelta(days=offset)
