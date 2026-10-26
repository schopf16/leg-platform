"""Helpers of the portal tests. Test addresses use reserved domains only (RFC 2606)."""

import functools
import re
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection
from django.test import Client
from django.urls import reverse
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.oath import TOTP

from leg_platform.accounts import bot_check, rate_limits
from leg_platform.accounts.models import AuthenticatorDevice, GraphConfiguration, User

def instance_mail_addresses(**values):
    """Decorator for a test class or a test method: the E-Mail chapter holds these values (``sender_address``,
    ``contact_address``) during the test. The row is written before each test; the transaction of the test rolls it
    back (D-116). The base address of the portal is set with ``override_settings(INSTANCE_BASE_URL=...)``."""

    def store():
        GraphConfiguration.objects.update_or_create(pk=1, defaults=values)

    def decorate(target):
        if isinstance(target, type):
            original_setup = target.setUp

            def setUp(self):
                store()
                original_setup(self)

            target.setUp = setUp
            return target

        @functools.wraps(target)
        def wrapper(self, *args, **kwargs):
            store()
            return target(self, *args, **kwargs)

        return wrapper

    return decorate


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


def hour_and_day_limits(test_class):
    """Run the tests of a class with the windows of one hour and one day only.

    The tests that send a dozen requests for one address in a row test the hourly and the daily
    window; the burst window (3 per 10 minutes, ``test_burst_limit.py``) would refuse them earlier.
    """
    return patch.object(rate_limits, "MAIL_ADDRESS_LIMITS", rate_limits.MAIL_ADDRESS_LIMITS[:2])(
        test_class
    )


def registration_data(email="new.person@example.org", **overrides):
    """The registration form: only the email address and the stamp of the bot check."""
    return {"email": email, "form_started": bot_check.new_stamp(), **overrides}


def reset_request_data(email, **overrides):
    """The form that asks for a link to reset the password, with the stamp of the bot check."""
    return {"email": email, "form_started": bot_check.new_stamp(), **overrides}


def password_data(password=PASSWORD, registration=None, **overrides):
    """The password step of the registration (D-058); registration is the token reference."""
    data = {
        "username": "ignored@example.org",
        "password1": password,
        "password2": password,
        **overrides,
    }
    if registration is not None:
        data["registration"] = str(registration)
    return data


def registration_reference(client, url="accounts:registration_method"):
    """The reference of the registration that a step page of the open link shows (the hidden field ``registration``)."""
    page = client.get(reverse(url))
    match = re.search(r'name="registration" value="(\d+)"', page.content.decode())
    assert match, "The step shows no registration reference."
    return match.group(1)


def choose_method(client, method="password", reference=None):
    """Step 2 of the registration, first screen: choose the sign-in method of the open link."""
    if reference is None:
        reference = registration_reference(client)
    return client.post(
        reverse("accounts:registration_method"), {"method": method, "registration": str(reference)}
    )


def completion_data(registration=None, **overrides):
    """The details step of the registration: no password, the privacy box ticked."""
    data = {**PROFILE, "privacy": "on", **overrides}
    if registration is not None:
        data["registration"] = str(registration)
    return data


def finish_registration(client, path, password=PASSWORD, details=None, **overrides):
    """Open the link at ``path`` and go through the choice of the method, the password and the details step.

    Returns the response of the last step. Extra keywords replace fields of the details form.
    """
    client.get(path)
    reference = registration_reference(client)
    choose_method(client, "password", reference)
    client.post(
        reverse("accounts:registration_password"), password_data(password, registration=reference)
    )
    return client.post(
        reverse("accounts:registration_complete"),
        completion_data(registration=reference, **{**(details or {}), **overrides}),
    )


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


def complete_second_factor(client, user):
    """Give the session of the client a verified TOTP device of the account."""
    device = AuthenticatorDevice.objects.filter(user=user, confirmed=True).first()
    if device is None:
        device = AuthenticatorDevice.objects.create_device(user, confirmed=True)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return device


class VerifiedClient(Client):
    """A client whose forced logins have completed the second factor (staff users need it)."""

    def force_login(self, user, backend=None):
        super().force_login(user, backend)
        if user.is_staff or user.is_superuser:
            complete_second_factor(self, user)


class ProvingClient(VerifiedClient):
    """A verified client that adds the proof of identity (the code of its device) to a post to the
    portal settings (D-088), unless ``auto_proof`` is off or the data brings its own proof."""

    auto_proof = True
    person = None

    def force_login(self, user, backend=None):
        super().force_login(user, backend)
        self.person = user

    def post(self, path, data=None, *args, **kwargs):
        if (
            self.auto_proof
            and path == reverse("admin:accounts_portalsettings_change", args=(1,))
            and isinstance(data, dict)
            and "otp_token" not in data
            and "current_password" not in data
            and self.person is not None
        ):
            device = AuthenticatorDevice.objects.filter(user=self.person, confirmed=True).first()
            if device is not None:
                data = {**data, "otp_token": code_for(device)}
        return super().post(path, data, *args, **kwargs)


def age_authentication(client, seconds=3600):
    """Make the login of the client's session older, so that the five minutes without the
    password for setting up two-factor authentication (D-055) are over."""
    from leg_platform.accounts.second_factor import AUTHENTICATED_AT_KEY

    session = client.session
    session[AUTHENTICATED_AT_KEY] = time.time() - seconds
    session.save()


def code_for(device):
    """The code that the device accepts now; an earlier use of a code is forgotten first."""
    AuthenticatorDevice.objects.filter(pk=device.pk).update(last_t=-1)
    device.refresh_from_db()
    generator = TOTP(
        device.bin_key, device.STEP, 0, device.DIGITS, device.drift
    )
    generator.time = time.time()
    return str(generator.token()).zfill(device.DIGITS)


class ParallelRunMixin:
    """``run_all``: the functions run at the same moment in their own threads and connections; returns the results."""

    def run_all(self, functions):
        barrier = threading.Barrier(len(functions))
        results = []

        def work(function):
            try:
                barrier.wait()
                results.append(function())
            finally:
                connection.close()

        threads = [threading.Thread(target=work, args=(function,)) for function in functions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results
