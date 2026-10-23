"""The way "phone lost" of turning off 2FA (D-062, block 8): the password and a recovery code, the
only confirmation besides the login where a recovery code counts."""

import re
import threading
from unittest import mock

from axes.models import AccessAttempt
from django.core import mail
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import profile_views, second_factor, totp
from leg_platform.accounts.forms import LostPhoneForm
from leg_platform.accounts.login_protection import LOGIN_FAILURE_LIMIT
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    LoginIpAttempt,
    MailJob,
    RecoveryCodeDevice,
    SecurityEvent,
    SessionCodeFailure,
    User,
)
from leg_platform.accounts.tests.helpers import PASSWORD, code_for, create_active_user, run_mail_queue
from leg_platform.accounts.tests.test_second_factor import (
    RECOVERY_CODES,
    create_user_with_totp,
    promote,
    recovery_codes_of,
)

PROFILE = reverse("accounts:profile")
LOST_URL = reverse("accounts:two_factor_lost_phone")
LOST_PAGE = f"{PROFILE}?edit=two_factor&step=lost_phone"
DISABLE_PAGE = f"{PROFILE}?edit=two_factor&step=disable"
SECOND = "second.lost.phone@example.org"
GENERIC = "Das Passwort oder der Wiederherstellungscode ist falsch. Prüfen Sie beide Angaben und versuchen Sie es erneut."
FIELD_ERRORS = re.compile(r'<div class="invalid-feedback d-block"[^>]*>(.*?)</div>', re.S)


def summary_of(html):
    """The entries of the error summary at the top of the form."""
    block = re.search(r'<div class="alert alert-danger error-summary".*?</ul>', html, re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block.group(0))).strip() if block else ""


def remaining(user):
    return totp.remaining_recovery_codes(user)


@tag("mariadb")
class LostPhoneSuccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()  # the default account with known codes
        cls.user.set_secondary_email(SECOND)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def lost(self, **data):
        return self.client.post(LOST_URL, data, REMOTE_ADDR="10.92.1.1")

    def test_the_password_and_a_recovery_code_turn_the_second_factor_off(self):
        other_session = Client()
        other_session.force_login(self.user)
        self.assertEqual(other_session.get(PROFILE).status_code, 200)

        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.lost(current_password=PASSWORD, otp_token=RECOVERY_CODES[0])

        self.assertRedirects(
            response, f"{PROFILE}#card-two-factor", fetch_redirect_response=False
        )
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertFalse(RecoveryCodeDevice.objects.filter(user=self.user).exists())
        # Every other session ended; this one stays.
        self.assertEqual(self.client.get(PROFILE).status_code, 200)
        self.assertEqual(other_session.get(PROFILE).status_code, 302)
        # The events: the use of the recovery code and the removal with its own event, kept for
        # the long period (L-2); the plain removal event is not written for this way.
        events = set(SecurityEvent.objects.filter(account_id=self.user.pk).values_list("event", flat=True))
        self.assertIn(SecurityEvent.Event.RECOVERY_CODE_USED, events)
        self.assertIn(SecurityEvent.Event.TWO_FACTOR_REMOVED_PHONE_LOST, events)
        self.assertNotIn(SecurityEvent.Event.TWO_FACTOR_DISABLED, events)
        self.assertTrue(
            SecurityEvent.objects.get(event=SecurityEvent.Event.TWO_FACTOR_REMOVED_PHONE_LOST).long_retention
        )
        self.assertTrue(any("phone lost" in line for line in logs.output))
        self.assertFalse(any(RECOVERY_CODES[0] in line or PASSWORD in line for line in logs.output))

    def test_both_addresses_are_told_of_the_use_of_the_code_and_of_the_removal(self):
        self.lost(current_password=PASSWORD, otp_token=RECOVERY_CODES[0])
        run_mail_queue()

        for address in (self.user.email, SECOND):
            with self.subTest(address=address):
                self.assertEqual(len([m for m in mail.outbox if address in m.to]), 2)

    def test_the_code_is_written_with_dashes_and_in_capitals_too(self):
        code = RECOVERY_CODES[1]
        formatted = f"{code[:4].upper()}-{code[4:]}"

        response = self.lost(current_password=PASSWORD, otp_token=formatted)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())

    def test_the_removal_is_the_one_of_turning_off(self):
        # After it the account logs in with the password only and can set up 2FA again.
        self.lost(current_password=PASSWORD, otp_token=RECOVERY_CODES[0])

        self.assertEqual(self.client.get(f"{PROFILE}?edit=two_factor").status_code, 200)
        self.assertContains(
            self.client.get(f"{PROFILE}?edit=two_factor"),
            "Mit einem Code aus einer App schützen Sie Ihr Konto zusätzlich.",
        )


@tag("mariadb")
class LostPhoneRefusalTests(TestCase):
    """Negative tests: nothing is changed and no code is used up unless both are right."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()
        cls.other, _other_device = create_user_with_totp("someone.else.lost@example.org")
        cls.other_code = recovery_codes_of(cls.other.email)[0]

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def lost(self, **data):
        return self.client.post(LOST_URL, data, REMOTE_ADDR="10.92.2.1")

    def assert_nothing_changed(self, before):
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(remaining(self.user), before)
        self.assertFalse(MailJob.objects.exists())

    def test_wrong_password_wrong_code_and_the_code_of_another_account_give_the_same_error(self):
        before = remaining(self.user)
        wrong_password = self.lost(current_password="a wrong password for the check", otp_token=RECOVERY_CODES[0])
        wrong_code = self.lost(current_password=PASSWORD, otp_token="abcd2346")
        other_code = self.lost(current_password=PASSWORD, otp_token=self.other_code)

        answers = []
        for response in (wrong_password, wrong_code, other_code):
            self.assertEqual(response.status_code, 200)
            html = response.content.decode()
            self.assertIn(GENERIC, html)
            answers.append(summary_of(html))
        self.assertEqual(len(set(answers)), 1)
        # A wrong password did not reach the check of the code: the right code was not used up.
        self.assert_nothing_changed(before)
        self.assertEqual(remaining(self.other), 3)

    def test_a_missing_field_and_one_proof_alone_change_nothing(self):
        before = remaining(self.user)
        for label, data in (
            ("password alone", {"current_password": PASSWORD}),
            ("code alone", {"otp_token": RECOVERY_CODES[0]}),
            ("nothing", {}),
            ("empty fields", {"current_password": "", "otp_token": ""}),
        ):
            with self.subTest(case=label):
                response = self.lost(**data)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Der Zahlencode ist abgeschaltet")
                self.assert_nothing_changed(before)

    def test_an_app_code_and_a_shapeless_value_are_not_a_recovery_code(self):
        before = remaining(self.user)

        for value in (code_for(self.device), "not a recovery code at all", "abcd-efg", "abcd1890"):
            with self.subTest(value=value):
                response = self.lost(current_password=PASSWORD, otp_token=value)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Das ist kein Wiederherstellungscode.")
                self.assertNotContains(response, GENERIC)
                self.assert_nothing_changed(before)

    def test_the_code_is_used_up_only_when_everything_else_is_right(self):
        before = remaining(self.user)
        self.lost(current_password="a wrong password for the check", otp_token=RECOVERY_CODES[2])
        self.assertEqual(remaining(self.user), before)

        done = self.lost(current_password=PASSWORD, otp_token=RECOVERY_CODES[2])

        self.assertEqual(done.status_code, 302)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())

    def test_a_used_code_does_not_work_again(self):
        user, _device = create_user_with_totp("used.once@example.org")
        client = Client()
        client.force_login(user)
        code = recovery_codes_of(user.email)[0]
        # Use up the code, as the check of the second factor does at the login.
        self.assertIsNotNone(totp.verify_code(user, code))

        response = client.post(LOST_URL, {"current_password": PASSWORD, "otp_token": code}, REMOTE_ADDR="10.92.2.2")

        self.assertContains(response, GENERIC)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=user).exists())

    def test_the_wrong_codes_count_for_the_session_not_for_the_account(self):
        self.lost(current_password=PASSWORD, otp_token="abcd2346")
        self.lost(current_password=PASSWORD, otp_token="abcd2347")
        self.lost(current_password="a wrong password for the check", otp_token="abcd2342")
        self.lost(current_password=PASSWORD, otp_token="shape error")

        # Two wrong codes and one wrong password of this session; the shape error did not count.
        # The budget of the account (which pauses the login) is untouched (D-062, block 9).
        self.assertEqual(AuthenticatorDevice.objects.get(pk=self.device.pk).failed_codes, 0)
        self.assertEqual(SessionCodeFailure.objects.get(account=self.user).failures, 3)

    def test_guessing_the_password_here_ends_the_session_and_locks_nothing(self):
        for number in range(LOGIN_FAILURE_LIMIT):
            response = self.lost(
                current_password=f"a wrong password for the check {number}", otp_token=RECOVERY_CODES[0]
            )

        # The fifth wrong password ends the session; it is not a failed login (block 10, M-2).
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(remaining(self.user), 3)
        self.assertFalse(AccessAttempt.objects.exists())
        self.assertFalse(LoginIpAttempt.objects.exists())

    def test_a_right_password_does_not_start_the_count_again_for_the_code(self):
        # Password right, code wrong, five times: the right password in between must not reset
        # the count, or the code could be guessed without a limit.
        for number in range(second_factor.SESSION_CODE_LIMIT):
            response = self.lost(current_password=PASSWORD, otp_token=f"abcd234{'67'[number % 2]}")

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())

    def test_a_lockout_of_the_failed_logins_still_refuses_the_check(self):
        for _attempt in range(LOGIN_FAILURE_LIMIT):
            Client().post(
                reverse("accounts:login"),
                {"username": self.user.email, "password": "a wrong password for the lockout"},
                REMOTE_ADDR="10.92.2.9",
            )

        response = self.lost(current_password=PASSWORD, otp_token=RECOVERY_CODES[0])

        self.assertEqual(response.status_code, 429)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())


@tag("mariadb")
class LostPhoneAccessTests(TestCase):
    def test_a_visitor_and_a_missing_csrf_token_get_nothing(self):
        user, _device = create_user_with_totp()

        anonymous = Client().post(LOST_URL, {"current_password": PASSWORD, "otp_token": RECOVERY_CODES[0]})
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(user)

        self.assertRedirects(anonymous, f"{reverse('accounts:login')}?next={LOST_URL}", fetch_redirect_response=False)
        self.assertEqual(
            browser.post(LOST_URL, {"current_password": PASSWORD, "otp_token": RECOVERY_CODES[0]}).status_code, 403
        )
        self.assertTrue(AuthenticatorDevice.objects.filter(user=user).exists())

    def test_the_account_of_the_request_is_the_only_one_that_changes(self):
        user, _device = create_user_with_totp()
        other, other_device = create_user_with_totp("untouched.lost@example.org")
        client = Client()
        client.force_login(user)

        client.post(
            LOST_URL,
            {"current_password": PASSWORD, "otp_token": RECOVERY_CODES[0], "user": other.pk, "email": other.email},
            REMOTE_ADDR="10.92.3.1",
        )

        self.assertFalse(AuthenticatorDevice.objects.filter(user=user).exists())
        self.assertTrue(AuthenticatorDevice.objects.filter(pk=other_device.pk).exists())

    def test_staff_users_and_administrators_cannot_use_it(self):
        user, _device = create_user_with_totp()
        promote(user)
        client = Client()
        client.force_login(user)
        from leg_platform.accounts.tests.helpers import complete_second_factor

        complete_second_factor(client, user)

        get = client.get(LOST_URL)
        post = client.post(LOST_URL, {"current_password": PASSWORD, "otp_token": RECOVERY_CODES[0]})

        self.assertRedirects(get, reverse("accounts:two_factor"), fetch_redirect_response=False)
        self.assertRedirects(post, reverse("accounts:two_factor"), fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=user).exists())
        self.assertNotContains(client.get(DISABLE_PAGE), "Handy verloren?")

    def test_an_account_without_a_second_factor_has_nothing_to_turn_off(self):
        user = create_active_user("no.factor.lost@example.org")
        client = Client()
        client.force_login(user)

        self.assertRedirects(client.get(LOST_URL), reverse("accounts:two_factor"), fetch_redirect_response=False)
        self.assertRedirects(
            client.post(LOST_URL, {"current_password": PASSWORD, "otp_token": "abcd2345"}),
            reverse("accounts:two_factor"),
            fetch_redirect_response=False,
        )


@tag("mariadb")
class LostPhonePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_way_is_a_plain_link_under_the_code_field_of_the_card_turn_off(self):
        html = self.client.get(DISABLE_PAGE).content.decode()

        self.assertRegex(html, r'<a href="[^"]*step=lost_phone[^"]*">Handy verloren\?</a>')
        # Without a script: a link to a state of the same card.
        self.assertLess(html.index('name="otp_token"'), html.index("Handy verloren?"))

    def test_no_other_form_offers_it_or_mentions_recovery_codes(self):
        for url in (
            f"{PROFILE}?edit=email",
            f"{PROFILE}?edit=password",
            f"{PROFILE}?edit=delete",
            f"{PROFILE}?edit=two_factor&step=codes_new",
            DISABLE_PAGE,
        ):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertNotIn("Wiederherstellungscode ein", html)
                self.assertNotIn("abcd-efgh", html)
                if "step=disable" not in url:
                    self.assertNotIn("Handy verloren?", html)

    def test_the_form_has_the_password_with_the_eye_and_the_code_as_a_plain_text_field(self):
        html = self.client.get(LOST_PAGE).content.decode()

        password = re.search(r'<input[^>]*name="current_password"[^>]*>', html).group(0)
        code = re.search(r'<input[^>]*name="otp_token"[^>]*>', html).group(0)
        self.assertIn('type="password"', password)
        self.assertRegex(html, r'class="password-field">\s*<input[^>]*name="current_password"')
        self.assertIn("data-password-toggle", html)
        self.assertIn('type="text"', code)
        for attribute in ("inputmode", "maxlength", "pattern"):
            self.assertNotIn(attribute, code)
        self.assertNotIn("value=", code)
        self.assertIn("Wiederherstellungscode", html)
        self.assertIn("Handy verloren?", html)
        # The way for somebody with neither the app nor a code.
        self.assertIn("Sie haben weder Ihr Handy noch einen Wiederherstellungscode?", html)
        self.assertIn("Betrieb", html)

    def test_a_failing_form_does_not_write_the_values_back(self):
        html = self.client.post(
            LOST_URL, {"current_password": PASSWORD, "otp_token": "abcd2346"}, REMOTE_ADDR="10.92.4.1"
        ).content.decode()

        self.assertNotIn(PASSWORD, html)
        self.assertNotIn("abcd2346", html)

    def test_every_password_field_of_the_profile_has_the_eye_and_starts_hidden(self):
        user = create_active_user("eyes@example.org")
        client = Client()
        client.force_login(user)
        pages = [f"{PROFILE}?edit=email", f"{PROFILE}?edit=password", f"{PROFILE}?edit=delete"]
        for client_, url in [(client, page) for page in pages] + [(self.client, LOST_PAGE)]:
            with self.subTest(url=url):
                html = client_.get(url).content.decode()
                fields = re.findall(r'<input[^>]*type="password"[^>]*>', html)
                self.assertTrue(fields)
                # Hidden by default: a password field, with the button that the script enables.
                self.assertEqual(html.count("data-password-toggle"), len(fields))
                self.assertIn("password-toggle.js", html)

    def test_the_way_back_is_called_cancel_like_everywhere_else(self):
        html = self.client.get(LOST_PAGE).content.decode()

        self.assertRegex(html, r'href="/profile/#card-two-factor">Abbrechen</a>')
        self.assertNotIn("Zurück", html)


@tag("mariadb")
class LostPhoneOneStepTests(TestCase):
    """L-1 (security review, block 9): the check of the code and the removal are one step."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def lost(self, **data):
        return self.client.post(LOST_URL, data or {"current_password": PASSWORD, "otp_token": RECOVERY_CODES[0]}, REMOTE_ADDR="10.94.1.1")

    def assert_given_back(self):
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(remaining(self.user), 3)
        self.assertFalse(MailJob.objects.exists())
        self.assertFalse(SecurityEvent.objects.filter(account_id=self.user.pk).exists())

    def test_a_refused_removal_gives_the_code_back(self):
        with mock.patch.object(totp, "disable", side_effect=totp.SecondFactorError("refused")):
            response = self.lost()

        self.assertRedirects(response, reverse("accounts:two_factor"), fetch_redirect_response=False)
        self.assert_given_back()
        # The code works afterwards.
        self.assertEqual(self.lost().status_code, 302)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())

    def test_a_promotion_to_staff_between_the_start_and_the_check_keeps_code_and_second_factor(self):
        real = profile_views.LostPhoneForm

        def promote_while_building(*args, **kwargs):
            # After the checks of the dispatch, before the lock of the account.
            promote(self.user)
            return real(*args, **kwargs)

        with mock.patch.object(profile_views, "LostPhoneForm", side_effect=promote_while_building):
            response = self.lost()

        self.assertRedirects(response, reverse("accounts:two_factor"), fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(remaining(self.user), 3)
        self.assertFalse(MailJob.objects.exists())
        self.assertFalse(
            SecurityEvent.objects.filter(
                account_id=self.user.pk,
                event__in=(
                    SecurityEvent.Event.RECOVERY_CODE_USED,
                    SecurityEvent.Event.TWO_FACTOR_REMOVED_PHONE_LOST,
                ),
            ).exists()
        )

    def test_the_second_factor_removed_between_the_start_and_the_check_is_refused_unseen(self):
        real = profile_views.LostPhoneForm

        def remove_while_building(*args, **kwargs):
            for device in totp.devices_for_user(self.user, confirmed=None):
                device.delete()
            return real(*args, **kwargs)

        with mock.patch.object(profile_views, "LostPhoneForm", side_effect=remove_while_building):
            response = self.lost()

        self.assertRedirects(response, reverse("accounts:two_factor"), fetch_redirect_response=False)
        self.assertFalse(MailJob.objects.exists())
        self.assertFalse(SessionCodeFailure.objects.exists())

    def test_a_paused_budget_with_the_right_password_and_the_right_code_removes_nothing(self):
        AuthenticatorDevice.objects.filter(pk=self.device.pk).update(
            failed_codes=totp.CODE_PAUSE_AT, failed_codes_since=timezone.now()
        )

        response = self.lost()

        self.assertContains(response, "pausiert", status_code=429)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(remaining(self.user), 3)
        self.assertFalse(SecurityEvent.objects.filter(account_id=self.user.pk).exists())


@tag("mariadb")
class LostPhoneFormStateTests(TestCase):
    def test_the_form_fails_closed_when_the_second_factor_went_between_building_and_checking(self):
        from django.test import RequestFactory

        user, _device = create_user_with_totp()
        request = RequestFactory().post("/")
        request.user = user
        form = LostPhoneForm(
            {"current_password": PASSWORD, "otp_token": RECOVERY_CODES[0]}, request=request
        )
        for device in totp.devices_for_user(user, confirmed=None):
            device.delete()

        self.assertFalse(form.is_valid())
        self.assertIn("Ihr Konto wurde inzwischen geändert", " ".join(form.non_field_errors()))
        self.assertFalse(SessionCodeFailure.objects.exists())


@tag("mariadb")
class LostPhoneParallelTests(TransactionTestCase):
    """Two posts with the same password and code: exactly one wins, the loser changes nothing."""

    def test_exactly_one_of_two_parallel_posts_with_the_same_code_wins(self):
        user, _device = create_user_with_totp()
        clients = []
        for _number in range(2):
            client = Client()
            client.force_login(user)
            clients.append(client)
        barrier = threading.Barrier(len(clients))
        results = []

        def work(client):
            try:
                barrier.wait()
                response = client.post(
                    LOST_URL,
                    {"current_password": PASSWORD, "otp_token": RECOVERY_CODES[0]},
                    REMOTE_ADDR="10.94.2.1",
                )
                results.append(response["Location"])
            finally:
                connection.close()

        threads = [threading.Thread(target=work, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        won = [location for location in results if location == f"{PROFILE}#card-two-factor"]
        refused = [location for location in results if location == reverse("accounts:two_factor")]
        self.assertEqual((len(won), len(refused)), (1, 1))
        self.assertFalse(AuthenticatorDevice.objects.filter(user=user).exists())
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.TWO_FACTOR_REMOVED_PHONE_LOST).count(), 1
        )
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.RECOVERY_CODE_USED).count(), 1)
        # The loser counted nothing, neither for the session nor as a failed login.
        self.assertFalse(SessionCodeFailure.objects.exists())
