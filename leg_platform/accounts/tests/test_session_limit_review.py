"""Security review of block 9, delta (D-062, block 10): the count of wrong entries in the
confirmations survives the renewal of the session key (M-1), a wrong password counts like a wrong
code and never reaches Axes (M-2), all sessions end at the cap of the account (M-1), parallel
requests cannot exceed the limits, and the key rotation of the secret key is handled (L-b)."""

import threading
from unittest import mock

from axes.models import AccessAttempt
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.db import connection
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts import profile_views, second_factor, totp
from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    ConfirmationFailure,
    LoginIpAttempt,
    MailJob,
    SecurityEvent,
    SessionCodeFailure,
    VerificationToken,
)
from leg_platform.accounts.password_reset import PASSWORD_RESET_LIFETIME
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    code_for,
    create_active_user,
    run_mail_queue,
)
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp, log_in, send_code

LOGIN = reverse("accounts:login")
PROFILE = reverse("accounts:profile")
DISABLE_URL = reverse("accounts:two_factor_disable")
DELETE_URL = reverse("accounts:account_delete")
LIMIT = second_factor.SESSION_CODE_LIMIT
CAP = second_factor.ACCOUNT_FAILURE_LIMIT
KEY = second_factor.SESSION_COUNTER_KEY
SECOND = "second.review.limit@example.org"


def wrong_code(client, address="10.95.1.1"):
    return client.post(DISABLE_URL, {"otp_token": "000001"}, REMOTE_ADDR=address)


def counter_of(user):
    rows = SessionCodeFailure.objects.filter(account=user)
    return rows.get().failures if rows.exists() else 0


@tag("mariadb")
class RenewalOfTheKeyTests(TestCase):
    """M-1: Django renews the key of the session (``cycle_key``); the count must stay."""

    def setUp(self):
        self.user, self.device = create_user_with_totp("renewal.limit@example.org")
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_login_form_in_a_logged_in_session_does_not_set_the_count_back(self):
        for _attempt in range(LIMIT - 1):
            wrong_code(self.client)
        key_before = self.client.session.session_key
        identifier = self.client.session[KEY]

        # The password is right: the login asks for the code and renews the key of the session.
        response = log_in(self.client, self.user.email, address="10.95.2.1")

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(self.client.session.session_key, key_before)
        self.assertEqual(self.client.session[KEY], identifier)
        self.assertEqual(counter_of(self.user), LIMIT - 1)
        # One more wrong code ends the session, as it would have without the renewal.
        last = wrong_code(self.client)
        self.assertRedirects(last, LOGIN, fetch_redirect_response=False)

    def test_endless_guessing_by_renewing_the_key_is_not_possible(self):
        ended = False
        for _round in range(4):
            for _attempt in range(LIMIT - 1):
                if wrong_code(self.client).status_code == 302:
                    ended = True
                    break
            if ended:
                break
            log_in(self.client, self.user.email, address="10.95.2.2")

        self.assertTrue(ended)
        self.assertLessEqual(ConfirmationFailure.objects.filter(account=self.user).count(), CAP)
        self.assertEqual(self.client.get(PROFILE).status_code, 302)

    def test_a_landing_page_of_a_link_renews_the_key_and_keeps_the_count(self):
        for _attempt in range(3):
            wrong_code(self.client)
        identifier = self.client.session[KEY]
        raw = VerificationToken.objects.issue(
            self.user, VerificationToken.Purpose.PASSWORD_RESET, PASSWORD_RESET_LIFETIME
        )

        key_before = self.client.session.session_key

        self.client.get(reverse("accounts:password_reset_verify", args=[raw]))

        # The landing page renews the key of the session; the identifier and the count stay.
        session = self.client.session
        self.assertNotEqual(session.session_key, key_before)
        self.assertEqual(session[KEY], identifier)
        self.assertEqual(counter_of(self.user), 3)

    def test_a_session_without_an_identifier_gets_one_from_its_key_at_the_first_use(self):
        session = self.client.session
        del session[KEY]
        session.save()
        key = self.client.session.session_key

        wrong_code(self.client)

        self.assertEqual(self.client.session[KEY], key)
        log_in(self.client, self.user.email, address="10.95.2.3")
        self.assertEqual(counter_of(self.user), 1)

    def test_the_browser_cannot_choose_the_identifier(self):
        wrong_code(self.client)
        identifier = self.client.session[KEY]
        self.client.cookies[KEY] = "chosen-by-the-browser"

        self.client.post(
            DISABLE_URL, {"otp_token": "000001", KEY: "chosen-by-the-browser"}, REMOTE_ADDR="10.95.2.4"
        )

        self.assertEqual(self.client.session[KEY], identifier)
        self.assertEqual(counter_of(self.user), 2)


@tag("mariadb")
class WrongPasswordsTests(TestCase):
    """M-2: a wrong password in a confirmation is no failed login."""

    def setUp(self):
        self.user = create_active_user("passwords.limit@example.org")
        self.client = Client()
        self.client.force_login(self.user)

    def test_wrong_passwords_never_reach_axes_the_ip_counter_or_the_lockout(self):
        for _session in range(3):
            client = Client()
            client.force_login(self.user)
            for number in range(LIMIT):
                client.post(
                    DELETE_URL,
                    {"current_password": f"a wrong password {number}", "confirm": "on"},
                    REMOTE_ADDR="10.95.3.1",
                )

        self.assertEqual(AccessAttempt.objects.count(), 0)
        self.assertEqual(LoginIpAttempt.objects.count(), 0)
        # The owner logs in at once; nothing is locked.
        response = log_in(Client(), self.user.email, address="10.95.3.2")
        self.assertRedirects(response, reverse("home"))

    def test_a_wrong_password_in_the_password_card_counts_for_the_session(self):
        client = Client()
        client.force_login(self.user)
        for _attempt in range(LIMIT):
            response = client.post(
                reverse("accounts:password_change"),
                {"current_password": "a wrong one", "new_password1": "x" * 20, "new_password2": "x" * 20},
                REMOTE_ADDR="10.95.3.3",
            )

        self.assertRedirects(response, LOGIN, fetch_redirect_response=False)
        self.assertEqual(AccessAttempt.objects.count(), 0)

    def test_the_wrong_password_and_the_wrong_code_share_the_count_in_the_lost_phone_way(self):
        user, _device = create_user_with_totp("lost.share@example.org")
        client = Client()
        client.force_login(user)
        url = reverse("accounts:two_factor_lost_phone")
        answers = [
            ("a wrong password", "abcd2342"),
            (PASSWORD, "abcd2343"),
            ("a wrong password", "abcd2344"),
            (PASSWORD, "abcd2346"),
            ("a wrong password", "abcd2347"),
        ]

        for password, code in answers:
            response = client.post(url, {"current_password": password, "otp_token": code}, REMOTE_ADDR="10.95.3.4")

        self.assertRedirects(response, LOGIN, fetch_redirect_response=False)
        self.assertEqual(AccessAttempt.objects.count(), 0)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=user).exists())


@tag("mariadb")
class CapOfTheAccountTests(TestCase):
    """15 wrong entries in 24 hours over all sessions end all sessions; the login is not paused."""

    def setUp(self):
        self.user, self.device = create_user_with_totp("cap.limit@example.org")
        self.user.set_secondary_email(SECOND)

    def sessions(self, count):
        clients = []
        for _number in range(count):
            client = Client()
            client.force_login(self.user)
            clients.append(client)
        return clients

    def test_the_fifteenth_wrong_entry_ends_all_sessions_once(self):
        clients = self.sessions(4)
        last = None
        # Four wrong entries per session stay under the limit of a session; the fifteenth over
        # all sessions reaches the cap.
        for number in range(CAP):
            last = wrong_code(clients[number // (LIMIT - 1)])

        self.assertRedirects(last, LOGIN, fetch_redirect_response=False)
        for client in clients:
            self.assertEqual(client.get(PROFILE).status_code, 302)
        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.ALL_SESSIONS_ENDED)
        self.assertEqual(events.count(), 1)
        self.assertEqual(
            MailJob.objects.filter(kind=MailJob.Kind.ALL_SESSIONS_ENDED).count(), 2
        )
        run_mail_queue()
        self.assertEqual(
            sorted(address for message in mail.outbox for address in message.to),
            sorted([self.user.email, SECOND]),
        )
        self.assertFalse(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.SESSION_ENDED_WRONG_CODES).exists()
        )

    def test_the_login_is_not_paused_by_it(self):
        clients = self.sessions(4)
        for number in range(CAP):
            wrong_code(clients[number // (LIMIT - 1)])

        self.assertEqual(AuthenticatorDevice.objects.get(pk=self.device.pk).failed_codes, 0)
        self.assertFalse(totp.code_attempts_paused(self.user))
        self.assertEqual(AccessAttempt.objects.count(), 0)
        stepper = Client()
        log_in(stepper, self.user.email, address="10.95.4.1")
        self.assertRedirects(send_code(stepper, code_for(self.device), "10.95.4.1"), reverse("home"))
        # The count starts again after the cap.
        self.assertEqual(ConfirmationFailure.objects.filter(account=self.user).count(), 0)

    def test_fourteen_wrong_entries_end_no_other_session(self):
        clients = self.sessions(4)
        for number in range(CAP - 1):
            wrong_code(clients[number // (LIMIT - 1)])

        for client in clients:
            self.assertEqual(client.get(PROFILE).status_code, 200)
        self.assertFalse(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.ALL_SESSIONS_ENDED).exists()
        )

    def test_old_entries_are_outside_the_window(self):
        from datetime import timedelta

        from django.utils import timezone

        for _number in range(CAP - 1):
            ConfirmationFailure.objects.create(
                account=self.user, occurred_at=timezone.now() - timedelta(hours=25)
            )
        client = self.sessions(1)[0]

        wrong_code(client)

        self.assertEqual(client.get(PROFILE).status_code, 200)
        self.assertEqual(ConfirmationFailure.objects.filter(account=self.user).count(), 1)

    def test_a_request_that_was_already_running_when_the_sessions_ended_is_refused(self):
        # The sessions end between the start of the request and the check: the request finds the
        # session hash changed and is refused unseen; the code is not used up.
        client = self.sessions(1)[0]
        real = profile_views.ReauthenticationForm

        def end_the_sessions_while_building(*args, **kwargs):
            self.user.end_all_sessions()
            return real(*args, **kwargs)

        with mock.patch.object(
            profile_views, "ReauthenticationForm", side_effect=end_the_sessions_while_building
        ):
            response = client.post(
                DISABLE_URL, {"otp_token": code_for(self.device)}, REMOTE_ADDR="10.95.4.2"
            )

        self.assertRedirects(response, LOGIN, fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(totp.remaining_recovery_codes(self.user), 3)


@tag("mariadb")
class ParallelRequestsTests(TransactionTestCase):
    """Parallel wrong entries in one session (and in many sessions) cannot exceed the limits."""

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

    def shared_session_clients(self, user, count, legacy=False):
        first = Client()
        first.force_login(user)
        if legacy:
            session = first.session
            del session[KEY]
            session.save()
        clients = [first]
        for _number in range(count - 1):
            client = Client()
            client.cookies[settings.SESSION_COOKIE_NAME] = first.cookies[settings.SESSION_COOKIE_NAME].value
            clients.append(client)
        return clients

    def assert_limited(self, user):
        failures = counter_of(user)
        self.assertLessEqual(failures, LIMIT)
        self.assertEqual(ConfirmationFailure.objects.filter(account=user).count(), failures)
        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.SESSION_ENDED_WRONG_CODES)
        self.assertEqual(events.count(), 1 if failures == LIMIT else 0)
        self.assertEqual(
            MailJob.objects.filter(kind=MailJob.Kind.SESSION_ENDED_WRONG_CODES).count(),
            1 if failures == LIMIT else 0,
        )
        self.assertEqual(AccessAttempt.objects.count(), 0)

    def test_parallel_wrong_codes_of_one_session_cannot_exceed_the_limit(self):
        user, device = create_user_with_totp("parallel.one@example.org")
        clients = self.shared_session_clients(user, 12)

        self.run_all([lambda client=client: wrong_code(client).status_code for client in clients])

        self.assert_limited(user)
        self.assertEqual(AuthenticatorDevice.objects.get(pk=device.pk).failed_codes, 0)

    def test_the_same_for_a_session_without_an_identifier(self):
        user, _device = create_user_with_totp("parallel.legacy@example.org")
        clients = self.shared_session_clients(user, 12, legacy=True)

        self.run_all([lambda client=client: wrong_code(client).status_code for client in clients])

        self.assert_limited(user)

    def test_parallel_wrong_passwords_of_one_session_cannot_exceed_the_limit(self):
        user = create_active_user("parallel.password@example.org")
        clients = self.shared_session_clients(user, 12)

        self.run_all(
            [
                lambda client=client: client.post(
                    DELETE_URL,
                    {"current_password": "a wrong password", "confirm": "on"},
                    REMOTE_ADDR="10.95.5.1",
                ).status_code
                for client in clients
            ]
        )

        self.assertLessEqual(counter_of(user), LIMIT)
        self.assertEqual(AccessAttempt.objects.count(), 0)

    def test_parallel_wrong_codes_of_many_sessions_stay_at_the_cap(self):
        user, _device = create_user_with_totp("parallel.many@example.org")
        clients = []
        for _number in range(10):
            client = Client()
            client.force_login(user)
            clients.append(client)

        self.run_all([lambda client=client: wrong_code(client).status_code for client in clients * 3])

        # Ten sessions, three entries each: the cap of 15 ended every session once, and the
        # count started again; the events and notices follow the number of times it was reached.
        reached = SecurityEvent.objects.filter(event=SecurityEvent.Event.ALL_SESSIONS_ENDED).count()
        self.assertGreaterEqual(reached, 1)
        self.assertEqual(
            MailJob.objects.filter(kind=MailJob.Kind.ALL_SESSIONS_ENDED).count(), reached
        )
        self.assertLess(ConfirmationFailure.objects.filter(account=user).count(), CAP)
        self.assertEqual(AccessAttempt.objects.count(), 0)


@tag("mariadb")
class KeyRotationOfTheSecretKeyTests(TestCase):
    """L-b: after a rotation of ``SECRET_KEY`` the count is found under a fallback key."""

    OLD = "old-secret-key-for-the-rotation-test-" + "o" * 30
    NEW = "new-secret-key-for-the-rotation-test-" + "n" * 30

    def test_a_count_under_the_old_key_is_found_and_moved_to_the_new_key(self):
        user = create_active_user("rotation.limit@example.org")
        request = RequestFactory().post("/")
        request.session = SessionStore()
        request.session[KEY] = "an-identifier"
        with override_settings(SECRET_KEY=self.OLD, SECRET_KEY_FALLBACKS=[]):
            old_digest = keyed_digest("an-identifier", second_factor.SESSION_CODE_FAILURE_PURPOSE)
        SessionCodeFailure.objects.create(account=user, session_digest=old_digest, failures=3)

        with override_settings(SECRET_KEY=self.NEW, SECRET_KEY_FALLBACKS=[self.OLD]):
            row, digest = second_factor._counter_row(request, user)
            new_digest = keyed_digest("an-identifier", second_factor.SESSION_CODE_FAILURE_PURPOSE)

        self.assertEqual(row.failures, 3)
        self.assertEqual(digest, new_digest)
        self.assertNotEqual(old_digest, new_digest)
        self.assertEqual(SessionCodeFailure.objects.get().session_digest, new_digest)
