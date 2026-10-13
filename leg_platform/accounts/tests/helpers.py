"""Helpers of the portal tests. Test addresses use reserved domains only (RFC 2606)."""

import re
from urllib.parse import urlsplit

from django.conf import settings

from leg_platform.accounts.models import User

PASSWORD = "copper kettle hums beside quiet orchard"
OTHER_PASSWORD = "paper lantern rests under silver rain"

PROFILE = {
    "salutation": "mrs",
    "first_name": "Anna",
    "last_name": "Example",
    "organisation_name": "",
    "address_line_1": "",
    "street": "Examplestrasse",
    "house_number": "12a",
    "post_office_box_number": "",
    "postal_code": "3000",
    "town": "Bern",
    "country": "CH",
    "phone_number": "",
}


def registration_data(email="new.person@example.org", **overrides):
    """The registration form: only the email address; anything else is ignored."""
    return {"email": email, **overrides}


def completion_data(password=PASSWORD, registration=None, **overrides):
    """The form of the completion page; registration is the token reference the page contains."""
    data = {**PROFILE, "password1": password, "password2": password, **overrides}
    if registration is not None:
        data["registration"] = str(registration)
    return data


def create_active_user(email="active.person@example.org", **profile):
    """Create an account that completed its registration, through the central account logic."""
    values = {
        "first_name": "Anna",
        "last_name": "Example",
        "street": "Examplestrasse",
        "house_number": "12a",
        "town": "Bern",
        "swiss_postal_code": 3000,
        **profile,
    }
    user = User.objects.create_user(email, **values)
    user.complete_registration(PASSWORD)
    return user


def verification_path(message):
    """Return the path of the verification link in an email, checking its origin."""
    match = re.search(r"^(https?://\S+/register/verify/\S+)$", message.body, re.MULTILINE)
    assert match, "The email contains no verification link."
    link = match.group(1)
    assert link.startswith(settings.INSTANCE_BASE_URL + "/"), "The link is not built from the base URL."
    return urlsplit(link).path


def run_mail_queue():
    """Let the background worker process the queued emails (the requests only store jobs)."""
    from leg_platform.accounts.mail_queue import process_pending

    done, failed = process_pending()
    assert failed == 0, "A mail job failed."
    return done
