"""Helpers of the onboarding tests (D-103): the directory lookup replaced by a fixed answer, a contract PDF, and a
complete draft. Reserved e-mail domains only."""

from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import patch

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.community import applications, buildings, contracts
from leg_platform.community.models import Direction, ManagementSettings
from leg_platform.community.tests.helpers import staff_user

IDENTIFIER = "1A2B3C4D5E6"
GOOD_IBAN = "CH9300762011623852957"
OTHER_IBAN = "DE89370400440532013000"
ADDRESS = {"street": "Ittigenstrasse", "house_number": "51", "postal_code": "3063", "town": "Ittigen"}
TODAY = date(2026, 10, 19)


def configure_numbers():
    """Country and identifier of the metering point numbers (empty until staff enter them, D-102)."""
    ManagementSettings.objects.update_or_create(id=1, defaults={"country_code": "CH", "identifier": IDENTIFIER})


def entered(last):
    """The typed end of a metering point number."""
    return str(last)


def full_number(last):
    return f"CH{IDENTIFIER}{str(last).zfill(20)}"


def candidate(egid, **address):
    values = {**ADDRESS, **address}
    return {"egid": egid, **values}


@contextmanager
def directory(*candidates):
    """The address directory offers exactly these buildings (``addresses.services.find_buildings`` is replaced)."""
    with patch.object(buildings, "find_candidates", return_value=list(candidates)) as lookup:
        yield lookup


def customer(email="customer@example.org", **profile):
    return create_active_user(email, **profile)


def pdf(extra=b"", size=0):
    """A small document that passes the checks; ``extra`` is placed in the body."""
    body = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n" + extra + b"\ntrailer\n<< /Root 1 0 R >>\n"
    body += b"%" * max(0, size - len(body) - 6)
    return body + b"\n%%EOF"


def publish_contract(staff=None, data=None, day=date(2026, 10, 7)):
    """Upload a version and make it current; returns it."""
    staff = staff or staff_user("contract.staff@example.org")
    version = contracts.upload_version(staff, data or pdf(), day)
    return contracts.make_current(staff, version)


def start_with_building(person, egid=90004269, **address):
    """A draft with one new building."""
    application = applications.start_draft(person)
    with directory(candidate(egid, **address)):
        choice = applications.choose_building(person, application, **{**ADDRESS, **address})
    return application, choice.building


def ready_draft(person, *, egid=90004269, feed_in=False, accept=True, start=True, number=1, address=None):
    """A draft that can be submitted (contract published by the caller or here)."""
    configure_numbers()
    application, building = start_with_building(person, egid, **(address or {}))
    applications.add_metering_point(person, application, building.pk, direction=Direction.CONSUMPTION, entered_number=entered(number))
    if feed_in:
        applications.add_metering_point(
            person, application, building.pk, direction=Direction.FEED_IN, entered_number=entered(number + 1)
        )
        applications.save_bank_connection(person, application, GOOD_IBAN)
    if accept and not contracts.has_accepted_current(person):
        version = contracts.current_version() or publish_contract()
        applications.accept_contract_step(
            person, application, version.pk, agrees_contract=True
        )
    if start:
        applications.set_start(person, application, asap=True, today=TODAY)
    application.refresh_from_db()
    return application, building


def months_later(day, months):
    from leg_platform.community.application_common import add_months

    return add_months(day, months)


def days(count):
    return timedelta(days=count)
