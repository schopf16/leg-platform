"""Negative authorization and access tests for the design package (phases 1 to 4, D-056 to D-059).

These tests complement the existing ones; they do not repeat them. They state what must NOT
happen:

* a person without a login reaches no page and no action of a logged-in person and learns nothing;
* an account never reads or changes the data, the requests, the links or the codes of another
  account;
* staff users without the right and normal accounts cannot change the portal settings, and the
  administration is offered to staff users only;
* the second address is no login name and no target of a password reset;
* a wrong password or a wrong code blocks every sensitive action, and the deletion needs the
  password and the box;
* every POST needs the CSRF token, POST-only addresses refuse GET, and a GET never changes state;
* the registration steps need the state of a valid link, a double submit and a forged ``username``
  or extra field change nothing, and a hit of the bot check looks like a request;
* the pages of a logged-in person are not stored.

Every group, account and permission is created inside the tests. Addresses are under
``example.org`` (RFC 2606).
"""

import re
from datetime import timedelta

from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import second_factor
from leg_platform.accounts.login_protection import LOGIN_FAILURE_LIMIT
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    HiddenTask,
    MailJob,
    PendingEmailChange,
    PortalSettings,
    RecoveryCode,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    VerifiedClient,
    age_authentication,
    code_for,
    completion_data,
    create_active_user,
    password_data,
    registration_data,
    reset_request_data,
    run_mail_queue,
    verification_path,
)
from leg_platform.accounts.tests.test_second_factor import (
    create_user_with_totp,
    log_in,
    recovery_codes_of,
    send_code,
    stored_digests,
)
from leg_platform.accounts.views import ENROLMENT_SESSION_KEY

ADDRESS_DATABASES = {"default", "addresses"}
HOME_URL = reverse("home")
LOGIN_URL = reverse("accounts:login")
PROFILE_URL = reverse("accounts:profile")
REGISTRATION_URL = reverse("accounts:registration")
PASSWORD_URL = reverse("accounts:registration_password")
COMPLETE_URL = reverse("accounts:registration_complete")
RESET_URL = reverse("accounts:password_reset")
PORTAL_SETTINGS_URL = reverse("admin:accounts_portalsettings_change", args=(1,))
SECOND = "second.address@example.org"
NEW = "new.address@example.org"

CARDS = ("personal", "email", "password", "two_factor", "delete")
TWO_FACTOR_STEPS = ("setup", "app", "codes_new", "disable", "codes")
# The pages and actions of a logged-in person that are addressed by name.
LOGGED_IN_NAMES = (
    "profile",
    "profile_edit",
    "email_change_request",
    "account_delete",
    "password_change",
    "two_factor",
    "two_factor_setup",
    "two_factor_confirm",
    "two_factor_qr",
    "recovery_codes",
    "recovery_codes_done",
    "two_factor_disable",
)
# A payload that would do harm if an action accepted it from a person without the right.
HARMFUL_PAYLOAD = {
    "current_password": PASSWORD,
    "otp_token": "000000",
    "confirm": "on",
    "saved": "on",
    "email": NEW,
    "secondary_email": SECOND,
    "new_password1": OTHER_PASSWORD,
    "new_password2": OTHER_PASSWORD,
    "first_name": "Changed",
    "last_name": "Changed",
    "is_staff": "on",
    "is_superuser": "on",
}


def logged_in_urls():
    """Every address of a logged-in person, including the states of the profile cards."""
    urls = [HOME_URL, PROFILE_URL]
    urls += [f"{PROFILE_URL}?edit={card}" for card in CARDS]
    urls += [f"{PROFILE_URL}?edit=two_factor&step={step}" for step in TWO_FACTOR_STEPS]
    urls += [reverse(f"accounts:{name}") for name in LOGGED_IN_NAMES[1:]]
    urls += [reverse("accounts:email_change_cancel", args=[kind]) for kind in ("login", "secondary", "bogus")]
    urls += [reverse("accounts:task_hide", args=[key]) for key in ("address", "two_factor", "bogus")]
    return urls


def snapshot():
    """The state that an unauthorised request must not change."""
    return {
        "users": list(
            User._base_manager.order_by("pk").values_list(
                "pk",
                "email",
                "secondary_email",
                "status",
                "password",
                "is_staff",
                "is_superuser",
                "first_name",
            )
        ),
        "hidden": HiddenTask.objects.count(),
        "pending": list(PendingEmailChange.objects.order_by("pk").values_list("pk", "address")),
        "tokens": VerificationToken.objects.count(),
        "jobs": MailJob.objects.count(),
        "devices": list(AuthenticatorDevice.objects.order_by("pk").values_list("pk", "confirmed")),
        "codes": sorted(RecoveryCode.objects.values_list("digest", flat=True)),
        "portal": list(PortalSettings.objects.values_list("privacy_url", "imprint_url")),
    }


def strip_token(response):
    return re.sub(rb'name="csrfmiddlewaretoken" value="[^"]+"', b"", response.content)


def pending(user, kind, address, hours=24):
    return PendingEmailChange.objects.create(
        user=user, kind=kind, address=address, expires_at=timezone.now() + timedelta(hours=hours)
    )


def request_link(client, email, address):
    """Step 1 of the registration and the worker; returns the path of the link of the mail."""
    client.post(REGISTRATION_URL, registration_data(email), REMOTE_ADDR=address)
    run_mail_queue()
    return verification_path(mail.outbox[-1])


class LoginRedirectMixin:
    def assert_login_redirect(self, response, *, exact=False):
        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        if exact:
            self.assertEqual(location, LOGIN_URL)
        else:
            self.assertTrue(
                location == LOGIN_URL or location.startswith(f"{LOGIN_URL}?next="), location
            )
        self.assertEqual(response.content, b"")


@tag("mariadb")
class AnonymousAccessTests(LoginRedirectMixin, TestCase):
    """A person without a login: every logged-in address leads to the login, with no body."""

    def setUp(self):
        self.victim, self.device = create_user_with_totp(
            "victim.person@example.org", first_name="Vanessa", last_name="Hiddenname"
        )
        self.victim.set_secondary_email("victim.second@example.org")
        pending(self.victim, "login", "victim.pending@example.org")
        HiddenTask.objects.create(user=self.victim, task_key="address")
        self.markers = (
            "Vanessa",
            "Hiddenname",
            "victim.person@example.org",
            "victim.second@example.org",
            "victim.pending@example.org",
        )
        self.anonymous = Client()

    def test_every_address_of_a_logged_in_person_leads_to_the_login_without_a_body(self):
        before = snapshot()
        for url in logged_in_urls():
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    if method == "get":
                        response = self.anonymous.get(url)
                    else:
                        response = self.anonymous.post(url, HARMFUL_PAYLOAD)
                    if url == HOME_URL and method == "post":
                        # The start page answers a script that posts to it with "method not allowed".
                        self.assertIn(response.status_code, (302, 405))
                        self.assertEqual(response.content, b"")
                        continue
                    self.assert_login_redirect(response)
                    for marker in self.markers:
                        self.assertNotIn(marker.encode(), response.content)
        self.assertEqual(snapshot(), before)

    def test_the_start_page_and_the_task_action_lead_to_the_plain_login_address(self):
        self.assert_login_redirect(self.anonymous.get(HOME_URL), exact=True)
        for key in ("address", "two_factor", "bogus"):
            with self.subTest(key=key):
                response = self.anonymous.post(reverse("accounts:task_hide", args=[key]))
                self.assert_login_redirect(response, exact=True)
        self.assertEqual(HiddenTask.objects.count(), 1)

    def test_a_login_link_with_a_foreign_target_is_not_followed(self):
        # The page of the login ignores an external ``next`` (existing test); here the redirect that
        # a protected address makes carries only the own path.
        response = self.anonymous.get(f"{PROFILE_URL}?edit=delete&next=https://evil.example/")
        self.assertEqual(
            response["Location"], f"{LOGIN_URL}?next={PROFILE_URL}%3Fedit%3Ddelete%26next%3Dhttps%3A//evil.example/"
        )

    def test_every_post_without_the_csrf_token_is_refused_before_anything_happens(self):
        client = Client(enforce_csrf_checks=True)
        before = snapshot()
        for url in logged_in_urls():
            with self.subTest(url=url):
                self.assertEqual(client.post(url, HARMFUL_PAYLOAD).status_code, 403)
        self.assertEqual(snapshot(), before)

    def test_the_pages_of_the_links_show_nothing_without_the_state_of_a_link(self):
        before = snapshot()
        for name in (
            "email_change_confirm",
            "email_change_stop_confirm",
            "two_factor_reset_confirm",
            "password_reset_set",
        ):
            url = reverse(f"accounts:{name}")
            for response in (self.anonymous.get(url), self.anonymous.post(url, {})):
                with self.subTest(name=name):
                    self.assertIn(response.status_code, (200, 302))
                    for marker in self.markers:
                        self.assertNotIn(marker.encode(), response.content)
                    self.assertNotIn("_auth_user_id", self.anonymous.session)
        self.assertEqual(snapshot(), before)

    def test_a_forged_or_unknown_link_leaks_nothing_and_changes_nothing(self):
        before = snapshot()
        forged = "A" * 43
        for name in (
            "registration_verify",
            "password_reset_verify",
            "email_change_verify",
            "email_change_stop_verify",
            "two_factor_reset_verify",
        ):
            with self.subTest(name=name):
                response = self.anonymous.get(reverse(f"accounts:{name}", args=[forged]), follow=True)
                self.assertContains(response, "Link nicht mehr gültig")
                for marker in self.markers:
                    self.assertNotContains(response, marker)
        self.assertEqual(snapshot(), before)

    def test_a_session_that_ended_gives_no_access(self):
        user = create_active_user("leaving.person@example.org")
        client = Client()
        client.force_login(user)
        copy_of_session = Client()
        copy_of_session.cookies.update(client.cookies)
        self.assertEqual(copy_of_session.get(PROFILE_URL).status_code, 200)

        client.post(reverse("accounts:logout"))

        self.assert_login_redirect(copy_of_session.get(PROFILE_URL))
        self.assert_login_redirect(copy_of_session.post(reverse("accounts:account_delete"), HARMFUL_PAYLOAD))
        self.assertTrue(User.objects.filter(pk=user.pk).exists())


@tag("mariadb")
class CrossAccountTests(TestCase):
    """Account A never reads or changes what belongs to account B."""

    databases = ADDRESS_DATABASES

    @classmethod
    def setUpTestData(cls):
        cls.a = create_active_user("user.a@example.org", first_name="Alma", last_name="Alphaname")
        cls.b = create_active_user("user.b@example.org", first_name="Bertram", last_name="Betaname")
        cls.b.set_secondary_email("b.second@example.org")

    def setUp(self):
        self.client_a = Client()
        self.client_a.force_login(self.a)
        self.client_b = Client()
        self.client_b.force_login(self.b)
        self.b_markers = (
            "Bertram",
            "Betaname",
            "user.b@example.org",
            "b.second@example.org",
            "b.pending@example.org",
        )

    def test_hiding_a_task_hides_it_for_the_own_account_only(self):
        response = self.client_a.post(
            reverse("accounts:task_hide", args=["two_factor"]),
            {"user": self.b.pk, "user_id": self.b.pk, "account": self.b.pk},
        )

        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        self.assertEqual(list(HiddenTask.objects.values_list("user_id", "task_key")), [(self.a.pk, "two_factor")])
        self.assertNotContains(self.client_a.get(HOME_URL), "2FA einrichten")
        self.assertContains(self.client_b.get(HOME_URL), "2FA einrichten")

    def test_cancelling_a_request_leaves_the_requests_and_links_of_another_account_alone(self):
        for user in (self.a, self.b):
            pending(user, "login", f"pending.{user.pk}@example.org")
            pending(user, "secondary", f"second.{user.pk}@example.org")
            VerificationToken.objects.issue(
                user, VerificationToken.Purpose.EMAIL_CHANGE, timedelta(hours=1), target_email=f"pending.{user.pk}@example.org"
            )
            VerificationToken.objects.issue(
                user, VerificationToken.Purpose.SECONDARY_EMAIL, timedelta(hours=1), target_email=f"second.{user.pk}@example.org"
            )

        for kind in ("login", "secondary"):
            self.client_a.post(reverse("accounts:email_change_cancel", args=[kind]), {"user": self.b.pk})

        self.assertFalse(PendingEmailChange.objects.filter(user=self.a).exists())
        self.assertFalse(VerificationToken.objects.filter(user=self.a).exists())
        self.assertEqual(PendingEmailChange.objects.filter(user=self.b).count(), 2)
        self.assertEqual(VerificationToken.objects.filter(user=self.b).count(), 2)
        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.EMAIL_CHANGE_CANCELLED)
        self.assertEqual({event.account_id for event in events}, {self.a.pk})

    def test_the_profile_and_the_start_page_show_nothing_of_another_account(self):
        pending(self.b, "login", "b.pending@example.org")
        urls = [HOME_URL, PROFILE_URL]
        urls += [f"{PROFILE_URL}?edit={card}" for card in CARDS]
        urls += [f"{PROFILE_URL}?edit=two_factor&step={step}" for step in TWO_FACTOR_STEPS]
        urls += [reverse("accounts:email_change_request"), reverse("accounts:account_delete")]
        for url in urls:
            with self.subTest(url=url):
                page = self.client_a.get(url)
                for marker in self.b_markers:
                    self.assertNotContains(page, marker)

    def test_the_stop_link_of_one_account_stops_only_the_requests_of_that_account(self):
        pending(self.a, "login", NEW)
        pending(self.b, "login", "b.pending@example.org")
        raw = VerificationToken.objects.issue(
            self.a, VerificationToken.Purpose.EMAIL_CHANGE_STOP, timedelta(hours=1)
        )
        other_raw = VerificationToken.objects.issue(
            self.b, VerificationToken.Purpose.EMAIL_CHANGE_STOP, timedelta(hours=1)
        )
        # B is logged in and opens the link of A: the link decides, B's own requests stay.
        stranger = self.client_b
        stranger.get(reverse("accounts:email_change_stop_verify", args=[raw]))
        stranger.post(reverse("accounts:email_change_stop_confirm"), {})

        self.assertFalse(PendingEmailChange.objects.filter(user=self.a).exists())
        self.assertEqual(PendingEmailChange.objects.filter(user=self.b).count(), 1)
        self.assertTrue(
            VerificationToken.objects.filter(user=self.b, purpose="email_change_stop").exists()
        )
        self.assertEqual(self.client_b.get(PROFILE_URL).status_code, 200)
        # The link of B was not used by that.
        self.assertIsNotNone(
            VerificationToken.objects.find_valid(other_raw, VerificationToken.Purpose.EMAIL_CHANGE_STOP)
        )

    def test_the_confirmation_of_a_link_changes_the_account_of_the_link_only(self):
        pending(self.a, "login", NEW)
        raw = VerificationToken.objects.issue(
            self.a, VerificationToken.Purpose.EMAIL_CHANGE, timedelta(hours=1), target_email=NEW
        )
        # B is logged in and confirms the link of A.
        self.client_b.get(reverse("accounts:email_change_verify", args=[raw]))
        self.client_b.post(reverse("accounts:email_change_confirm"), {})

        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.email, NEW)
        self.assertEqual(self.b.email, "user.b@example.org")
        self.assertEqual(self.b.secondary_email, "b.second@example.org")
        self.assertEqual(self.client_b.get(PROFILE_URL).status_code, 200)

    def test_a_link_of_one_purpose_is_dead_for_every_other_purpose(self):
        purposes = VerificationToken.Purpose
        tokens = {
            purposes.EMAIL_CHANGE: VerificationToken.objects.issue(
                self.b, purposes.EMAIL_CHANGE, timedelta(hours=1), target_email=NEW
            ),
            purposes.SECONDARY_EMAIL: VerificationToken.objects.issue(
                self.b, purposes.SECONDARY_EMAIL, timedelta(hours=1), target_email=SECOND
            ),
            purposes.EMAIL_CHANGE_STOP: VerificationToken.objects.issue(
                self.b, purposes.EMAIL_CHANGE_STOP, timedelta(hours=1)
            ),
            purposes.PASSWORD_RESET: VerificationToken.objects.issue(
                self.b, purposes.PASSWORD_RESET, timedelta(hours=1)
            ),
            purposes.TWO_FACTOR_RESET: VerificationToken.objects.issue(
                self.b, purposes.TWO_FACTOR_RESET, timedelta(hours=1)
            ),
        }
        pending(self.b, "login", NEW)
        pending(self.b, "secondary", SECOND)
        accepted = {
            "registration_verify": {purposes.REGISTRATION},
            "password_reset_verify": {purposes.PASSWORD_RESET},
            "email_change_verify": {purposes.EMAIL_CHANGE, purposes.SECONDARY_EMAIL},
            "email_change_stop_verify": {purposes.EMAIL_CHANGE_STOP},
            "two_factor_reset_verify": {purposes.TWO_FACTOR_RESET},
        }
        before = snapshot()
        for name, allowed in accepted.items():
            for purpose, raw in tokens.items():
                if purpose in allowed:
                    continue
                with self.subTest(view=name, purpose=purpose):
                    client = Client()
                    response = client.get(reverse(f"accounts:{name}", args=[raw]), follow=True)
                    self.assertContains(response, "Link nicht mehr gültig")
        self.assertEqual(snapshot(), before)

    def test_a_confirmation_session_cannot_be_used_for_the_stop_page_and_the_other_way_round(self):
        pending(self.b, "login", NEW)
        confirm = VerificationToken.objects.issue(
            self.b, VerificationToken.Purpose.EMAIL_CHANGE, timedelta(hours=1), target_email=NEW
        )
        stop = VerificationToken.objects.issue(
            self.b, VerificationToken.Purpose.EMAIL_CHANGE_STOP, timedelta(hours=1)
        )
        client = Client()
        client.get(reverse("accounts:email_change_verify", args=[confirm]))
        session = client.session
        session["email_change_stop"] = session.get("email_change", "")
        session.save()
        before = snapshot()

        stopped = client.post(reverse("accounts:email_change_stop_confirm"), {}, follow=True)

        self.assertContains(stopped, "Link nicht mehr gültig")
        self.assertEqual(snapshot(), before)
        other = Client()
        other.get(reverse("accounts:email_change_stop_verify", args=[stop]))
        done = other.post(reverse("accounts:email_change_confirm"), {}, follow=True)
        self.assertContains(done, "Link nicht mehr gültig")
        self.b.refresh_from_db()
        self.assertEqual(self.b.email, "user.b@example.org")

    def test_extra_fields_cannot_make_a_person_privileged_or_change_the_address(self):
        response = self.client_a.post(
            reverse("accounts:profile_edit"),
            {
                "salutation": "mrs",
                "first_name": "Changed",
                "last_name": "Alphaname",
                "street": "Examplestrasse",
                "house_number": "12a",
                "postal_code": "3000",
                "town": "Bern",
                "country": "CH",
                "is_staff": "on",
                "is_superuser": "on",
                "is_active": "",
                "email": "evil@example.org",
                "normalized_email": "evil@example.org",
                "secondary_email": "evil.second@example.org",
                "status": "blocked",
                "retention_obligation": "on",
                "password_change_required": "on",
                "customer_number": "1",
                "id": self.b.pk,
                "pk": self.b.pk,
            },
        )

        self.assertRedirects(response, f"{PROFILE_URL}#card-personal", fetch_redirect_response=False)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.first_name, "Changed")
        self.assertFalse(self.a.is_staff or self.a.is_superuser)
        self.assertEqual(self.a.email, "user.a@example.org")
        self.assertEqual(self.a.secondary_email, "")
        self.assertEqual(self.a.status, User.Status.ACTIVE)
        self.assertTrue(self.a.is_active)
        self.assertFalse(self.a.retention_obligation)
        self.assertFalse(self.a.password_change_required)
        self.assertEqual(self.b.first_name, "Bertram")
        self.assertEqual(self.b.email, "user.b@example.org")


@tag("mariadb")
class CrossAccountSecondFactorTests(TestCase):
    """The recovery codes, the devices and the setup of one account are of no use to another."""

    @classmethod
    def setUpTestData(cls):
        cls.a, cls.device_a = create_user_with_totp("user.a@example.org")
        cls.b, cls.device_b = create_user_with_totp("user.b@example.org")
        cls.codes_b = recovery_codes_of(cls.b.email)

    def setUp(self):
        self.client_a = Client()
        self.client_a.force_login(self.a)

    def test_a_recovery_code_of_another_account_does_not_complete_the_login(self):
        before = stored_digests(self.b)
        client = Client()
        log_in(client, self.a.email, address="10.81.0.1")

        response = send_code(client, self.codes_b[0], address="10.81.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", client.session)
        self.assertRedirects(
            client.get(PROFILE_URL), f"{LOGIN_URL}?next={PROFILE_URL}", fetch_redirect_response=False
        )
        self.assertEqual(stored_digests(self.b), before)

    def test_a_recovery_code_of_another_account_is_no_proof_for_a_sensitive_action(self):
        before = snapshot()
        for name, data in (
            ("account_delete", {"confirm": "on"}),
            ("two_factor_disable", {}),
            ("recovery_codes", {}),
            ("email_change_request", {"email": NEW, "secondary_email": ""}),
        ):
            with self.subTest(action=name):
                response = self.client_a.post(
                    reverse(f"accounts:{name}"),
                    {"current_password": PASSWORD, "otp_token": self.codes_b[0], **data},
                    REMOTE_ADDR="10.81.0.2",
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(snapshot(), before)

    def test_new_codes_and_turning_off_change_the_own_codes_and_devices_only(self):
        digests_b = stored_digests(self.b)
        digests_a = stored_digests(self.a)

        self.client_a.post(
            reverse("accounts:recovery_codes"),
            {"current_password": PASSWORD, "otp_token": code_for(self.device_a)},
        )
        self.assertNotEqual(stored_digests(self.a), digests_a)
        self.assertEqual(stored_digests(self.b), digests_b)

        self.client_a.post(
            reverse("accounts:two_factor_disable"),
            {"current_password": PASSWORD, "otp_token": code_for(self.device_a)},
        )
        self.assertFalse(second_factor.has_second_factor(self.a))
        self.assertTrue(second_factor.has_second_factor(self.b))
        self.assertEqual(stored_digests(self.b), digests_b)
        self.assertTrue(AuthenticatorDevice.objects.filter(pk=self.device_b.pk, confirmed=True).exists())

    def test_the_recovery_codes_are_never_in_a_page_that_is_opened_again(self):
        for url in (
            PROFILE_URL,
            f"{PROFILE_URL}?edit=two_factor&step=codes",
            f"{PROFILE_URL}?edit=two_factor&step=codes_new",
            reverse("accounts:recovery_codes"),
            reverse("accounts:two_factor"),
        ):
            with self.subTest(url=url):
                page = self.client_a.get(url).content.decode()
                for code in recovery_codes_of(self.a.email):
                    self.assertNotIn(code, page)
                    self.assertNotIn(f"{code[:4]}-{code[4:]}", page)

    def test_the_unfinished_setup_of_another_account_cannot_be_finished_or_seen(self):
        # A starts the setup (session of A only); B puts the reference of A's device in its own session.
        plain_a = create_active_user("plain.a@example.org")
        plain_b = create_active_user("plain.b@example.org")
        client_a, client_b = Client(), Client()
        client_a.force_login(plain_a)
        client_b.force_login(plain_b)
        client_a.post(reverse("accounts:two_factor_setup"), {})
        device = AuthenticatorDevice.objects.get(user=plain_a)
        self.assertFalse(device.confirmed)
        session = client_b.session
        session[ENROLMENT_SESSION_KEY] = device.pk
        session.save()

        self.assertRedirects(
            client_b.get(reverse("accounts:two_factor_confirm")),
            reverse("accounts:two_factor_setup"),
            fetch_redirect_response=False,
        )
        self.assertEqual(client_b.get(reverse("accounts:two_factor_qr")).status_code, 404)
        client_b.post(reverse("accounts:two_factor_confirm"), {"otp_token": code_for(device)})

        self.assertFalse(second_factor.has_second_factor(plain_b))
        device.refresh_from_db()
        self.assertFalse(device.confirmed)
        # The own session of A still reaches the QR code.
        self.assertEqual(client_a.get(reverse("accounts:two_factor_qr")).status_code, 200)


@tag("mariadb")
class ReauthenticationTests(TestCase):
    """A wrong password or a wrong code blocks the sensitive actions (docs/SECURITY.md, section 3)."""

    def setUp(self):
        self.counter = 0

    def account(self, name):
        """A new account with a second factor, logged in, and the client."""
        self.counter += 1
        user, device = create_user_with_totp(f"reauth.{name}@example.org")
        user.set_secondary_email(f"reauth.{name}.second@example.org")
        client = Client()
        client.force_login(user)
        return user, device, client, f"10.82.{self.counter}.1"

    # The actions: address, extra fields, whether the effect happened, and the success data.
    ACTIONS = {
        "email": ("accounts:email_change_request", {"email": NEW, "secondary_email": ""}),
        "new_codes": ("accounts:recovery_codes", {}),
        "disable": ("accounts:two_factor_disable", {}),
        "delete": ("accounts:account_delete", {"confirm": "on"}),
    }

    def effect(self, name, user, digests):
        if name == "email":
            return PendingEmailChange.objects.filter(user=user).exists()
        if name == "new_codes":
            return stored_digests(user) != digests
        if name == "disable":
            return not second_factor.has_second_factor(user)
        return not User.objects.filter(pk=user.pk).exists()

    def test_a_missing_or_wrong_code_changes_nothing_and_the_right_one_alone_does(self):
        for name, (url_name, extra) in self.ACTIONS.items():
            user, device, client, address = self.account(name)
            digests = stored_digests(user)
            jobs = MailJob.objects.count()
            other_code = recovery_codes_of(f"someone.else.{name}@example.org")[0]
            url = reverse(url_name)
            attempts = {
                "wrong code": {"otp_token": "000000"},
                "right password, wrong code": {"current_password": PASSWORD, "otp_token": "000000"},
                "right password alone": {"current_password": PASSWORD},
                "password in the code field": {"otp_token": PASSWORD},
                "code of nobody": {"otp_token": other_code},
                "nothing": {},
            }
            for label, data in attempts.items():
                with self.subTest(action=name, attempt=label):
                    response = client.post(url, {**extra, **data}, REMOTE_ADDR=address)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(self.effect(name, user, digests))
                    self.assertEqual(MailJob.objects.count(), jobs)
            # One proof (D-062): with a second factor the code alone is the proof.
            with self.subTest(action=name, attempt="the right code alone"):
                client.post(
                    url,
                    {**extra, "otp_token": code_for(device)},
                    REMOTE_ADDR=address,
                )
                self.assertTrue(self.effect(name, user, digests))

    def test_removing_the_second_address_needs_the_password(self):
        user = create_active_user("reauth.remove@example.org")
        user.set_secondary_email(SECOND)
        client = Client()
        client.force_login(user)
        data = {"email": user.email, "secondary_email": ""}

        for password in ("", OTHER_PASSWORD, None):
            with self.subTest(password=password):
                payload = dict(data)
                if password is not None:
                    payload["current_password"] = password
                response = client.post(reverse("accounts:email_change_request"), payload, REMOTE_ADDR="10.82.90.1")
                self.assertEqual(response.status_code, 200)
                user.refresh_from_db()
                self.assertEqual(user.secondary_email, SECOND)

        client.post(
            reverse("accounts:email_change_request"),
            {**data, "current_password": PASSWORD},
            REMOTE_ADDR="10.82.90.1",
        )
        user.refresh_from_db()
        self.assertEqual(user.secondary_email, "")

    def test_the_setup_of_the_second_factor_after_the_five_minutes_needs_the_password(self):
        user = create_active_user("reauth.setup@example.org")
        client = Client()
        client.force_login(user)
        age_authentication(client)
        url = reverse("accounts:two_factor_setup")

        for data in ({}, {"current_password": ""}, {"current_password": OTHER_PASSWORD}):
            with self.subTest(data=data):
                self.assertEqual(client.post(url, data, REMOTE_ADDR="10.82.91.1").status_code, 200)
                self.assertFalse(AuthenticatorDevice.objects.filter(user=user).exists())
                self.assertNotIn(ENROLMENT_SESSION_KEY, client.session)

        client.post(url, {"current_password": PASSWORD}, REMOTE_ADDR="10.82.91.1")
        self.assertTrue(AuthenticatorDevice.objects.filter(user=user).exists())

    def test_guessing_the_password_in_the_deletion_form_is_stopped(self):
        user = create_active_user("reauth.guess@example.org")
        client = Client()
        client.force_login(user)
        url = reverse("accounts:account_delete")

        for number in range(LOGIN_FAILURE_LIMIT):
            client.post(url, {"current_password": f"wrong password {number}", "confirm": "on"}, REMOTE_ADDR="10.82.92.1")
        client.post(url, {"current_password": PASSWORD, "confirm": "on"}, REMOTE_ADDR="10.82.92.1")

        self.assertTrue(User.objects.filter(pk=user.pk).exists())


@tag("mariadb")
class AccountDeletionNegativeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org", first_name="Holdername")
        cls.other = create_active_user("untouched.person@example.org")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("accounts:account_delete")

    def test_without_the_box_or_without_the_password_nothing_is_deleted(self):
        cases = {
            "no box": {"current_password": PASSWORD},
            "empty box": {"current_password": PASSWORD, "confirm": ""},
            "box is false": {"current_password": PASSWORD, "confirm": "false"},
            "no password": {"confirm": "on"},
            "empty password": {"current_password": "", "confirm": "on"},
            "nothing": {},
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                response = self.client.post(self.url, data, REMOTE_ADDR="10.83.0.1")
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Ihr Konto wurde gelöscht")
                self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
                self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)
        self.assertEqual(MailJob.objects.count(), 0)
        self.assertFalse(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DELETED).exists()
        )

    def test_a_deletion_removes_the_own_account_only_and_ends_every_session_of_it(self):
        second_session = Client()
        second_session.force_login(self.user)
        HiddenTask.objects.create(user=self.other, task_key="address")
        pending(self.other, "login", "other.pending@example.org")
        before_other = User.objects.filter(pk=self.other.pk).values_list("email", "password").get()

        response = self.client.post(
            self.url,
            {"current_password": PASSWORD, "confirm": "on", "user": self.other.pk, "id": self.other.pk},
            REMOTE_ADDR="10.83.0.2",
        )

        self.assertContains(response, "Ihr Konto wurde gelöscht")
        self.assertNotContains(response, "Holdername")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertEqual(User.objects.filter(pk=self.other.pk).values_list("email", "password").get(), before_other)
        self.assertEqual(HiddenTask.objects.filter(user=self.other).count(), 1)
        self.assertEqual(PendingEmailChange.objects.filter(user=self.other).count(), 1)
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 302)
        self.assertEqual(second_session.get(PROFILE_URL).status_code, 302)
        login = Client().post(
            LOGIN_URL, {"username": self.user.email, "password": PASSWORD}, REMOTE_ADDR="10.83.0.3"
        )
        self.assertNotIn("_auth_user_id", login.wsgi_request.session)
        run_mail_queue()
        self.assertEqual({address for message in mail.outbox for address in message.to}, {self.user.email})

    def test_the_result_page_is_not_reachable_again_by_address(self):
        # It is the answer of the deletion only; no address shows it again.
        for url in (self.url, PROFILE_URL):
            response = Client().get(url)
            self.assertEqual(response.status_code, 302)
            self.assertNotIn(b"wurde gel", response.content)


@tag("mariadb")
class AdministrationAccessTests(TestCase):
    """The portal settings need the right to see or to change them (D-073); the menu entry is for staff only."""

    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.person = create_active_user("normal.person@example.org")
        cls.staff = create_active_user("staff.person@example.org")
        cls.staff.set_privileges(is_staff=True, is_superuser=False)

    def attempt(self, client):
        """Try every way of reaching or changing the settings; return the final responses."""
        changelist = reverse("admin:accounts_portalsettings_changelist")
        history = reverse("admin:accounts_portalsettings_history", args=(1,))
        payload = {"privacy_url": "https://evil.example/privacy", "imprint_url": "https://evil.example/", "_save": "Sichern"}
        return [
            client.get(PORTAL_SETTINGS_URL),
            client.post(PORTAL_SETTINGS_URL, payload),
            client.get(changelist),
            client.get(history),
            client.post(reverse("admin:accounts_portalsettings_add"), payload),
            client.post(reverse("admin:accounts_portalsettings_delete", args=(1,)), {"post": "yes"}),
        ]

    def test_a_person_without_staff_status_is_sent_away_and_changes_nothing(self):
        group = Group.objects.create(name="Test settings editors")
        group.permissions.add(*Permission.objects.filter(content_type__app_label="accounts", content_type__model="portalsettings"))
        self.person.set_access_relations(groups=[group], user_permissions=[])
        client = VerifiedClient()
        client.force_login(self.person)
        before = snapshot()

        for response in self.attempt(client):
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response["Location"])
            self.assertNotIn("Portal-Einstellungen", response.content.decode())
        self.assertEqual(snapshot(), before)

    def test_without_any_login_nothing_can_be_reached_or_changed(self):
        before = snapshot()

        for response in self.attempt(Client()):
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response["Location"])
        self.assertEqual(snapshot(), before)

    def test_a_staff_user_without_the_rights_is_refused_everywhere_and_changes_nothing(self):
        client = VerifiedClient()
        client.force_login(self.staff)
        before = snapshot()

        for response in self.attempt(client):
            self.assertIn(response.status_code, (302, 403, 404))
            if response.status_code == 302:
                self.assertNotIn("/admin/accounts/portalsettings/1/change/", response["Location"])
        self.assertEqual(client.get(PORTAL_SETTINGS_URL).status_code, 403)
        self.assertEqual(client.post(PORTAL_SETTINGS_URL, {"privacy_url": "https://evil.example/"}).status_code, 403)
        self.assertEqual(snapshot(), before)
        self.assertNotContains(client.get(reverse("admin:index")), "Portal-Einstellungen")

    def test_a_staff_user_with_all_rights_of_the_model_still_cannot_add_delete_or_read_the_history(self):
        # D-073: view and change are ordinary rights; the single row cannot be added or deleted,
        # and the history names the acting accounts (system administrators only).
        group = Group.objects.create(name="Test settings staff")
        group.permissions.add(*Permission.objects.filter(content_type__app_label="accounts", content_type__model="portalsettings"))
        self.staff.set_access_relations(groups=[group], user_permissions=[])
        client = VerifiedClient()
        client.force_login(self.staff)

        self.assertEqual(client.get(PORTAL_SETTINGS_URL).status_code, 200)
        self.assertEqual(client.get(reverse("admin:accounts_portalsettings_history", args=(1,))).status_code, 403)
        self.assertEqual(client.get(reverse("admin:accounts_portalsettings_add")).status_code, 403)
        delete = client.post(reverse("admin:accounts_portalsettings_delete", args=(1,)), {"post": "yes"})
        self.assertEqual(delete.status_code, 403)
        self.assertTrue(PortalSettings.objects.filter(pk=1).exists())

    def test_the_menu_entry_administration_is_for_staff_users_only(self):
        group = Group.objects.create(name="Test everything")
        group.permissions.set(Permission.objects.all())
        self.person.set_access_relations(groups=[group], user_permissions=list(Permission.objects.all()))
        person_client = VerifiedClient()
        person_client.force_login(self.person)
        staff_client = VerifiedClient()
        staff_client.force_login(self.staff)

        for url in (HOME_URL, PROFILE_URL, f"{PROFILE_URL}?edit=email", reverse("accounts:two_factor")):
            with self.subTest(url=url):
                page = person_client.get(url)
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, "Verwaltung")
                self.assertNotContains(page, 'href="/admin/"')
                staff_page = staff_client.get(url)
                self.assertContains(staff_page, 'href="/admin/"')
                self.assertContains(staff_page, "Verwaltung")


@tag("mariadb")
class SecondAddressNegativeTests(TestCase):
    """The second address is never a login name and never the target of a password reset."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")
        cls.user.set_secondary_email(SECOND)

    def test_no_spelling_of_the_second_address_logs_in(self):
        unknown = Client().post(
            LOGIN_URL, {"username": "nobody@example.org", "password": PASSWORD}, REMOTE_ADDR="10.84.0.1"
        )
        for number, spelling in enumerate(
            (SECOND, SECOND.upper(), f"  {SECOND}  ", SECOND.title())
        ):
            with self.subTest(spelling=spelling):
                client = Client()
                response = client.post(
                    LOGIN_URL, {"username": spelling, "password": PASSWORD}, REMOTE_ADDR=f"10.84.1.{number}"
                )
                self.assertEqual(response.status_code, unknown.status_code)
                self.assertNotIn("_auth_user_id", client.session)
                self.assertEqual(client.get(PROFILE_URL).status_code, 302)
                # The answer is the one of an unknown address (the field keeps what was typed).
                clean = lambda page: re.sub(rb'value="[^"]*"', b"", strip_token(page))
                self.assertEqual(clean(response), clean(unknown))

    def test_the_second_address_gets_no_reset_and_the_answer_is_that_of_an_unknown_address(self):
        unknown = Client().post(RESET_URL, reset_request_data("nobody@example.org"), REMOTE_ADDR="10.84.2.1")
        for number, spelling in enumerate((SECOND, SECOND.upper())):
            with self.subTest(spelling=spelling):
                response = Client().post(RESET_URL, reset_request_data(spelling), REMOTE_ADDR=f"10.84.3.{number}")
                self.assertEqual((response.status_code, response["Location"]), (unknown.status_code, unknown["Location"]))
        run_mail_queue()
        self.assertEqual([message for message in mail.outbox if SECOND in message.to], [])
        self.assertFalse(
            VerificationToken.objects.filter(purpose=VerificationToken.Purpose.PASSWORD_RESET).exists()
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_a_reset_link_of_the_account_cannot_be_used_through_the_second_address(self):
        # The link goes to the login address only; a request for the second address makes none.
        Client().post(RESET_URL, reset_request_data(self.user.email), REMOTE_ADDR="10.84.4.1")
        run_mail_queue()
        self.assertEqual({address for message in mail.outbox for address in message.to}, {self.user.email})


@tag("mariadb")
class CsrfAndMethodTests(TestCase):
    """POST needs the CSRF token everywhere, POST-only addresses refuse GET, GET changes nothing."""

    databases = ADDRESS_DATABASES

    def setUp(self):
        self.user, self.device = create_user_with_totp("holder.person@example.org")
        self.user.set_secondary_email(SECOND)
        pending(self.user, "login", NEW)
        self.client = Client()
        self.client.force_login(self.user)

    def test_every_post_endpoint_refuses_a_request_without_the_token(self):
        urls = [
            reverse("accounts:task_hide", args=["two_factor"]),
            reverse("accounts:logout"),
            reverse("accounts:login"),
            reverse("accounts:login_second_factor"),
            REGISTRATION_URL,
            reverse("accounts:registration_resend"),
            PASSWORD_URL,
            COMPLETE_URL,
            RESET_URL,
            reverse("accounts:password_reset_resend"),
            reverse("accounts:password_reset_set"),
            reverse("accounts:email_change_confirm"),
            reverse("accounts:email_change_stop_confirm"),
            reverse("accounts:two_factor_reset_confirm"),
            reverse("addresses:lookup"),
            *[reverse(f"accounts:{name}") for name in LOGGED_IN_NAMES[1:]],
            reverse("accounts:email_change_cancel", args=["secondary"]),
        ]
        logged_in = Client(enforce_csrf_checks=True)
        logged_in.force_login(self.user)
        anonymous = Client(enforce_csrf_checks=True)
        before = snapshot()
        for url in urls:
            for label, client in (("logged in", logged_in), ("anonymous", anonymous)):
                with self.subTest(url=url, who=label):
                    for data in (HARMFUL_PAYLOAD, {**HARMFUL_PAYLOAD, "csrfmiddlewaretoken": "wrong"}):
                        response = client.post(url, data)
                        self.assertEqual(response.status_code, 403)
        self.assertEqual(snapshot(), before)
        self.assertEqual(logged_in.get(PROFILE_URL).status_code, 200)

    def test_the_post_only_addresses_refuse_get_and_change_nothing(self):
        before = snapshot()
        for name, args in (
            ("task_hide", ["two_factor"]),
            ("email_change_cancel", ["login"]),
            ("recovery_codes_done", []),
            ("logout", []),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"accounts:{name}", args=args)).status_code, 405)
        anonymous = Client()
        for name in ("registration_resend", "password_reset_resend"):
            with self.subTest(name=name):
                self.assertEqual(anonymous.get(reverse(f"accounts:{name}")).status_code, 405)
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)

    def test_the_get_only_addresses_refuse_post(self):
        before = snapshot()
        for name in ("two_factor", "two_factor_qr"):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(f"accounts:{name}"), HARMFUL_PAYLOAD).status_code, 405)
        self.assertEqual(snapshot(), before)

    def test_no_state_changes_through_a_get_request_with_every_parameter(self):
        before = snapshot()
        query = "confirm=on&current_password=x&otp_token=1&saved=on&user=1&email=x@example.org&secondary_email="
        for url in logged_in_urls():
            separator = "&" if "?" in url else "?"
            with self.subTest(url=url):
                response = self.client.get(f"{url}{separator}{query}")
                self.assertIn(response.status_code, (200, 302, 404, 405))
                self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)

    def test_a_failed_csrf_check_does_not_hide_a_task_or_delete_the_account(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        client.post(reverse("accounts:task_hide", args=["two_factor"]))
        client.post(reverse("accounts:account_delete"), {"current_password": PASSWORD, "confirm": "on", "otp_token": code_for(self.device)})

        self.assertFalse(HiddenTask.objects.exists())
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())


@tag("mariadb")
class RegistrationNegativeTests(TestCase):
    """The steps of the registration need the state of a valid link; abuse changes nothing."""

    databases = ADDRESS_DATABASES

    def test_steps_two_and_three_without_a_link_lead_to_the_page_of_an_invalid_link(self):
        client = Client()
        path = request_link(client, "new.person@example.org", "10.85.0.1")
        user = User.objects.get(email="new.person@example.org")
        token = VerificationToken.objects.get(user=user)
        fresh = Client()
        before = snapshot()
        invalid = reverse("accounts:registration_invalid")

        for url in (PASSWORD_URL, COMPLETE_URL):
            with self.subTest(url=url):
                self.assertRedirects(fresh.get(url), invalid, fetch_redirect_response=False)
                self.assertRedirects(
                    fresh.post(url, {**password_data(registration=token.pk), **completion_data(registration=token.pk)}),
                    invalid,
                    fetch_redirect_response=False,
                )
        self.assertEqual(snapshot(), before)
        self.assertEqual(User.objects.get(pk=user.pk).status, User.Status.UNVERIFIED)
        self.assertNotIn("_auth_user_id", fresh.session)
        # The link itself still works for the person who has it.
        self.assertRedirects(Client().get(path), PASSWORD_URL, fetch_redirect_response=False)

    def test_the_details_step_needs_the_password_step_of_the_same_link(self):
        client = Client()
        path = request_link(client, "new.person@example.org", "10.85.0.2")
        client.get(path)
        token = VerificationToken.objects.get()
        user = token.user

        response = client.post(COMPLETE_URL, completion_data(registration=token.pk))

        self.assertRedirects(response, PASSWORD_URL, fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.UNVERIFIED)
        self.assertFalse(user.has_usable_password())
        self.assertNotIn("_auth_user_id", client.session)

    def test_an_expired_link_cannot_complete_a_registration_that_was_started(self):
        client = Client()
        path = request_link(client, "new.person@example.org", "10.85.0.3")
        client.get(path)
        token = VerificationToken.objects.get()
        client.post(PASSWORD_URL, password_data(registration=token.pk))
        VerificationToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() - timedelta(seconds=1))

        response = client.post(COMPLETE_URL, completion_data(registration=token.pk))

        self.assertRedirects(response, reverse("accounts:registration_invalid"), fetch_redirect_response=False)
        self.assertEqual(User.objects.get(pk=token.user_id).status, User.Status.UNVERIFIED)
        self.assertRedirects(Client().get(path), reverse("accounts:registration_invalid"), fetch_redirect_response=False)

    def test_a_registration_link_of_an_active_account_opens_nothing(self):
        user = create_active_user("active.person@example.org")
        raw = VerificationToken.objects.issue(user, VerificationToken.Purpose.REGISTRATION, timedelta(hours=1))
        client = Client()

        response = client.get(reverse("accounts:registration_verify", args=[raw]))

        self.assertRedirects(response, reverse("accounts:registration_invalid"), fetch_redirect_response=False)
        self.assertEqual(client.get(PASSWORD_URL).status_code, 302)
        self.assertNotIn("_auth_user_id", client.session)

    def test_a_second_submit_and_a_copy_of_the_session_complete_nothing_twice(self):
        client = Client()
        path = request_link(client, "new.person@example.org", "10.85.0.4")
        client.get(path)
        token = VerificationToken.objects.get()
        client.post(PASSWORD_URL, password_data(registration=token.pk))
        clone = Client()
        clone.cookies.update(client.cookies)

        first = client.post(COMPLETE_URL, completion_data(registration=token.pk, first_name="First"))
        user = User.objects.get(email="new.person@example.org")
        number = user.customer_number_id
        again = client.post(COMPLETE_URL, completion_data(registration=token.pk, first_name="Second"))
        copied = clone.post(COMPLETE_URL, completion_data(registration=token.pk, first_name="Clone"))

        self.assertRedirects(first, HOME_URL, fetch_redirect_response=False)
        self.assertRedirects(again, HOME_URL, fetch_redirect_response=False)
        self.assertRedirects(copied, reverse("accounts:registration_invalid"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.customer_number_id), ("First", number))
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.REGISTRATION_COMPLETED).count(), 1
        )
        self.assertNotIn("_auth_user_id", clone.session)
        self.assertEqual(clone.get(PROFILE_URL).status_code, 302)
        self.assertFalse(VerificationToken.objects.filter(pk=token.pk).exists())
        # The link is used up: it cannot log in again.
        self.assertRedirects(Client().get(path), reverse("accounts:registration_invalid"), fetch_redirect_response=False)

    def test_a_forged_username_neither_changes_the_address_nor_touches_another_account(self):
        victim = create_active_user("victim.person@example.org")
        victim_hash = victim.password
        client = Client()
        path = request_link(client, "new.person@example.org", "10.85.0.5")
        client.get(path)
        token = VerificationToken.objects.get()

        client.post(
            PASSWORD_URL,
            password_data(OTHER_PASSWORD, registration=token.pk, username="victim.person@example.org"),
        )
        client.post(
            COMPLETE_URL,
            completion_data(registration=token.pk, username="victim.person@example.org", email="victim.person@example.org"),
        )

        user = User.objects.get(pk=token.user_id)
        self.assertEqual(user.email, "new.person@example.org")
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertTrue(user.check_password(OTHER_PASSWORD))
        victim.refresh_from_db()
        self.assertEqual(victim.password, victim_hash)
        self.assertTrue(victim.check_password(PASSWORD))
        self.assertEqual(victim.email, "victim.person@example.org")
        self.assertEqual(User.objects.filter(email="victim.person@example.org").count(), 1)

    def test_extra_fields_of_the_details_step_cannot_make_the_new_account_privileged(self):
        client = Client()
        path = request_link(client, "new.person@example.org", "10.85.0.6")
        client.get(path)
        token = VerificationToken.objects.get()
        client.post(PASSWORD_URL, password_data(registration=token.pk))

        client.post(
            COMPLETE_URL,
            completion_data(
                registration=token.pk,
                is_staff="on",
                is_superuser="on",
                status="blocked",
                email="evil@example.org",
                normalized_email="evil@example.org",
                secondary_email="evil.second@example.org",
                retention_obligation="on",
                password_change_required="on",
                privacy_accepted_url="https://evil.example/privacy",
                customer_number="1",
            ),
        )

        user = User.objects.get(pk=token.user_id)
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertEqual(user.email, "new.person@example.org")
        self.assertEqual(user.secondary_email, "")
        self.assertFalse(user.retention_obligation or user.password_change_required)
        self.assertNotIn("evil", user.privacy_accepted_url)
        self.assertFalse(user.groups.exists() or user.user_permissions.exists())

    def test_a_hit_of_the_bot_check_for_an_address_with_an_account_sends_nothing_and_looks_the_same(self):
        create_active_user("known.person@example.org")
        pages = []
        for number, extra in enumerate(({}, {"contact_fax_number": "https://spam.example"}, {"form_started": "forged"})):
            client = Client()
            response = client.post(
                REGISTRATION_URL,
                registration_data("known.person@example.org", **extra),
                REMOTE_ADDR=f"10.85.1.{number}",
            )
            pages.append((response.status_code, response["Location"], strip_token(client.get(response["Location"]))))
            if number == 0:
                run_mail_queue()
                mail.outbox.clear()
                MailJob.objects.all().delete()
            else:
                run_mail_queue()
                self.assertEqual(mail.outbox, [])
                self.assertEqual(MailJob.objects.count(), 0)
        self.assertEqual(pages[0], pages[1])
        self.assertEqual(pages[0], pages[2])

    def test_a_hit_of_the_bot_check_creates_no_account_and_no_session(self):
        for number, data in enumerate(
            (
                {"email": "bot.one@example.org", "contact_fax_number": "x"},
                {"email": "bot.two@example.org"},
                {"email": "bot.three@example.org", "form_started": "forged"},
            )
        ):
            with self.subTest(case=number):
                client = Client()
                response = client.post(REGISTRATION_URL, data, REMOTE_ADDR=f"10.85.2.{number}")
                self.assertEqual(response.status_code, 302)
                run_mail_queue()
        self.assertFalse(User._base_manager.filter(email__startswith="bot.").exists())
        self.assertEqual(MailJob.objects.count(), 0)
        self.assertEqual(mail.outbox, [])


@tag("mariadb")
class NoStoreTests(TestCase):
    """The pages of a logged-in person are not stored by the browser or a cache."""

    databases = ADDRESS_DATABASES

    def assert_no_store(self, response, url):
        self.assertIn("no-store", response.get("Cache-Control", ""), url)

    def test_every_page_of_a_logged_in_person_says_no_store(self):
        user, device = create_user_with_totp("holder.person@example.org")
        user.set_secondary_email(SECOND)
        pending(user, "login", NEW)
        client = Client()
        client.force_login(user)
        urls = [HOME_URL, PROFILE_URL]
        urls += [f"{PROFILE_URL}?edit={card}" for card in CARDS]
        urls += [f"{PROFILE_URL}?edit=two_factor&step={step}" for step in TWO_FACTOR_STEPS]
        urls += [reverse(f"accounts:{name}") for name in LOGGED_IN_NAMES if name not in ("two_factor_qr",)]
        for url in urls:
            with self.subTest(url=url):
                response = client.get(url, follow=False)
                self.assertIn(response.status_code, (200, 302, 405))
                # A page is not stored. A redirect carries no page (the setup pages of an account
                # that has a second factor already lead to the overview).
                if response.status_code == 200:
                    self.assert_no_store(response, url)

    def test_the_answers_that_show_secrets_and_results_say_no_store(self):
        plain = create_active_user("plain.person@example.org")
        client = Client()
        client.force_login(plain)
        client.post(reverse("accounts:two_factor_setup"), {})
        self.assert_no_store(client.get(reverse("accounts:two_factor_qr")), "qr")
        self.assert_no_store(client.get(reverse("accounts:two_factor_confirm")), "confirm")
        device = AuthenticatorDevice.objects.get(user=plain)
        shown = client.post(reverse("accounts:two_factor_confirm"), {"otp_token": code_for(device)})
        self.assertContains(shown, 'name="saved"')
        self.assert_no_store(shown, "codes after the setup")
        renewed = client.post(
            reverse("accounts:recovery_codes"),
            {"current_password": PASSWORD, "otp_token": code_for(device)},
            REMOTE_ADDR="10.86.0.1",
        )
        self.assertContains(renewed, 'name="saved"')
        self.assert_no_store(renewed, "new codes")
        deleted = client.post(
            reverse("accounts:account_delete"),
            {"current_password": PASSWORD, "otp_token": code_for(device), "confirm": "on"},
            REMOTE_ADDR="10.86.0.1",
        )
        self.assertContains(deleted, "Ihr Konto wurde gelöscht")
        self.assert_no_store(deleted, "deleted")

    def test_the_pages_of_the_registration_and_of_the_links_say_no_store(self):
        client = Client()
        path = request_link(client, "new.person@example.org", "10.86.1.1")
        token = VerificationToken.objects.get()
        self.assert_no_store(client.get(REGISTRATION_URL), REGISTRATION_URL)
        self.assert_no_store(client.get(reverse("accounts:registration_sent")), "sent")
        self.assert_no_store(client.get(path), path)
        client.get(path)
        self.assert_no_store(client.get(PASSWORD_URL), PASSWORD_URL)
        client.post(PASSWORD_URL, password_data(registration=token.pk))
        self.assert_no_store(client.get(COMPLETE_URL), COMPLETE_URL)
        self.assert_no_store(client.get(LOGIN_URL), LOGIN_URL)
        self.assert_no_store(client.get(reverse("accounts:registration_invalid")), "invalid")

    def test_the_pages_of_the_administration_say_no_store(self):
        administrator = create_active_user("administrator@example.org")
        administrator.set_privileges(is_staff=True, is_superuser=True)
        client = VerifiedClient()
        client.force_login(administrator)
        for url in (reverse("admin:index"), PORTAL_SETTINGS_URL):
            with self.subTest(url=url):
                self.assert_no_store(client.get(url), url)
