"""Helpers of the tests of the customer's pages (D-103). Reserved e-mail domains only; no real address, number or IBAN.

The address directory is replaced by a fixed answer (``directory``): the pages never reach the real directory or swisstopo
in a test.
"""

from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community import applications, buildings
from leg_platform.community.models import Direction
from leg_platform.community.tests.onboarding import (  # noqa: F401  (re-exported for the tests)
    ADDRESS,
    GOOD_IBAN,
    IDENTIFIER,
    OTHER_IBAN,
    TODAY,
    candidate,
    configure_numbers,
    customer,
    full_number,
    publish_contract,
)

BUILDINGS = reverse("customer:buildings")
START = reverse("customer:application_start")
HOME = reverse("home")
LOGIN = reverse("accounts:login")
DAY = date(2026, 10, 19)


def login(user):
    """A client of the account (a staff account would pass the second factor too)."""
    client = VerifiedClient()
    client.force_login(user)
    return client


@contextmanager
def directory(*candidates, suggestions=()):
    """The directory offers exactly these buildings and these typing suggestions (both lookups are replaced)."""
    with patch.object(buildings, "find_candidates", return_value=list(candidates)) as lookup, patch.object(
        buildings, "suggest_candidates", return_value=list(suggestions)
    ):
        yield lookup


def step_url(application, name):
    return reverse(f"customer:application_{name}", args=[application.pk])


def draft_with_building(person, egid=90004269, **address):
    """A draft with one new building (the first step is done)."""
    application = applications.start_draft(person)
    with directory(candidate(egid, **address)):
        choice = applications.choose_building(person, application, **{**ADDRESS, **address})
    return application, choice.building


def draft_with_points(person, *, feed_in=False, egid=90004269, number=1, **address):
    """A draft whose building and metering points are done."""
    configure_numbers()
    application, building = draft_with_building(person, egid, **address)
    applications.add_metering_point(
        person, application, building.pk, direction=Direction.CONSUMPTION, entered_number=str(number), name="House"
    )
    if feed_in:
        applications.add_metering_point(
            person, application, building.pk, direction=Direction.FEED_IN, entered_number=str(number + 1), name="PV"
        )
    return application, building
