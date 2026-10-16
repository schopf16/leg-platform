"""Password change, two-factor authentication and the central second factor check
(docs/SECURITY.md, sections 3 and 11; D-004, D-017, D-040)."""

import hashlib
import re
import threading
import time
from base64 import b32encode
from datetime import timedelta
from io import StringIO
from unittest import mock

from axes.models import AccessAttempt, AccessAttemptExpiration
from django.conf import settings
from django.contrib import admin
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
    tag,
)
from django.urls import reverse
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.oath import TOTP

from leg_platform.accounts import encryption, second_factor, totp
from leg_platform.accounts.login_protection import LOGIN_FAILURE_LIMIT
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    ConfirmationFailure,
    LoginIpAttempt,
    LoginIpBucket,
    MailJob,
    RateLimitEvent,
    RecoveryCode,
    RecoveryCodeDevice,
    SecurityEvent,
    SessionCodeFailure,
    User,
    VerificationToken,
    recovery_code_digest,
)
from leg_platform.accounts.password_reset import PASSWORD_RESET_LIFETIME
from leg_platform.accounts.tests.helpers import (
    age_authentication,
    OTHER_PASSWORD,
    PASSWORD,
    code_for,
    complete_second_factor,
    create_active_user,
    run_mail_queue,
)

LOGIN_URL = reverse("accounts:login")
CODE_URL = reverse("accounts:login_second_factor")
RECOVERY_CODES = ["abcd2345", "efgh6723", "ijkl4567"]
SECRET_SENTINEL = "otp-sentinel-654321"
DEFAULT_EMAIL = "active.person@example.org"
RECOVERY_DIGESTS = {recovery_code_digest(code) for code in RECOVERY_CODES}


def recovery_codes_of(email):
    """The codes of an account; the default account has the fixed ones, others their own."""
    if email == DEFAULT_EMAIL:
        return RECOVERY_CODES
    return [
        b32encode(hashlib.sha256(f"{email}{number}".encode()).digest()[:5]).decode().lower()
        for number in range(3)
    ]


def stored_digests(user=None):
    codes = RecoveryCode.objects.all()
    if user is not None:
        codes = codes.filter(device__user=user)
    return set(codes.values_list("digest", flat=True))


def codes_in(response):
    """The recovery codes that a page shows (the only place they are ever readable)."""
    found = re.findall(r"<li>([a-z2-7]{4})-([a-z2-7]{4})</li>", response.content.decode())
    return [first + second for first, second in found]


def create_user_with_totp(email=DEFAULT_EMAIL, **profile):
    user = create_active_user(email, **profile)
    device = AuthenticatorDevice.objects.create_device(user, confirmed=True)
    recovery = RecoveryCodeDevice.objects.create(user=user, name="recovery codes", confirmed=True)
    recovery.add_codes(recovery_codes_of(email))
    return user, device


def promote(user):
    user.set_privileges(is_staff=True, is_superuser=False)
    return user


def create_totp_device(user):
    return AuthenticatorDevice.objects.create_device(user, confirmed=True)


MIDDLEWARE_WITHOUT_SECOND_FACTOR = [
    name for name in settings.MIDDLEWARE if "SecondFactorMiddleware" not in name
]


def log_in(client, email, password=PASSWORD, address="10.1.0.1", **extra):
    return client.post(
        LOGIN_URL, {"username": email, "password": password}, REMOTE_ADDR=address, **extra
    )


def send_code(client, code, address="10.1.0.1"):
    return client.post(CODE_URL, {"otp_token": code}, REMOTE_ADDR=address)


@tag("mariadb")
class SecondFactorLoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def test_the_password_alone_does_not_log_in(self):
        response = log_in(self.client, self.user.email)

        self.assertRedirects(response, CODE_URL)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertRedirects(
            self.client.get(reverse("accounts:profile")), f"{LOGIN_URL}?next=/profile/"
        )

    def test_the_password_and_the_code_log_in(self):
        log_in(self.client, self.user.email)

        response = send_code(self.client, code_for(self.device))

        self.assertRedirects(response, reverse("home"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertEqual(self.client.session[DEVICE_ID_SESSION_KEY], self.device.persistent_id)
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)

    def test_a_wrong_code_does_not_log_in(self):
        log_in(self.client, self.user.email)

        response = send_code(self.client, "000000")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(response, "alert-danger", count=0)
        self.assertContains(response, "invalid-feedback")

    def test_the_code_page_needs_the_password_step(self):
        self.assertRedirects(self.client.get(CODE_URL), LOGIN_URL, fetch_redirect_response=False)
        response = send_code(self.client, code_for(self.device))

        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_password_step_of_another_account_is_not_enough_for_a_code_of_this_one(self):
        other, other_device = create_user_with_totp("other.person@example.org")
        log_in(self.client, other.email)

        send_code(self.client, code_for(self.device))

        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_pending_login_expires(self):
        log_in(self.client, self.user.email)
        session = self.client.session
        session[second_factor.PENDING_SESSION_KEY]["started"] -= 301
        session.save()

        response = send_code(self.client, code_for(self.device))

        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_pending_login_ends_when_the_account_is_blocked(self):
        log_in(self.client, self.user.email)
        self.user.set_status(User.Status.BLOCKED)

        response = send_code(self.client, code_for(self.device))

        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_pending_login_ends_when_the_second_factor_was_removed(self):
        log_in(self.client, self.user.email)
        AuthenticatorDevice.objects.all().delete()

        self.assertRedirects(self.client.get(CODE_URL), LOGIN_URL, fetch_redirect_response=False)

    def test_a_code_cannot_be_used_twice(self):
        log_in(self.client, self.user.email)
        code = code_for(self.device)
        send_code(self.client, code)
        other_client = Client()
        log_in(other_client, self.user.email, address="10.1.0.2")

        send_code(other_client, code, address="10.1.0.2")

        self.assertNotIn("_auth_user_id", other_client.session)

    def test_a_recovery_code_works_once_and_in_any_notation(self):
        log_in(self.client, self.user.email)

        response = send_code(self.client, " ABCD-2345 ")

        self.assertRedirects(response, reverse("home"))
        self.assertEqual(
            self.client.session[DEVICE_ID_SESSION_KEY],
            RecoveryCodeDevice.objects.get().persistent_id,
        )
        self.assertEqual(len(stored_digests()), len(RECOVERY_CODES) - 1)
        again = Client()
        log_in(again, self.user.email, address="10.1.0.2")
        send_code(again, "abcd2345", address="10.1.0.2")
        self.assertNotIn("_auth_user_id", again.session)

    def test_the_use_of_a_recovery_code_is_logged_without_the_code(self):
        log_in(self.client, self.user.email)

        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                send_code(self.client, "abcd2345")

        self.assertEqual(len(logs.output), 1)
        self.assertIn(f"account {self.user.pk}", logs.output[0])
        self.assertNotIn("abcd", logs.output[0])

    def test_the_session_id_changes_with_the_password_and_with_the_code(self):
        session = self.client.session
        session["probe"] = "value"
        session.save()
        planted = self.client.session.session_key

        log_in(self.client, self.user.email)
        after_password = self.client.session.session_key
        send_code(self.client, code_for(self.device))
        after_code = self.client.session.session_key

        self.assertNotEqual(planted, after_password)
        self.assertNotEqual(after_password, after_code)

    def test_an_internal_redirect_target_is_followed_and_an_external_one_ignored(self):
        for target, expected in (("/profile/", "/profile/"), ("https://attacker.example.net/", "/")):
            with self.subTest(target=target):
                client = Client()
                client.post(
                    LOGIN_URL,
                    {"username": self.user.email, "password": PASSWORD, "next": target},
                    REMOTE_ADDR="10.1.1.1",
                )
                response = send_code(client, code_for(self.device), address="10.1.1.1")
                self.assertRedirects(response, expected, fetch_redirect_response=False)

    def test_a_wrong_password_never_reaches_the_code_step(self):
        response = log_in(self.client, self.user.email, "wrong password for this account")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(second_factor.PENDING_SESSION_KEY, self.client.session)

    def test_an_account_without_a_second_factor_logs_in_with_the_password_alone(self):
        plain = create_active_user("plain.person@example.org")

        response = log_in(self.client, plain.email)

        self.assertRedirects(response, reverse("home"))
        self.assertEqual(self.client.session["_auth_user_id"], str(plain.pk))

    def test_the_ip_slot_is_released_after_a_complete_login(self):
        log_in(self.client, self.user.email)
        self.assertEqual(LoginIpAttempt.objects.count(), 0)

        send_code(self.client, code_for(self.device))

        self.assertEqual(LoginIpAttempt.objects.count(), 0)

    def test_a_wrong_code_keeps_its_ip_attempt(self):
        log_in(self.client, self.user.email)

        send_code(self.client, "000000")

        self.assertEqual(LoginIpAttempt.objects.filter(pending=False).count(), 1)
        self.assertEqual(LoginIpAttempt.objects.filter(pending=True).count(), 0)

    def test_wrong_codes_lock_the_account_and_a_correct_code_is_then_refused(self):
        log_in(self.client, self.user.email)
        responses = [send_code(self.client, "000000") for _ in range(LOGIN_FAILURE_LIMIT)]

        self.assertEqual(responses[-1].status_code, 429)
        # The lockout answer after wrong codes is never stored (security review of unit A, L-4).
        self.assertIn("no-store", responses[-1]["Cache-Control"])
        response = send_code(self.client, code_for(self.device))

        self.assertEqual(response.status_code, 429)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("_auth_user_id", self.client.session)
        # The same account is locked at the password step as well.
        self.assertEqual(log_in(Client(), self.user.email, address="10.1.9.9").status_code, 429)

    def test_wrong_codes_from_one_address_lock_that_address(self):
        others = [create_user_with_totp(f"person{n}@example.org")[0] for n in range(LOGIN_FAILURE_LIMIT)]
        for other in others:
            client = Client()
            log_in(client, other.email, address="10.1.8.1")
            send_code(client, "000000", address="10.1.8.1")

        response = log_in(Client(), self.user.email, address="10.1.8.1")

        self.assertEqual(response.status_code, 429)

    def test_the_code_is_not_stored_by_the_abuse_protection(self):
        log_in(self.client, self.user.email)

        send_code(self.client, SECRET_SENTINEL)

        self.assertTrue(AccessAttempt.objects.exists())
        for attempt in AccessAttempt.objects.all():
            self.assertNotIn(SECRET_SENTINEL, attempt.post_data)

    def test_the_code_step_needs_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)

        self.assertEqual(client.post(CODE_URL, {"otp_token": "123456"}).status_code, 403)

    def test_the_code_page_is_not_cached(self):
        log_in(self.client, self.user.email)

        self.assertIn("no-store", self.client.get(CODE_URL)["Cache-Control"])


@tag("mariadb")
class CentralCheckTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = promote(create_active_user("staff.person@example.org"))
        cls.administrator = create_active_user("admin.person@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)

    def test_the_check_fails_closed_without_the_middleware(self):
        request = RequestFactory().get("/")
        request.user = self.staff

        self.assertFalse(second_factor.is_second_factor_complete(request.user))

    def test_staff_users_and_administrators_must_use_the_second_factor(self):
        plain = create_active_user("plain.person@example.org")

        self.assertTrue(second_factor.requires_second_factor(self.staff))
        self.assertTrue(second_factor.requires_second_factor(self.administrator))
        self.assertFalse(second_factor.requires_second_factor(plain))

    def test_a_staff_session_without_a_second_factor_can_only_set_it_up(self):
        client = Client()
        log_in(client, self.staff.email)

        self.assertEqual(client.session["_auth_user_id"], str(self.staff.pk))
        for url in (
            reverse("accounts:profile"),
            reverse("accounts:password_change"),
            reverse("accounts:two_factor"),
            reverse("accounts:recovery_codes"),
            "/admin/",
            "/",
        ):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertRedirects(
                    response,
                    reverse("accounts:two_factor_setup"),
                    fetch_redirect_response=False,
                )
        self.assertEqual(client.get(reverse("accounts:two_factor_setup")).status_code, 200)

    def test_such_a_session_can_log_out(self):
        client = Client()
        log_in(client, self.staff.email)

        client.post(reverse("accounts:logout"))

        self.assertNotIn("_auth_user_id", client.session)

    def test_a_staff_session_with_a_second_factor_that_was_not_completed_is_ended(self):
        create_totp_device(self.staff)
        client = Client()
        client.force_login(self.staff)

        response = client.get(reverse("accounts:profile"))

        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", client.session)

    def test_a_staff_session_with_a_completed_second_factor_has_its_privileges(self):
        client = Client()
        client.force_login(self.administrator)
        complete_second_factor(client, self.administrator)

        self.assertEqual(client.get(reverse("accounts:profile")).status_code, 200)
        self.assertEqual(client.get("/admin/").status_code, 200)

    def test_a_promotion_takes_effect_in_the_open_session(self):
        user = create_active_user("promoted.person@example.org")
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(reverse("accounts:profile")).status_code, 200)

        promote(user)

        self.assertRedirects(
            client.get(reverse("accounts:profile")),
            reverse("accounts:two_factor_setup"),
            fetch_redirect_response=False,
        )

    @override_settings(MIDDLEWARE=MIDDLEWARE_WITHOUT_SECOND_FACTOR)
    def test_the_administration_stays_closed_without_the_middleware(self):
        client = Client()
        client.force_login(self.administrator)

        response = client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("admin:login")))

    def test_the_administration_login_and_password_change_are_the_portal_pages(self):
        client = Client()

        login_response = client.get(reverse("admin:login"), {"next": "/admin/"})
        client.force_login(self.administrator)
        complete_second_factor(client, self.administrator)
        password_response = client.get(reverse("admin:password_change"))

        self.assertRedirects(
            login_response, f"{LOGIN_URL}?next=/admin/", fetch_redirect_response=False
        )
        self.assertRedirects(
            password_response, reverse("accounts:password_change"), fetch_redirect_response=False
        )

    def test_the_devices_are_not_in_the_administration(self):
        registered = {model._meta.label for model in admin.site._registry}

        self.assertNotIn("accounts.AuthenticatorDevice", registered)
        self.assertNotIn("accounts.RecoveryCodeDevice", registered)
        self.assertNotIn("accounts.RecoveryCode", registered)
        client = Client()
        client.force_login(self.administrator)
        complete_second_factor(client, self.administrator)
        self.assertEqual(client.get("/admin/accounts/authenticatordevice/").status_code, 404)
        self.assertEqual(client.get("/admin/accounts/recoverycodedevice/").status_code, 404)


@tag("mariadb")
class EnrolmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()

    def setUp(self):
        self.client.force_login(self.user)
        # These tests are about the password prompt; the five minutes without it have their own
        # tests (RecentAuthenticationTests).
        age_authentication(self.client)

    def start(self, password=PASSWORD):
        return self.client.post(
            reverse("accounts:two_factor_setup"),
            {"current_password": password},
            REMOTE_ADDR="10.2.0.1",
        )

    def test_the_setup_asks_for_the_password_first(self):
        response = self.start("wrong password for this account")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AuthenticatorDevice.objects.exists())

    def test_the_setup_creates_an_unconfirmed_secret(self):
        response = self.start()

        self.assertRedirects(response, reverse("accounts:two_factor_confirm"))
        device = AuthenticatorDevice.objects.get()
        self.assertFalse(device.confirmed)
        self.assertFalse(second_factor.has_second_factor(self.user))
        self.assertEqual(len(device.bin_key), 20)

    def test_starting_again_replaces_the_unconfirmed_secret(self):
        self.start()
        first = AuthenticatorDevice.objects.get().bin_key

        self.start()

        self.assertNotEqual(AuthenticatorDevice.objects.get().bin_key, first)

    def test_the_confirmation_page_shows_the_secret_and_a_qr_code_of_its_own(self):
        self.start()
        device = AuthenticatorDevice.objects.get()

        response = self.client.get(reverse("accounts:two_factor_confirm"))

        self.assertContains(response, reverse("accounts:two_factor_qr"))
        self.assertContains(response, totp.secret_for_display(device))
        self.assertNotContains(response, "data:image")
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_qr_code_is_an_svg_that_is_not_cached(self):
        self.start()
        device = AuthenticatorDevice.objects.get()

        response = self.client.get(reverse("accounts:two_factor_qr"))

        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(response.content.startswith(b"<svg"))
        self.assertNotIn(b"<script", response.content)
        self.assertTrue(device.config_url.startswith("otpauth://totp/"))
        self.assertIn("issuer=", device.config_url)

    def test_the_qr_code_of_another_account_cannot_be_reached(self):
        other = create_active_user("other.person@example.org")
        AuthenticatorDevice.objects.create_device(other, confirmed=False)

        response = self.client.get(reverse("accounts:two_factor_qr"))

        self.assertEqual(response.status_code, 404)

    def test_the_confirmation_without_a_secret_leads_to_the_start(self):
        response = self.client.get(reverse("accounts:two_factor_confirm"))

        self.assertRedirects(response, reverse("accounts:two_factor_setup"))

    def test_a_wrong_code_does_not_turn_the_second_factor_on(self):
        self.start()

        response = self.client.post(reverse("accounts:two_factor_confirm"), {"otp_token": "000000"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AuthenticatorDevice.objects.get().confirmed)
        self.assertFalse(RecoveryCodeDevice.objects.exists())

    def test_the_right_code_turns_it_on_and_shows_the_recovery_codes_once(self):
        self.start()
        device = AuthenticatorDevice.objects.get()

        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("accounts:two_factor_confirm"), {"otp_token": code_for(device)}
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuthenticatorDevice.objects.get().confirmed)
        codes = codes_in(response)
        self.assertEqual(len(codes), totp.RECOVERY_CODE_COUNT)
        self.assertEqual(stored_digests(), {recovery_code_digest(code) for code in codes})
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.session[DEVICE_ID_SESSION_KEY], device.persistent_id)
        self.assertEqual(len(logs.output), 1)
        for text in (device.bin_key.hex(), *codes):
            self.assertNotIn(text, logs.output[0])
        overview = self.client.get(reverse("accounts:two_factor"))
        self.assertNotContains(overview, totp.format_recovery_code(codes[0]))
        self.assertContains(overview, "Sie haben 10 unbenutzte Wiederherstellungscodes.")

    def test_the_session_id_changes_when_the_second_factor_is_turned_on(self):
        self.start()
        before = self.client.session.session_key

        self.client.post(
            reverse("accounts:two_factor_confirm"), {"otp_token": code_for(AuthenticatorDevice.objects.get())}
        )

        self.assertNotEqual(self.client.session.session_key, before)

    def test_the_account_is_notified_without_a_link(self):
        self.start()
        self.client.post(
            reverse("accounts:two_factor_confirm"), {"otp_token": code_for(AuthenticatorDevice.objects.get())}
        )

        self.assertEqual(MailJob.objects.get().kind, MailJob.Kind.TWO_FACTOR_ENABLED)
        run_mail_queue()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertNotIn("http", mail.outbox[0].body)

    def test_the_setup_is_refused_when_a_second_factor_exists(self):
        create_totp_device(self.user)

        response = self.client.get(reverse("accounts:two_factor_setup"))

        self.assertRedirects(response, reverse("accounts:two_factor"))
        with self.assertRaises(totp.SecondFactorError):
            totp.start_enrolment(self.user)

    def test_the_setup_password_check_ends_the_session_after_five_wrong_passwords(self):
        # A wrong password in a confirmation counts for the session, not as a failed login (D-062,
        # block 10): the fifth ends the session, and Axes and the IP counter stay untouched.
        for _attempt in range(second_factor.SESSION_CODE_LIMIT):
            response = self.start("wrong password for this account")

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(self.start().status_code, 302)
        self.assertFalse(AuthenticatorDevice.objects.exists())
        self.assertFalse(AccessAttempt.objects.exists())
        self.assertFalse(LoginIpAttempt.objects.exists())

    def test_the_pages_need_a_login(self):
        anonymous = Client()
        for name in (
            "two_factor",
            "two_factor_setup",
            "two_factor_confirm",
            "two_factor_qr",
            "recovery_codes",
            "two_factor_disable",
            "password_change",
        ):
            with self.subTest(name=name):
                url = reverse(f"accounts:{name}")
                self.assertRedirects(
                    anonymous.get(url), f"{LOGIN_URL}?next={url}", fetch_redirect_response=False
                )
                self.assertRedirects(
                    anonymous.post(url, {}), f"{LOGIN_URL}?next={url}", fetch_redirect_response=False
                )

    def test_the_posts_need_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for name in (
            "two_factor_setup",
            "two_factor_confirm",
            "recovery_codes",
            "two_factor_disable",
            "password_change",
        ):
            with self.subTest(name=name):
                self.assertEqual(client.post(reverse(f"accounts:{name}"), {}).status_code, 403)

    def test_the_pages_are_not_cached(self):
        for name in ("two_factor", "two_factor_setup", "password_change"):
            with self.subTest(name=name):
                self.assertIn("no-store", self.client.get(reverse(f"accounts:{name}"))["Cache-Control"])

    def test_a_staff_user_turns_the_second_factor_on_in_the_restricted_session(self):
        staff = promote(create_active_user("staff.person@example.org"))
        client = Client()
        log_in(client, staff.email, address="10.2.0.5")
        client.post(
            reverse("accounts:two_factor_setup"), {"current_password": PASSWORD}, REMOTE_ADDR="10.2.0.5"
        )

        response = client.post(
            reverse("accounts:two_factor_confirm"),
            {"otp_token": code_for(AuthenticatorDevice.objects.get(user=staff))},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get(reverse("accounts:profile")).status_code, 200)


@tag("mariadb")
class RecoveryCodesAndRemovalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def setUp(self):
        self.client.force_login(self.user)
        complete_second_factor(self.client, self.user)

    def data(self, **overrides):
        return {"current_password": PASSWORD, "otp_token": code_for(self.device), **overrides}

    def test_new_recovery_codes_need_the_code_and_nothing_else_is_a_proof(self):
        url = reverse("accounts:recovery_codes")
        for name, data in (
            ("wrong code", {"otp_token": "000000"}),
            ("the password alone", {"current_password": PASSWORD}),
            ("the password in the code field", {"otp_token": PASSWORD}),
            ("nothing", {}),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(url, data).status_code, 200)
                self.assertEqual(stored_digests(), RECOVERY_DIGESTS)

    def test_new_recovery_codes_replace_the_old_ones(self):
        response = self.client.post(reverse("accounts:recovery_codes"), self.data())

        self.assertEqual(response.status_code, 200)
        codes = codes_in(response)
        self.assertEqual(len(codes), totp.RECOVERY_CODE_COUNT)
        self.assertEqual(stored_digests(), {recovery_code_digest(code) for code in codes})
        self.assertFalse(set(codes) & set(RECOVERY_CODES))
        client = Client()
        log_in(client, self.user.email, address="10.3.0.1")
        send_code(client, RECOVERY_CODES[0], address="10.3.0.1")
        self.assertNotIn("_auth_user_id", client.session)
        run_mail_queue()
        self.assertEqual(len(mail.outbox), 1)

    def test_the_second_factor_can_be_turned_off_by_a_user(self):
        response = self.client.post(reverse("accounts:two_factor_disable"), self.data())

        self.assertRedirects(
            response,
            f"{reverse('accounts:profile')}#card-two-factor",
            fetch_redirect_response=False,
        )
        self.assertFalse(AuthenticatorDevice.objects.exists())
        self.assertFalse(RecoveryCodeDevice.objects.exists())
        client = Client()
        self.assertRedirects(
            log_in(client, self.user.email, address="10.3.0.2"), reverse("home")
        )
        self.assertEqual(MailJob.objects.get().kind, MailJob.Kind.TWO_FACTOR_DISABLED)

    def test_turning_off_the_second_factor_ends_every_other_session(self):
        other = Client()
        other.force_login(self.user)
        self.assertEqual(other.get(reverse("accounts:profile")).status_code, 200)

        self.client.post(reverse("accounts:two_factor_disable"), self.data())

        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)
        self.assertRedirects(
            other.get(reverse("accounts:profile")),
            f"{LOGIN_URL}?next=/profile/",
            fetch_redirect_response=False,
        )

    def test_turning_it_off_needs_the_code_and_nothing_else_is_a_proof(self):
        url = reverse("accounts:two_factor_disable")
        for data in (
            {"otp_token": "000000"},
            {"current_password": PASSWORD},
            {"otp_token": PASSWORD},
            {},
        ):
            self.client.post(url, data)

        self.assertTrue(AuthenticatorDevice.objects.exists())
        self.assertTrue(RecoveryCodeDevice.objects.exists())

    def test_turning_it_off_does_not_touch_other_accounts(self):
        other, other_device = create_user_with_totp("other.person@example.org")

        self.client.post(reverse("accounts:two_factor_disable"), self.data())

        self.assertTrue(AuthenticatorDevice.objects.filter(pk=other_device.pk).exists())
        self.assertEqual(len(stored_digests()), len(RECOVERY_CODES))

    def test_staff_users_and_administrators_cannot_turn_it_off(self):
        promote(self.user)

        response = self.client.post(reverse("accounts:two_factor_disable"), self.data())

        self.assertRedirects(
            response, reverse("accounts:two_factor"), fetch_redirect_response=False
        )
        self.assertTrue(AuthenticatorDevice.objects.exists())
        with self.assertRaises(totp.SecondFactorError):
            totp.disable(self.user)
        self.assertTrue(AuthenticatorDevice.objects.exists())

    def test_without_a_second_factor_the_pages_lead_back_to_the_overview(self):
        AuthenticatorDevice.objects.all().delete()
        for name in ("recovery_codes", "two_factor_disable"):
            with self.subTest(name=name):
                self.assertRedirects(
                    self.client.get(reverse(f"accounts:{name}")),
                    reverse("accounts:two_factor"),
                    fetch_redirect_response=False,
                )

    def test_the_password_check_of_the_forms_ends_the_session_after_five_wrong_passwords(self):
        # An account without a second factor gives the password (D-062); a wrong one counts for the
        # session, not as a failed login (block 10): guessing is limited to five tries per session.
        plain = create_active_user("plain.guess@example.org")
        client = Client()
        client.force_login(plain)
        for _attempt in range(second_factor.SESSION_CODE_LIMIT):
            response = client.post(
                reverse("accounts:account_delete"),
                {"current_password": "wrong password for this account", "confirm": "on"},
                REMOTE_ADDR="10.3.0.3",
            )

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(
            client.post(
                reverse("accounts:account_delete"),
                {"current_password": PASSWORD, "confirm": "on"},
                REMOTE_ADDR="10.3.0.3",
            ).status_code,
            302,
        )
        self.assertTrue(User.objects.filter(pk=plain.pk).exists())
        self.assertFalse(AccessAttempt.objects.exists())

    def test_the_code_check_of_the_forms_still_refuses_during_a_lockout(self):
        # The lockout of the login (failed logins of the account) refuses the check of a code in
        # the profile, even a correct one; the wrong codes of the profile themselves no longer count
        # as failed logins (D-062, block 9).
        for _attempt in range(LOGIN_FAILURE_LIMIT):
            log_in(Client(), self.user.email, "wrong password for this account", address="10.3.0.4")

        response = self.client.post(
            reverse("accounts:recovery_codes"), self.data(), REMOTE_ADDR="10.3.0.4"
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(stored_digests(), RECOVERY_DIGESTS)

    def test_the_data_of_a_failed_check_is_not_stored_by_the_abuse_protection(self):
        # A wrong password at the login is recorded by the abuse protection; what it records
        # holds no password (the wrong codes of the profile are no failed logins any more).
        log_in(Client(), self.user.email, "password-sentinel-1", address="10.3.0.5")

        self.assertTrue(AccessAttempt.objects.exists())
        for attempt in AccessAttempt.objects.all():
            self.assertNotIn("password-sentinel-1", attempt.post_data)
            self.assertNotIn("654321", attempt.post_data)

    def test_a_successful_check_releases_its_ip_slots(self):
        self.client.post(reverse("accounts:recovery_codes"), self.data())

        self.assertEqual(LoginIpAttempt.objects.count(), 0)


@tag("mariadb")
class VerifyCodeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def test_a_code_of_the_app_is_accepted_once(self):
        code = code_for(self.device)

        self.assertEqual(totp.verify_code(self.user, code), AuthenticatorDevice.objects.get())
        self.assertIsNone(totp.verify_code(self.user, code))

    def test_codes_of_the_wrong_kind_or_length_are_rejected(self):
        for code in ("", "12345", "1234567", "abcd23", "abcd234", "abcd23456", "ABCD2345X"):
            with self.subTest(code=code):
                self.assertIsNone(totp.verify_code(self.user, code))

    def test_another_account_has_its_own_codes(self):
        other, _device = create_user_with_totp("other.person@example.org")

        self.assertIsNone(totp.verify_code(self.user, "wxyz2345"))
        self.assertIsNotNone(totp.verify_code(other, recovery_codes_of(other.email)[0]))
        # Only the recovery code of the account that used it is gone.
        self.assertEqual(len(stored_digests()), len(RECOVERY_CODES) * 2 - 1)
        self.assertEqual(len(stored_digests(self.user)), len(RECOVERY_CODES))

    def test_recovery_codes_without_an_authenticator_app_are_no_factor(self):
        AuthenticatorDevice.objects.all().delete()

        self.assertFalse(second_factor.has_second_factor(self.user))
        self.assertIsNone(totp.verify_code(self.user, "abcd2345"))
        self.assertEqual(stored_digests(), RECOVERY_DIGESTS)

    def test_an_unconfirmed_device_does_not_verify(self):
        AuthenticatorDevice.objects.all().delete()
        pending = AuthenticatorDevice.objects.create_device(self.user, confirmed=False)

        self.assertIsNone(totp.verify_code(self.user, code_for(pending)))

    def test_failures_do_not_lock_the_device_itself(self):
        # Repeated failures are limited by the abuse protection of the login, so that someone
        # who knows the password cannot keep the owner out for as long as they like (D-040).
        for _attempt in range(4):
            self.assertIsNone(totp.verify_code(self.user, "000000"))

        self.assertIsNotNone(totp.verify_code(self.user, code_for(self.device)))

    def test_the_key_is_stored_encrypted_and_the_recovery_codes_only_as_digests(self):
        key = self.device.bin_key
        row = AuthenticatorDevice.objects.values().get()

        self.assertNotIn(key.hex(), str(row))
        self.assertNotIn(b32encode(key).decode(), str(row))
        for digest in RecoveryCode.objects.values_list("digest", flat=True):
            self.assertNotIn(digest, RECOVERY_CODES)
            self.assertEqual(len(digest), 64)
        for code in RECOVERY_CODES:
            self.assertNotIn(code, str(list(RecoveryCode.objects.values())))

    def test_a_key_that_cannot_be_decrypted_verifies_nothing(self):
        AuthenticatorDevice.objects.update(encrypted_key="damaged")

        with self.assertLogs("leg_platform.accounts.models", "ERROR"):
            self.assertIsNone(totp.verify_code(self.user, "123456"))

    def test_a_key_under_an_old_secret_key_still_works_and_is_encrypted_again(self):
        old_key = "old-secret-key-that-is-long-enough-for-the-tests-0123456789"
        with override_settings(SECRET_KEY=old_key):
            user, device = create_user_with_totp("rotated.person@example.org")
            old_token = device.encrypted_key
            key = device.bin_key

        with override_settings(SECRET_KEY_FALLBACKS=[old_key]):
            device.refresh_from_db()
            self.assertEqual(device.bin_key, key)
            code = code_for(device)
            self.assertIsNotNone(totp.verify_code(user, code))

        device.refresh_from_db()
        self.assertNotEqual(device.encrypted_key, old_token)
        self.assertEqual(device.bin_key, key)

    def test_the_secret_is_shown_in_groups_and_decodes_to_the_key(self):
        device = AuthenticatorDevice.objects.get()
        text = totp.secret_for_display(device)

        self.assertRegex(text, r"^([A-Z2-7]{4} )+[A-Z2-7]{4}$")
        self.assertEqual(len(text.replace(" ", "")), 32)


@tag("mariadb")
class PasswordChangeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("accounts:password_change")

    def post(self, client=None, **overrides):
        data = {
            "current_password": PASSWORD,
            "new_password1": OTHER_PASSWORD,
            "new_password2": OTHER_PASSWORD,
            **overrides,
        }
        return (client or self.client).post(self.url, data, REMOTE_ADDR="10.4.0.1")

    def test_the_page_needs_a_login(self):
        self.assertRedirects(Client().get(self.url), f"{LOGIN_URL}?next={self.url}")

    def test_the_password_is_changed(self):
        response = self.post()

        self.assertRedirects(
            response, f"{reverse('accounts:profile')}#card-password", fetch_redirect_response=False
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OTHER_PASSWORD))
        self.assertFalse(self.user.check_password(PASSWORD))

    def test_the_current_password_is_required_and_checked(self):
        for name, overrides in (
            ("wrong", {"current_password": "wrong password for this account"}),
            ("empty", {"current_password": ""}),
        ):
            with self.subTest(name=name):
                response = self.post(**overrides)
                self.assertEqual(response.status_code, 200)
                self.user.refresh_from_db()
                self.assertTrue(self.user.check_password(PASSWORD))

    def test_the_rules_for_passwords_apply(self):
        for name, overrides in (
            ("short", {"new_password1": "kettle orchard", "new_password2": "kettle orchard"}),
            ("numeric", {"new_password1": "1234567890123456", "new_password2": "1234567890123456"}),
            ("different", {"new_password2": "another lantern rests under silver rain"}),
            ("similar to the address", {"new_password1": self.user.email, "new_password2": self.user.email}),
        ):
            with self.subTest(name=name):
                response = self.post(**overrides)
                self.assertEqual(response.status_code, 200)
                self.user.refresh_from_db()
                self.assertTrue(self.user.check_password(PASSWORD))

    def test_this_session_stays_and_every_other_session_ends(self):
        other = Client()
        other.force_login(self.user)
        self.assertEqual(other.get(reverse("accounts:profile")).status_code, 200)
        before = self.client.session.session_key

        self.post()

        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)
        self.assertNotEqual(self.client.session.session_key, before)
        self.assertRedirects(
            other.get(reverse("accounts:profile")),
            f"{LOGIN_URL}?next=/profile/",
            fetch_redirect_response=False,
        )

    def test_the_second_factor_of_the_session_is_kept(self):
        user, device = create_user_with_totp("second.person@example.org")
        client = Client()
        client.force_login(user)
        complete_second_factor(client, user)

        self.post(client)

        self.assertEqual(client.session[DEVICE_ID_SESSION_KEY], device.persistent_id)

    def test_the_account_is_notified_without_a_link(self):
        self.post()
        run_mail_queue()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertNotIn("http", mail.outbox[0].body.replace("http://127.0.0.1:8000", ""))

    def test_the_new_password_works_for_the_login_and_the_old_one_does_not(self):
        self.post()

        self.assertEqual(
            log_in(Client(), self.user.email, PASSWORD, address="10.4.0.2").status_code, 200
        )
        self.assertRedirects(
            log_in(Client(), self.user.email, OTHER_PASSWORD, address="10.4.0.3"),
            reverse("home"),
        )

    def test_the_current_password_check_ends_the_session_after_five_wrong_passwords(self):
        for _attempt in range(second_factor.SESSION_CODE_LIMIT):
            response = self.post(current_password="wrong password for this account")

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(self.post().status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertFalse(AccessAttempt.objects.exists())

    def test_the_current_password_check_still_refuses_during_a_lockout(self):
        # The failed logins of the account lock it (Axes); that lockout refuses the check, even
        # with the right password.
        for _attempt in range(LOGIN_FAILURE_LIMIT):
            log_in(Client(), self.user.email, "wrong password for this account", address="10.4.0.9")

        self.assertEqual(self.post().status_code, 429)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_passwords_are_not_stored_by_the_abuse_protection(self):
        # A wrong password of the profile is not a failed login and leaves no record at all; the
        # record of a failed login holds no password either.
        self.post(
            current_password="current-sentinel-1",
            new_password1="new-sentinel-2",
            new_password2="new-sentinel-3",
        )
        self.assertFalse(AccessAttempt.objects.exists())
        log_in(Client(), self.user.email, "current-sentinel-1", address="10.4.0.8")

        self.assertTrue(AccessAttempt.objects.exists())
        for attempt in AccessAttempt.objects.all():
            for sentinel in ("current-sentinel-1", "new-sentinel-2", "new-sentinel-3"):
                self.assertNotIn(sentinel, attempt.post_data)

    def test_a_blocked_account_cannot_change_its_password(self):
        self.user.set_status(User.Status.BLOCKED)

        response = self.post()

        self.assertRedirects(response, f"{LOGIN_URL}?next={self.url}", fetch_redirect_response=False)

    def test_the_profile_links_to_the_pages(self):
        response = self.client.get(reverse("accounts:profile"))

        # The cards open in place: the links lead to the state of the card (D-059).
        self.assertContains(response, "?edit=password#card-password")
        self.assertContains(response, "2FA einrichten")
        self.assertContains(response, "Zwei-Faktor-Anmeldung (2FA)")


@tag("mariadb")
class EnrolmentBindingTests(TestCase):
    """Only the session that started the setup, and only for a short time, can finish it."""

    def setUp(self):
        self.user = create_active_user()
        self.client.force_login(self.user)
        self.client.post(
            reverse("accounts:two_factor_setup"),
            {"current_password": PASSWORD},
            REMOTE_ADDR="10.5.0.1",
        )
        self.device = AuthenticatorDevice.objects.get()

    def test_another_session_of_the_account_cannot_see_or_confirm_the_secret(self):
        other = Client()
        other.force_login(self.user)

        self.assertRedirects(
            other.get(reverse("accounts:two_factor_confirm")),
            reverse("accounts:two_factor_setup"),
            fetch_redirect_response=False,
        )
        self.assertEqual(other.get(reverse("accounts:two_factor_qr")).status_code, 404)
        response = other.post(
            reverse("accounts:two_factor_confirm"), {"otp_token": code_for(self.device)}
        )
        self.assertRedirects(response, reverse("accounts:two_factor_setup"), fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.get().confirmed)
        self.assertFalse(RecoveryCodeDevice.objects.exists())

    def test_an_old_secret_cannot_be_used(self):
        AuthenticatorDevice.objects.filter(pk=self.device.pk).update(
            created_at=self.device.created_at - totp.ENROLMENT_LIFETIME * 2
        )

        self.assertRedirects(
            self.client.get(reverse("accounts:two_factor_confirm")),
            reverse("accounts:two_factor_setup"),
            fetch_redirect_response=False,
        )
        self.assertEqual(self.client.get(reverse("accounts:two_factor_qr")).status_code, 404)
        self.assertIsNone(totp.confirm_enrolment(self.user, self.device.pk, code_for(self.device)))

    def test_the_secret_of_another_account_cannot_be_confirmed(self):
        other = create_active_user("other.person@example.org")

        self.assertIsNone(totp.confirm_enrolment(other, self.device.pk, code_for(self.device)))


@tag("mariadb")
class SupportingBehaviourTests(TestCase):
    def test_a_password_reset_does_not_remove_or_bypass_the_second_factor(self):
        from leg_platform.accounts.models import VerificationToken
        from leg_platform.accounts.password_reset import PASSWORD_RESET_LIFETIME

        user, device = create_user_with_totp()
        raw = VerificationToken.objects.issue(
            user, VerificationToken.Purpose.PASSWORD_RESET, PASSWORD_RESET_LIFETIME
        )
        client = Client()
        client.get(reverse("accounts:password_reset_verify", args=[raw]))

        client.post(
            reverse("accounts:password_reset_set"),
            {"new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
        )

        user.refresh_from_db()
        self.assertTrue(user.check_password(OTHER_PASSWORD))
        self.assertTrue(AuthenticatorDevice.objects.filter(pk=device.pk, confirmed=True).exists())
        self.assertEqual(stored_digests(), RECOVERY_DIGESTS)
        self.assertNotIn("_auth_user_id", client.session)
        response = log_in(Client(), user.email, OTHER_PASSWORD, address="10.5.1.1")
        self.assertRedirects(response, CODE_URL)

    def test_a_password_change_is_logged_and_ends_the_reset_links(self):
        from leg_platform.accounts.models import VerificationToken
        from leg_platform.accounts.password_reset import PASSWORD_RESET_LIFETIME

        user = create_active_user()
        VerificationToken.objects.issue(
            user, VerificationToken.Purpose.PASSWORD_RESET, PASSWORD_RESET_LIFETIME
        )
        self.client.force_login(user)

        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(
                    reverse("accounts:password_change"),
                    {
                        "current_password": PASSWORD,
                        "new_password1": OTHER_PASSWORD,
                        "new_password2": OTHER_PASSWORD,
                    },
                )

        self.assertIn(f"account {user.pk}", logs.output[0])
        self.assertNotIn(OTHER_PASSWORD, logs.output[0])
        self.assertFalse(VerificationToken.objects.exists())

    def test_a_refused_change_leads_back_to_the_overview(self):
        user, _device = create_user_with_totp()
        self.client.force_login(user)
        complete_second_factor(self.client, user)

        with mock.patch.object(totp, "renew_recovery_codes", side_effect=totp.SecondFactorError):
            response = self.client.post(
                reverse("accounts:recovery_codes"),
                {"current_password": PASSWORD, "otp_token": code_for(_device)},
            )

        self.assertRedirects(response, reverse("accounts:two_factor"), fetch_redirect_response=False)


@tag("mariadb")
class RecoveryCodeNotificationTests(TestCase):
    def test_the_owner_is_notified_without_a_link_when_a_recovery_code_is_used(self):
        user, _device = create_user_with_totp()
        log_in(self.client, user.email)

        send_code(self.client, "abcd2345")

        self.assertEqual(MailJob.objects.get().kind, MailJob.Kind.RECOVERY_CODE_USED)
        run_mail_queue()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        self.assertNotIn("http", mail.outbox[0].body)
        self.assertNotIn("abcd", mail.outbox[0].body)

    def test_a_code_of_the_app_and_a_wrong_code_send_nothing(self):
        user, device = create_user_with_totp()
        log_in(self.client, user.email)

        send_code(self.client, "ijkl4568")
        send_code(self.client, code_for(device))

        self.assertFalse(MailJob.objects.exists())


@tag("mariadb")
class ParallelUseTests(TransactionTestCase):
    """The same code used by parallel requests succeeds once (docs/SECURITY.md, section 11)."""

    def run_in_threads(self, function, count=4):
        barrier = threading.Barrier(count)
        results = []

        def work():
            try:
                barrier.wait()
                results.append(function())
            finally:
                connection.close()

        threads = [threading.Thread(target=work) for _number in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def test_a_recovery_code_works_once_in_parallel(self):
        user, _device = create_user_with_totp()

        results = self.run_in_threads(lambda: totp.verify_code(user, "abcd2345"))

        self.assertEqual(len(results), 4)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.RECOVERY_CODE_USED).count(), 1)

    def test_a_code_of_the_app_works_once_in_parallel(self):
        user, device = create_user_with_totp()
        code = code_for(device)

        def verify_under_account_lock():
            # The precondition of totp.verify_code(): the caller holds the account row lock, as
            # second_factor.verify_second_factor() does in production.
            with transaction.atomic():
                User._base_manager.select_for_update().filter(pk=user.pk).first()
                return totp.verify_code(user, code)

        results = self.run_in_threads(verify_under_account_lock)

        self.assertEqual(sum(result is not None for result in results), 1)

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

    def test_parallel_wrong_codes_cannot_exceed_the_limit(self):
        user, device = create_user_with_totp()
        clients = []
        for number in range(8):
            address = f"10.7.0.{number + 1}"
            client = Client()
            log_in(client, user.email, address=address)
            clients.append((client, address))

        results = self.run_all(
            [
                lambda client=client, address=address: send_code(client, "000000", address).status_code
                for client, address in clients
            ]
        )

        failures = sum(AccessAttempt.objects.values_list("failures_since_start", flat=True))
        self.assertEqual(len(results), 8)
        self.assertLessEqual(failures, LOGIN_FAILURE_LIMIT)
        self.assertNotIn(500, results)
        self.assertIn(429, results)
        client, address = clients[0]
        self.assertEqual(send_code(client, code_for(device), address).status_code, 429)
        self.assertNotIn("_auth_user_id", client.session)

    def test_parallel_wrong_passwords_of_the_password_forms_stay_under_the_cap(self):
        # Wrong passwords in the confirmations are no failed logins (block 10); parallel requests
        # of many sessions are bounded by the cap of the account: exactly one end of all sessions.
        user = create_active_user()
        clients = []
        for number in range(8):
            client = Client()
            client.force_login(user)
            clients.append((client, f"10.7.1.{number + 1}"))

        results = self.run_all(
            [
                lambda client=client, address=address: client.post(
                    reverse("accounts:password_change"),
                    {
                        "current_password": "wrong password for this account",
                        "new_password1": OTHER_PASSWORD,
                        "new_password2": OTHER_PASSWORD,
                    },
                    REMOTE_ADDR=address,
                ).status_code
                for client, address in clients
                for _twice in range(2)
            ]
        )

        self.assertNotIn(500, results)
        self.assertEqual(AccessAttempt.objects.count(), 0)
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.ALL_SESSIONS_ENDED).count(), 1
        )
        self.assertLess(
            ConfirmationFailure.objects.filter(account=user).count(),
            second_factor.ACCOUNT_FAILURE_LIMIT,
        )

    def test_logout_in_parallel_with_wrong_passwords_of_the_same_sessions_does_not_deadlock(self):
        # D-072: the logout deletes the count of the session under the lock of the account, like
        # the confirmations do. Each session is used by two clients that share its cookie: one
        # logs out while the other enters wrong passwords. A guard against a lock cycle between
        # the logout and the counter rows (probabilistic; the deterministic proof is
        # ``test_the_logout_waits_for_the_lock_of_the_account``). Bounded: 6 sessions, 2 wrong
        # entries each, below the limit of the session (5) and the cap of the account (15).
        user = create_active_user()
        functions = []
        for number in range(6):
            confirming = Client()
            confirming.force_login(user)
            leaving = Client()
            cookie = settings.SESSION_COOKIE_NAME
            leaving.cookies[cookie] = confirming.cookies[cookie].value
            address = f"10.7.2.{number + 1}"

            def wrong_passwords(client=confirming, address=address):
                return [
                    client.post(
                        reverse("accounts:password_change"),
                        {
                            "current_password": "wrong password for this account",
                            "new_password1": OTHER_PASSWORD,
                            "new_password2": OTHER_PASSWORD,
                        },
                        REMOTE_ADDR=address,
                    ).status_code
                    for _twice in range(2)
                ]

            def log_out(client=leaving, address=address):
                return [client.post(reverse("accounts:logout"), REMOTE_ADDR=address).status_code]

            functions += [wrong_passwords, log_out]

        results = self.run_all(functions)

        statuses = [status for result in results for status in result]
        # A thread that raised (a deadlock is an exception of the test client) gives no result.
        self.assertEqual(len(results), len(functions))
        self.assertNotIn(500, statuses)
        self.assertFalse(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.ALL_SESSIONS_ENDED).exists()
        )


class EncryptionTests(SimpleTestCase):
    PURPOSE = b"LEG-Platform test purpose v1"

    def test_a_value_round_trips_and_is_not_readable(self):
        token = encryption.encrypt(b"secret-bytes-12345", self.PURPOSE)

        self.assertNotIn("secret-bytes", token)
        self.assertEqual(encryption.decrypt(token, self.PURPOSE), b"secret-bytes-12345")
        self.assertTrue(encryption.is_current(token, self.PURPOSE))

    def test_every_encryption_gives_a_different_token(self):
        self.assertNotEqual(
            encryption.encrypt(b"same", self.PURPOSE), encryption.encrypt(b"same", self.PURPOSE)
        )

    def test_another_purpose_a_damaged_token_and_odd_input_are_refused(self):
        token = encryption.encrypt(b"value", self.PURPOSE)
        damaged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        for name, value, purpose in (
            ("other purpose", token, b"LEG-Platform other purpose v1"),
            ("damaged", damaged, self.PURPOSE),
            ("empty", "", self.PURPOSE),
            ("not ascii", "\u00e4\u00f6\u00fc", self.PURPOSE),
        ):
            with self.subTest(name=name):
                with self.assertRaises(encryption.DecryptionError):
                    encryption.decrypt(value, purpose)

    def test_a_replaced_secret_key_refuses_unless_the_old_one_is_a_fallback(self):
        old_key = "old-secret-key-that-is-long-enough-for-the-tests-0123456789"
        with override_settings(SECRET_KEY=old_key):
            token = encryption.encrypt(b"value", self.PURPOSE)

        with self.assertRaises(encryption.DecryptionError):
            encryption.decrypt(token, self.PURPOSE)
        with override_settings(SECRET_KEY_FALLBACKS=[old_key]):
            self.assertEqual(encryption.decrypt(token, self.PURPOSE), b"value")
            self.assertFalse(encryption.is_current(token, self.PURPOSE))
            renewed = encryption.reencrypt(token, self.PURPOSE)
            self.assertTrue(encryption.is_current(renewed, self.PURPOSE))
        self.assertEqual(encryption.decrypt(renewed, self.PURPOSE), b"value")

    def test_errors_do_not_contain_the_value_or_the_key(self):
        try:
            encryption.decrypt("gAAAAA-damaged-token", self.PURPOSE)
        except encryption.DecryptionError as error:
            self.assertNotIn("damaged-token", str(error))
            self.assertNotIn(settings.SECRET_KEY, str(error))
            self.assertIsNone(error.__cause__)

    def test_the_purpose_labels_are_different(self):
        from leg_platform.accounts import models

        labels = [
            models.AUTHENTICATOR_SECRET_PURPOSE,
            models.RECOVERY_CODE_PURPOSE,
            models.VERIFICATION_TOKEN_PURPOSE,
        ]
        self.assertEqual(len(labels), len(set(labels)))


@tag("mariadb")
class DeviceRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def test_other_forms_of_a_code_are_refused(self):
        for code in ("+12345", "1_2345", "\u0661\u0662\u0663\u0664\u0665\u0666", "\u00b2\u00b2\u00b2\u00b2\u00b2\u00b2", " 12345", "123456\n"):
            with self.subTest(code=code):
                self.assertFalse(self.device.verify_token(code))

    def test_a_code_of_a_step_two_away_or_of_an_earlier_step_is_refused(self):
        generator = TOTP(self.device.bin_key, 30, 0, 6, self.device.drift)
        for offset in (-2, 2):
            generator.time = time.time() + offset * 30
            with self.subTest(offset=offset):
                self.assertFalse(self.device.verify_token(str(generator.token()).zfill(6)))
        current = code_for(self.device)
        self.assertTrue(self.device.verify_token(current))
        generator.time = time.time() - 30
        self.assertFalse(self.device.verify_token(str(generator.token()).zfill(6)))

    def test_a_second_device_per_account_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuthenticatorDevice.objects.create_device(self.user, confirmed=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RecoveryCodeDevice.objects.create(user=self.user, name="again", confirmed=True)

    def test_deleting_the_account_removes_its_devices_and_codes(self):
        other, _device = create_user_with_totp("other.person@example.org")

        self.user.delete()

        self.assertFalse(AuthenticatorDevice.objects.filter(user_id=self.user.pk).exists())
        self.assertEqual(stored_digests(), stored_digests(other))
        self.assertTrue(AuthenticatorDevice.objects.filter(user=other).exists())

    def test_recovery_codes_still_work_after_the_secret_key_was_rotated(self):
        old_key = "old-secret-key-that-is-long-enough-for-the-tests-0123456789"
        with override_settings(SECRET_KEY=old_key):
            user, _device = create_user_with_totp("rotated.person@example.org")

        with override_settings(SECRET_KEY_FALLBACKS=[old_key]):
            code = recovery_codes_of(user.email)[0]
            self.assertIsNotNone(totp.verify_code(user, code))
            self.assertIsNone(totp.verify_code(user, code))

    def test_renewing_the_codes_keeps_the_device_of_a_session_that_a_code_verified(self):
        before = RecoveryCodeDevice.objects.get().persistent_id

        totp.renew_recovery_codes(self.user)

        self.assertEqual(RecoveryCodeDevice.objects.get().persistent_id, before)
        self.assertEqual(len(stored_digests()), totp.RECOVERY_CODE_COUNT)

    def test_a_recovery_code_that_another_account_also_has_does_not_break_the_renewal(self):
        other, _device = create_user_with_totp("other.person@example.org")
        shared = recovery_codes_of(other.email)
        codes = [
            *shared,
            *("mnop2345", "qrst2345", "uvwx2345", "yzab2345", "cdef2345", "ghij2345", "klmn2345"),
        ]
        self.assertEqual(len(set(codes)), totp.RECOVERY_CODE_COUNT)

        with mock.patch.object(totp, "_new_recovery_code", side_effect=codes):
            renewed = totp.renew_recovery_codes(self.user)

        self.assertEqual(sorted(renewed), sorted(codes))
        # Each account uses only its own codes: using the shared code for one account leaves
        # the code of the other account untouched.
        self.assertIsNotNone(totp.verify_code(self.user, shared[0]))
        self.assertIsNone(totp.verify_code(self.user, shared[0]))
        self.assertIsNotNone(totp.verify_code(other, shared[0]))
        self.assertIsNone(totp.verify_code(other, shared[0]))

    def test_a_device_cannot_hold_the_same_recovery_code_twice(self):
        device = RecoveryCodeDevice.objects.get(user=self.user)

        with self.assertRaises(IntegrityError), transaction.atomic():
            RecoveryCode.objects.create(device=device, digest=recovery_code_digest(RECOVERY_CODES[0]))



def set_budget(device, failures, hours_ago=0):
    AuthenticatorDevice.objects.filter(pk=device.pk).update(
        failed_codes=failures, failed_codes_since=timezone.now() - timedelta(hours=hours_ago)
    )


def budget_of(device):
    return AuthenticatorDevice.objects.get(pk=device.pk).failed_codes


@tag("mariadb")
class CodeBudgetTests(TestCase):
    """The budget of wrong codes per account: notice, pause, relief (D-040)."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def test_the_owner_is_notified_once_when_the_notice_is_reached(self):
        for _attempt in range(totp.CODE_NOTICE_AT - 1):
            totp.record_failed_code(self.user)
        self.assertFalse(MailJob.objects.exists())

        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                totp.record_failed_code(self.user)
        for _attempt in range(3):
            totp.record_failed_code(self.user)

        job = MailJob.objects.get()
        self.assertEqual(job.kind, MailJob.Kind.CODE_ATTEMPTS_WARNING)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(self.user.email, logs.output[0])
        run_mail_queue()
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("http", mail.outbox[0].body)
        self.assertFalse(RateLimitEvent.objects.exists())

    def test_the_budget_restarts_after_the_window_and_warns_again(self):
        set_budget(self.device, totp.CODE_NOTICE_AT - 1, hours_ago=25)

        totp.record_failed_code(self.user)

        self.assertEqual(budget_of(self.device), 1)
        self.assertFalse(MailJob.objects.exists())

    def test_a_password_lockout_neither_counts_nor_sends_mail(self):
        for _attempt in range(LOGIN_FAILURE_LIMIT):
            log_in(Client(), self.user.email, "wrong password for this account", address="10.8.0.9")

        self.assertEqual(budget_of(self.device), 0)
        self.assertFalse(MailJob.objects.exists())

    def test_a_wrong_code_counts(self):
        log_in(self.client, self.user.email, address="10.8.0.1")
        send_code(self.client, "000000", address="10.8.0.1")
        self.assertEqual(budget_of(self.device), 1)

    def test_a_wrong_recovery_code_counts_and_a_right_one_does_not(self):
        log_in(self.client, self.user.email, address="10.8.0.2")

        send_code(self.client, "zzzz2222", address="10.8.0.2")
        self.assertEqual(budget_of(self.device), 1)
        send_code(self.client, "abcd2345", address="10.8.0.2")

        self.assertEqual(budget_of(self.device), 1)

    def test_a_successful_login_does_not_reset_the_budget(self):
        set_budget(self.device, 3)
        log_in(self.client, self.user.email, address="10.8.0.3")

        send_code(self.client, code_for(self.device), address="10.8.0.3")

        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(budget_of(self.device), 3)

    def test_the_forms_that_ask_for_a_code_count_for_the_session_not_for_the_budget(self):
        self.client.force_login(self.user)
        complete_second_factor(self.client, self.user)

        self.client.post(
            reverse("accounts:two_factor_disable"),
            {"current_password": PASSWORD, "otp_token": "000000"},
            REMOTE_ADDR="10.8.0.4",
        )

        # Not against the budget of the account, which also pauses the login (D-062, block 9).
        self.assertEqual(budget_of(self.device), 0)
        self.assertEqual(SessionCodeFailure.objects.get(account=self.user).failures, 1)

    def test_attempts_that_the_lockout_refuses_do_not_count(self):
        log_in(self.client, self.user.email, address="10.8.0.5")
        for _attempt in range(LOGIN_FAILURE_LIMIT + 3):
            send_code(self.client, "000000", address="10.8.0.5")

        self.assertEqual(budget_of(self.device), LOGIN_FAILURE_LIMIT)

    def test_a_used_up_budget_pauses_code_entry_even_for_a_correct_code(self):
        set_budget(self.device, totp.CODE_PAUSE_AT)
        log_in(self.client, self.user.email, address="10.8.0.6")

        response = send_code(self.client, code_for(self.device), address="10.8.0.6")

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "pausiert", status_code=429)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotContains(response, "\u00df", status_code=429)
        self.assertContains(response, reverse("accounts:password_reset"), status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(budget_of(self.device), totp.CODE_PAUSE_AT)
        # Nothing is recorded by Axes, so its lockout is not extended; the IP attempt is kept.
        self.assertFalse(AccessAttempt.objects.exists())
        self.assertEqual(LoginIpAttempt.objects.filter(pending=False).count(), 1)
        self.assertFalse(MailJob.objects.exists())

    def test_the_pause_ends_with_the_window(self):
        set_budget(self.device, totp.CODE_PAUSE_AT, hours_ago=25)
        log_in(self.client, self.user.email, address="10.8.0.7")

        response = send_code(self.client, code_for(self.device), address="10.8.0.7")

        self.assertRedirects(response, reverse("home"))

    def test_the_pause_does_not_affect_other_accounts(self):
        set_budget(self.device, totp.CODE_PAUSE_AT)
        other, other_device = create_user_with_totp("other.person@example.org")
        client = Client()
        log_in(client, other.email, address="10.8.0.8")

        response = send_code(client, code_for(other_device), address="10.8.0.8")

        self.assertRedirects(response, reverse("home"))

    def test_the_normal_lockout_page_has_no_pause_text(self):
        log_in(self.client, self.user.email, address="10.8.0.10")
        for _attempt in range(LOGIN_FAILURE_LIMIT):
            response = send_code(self.client, "000000", address="10.8.0.10")

        self.assertEqual(response.status_code, 429)
        self.assertNotContains(response, "pausiert", status_code=429)

    def reset_password(self):
        raw = VerificationToken.objects.issue(
            self.user, VerificationToken.Purpose.PASSWORD_RESET, PASSWORD_RESET_LIFETIME
        )
        client = Client()
        client.get(reverse("accounts:password_reset_verify", args=[raw]))
        client.post(
            reverse("accounts:password_reset_set"),
            {"new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
        )

    def test_a_password_reset_ends_the_pause_but_leaves_a_few_attempts(self):
        set_budget(self.device, totp.CODE_PAUSE_AT)

        self.reset_password()

        self.assertEqual(budget_of(self.device), totp.CODE_RELIEF_TO)
        self.assertFalse(totp.code_attempts_paused(self.user))

    def test_a_password_reset_never_gives_more_attempts_than_the_budget_has(self):
        set_budget(self.device, 3)

        self.reset_password()

        self.assertEqual(budget_of(self.device), 3)

    def test_a_new_setup_starts_with_a_new_budget(self):
        set_budget(self.device, totp.CODE_PAUSE_AT)
        totp.disable(self.user)

        device = totp.start_enrolment(self.user)

        self.assertEqual(device.failed_codes, 0)

    def test_an_account_without_a_second_factor_is_not_touched(self):
        plain = create_active_user("plain.person@example.org")

        totp.record_failed_code(plain)

        self.assertFalse(MailJob.objects.exists())
        self.assertFalse(totp.code_attempts_paused(plain))


@tag("mariadb")
class PendingLoginBindingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def test_a_password_change_between_the_two_steps_ends_the_pending_login(self):
        log_in(self.client, self.user.email, address="10.9.0.1")
        self.user.set_password(OTHER_PASSWORD)
        self.user.save()

        response = send_code(self.client, code_for(self.device), address="10.9.0.1")

        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_without_a_change_the_pending_login_still_works(self):
        log_in(self.client, self.user.email, address="10.9.0.2")

        response = send_code(self.client, code_for(self.device), address="10.9.0.2")

        self.assertRedirects(response, reverse("home"))


@tag("mariadb")
class CleanupTests(TestCase):
    def test_abandoned_setups_are_deleted_and_the_others_kept(self):
        user = create_active_user()
        stale = AuthenticatorDevice.objects.create_device(user, confirmed=False)
        AuthenticatorDevice.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - totp.ENROLMENT_LIFETIME * 2
        )
        young_user = create_active_user("young.person@example.org")
        young = AuthenticatorDevice.objects.create_device(young_user, confirmed=False)
        _owner, confirmed = create_user_with_totp("confirmed.person@example.org")
        AuthenticatorDevice.objects.filter(pk=confirmed.pk).update(
            created_at=timezone.now() - totp.ENROLMENT_LIFETIME * 2
        )
        output = StringIO()

        call_command("cleanup_login_ip_attempts", stdout=output)

        self.assertIn("1 abandoned second factor setups", output.getvalue())
        self.assertEqual(
            set(AuthenticatorDevice.objects.values_list("pk", flat=True)),
            {young.pk, confirmed.pk},
        )

    def test_expired_account_attempts_of_the_abuse_protection_are_deleted(self):
        log_in(Client(), "unknown.person@example.org", "wrong", address="10.9.1.1")
        AccessAttemptExpiration.objects.update(expires_at=timezone.now() - timedelta(minutes=1))

        call_command("cleanup_login_ip_attempts", stdout=StringIO())

        self.assertFalse(AccessAttempt.objects.exists())


class CleanupConcurrencyTests(TransactionTestCase):
    def test_a_bucket_held_by_a_request_is_skipped_and_never_waited_for(self):
        digest = "a" * 64
        bucket = LoginIpBucket.objects.create(digest=digest)
        LoginIpAttempt.objects.create(
            bucket=bucket, attempted_at=timezone.now() - timedelta(days=1), pending=False
        )
        locked, release = threading.Event(), threading.Event()

        def hold():
            try:
                with transaction.atomic():
                    LoginIpBucket.objects.select_for_update().get(digest=digest)
                    locked.set()
                    release.wait(20)
            finally:
                connection.close()

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(locked.wait(10))
        try:
            call_command("cleanup_login_ip_attempts", stdout=StringIO())
            self.assertTrue(LoginIpBucket.objects.filter(digest=digest).exists())
        finally:
            release.set()
            thread.join()

        call_command("cleanup_login_ip_attempts", stdout=StringIO())
        self.assertFalse(LoginIpBucket.objects.filter(digest=digest).exists())
        self.assertFalse(LoginIpAttempt.objects.exists())


@tag("mariadb")
class CodeBudgetPauseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def reset_password(self):
        raw = VerificationToken.objects.issue(
            self.user, VerificationToken.Purpose.PASSWORD_RESET, PASSWORD_RESET_LIFETIME
        )
        client = Client()
        client.get(reverse("accounts:password_reset_verify", args=[raw]))
        client.post(
            reverse("accounts:password_reset_set"),
            {"new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
        )

    def test_a_recovery_code_is_refused_during_the_pause(self):
        set_budget(self.device, totp.CODE_PAUSE_AT)
        log_in(self.client, self.user.email, address="10.8.1.1")

        response = send_code(self.client, "abcd2345", address="10.8.1.1")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(stored_digests()), len(RECOVERY_CODES))

    def test_the_forms_that_ask_for_a_code_refuse_a_correct_code_during_the_pause(self):
        set_budget(self.device, totp.CODE_PAUSE_AT)
        self.client.force_login(self.user)
        complete_second_factor(self.client, self.user)

        response = self.client.post(
            reverse("accounts:two_factor_disable"),
            {"current_password": PASSWORD, "otp_token": code_for(self.device)},
            REMOTE_ADDR="10.8.1.2",
        )

        self.assertEqual(response.status_code, 429)
        self.assertTrue(AuthenticatorDevice.objects.exists())

    def test_the_pause_is_logged_once(self):
        set_budget(self.device, totp.CODE_PAUSE_AT - 1)

        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                totp.record_failed_code(self.user)

        self.assertEqual(len(logs.output), 1)
        self.assertIn(f"account {self.user.pk}", logs.output[0])

    def test_a_second_reset_in_the_same_window_gives_no_more_attempts(self):
        set_budget(self.device, totp.CODE_PAUSE_AT)
        self.reset_password()
        self.assertEqual(budget_of(self.device), totp.CODE_RELIEF_TO)
        AuthenticatorDevice.objects.filter(pk=self.device.pk).update(
            failed_codes=totp.CODE_PAUSE_AT
        )

        self.reset_password()

        self.assertEqual(budget_of(self.device), totp.CODE_PAUSE_AT)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.CODE_ATTEMPTS_WARNING).exists())

    def test_relief_is_available_again_in_a_new_window(self):
        set_budget(self.device, totp.CODE_PAUSE_AT)
        self.reset_password()
        set_budget(self.device, totp.CODE_PAUSE_AT - 1, hours_ago=25)

        totp.record_failed_code(self.user)
        for _attempt in range(totp.CODE_PAUSE_AT):
            totp.record_failed_code(self.user)
        self.reset_password()

        self.assertEqual(budget_of(self.device), totp.CODE_RELIEF_TO)

    def test_a_real_password_reset_between_the_two_steps_ends_the_pending_login(self):
        log_in(self.client, self.user.email, address="10.8.1.3")

        self.reset_password()
        response = send_code(self.client, code_for(self.device), address="10.8.1.3")

        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_pending_login_without_a_stored_hash_ends_without_an_error(self):
        log_in(self.client, self.user.email, address="10.8.1.4")
        session = self.client.session
        del session[second_factor.PENDING_SESSION_KEY]["auth_hash"]
        session.save()

        response = send_code(self.client, code_for(self.device), address="10.8.1.4")

        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)


@tag("mariadb")
class ParallelBudgetTests(TransactionTestCase):
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

    def wrong_codes_in_parallel(self, user):
        clients = []
        for number in range(8):
            address = f"10.8.2.{number + 1}"
            client = Client()
            log_in(client, user.email, address=address)
            clients.append((client, address))
        return self.run_all(
            [
                lambda client=client, address=address: send_code(client, "000000", address).status_code
                for client, address in clients
            ]
        )

    def test_parallel_wrong_codes_send_one_mail_when_the_notice_is_crossed(self):
        user, device = create_user_with_totp()
        set_budget(device, totp.CODE_NOTICE_AT - 3)

        results = self.wrong_codes_in_parallel(user)

        self.assertNotIn(500, results)
        self.assertEqual(
            MailJob.objects.filter(kind=MailJob.Kind.CODE_ATTEMPTS_WARNING).count(), 1
        )

    def test_parallel_wrong_codes_cannot_pass_the_pause(self):
        user, device = create_user_with_totp()
        set_budget(device, totp.CODE_PAUSE_AT - 2)

        results = self.wrong_codes_in_parallel(user)

        self.assertNotIn(500, results)
        self.assertEqual(budget_of(device), totp.CODE_PAUSE_AT)
