"""Setting up two-factor authentication without the password within five minutes of the login,
and the login after completing the registration (owner decisions of 2026-10-14, D-055)."""

import time
from datetime import timedelta
from unittest.mock import patch

from axes.models import AccessAttempt
from django.core import mail
from django.test import Client, SimpleTestCase, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import AuthenticatorDevice, LoginIpAttempt, User, VerificationToken
from leg_platform.accounts.second_factor import (
    AUTHENTICATED_AT_KEY,
    RECENT_AUTHENTICATION_WINDOW,
    recently_authenticated,
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
    verification_path,
)
from leg_platform.accounts.tests.test_registration import register
from leg_platform.accounts.tests.test_second_factor import (
    CODE_URL,
    create_user_with_totp,
    log_in,
    promote,
    send_code,
)
from leg_platform.accounts.views import REGISTRATION_SESSION_KEY

SETUP_URL = reverse("accounts:two_factor_setup")
CONFIRM_URL = reverse("accounts:two_factor_confirm")
HOME_URL = reverse("home")
PROFILE_URL = reverse("accounts:profile")
COMPLETE_URL = reverse("accounts:registration_complete")
PASSWORD_URL = reverse("accounts:registration_password")
INVALID_URL = reverse("accounts:registration_invalid")
ADDRESS_DATABASES = {"default", "addresses", "address_import"}


class FakeRequest:
    def __init__(self, session):
        self.session = session


class WindowRuleTests(SimpleTestCase):
    def test_the_window_is_five_minutes(self):
        self.assertEqual(RECENT_AUTHENTICATION_WINDOW, timedelta(minutes=5))

    def test_only_a_number_in_the_past_and_within_the_window_counts(self):
        now = time.time()
        cases = {
            now: True,
            now - 299: True,
            now - 301: False,
            now + 60: False,  # a time in the future is never valid
            True: False,
            "now": False,
            None: False,
        }
        for stored, expected in cases.items():
            with self.subTest(stored=stored):
                self.assertIs(recently_authenticated(FakeRequest({AUTHENTICATED_AT_KEY: stored})), expected)
        self.assertFalse(recently_authenticated(FakeRequest({})))

    def test_a_shorter_age_can_be_asked_for(self):
        request = FakeRequest({AUTHENTICATED_AT_KEY: time.time() - 100})

        self.assertTrue(recently_authenticated(request, timedelta(minutes=5)))
        self.assertFalse(recently_authenticated(request, timedelta(seconds=60)))


@tag("mariadb")
class RecentAuthenticationTests(TestCase):
    def setUp(self):
        self.user = create_active_user("active.person@example.org")

    def fresh_client(self):
        client = Client()
        log_in(client, self.user.email, address="10.20.0.1")
        return client

    def test_a_login_by_the_password_starts_the_window(self):
        client = self.fresh_client()

        self.assertLess(time.time() - client.session[AUTHENTICATED_AT_KEY], 30)

    def test_with_a_second_factor_the_window_starts_after_the_code_not_after_the_password(self):
        user, device = create_user_with_totp("holder.person@example.org")
        client = Client()
        log_in(client, user.email, address="10.20.0.2")
        self.assertNotIn(AUTHENTICATED_AT_KEY, client.session)

        send_code(client, code_for(device), address="10.20.0.2")

        self.assertLess(time.time() - client.session[AUTHENTICATED_AT_KEY], 30)

    def test_a_fresh_session_sets_up_without_the_password(self):
        client = self.fresh_client()

        page = client.get(SETUP_URL)
        self.assertNotContains(page, 'name="current_password"')
        self.assertContains(page, "2FA einrichten")
        # Opening the page changes nothing.
        self.assertFalse(AuthenticatorDevice.objects.exists())

        response = client.post(SETUP_URL, {})

        self.assertRedirects(response, CONFIRM_URL, fetch_redirect_response=False)
        device = AuthenticatorDevice.objects.get()
        self.assertFalse(device.confirmed)
        self.assertEqual(device.user_id, self.user.pk)

    def test_a_password_sent_in_the_window_is_not_needed_and_not_stored_anywhere(self):
        client = self.fresh_client()

        response = client.post(SETUP_URL, {"current_password": "anything at all"})

        self.assertRedirects(response, CONFIRM_URL, fetch_redirect_response=False)

    def test_the_device_is_still_confirmed_only_by_a_valid_code(self):
        client = self.fresh_client()
        client.post(SETUP_URL, {})
        device = AuthenticatorDevice.objects.get()

        wrong = client.post(CONFIRM_URL, {"otp_token": "000000"})

        self.assertEqual(wrong.status_code, 200)
        device.refresh_from_db()
        self.assertFalse(device.confirmed)
        right = client.post(CONFIRM_URL, {"otp_token": code_for(device)})
        self.assertEqual(right.status_code, 200)
        device.refresh_from_db()
        self.assertTrue(device.confirmed)

    def test_after_five_minutes_the_password_is_asked_for_without_an_error_message(self):
        client = self.fresh_client()
        age_authentication(client, seconds=301)

        page = client.get(SETUP_URL)
        self.assertContains(page, 'name="current_password"')
        self.assertNotContains(page, "invalid-feedback")
        # The button of the card sends no password: the prompt comes without an error.
        prompt = client.post(SETUP_URL, {})
        self.assertEqual(prompt.status_code, 200)
        self.assertContains(prompt, 'name="current_password"')
        self.assertNotContains(prompt, "invalid-feedback")
        self.assertFalse(AuthenticatorDevice.objects.exists())
        # A wrong password is refused, the right one starts the setup.
        wrong = client.post(SETUP_URL, {"current_password": "wrong password for this account"}, REMOTE_ADDR="10.20.0.3")
        self.assertContains(wrong, "Das Passwort stimmt nicht. Bitte versuchen Sie es erneut.")
        self.assertFalse(AuthenticatorDevice.objects.exists())
        right = client.post(SETUP_URL, {"current_password": PASSWORD}, REMOTE_ADDR="10.20.0.3")
        self.assertRedirects(right, CONFIRM_URL, fetch_redirect_response=False)

    def test_the_limit_is_measured_with_the_clock_of_the_server(self):
        client = self.fresh_client()
        self.assertEqual(client.post(SETUP_URL, {}).status_code, 302)
        AuthenticatorDevice.objects.all().delete()

        with patch("time.time", return_value=time.time() + 301):
            late = client.post(SETUP_URL, {})

        self.assertEqual(late.status_code, 200)
        self.assertContains(late, 'name="current_password"')
        self.assertFalse(AuthenticatorDevice.objects.exists())

    def test_activity_does_not_extend_the_window(self):
        client = self.fresh_client()
        age_authentication(client, seconds=200)
        stored = client.session[AUTHENTICATED_AT_KEY]

        for url in (PROFILE_URL, reverse("accounts:two_factor"), PROFILE_URL):
            client.get(url)

        self.assertEqual(client.session[AUTHENTICATED_AT_KEY], stored)
        with patch("time.time", return_value=time.time() + 150):
            late = client.post(SETUP_URL, {})
        self.assertContains(late, 'name="current_password"')

    def test_a_password_change_does_not_extend_the_window(self):
        client = self.fresh_client()
        age_authentication(client, seconds=290)
        stored = client.session[AUTHENTICATED_AT_KEY]

        response = client.post(
            reverse("accounts:password_change"),
            {"current_password": PASSWORD, "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
            REMOTE_ADDR="10.20.0.4",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.session[AUTHENTICATED_AT_KEY], stored)
        with patch("time.time", return_value=time.time() + 20):
            self.assertContains(client.post(SETUP_URL, {}), 'name="current_password"')

    def test_the_time_cannot_be_set_by_the_client(self):
        client = self.fresh_client()
        age_authentication(client)

        response = client.post(SETUP_URL, {AUTHENTICATED_AT_KEY: str(time.time()), "authenticated_at": "9999999999"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="current_password"')

    def test_the_time_ends_with_the_session(self):
        client = self.fresh_client()

        client.post(reverse("accounts:logout"))

        self.assertNotIn(AUTHENTICATED_AT_KEY, client.session)

    def test_removing_the_second_factor_and_new_codes_still_need_the_password(self):
        user, device = create_user_with_totp("holder.person@example.org")
        client = VerifiedClient()
        client.force_login(user)
        self.assertLess(time.time() - client.session[AUTHENTICATED_AT_KEY], 30)
        for name in ("two_factor_disable", "recovery_codes"):
            with self.subTest(name=name):
                response = client.post(reverse(f"accounts:{name}"), {}, REMOTE_ADDR="10.20.0.5")

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Geben Sie Ihr aktuelles Passwort ein.")
        self.assertTrue(AuthenticatorDevice.objects.get(user=user).confirmed)

    def test_the_email_change_and_the_deletion_still_need_the_password(self):
        client = VerifiedClient()
        client.force_login(self.user)
        for name in ("email_change_request", "account_delete"):
            with self.subTest(name=name):
                response = client.post(reverse(f"accounts:{name}"), {}, REMOTE_ADDR="10.20.0.6")

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Geben Sie Ihr aktuelles Passwort ein.")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_a_staff_user_without_a_second_factor_benefits_after_the_login(self):
        staff = promote(create_active_user("staff.person@example.org"))
        client = Client()
        log_in(client, staff.email, address="10.20.0.7")

        page = client.get(SETUP_URL)

        self.assertNotContains(page, 'name="current_password"')
        self.assertContains(page, "Für Ihre Rolle ist die Zwei-Faktor-Anmeldung (2FA) Pflicht.")
        self.assertRedirects(client.post(SETUP_URL, {}), CONFIRM_URL, fetch_redirect_response=False)

    def test_starting_needs_a_login_and_a_csrf_token_and_the_page_changes_nothing(self):
        anonymous = Client().post(SETUP_URL, {})
        self.assertRedirects(anonymous, f"{reverse('accounts:login')}?next={SETUP_URL}", fetch_redirect_response=False)
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        with self.assertLogs("django.security.csrf", "WARNING"):
            self.assertEqual(browser.post(SETUP_URL, {}).status_code, 403)
        client = self.fresh_client()
        client.get(SETUP_URL)
        client.get(SETUP_URL)
        self.assertFalse(AuthenticatorDevice.objects.exists())

    def test_the_notification_and_the_log_still_follow_the_confirmation(self):
        from leg_platform.accounts.models import MailJob, SecurityEvent

        client = self.fresh_client()
        client.post(SETUP_URL, {})
        device = AuthenticatorDevice.objects.get()

        client.post(CONFIRM_URL, {"otp_token": code_for(device)})

        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_ENABLED).exists())
        self.assertTrue(SecurityEvent.objects.filter(event=SecurityEvent.Event.TWO_FACTOR_ENABLED).exists())


@tag("mariadb")
class RegistrationLoginTests(TestCase):
    # Completing the registration reads the address directory (advisory check, D-033).
    databases = ADDRESS_DATABASES

    def setUp(self):
        register(self.client)
        self.path = verification_path(mail.outbox[0])
        self.token = VerificationToken.objects.get()
        self.client.get(self.path)

    def complete(self, client=None, **overrides):
        client = client or self.client
        password_fields = {k: v for k, v in overrides.items() if k.startswith("password")}
        details = {k: v for k, v in overrides.items() if k not in password_fields}
        response = client.post(
            PASSWORD_URL, password_data(registration=self.token.pk, **password_fields)
        )
        if response.status_code != 302 or response["Location"] != COMPLETE_URL:
            return response
        return client.post(COMPLETE_URL, completion_data(registration=self.token.pk, **details))

    def test_the_completion_logs_the_person_in_and_leads_to_the_start_page(self):
        before = self.client.session.session_key

        response = self.complete()

        self.assertRedirects(response, HOME_URL)
        user = User.objects.get()
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        self.assertNotEqual(self.client.session.session_key, before)
        self.assertLess(time.time() - self.client.session[AUTHENTICATED_AT_KEY], 30)
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)

    def test_the_account_has_no_rights_and_the_number_is_issued_once(self):
        self.complete()

        user = User.objects.get()
        self.assertEqual((user.is_staff, user.is_superuser), (False, False))
        self.assertEqual(user.groups.count() + user.user_permissions.count(), 0)
        number = user.customer_number_id
        self.assertRegex(number, r"\A[0-9]{6}\Z")
        self.assertNotEqual(self.client.get("/admin/").status_code, 200)
        # A second submission with the used link changes nothing; the person is logged in, so
        # the answer is the start page (B3).
        again = self.complete()
        self.assertRedirects(again, HOME_URL, fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual(user.customer_number_id, number)

    def test_no_failed_attempt_or_lockout_is_recorded(self):
        self.complete()

        self.assertEqual(AccessAttempt.objects.count(), 0)
        self.assertEqual(LoginIpAttempt.objects.count(), 0)

    def test_the_session_hash_belongs_to_the_new_password(self):
        self.complete()
        user = User.objects.get()

        self.assertEqual(self.client.session["_auth_user_hash"], user.get_session_auth_hash())

    def test_a_used_link_cannot_log_anyone_in(self):
        self.complete()
        other = Client()
        opened = other.get(self.path)

        self.assertRedirects(opened, INVALID_URL, fetch_redirect_response=False)
        posted = self.complete(other)
        self.assertRedirects(posted, INVALID_URL, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", other.session)

    def test_a_replaced_link_cannot_log_anyone_in(self):
        stranger = Client()
        register(stranger, "new.person@example.org")  # replaces the pending registration
        self.assertFalse(VerificationToken.objects.filter(pk=self.token.pk).exists())

        response = self.complete()

        self.assertRedirects(response, INVALID_URL, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(User.objects.get().has_usable_password())

    def test_an_invalid_form_logs_nobody_in(self):
        response = self.complete(password2="a different passphrase that does not match")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertIn(REGISTRATION_SESSION_KEY, self.client.session)

    def test_the_start_page_after_the_registration_offers_the_setup_as_a_task(self):
        self.complete()
        user = User.objects.get()

        page = self.client.get(HOME_URL)

        self.assertContains(page, "Guten Tag, Anna Example")
        self.assertContains(page, user.customer_number_display)
        self.assertContains(page, "Zwei-Faktor-Anmeldung")
        self.assertContains(page, f'<form method="post" action="{SETUP_URL}"')
        self.assertNotContains(page, "Melden Sie sich mit Ihrer E-Mail-Adresse")
        # The button starts the setup in one more step; the old welcome page is gone (D-058).
        self.assertRedirects(self.client.post(SETUP_URL, {}), CONFIRM_URL, fetch_redirect_response=False)
        self.assertEqual(Client().get("/welcome/").status_code, 404)
