"""Wrong app codes in the confirmations of the profile count for the session, not for the account
(D-062, block 9, M-1).

Whoever only holds a session must not be able to pause the login of the account: the wrong codes
of the profile do not touch the budget of the account and are no failed logins. Five wrong codes
(of the right shape) end this session, with an entry in the security log and one notice to every
address. A format error counts nowhere.
"""

import time
from contextlib import contextmanager
from io import StringIO
from threading import Event, Thread
from types import SimpleNamespace
from unittest import mock

from axes.models import AccessAttempt
from django.core import mail
from django.core.management import call_command
from django.db import connection, transaction
from django.test import Client, TestCase, TransactionTestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import second_factor, totp
from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.login_protection import LOGIN_FAILURE_LIMIT
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    ConfirmationFailure,
    LoginIpAttempt,
    MailJob,
    PendingEmailChange,
    SecurityEvent,
    SessionCodeFailure,
    User,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    code_for,
    create_active_user,
    run_mail_queue,
)
from leg_platform.accounts.tests.test_second_factor import (
    CODE_URL,
    create_user_with_totp,
    log_in,
    send_code,
)

PROFILE = reverse("accounts:profile")
LOGIN = reverse("accounts:login")
EMAIL_URL = reverse("accounts:email_change_request")
PASSWORD_URL = reverse("accounts:password_change")
DELETE_URL = reverse("accounts:account_delete")
CODES_URL = reverse("accounts:recovery_codes")
DISABLE_URL = reverse("accounts:two_factor_disable")
LOST_URL = reverse("accounts:two_factor_lost_phone")
LIMIT = second_factor.SESSION_CODE_LIMIT
ENDED = "Aus Sicherheitsgründen wurden Sie abgemeldet"
WRONG = "000001"
SECOND = "second.session.limit@example.org"

# Every confirmation in state A, with the other fields that it needs to be form-valid.
FORMS = {
    "email": (EMAIL_URL, {"email": "new.session.limit@example.org", "secondary_email": ""}),
    "password": (PASSWORD_URL, {"new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD}),
    "delete": (DELETE_URL, {"confirm": "on"}),
    "codes": (CODES_URL, {}),
    "disable": (DISABLE_URL, {}),
}


def digest_of(client):
    """The digest under which the count of the session of the client is kept."""
    identifier = client.session[second_factor.SESSION_COUNTER_KEY]
    return keyed_digest(identifier, second_factor.SESSION_CODE_FAILURE_PURPOSE)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp("session.limit@example.org")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def wrong(self, url="disable", client=None, value=WRONG):
        path, data = FORMS[url]
        return (client or self.client).post(path, {**data, "otp_token": value}, REMOTE_ADDR="10.93.1.1")

    def budget(self):
        return AuthenticatorDevice.objects.get(pk=self.device.pk).failed_codes


@tag("mariadb")
class TheSessionEndsTests(Base):
    def test_five_wrong_codes_end_the_session_in_every_confirmation(self):
        for name, (path, data) in FORMS.items():
            with self.subTest(form=name):
                # An account of its own for each form: the cap of the account (15 over all
                # sessions) is not the subject here.
                user, _device = create_user_with_totp(f"{name}.session.limit@example.org")
                client = Client()
                client.force_login(user)
                for _attempt in range(LIMIT - 1):
                    self.assertEqual(self.wrong(name, client).status_code, 200)

                last = self.wrong(name, client)

                self.assertRedirects(last, LOGIN, fetch_redirect_response=False)
                # The session is gone: the profile asks for the login.
                self.assertEqual(client.get(PROFILE).status_code, 302)
                self.assertRedirects(
                    client.get(PROFILE), f"{LOGIN}?next={PROFILE}", fetch_redirect_response=False
                )

    def test_the_lost_phone_way_counts_the_wrong_recovery_codes_too(self):
        for value in ("abcd2346", "abcd2347", "abcd2342", "abcd2343", "abcd2344"):
            response = self.client.post(
                LOST_URL,
                {"current_password": PASSWORD, "otp_token": value},
                REMOTE_ADDR="10.93.1.2",
            )

        self.assertRedirects(response, LOGIN, fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(self.budget(), 0)

    def test_nothing_was_changed_by_the_wrong_codes(self):
        for _attempt in range(LIMIT):
            self.wrong("email")

        self.assertFalse(PendingEmailChange.objects.filter(user=self.user).exists())
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(PASSWORD))

    def test_the_person_is_told_at_the_login_page_and_can_log_in_with_the_right_code(self):
        for _attempt in range(LIMIT):
            self.wrong("password")

        page = self.client.get(LOGIN)

        self.assertContains(page, ENDED)
        self.assertContains(page, "Bitte melden Sie sich erneut an.")
        # The account was neither paused nor locked out: the password and the right code log in.
        stepper = Client()
        log_in(stepper, self.user.email, address="10.93.1.3")
        response = send_code(stepper, code_for(self.device), "10.93.1.3")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(stepper.get(PROFILE).status_code, 200)

    def test_the_message_is_shown_once(self):
        for _attempt in range(LIMIT):
            self.wrong()
        self.client.get(LOGIN)

        self.assertNotContains(self.client.get(LOGIN), ENDED)


@tag("mariadb")
class TheAccountIsNotAffectedTests(Base):
    def test_the_budget_of_the_account_and_the_abuse_protection_stay_untouched(self):
        # Twelve real wrong codes in three sessions (under the limit of a session and under the cap
        # of the account): the budget of the account, which also pauses the login, is untouched.
        for _session in range(3):
            client = Client()
            client.force_login(self.user)
            for number in range(LIMIT - 1):
                self.wrong("disable", client, f"{number + 100:06d}")

        self.assertEqual(self.budget(), 0)
        self.assertFalse(totp.code_attempts_paused(self.user))
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.CODE_ATTEMPTS_WARNING).exists())
        self.assertEqual(AccessAttempt.objects.count(), 0)
        self.assertEqual(LoginIpAttempt.objects.count(), 0)
        self.assertFalse(
            SecurityEvent.objects.filter(
                event__in=(
                    SecurityEvent.Event.CODE_ATTEMPTS_WARNING,
                    SecurityEvent.Event.CODE_ATTEMPTS_PAUSED,
                    SecurityEvent.Event.LOGIN_LOCKOUT,
                )
            ).exists()
        )
        # The login of the account works at once.
        stepper = Client()
        log_in(stepper, self.user.email, address="10.93.2.1")
        self.assertEqual(send_code(stepper, code_for(self.device), "10.93.2.1").status_code, 302)

    def test_the_budget_still_counts_at_the_login(self):
        # The login step keeps the budget of the account (D-040).
        stepper = Client()
        log_in(stepper, self.user.email, address="10.93.2.2")
        send_code(stepper, "000002", "10.93.2.2")

        self.assertEqual(self.budget(), 1)

    def test_a_paused_account_still_refuses_even_the_right_code(self):
        AuthenticatorDevice.objects.filter(pk=self.device.pk).update(
            failed_codes=totp.CODE_PAUSE_AT, failed_codes_since=timezone.now()
        )

        response = self.client.post(DISABLE_URL, {"otp_token": code_for(self.device)}, REMOTE_ADDR="10.93.2.3")

        self.assertContains(response, "pausiert", status_code=429)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertFalse(SessionCodeFailure.objects.exists())


@tag("mariadb")
class ThePerSessionCounterTests(Base):
    def test_another_session_of_the_account_is_not_touched(self):
        other = Client()
        other.force_login(self.user)
        for _attempt in range(LIMIT - 1):
            self.wrong("disable", other)
        for _attempt in range(LIMIT):
            self.wrong("disable")

        # The first session ended; the other one has four and goes on.
        self.assertEqual(self.client.get(PROFILE).status_code, 302)
        self.assertEqual(other.get(PROFILE).status_code, 200)
        self.assertEqual(SessionCodeFailure.objects.get(session_digest=digest_of(other)).failures, LIMIT - 1)

    def test_a_right_code_sets_the_count_back(self):
        for _attempt in range(LIMIT - 1):
            self.wrong("codes")

        done = self.client.post(CODES_URL, {"otp_token": code_for(self.device)}, REMOTE_ADDR="10.93.3.1")

        self.assertEqual(done.status_code, 200)
        self.assertFalse(SessionCodeFailure.objects.exists())
        # Four more wrong codes do not end the session: the count started again.
        for _attempt in range(LIMIT - 1):
            self.assertEqual(self.wrong("email").status_code, 200)
        self.assertEqual(self.client.get(PROFILE).status_code, 200)

    def test_a_format_error_counts_nowhere(self):
        for value in ("not a code", "12345", "1234567", "abcd2345", ""):
            for _attempt in range(LIMIT):
                self.assertEqual(self.wrong("email", value=value).status_code, 200)

        self.assertFalse(SessionCodeFailure.objects.exists())
        self.assertEqual(self.client.get(PROFILE).status_code, 200)

    def test_a_wrong_password_in_state_b_counts_for_the_session_like_a_wrong_code(self):
        user = create_active_user("state.b.limit@example.org")
        client = Client()
        client.force_login(user)

        for attempt in range(LOGIN_FAILURE_LIMIT - 1):
            response = client.post(
                DELETE_URL,
                {"current_password": "a wrong password for the test", "confirm": "on"},
                REMOTE_ADDR="10.93.3.5",
            )
            self.assertEqual(response.status_code, 200)

        self.assertEqual(SessionCodeFailure.objects.get(account=user).failures, LIMIT - 1)
        # Not a failed login: neither Axes nor the IP counter know about it (block 10, M-2).
        self.assertEqual(AccessAttempt.objects.count(), 0)
        self.assertEqual(LoginIpAttempt.objects.count(), 0)
        # The fifth ends the session.
        last = client.post(
            DELETE_URL,
            {"current_password": "a wrong password for the test", "confirm": "on"},
            REMOTE_ADDR="10.93.3.5",
        )
        self.assertRedirects(last, LOGIN, fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        # The owner logs in at once.
        stepper = Client()
        response = log_in(stepper, user.email, address="10.93.3.6")
        self.assertRedirects(response, reverse("home"))

    def test_a_right_password_in_state_b_starts_the_count_again(self):
        user = create_active_user("state.b.reset@example.org")
        client = Client()
        client.force_login(user)
        for _attempt in range(LIMIT - 1):
            client.post(
                PASSWORD_URL,
                {"current_password": "a wrong password for the test", "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
                REMOTE_ADDR="10.93.3.7",
            )

        done = client.post(
            PASSWORD_URL,
            {"current_password": PASSWORD, "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
            REMOTE_ADDR="10.93.3.7",
        )

        self.assertEqual(done.status_code, 302)
        self.assertFalse(SessionCodeFailure.objects.filter(account=user).exists())

    def test_the_count_is_not_in_the_session_and_cannot_be_set_by_the_browser(self):
        for _attempt in range(3):
            self.wrong("email")
        # Server side, found by a keyed digest of the key; the session holds no counter and the
        # table no key.
        self.assertFalse(any("fail" in key or "code" in key for key in self.client.session.keys()))
        row = SessionCodeFailure.objects.get()
        self.assertEqual(row.failures, 3)
        self.assertNotEqual(row.session_digest, self.client.session.session_key)

        # Values of the request do nothing to it.
        self.client.cookies["session_code_failures"] = "0"
        path, data = FORMS["email"]
        self.client.post(
            path,
            {**data, "otp_token": WRONG, "failures": "0", "session_code_failures": "0", "session_digest": "x"},
            HTTP_X_SESSION_CODE_FAILURES="0",
            REMOTE_ADDR="10.93.3.2",
        )
        self.assertEqual(SessionCodeFailure.objects.get().failures, 4)
        self.assertRedirects(self.wrong("email"), LOGIN, fetch_redirect_response=False)

    def test_the_cleanup_removes_the_old_rows_and_keeps_the_current_ones(self):
        old = SessionCodeFailure.objects.create(
            account=self.user, session_digest="x" * 64, failures=2,
            updated_at=timezone.now() - second_factor.SESSION_CODE_FAILURE_RETENTION * 2,
        )
        old_entry = ConfirmationFailure.objects.create(
            account=self.user, occurred_at=timezone.now() - second_factor.ACCOUNT_FAILURE_WINDOW * 2
        )
        self.wrong()

        call_command("cleanup_confirmation_failures", stdout=StringIO())

        self.assertFalse(SessionCodeFailure.objects.filter(pk=old.pk).exists())
        self.assertFalse(ConfirmationFailure.objects.filter(pk=old_entry.pk).exists())
        self.assertEqual(SessionCodeFailure.objects.count(), 1)
        self.assertEqual(ConfirmationFailure.objects.count(), 1)

    def test_a_normal_logout_deletes_the_count_and_the_end_by_wrong_codes_keeps_it(self):
        for _attempt in range(3):
            self.wrong()
        self.assertEqual(SessionCodeFailure.objects.count(), 1)

        self.client.post(reverse("accounts:logout"))

        self.assertFalse(SessionCodeFailure.objects.exists())
        other = Client()
        other.force_login(self.user)
        for _attempt in range(LIMIT):
            self.wrong("disable", other)
        # The row of the ended session stays: it refuses a request that was already running.
        self.assertEqual(SessionCodeFailure.objects.get().failures, LIMIT)

    def test_the_rows_go_with_the_account(self):
        self.wrong()
        self.assertEqual(SessionCodeFailure.objects.count(), 1)

        self.user.delete()

        self.assertFalse(SessionCodeFailure.objects.exists())


@tag("mariadb")
class TheNoticeAndTheEntryTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user.set_secondary_email(SECOND)

    def test_one_event_and_one_notice_to_every_address(self):
        for _attempt in range(LIMIT):
            self.wrong()

        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.SESSION_ENDED_WRONG_CODES)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().account_id, self.user.pk)
        jobs = MailJob.objects.filter(kind=MailJob.Kind.SESSION_ENDED_WRONG_CODES)
        self.assertEqual(jobs.count(), 2)
        run_mail_queue()
        self.assertEqual(
            sorted(address for message in mail.outbox for address in message.to),
            sorted([self.user.email, SECOND]),
        )
        body = mail.outbox[0].body
        self.assertIn("Sitzung", mail.outbox[0].subject)
        self.assertIn("ungewöhnliche Eingaben", body)
        self.assertNotIn(WRONG, body)

    def test_the_entry_and_the_log_line_hold_no_secret(self):
        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                for _attempt in range(LIMIT):
                    self.wrong()

        line = next(line for line in logs.output if "session was ended" in line)
        self.assertIn(f"account {self.user.pk}", line)
        self.assertNotIn(WRONG, line)
        self.assertNotIn(self.user.email, line)

    def test_a_late_request_of_the_ended_session_is_refused_and_tells_nobody_again(self):
        # A parallel request that was already running when the session ended (the logout is
        # switched off to keep the session for it): it is refused unseen and counts nothing.
        with mock.patch.object(second_factor, "end_session_for_wrong_codes"):
            for _attempt in range(LIMIT):
                self.wrong()
            late = self.client.post(DISABLE_URL, {"otp_token": code_for(self.device)}, REMOTE_ADDR="10.93.4.1")
            again = self.wrong()

        self.assertEqual(late.status_code, 302)
        self.assertEqual(again.status_code, 302)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(SessionCodeFailure.objects.get().failures, LIMIT)
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.SESSION_ENDED_WRONG_CODES).count(), 1
        )
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.SESSION_ENDED_WRONG_CODES).count(), 2)

    def test_the_event_is_kept_for_the_standard_period(self):
        for _attempt in range(LIMIT):
            self.wrong()

        entry = SecurityEvent.objects.get(event=SecurityEvent.Event.SESSION_ENDED_WRONG_CODES)
        self.assertFalse(entry.long_retention)


@tag("mariadb")
class ConfirmationCounterLockOrderTests(TransactionTestCase):
    """The counter rows are changed only under the lock of the account row (D-072).

    The order is always account row, then counter rows: the requests (``second_factor``), the
    login and logout (``forget_confirmation_failures``) and the clean-up. A second connection that
    holds the lock of the account stands for a request that is running.
    """

    def setUp(self):
        self.user = create_active_user()
        self.other = create_active_user("other.lock.order@example.org")
        now = timezone.now()
        self.long_ago = now - second_factor.SESSION_CODE_FAILURE_RETENTION * 2
        self.day_ago = now - second_factor.ACCOUNT_FAILURE_WINDOW * 2

    def add_rows(self, account, letter):
        """One expired and one current row of each kind; the digests differ per account."""
        old_session = SessionCodeFailure.objects.create(
            account=account, session_digest=letter * 64, failures=1, updated_at=self.long_ago
        )
        SessionCodeFailure.objects.create(account=account, session_digest=letter.upper() * 64)
        old_entry = ConfirmationFailure.objects.create(account=account, occurred_at=self.day_ago)
        ConfirmationFailure.objects.create(account=account, occurred_at=timezone.now())
        return old_session, old_entry

    @contextmanager
    def account_locked(self, account):
        locked, release = Event(), Event()

        def hold():
            try:
                with transaction.atomic():
                    User._base_manager.select_for_update().get(pk=account.pk)
                    locked.set()
                    release.wait(20)
            finally:
                connection.close()

        holder = Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(locked.wait(10))
            yield
        finally:
            release.set()
            holder.join()

    def test_the_cleanup_removes_the_expired_rows_of_every_account_and_keeps_the_others(self):
        old = [self.add_rows(self.user, "a"), self.add_rows(self.other, "b")]
        output = StringIO()

        call_command("cleanup_confirmation_failures", stdout=output)

        for old_session, old_entry in old:
            self.assertFalse(SessionCodeFailure.objects.filter(pk=old_session.pk).exists())
            self.assertFalse(ConfirmationFailure.objects.filter(pk=old_entry.pk).exists())
        for account in (self.user, self.other):
            self.assertEqual(SessionCodeFailure.objects.filter(account=account).count(), 1)
            self.assertEqual(ConfirmationFailure.objects.filter(account=account).count(), 1)
        self.assertIn("Deleted 2 counts of sessions and 2 entries", output.getvalue())
        self.assertNotIn("Left", output.getvalue())

    def test_the_cleanup_skips_an_account_that_a_request_holds_and_never_waits_for_it(self):
        held_session, held_entry = self.add_rows(self.user, "a")
        free_session, free_entry = self.add_rows(self.other, "b")
        output = StringIO()

        with self.account_locked(self.user):
            started = time.monotonic()
            call_command("cleanup_confirmation_failures", stdout=output)
            elapsed = time.monotonic() - started

            # The holder lets go after 20 s at the latest: a cleanup that waited for it would
            # have been late, and would have deleted the rows.
            self.assertLess(elapsed, 15)
            self.assertTrue(SessionCodeFailure.objects.filter(pk=held_session.pk).exists())
            self.assertTrue(ConfirmationFailure.objects.filter(pk=held_entry.pk).exists())
            self.assertFalse(SessionCodeFailure.objects.filter(pk=free_session.pk).exists())
            self.assertFalse(ConfirmationFailure.objects.filter(pk=free_entry.pk).exists())
            self.assertIn("Left 2 for the next run", output.getvalue())

        call_command("cleanup_confirmation_failures", stdout=StringIO())

        self.assertFalse(SessionCodeFailure.objects.filter(pk=held_session.pk).exists())
        self.assertFalse(ConfirmationFailure.objects.filter(pk=held_entry.pk).exists())
        self.assertEqual(SessionCodeFailure.objects.filter(account=self.user).count(), 1)
        self.assertEqual(ConfirmationFailure.objects.filter(account=self.user).count(), 1)

    def test_the_cleanup_takes_the_lock_of_the_account_before_it_deletes(self):
        self.add_rows(self.user, "a")
        calls = []
        real = User._base_manager.select_for_update

        def record(*args, **kwargs):
            calls.append(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(User._base_manager, "select_for_update", side_effect=record):
            call_command("cleanup_confirmation_failures", stdout=StringIO())

        # One lock for each of the two kinds of rows; never a wait for a request.
        self.assertEqual(calls, [{"skip_locked": True}, {"skip_locked": True}])

    def forget_in_a_thread(self, user, identifier):
        """Start ``forget_confirmation_failures`` in a thread; return its events and the thread."""
        request = SimpleNamespace(session={second_factor.SESSION_COUNTER_KEY: identifier})
        entered, done = Event(), Event()
        errors = []
        real = second_factor._session_digests

        def entering(request):
            entered.set()
            return real(request)

        def forget():
            try:
                with mock.patch.object(second_factor, "_session_digests", side_effect=entering):
                    second_factor.forget_confirmation_failures(request, user)
            except Exception as error:  # reported by the assertion of the test
                errors.append(error)
            finally:
                done.set()
                connection.close()

        thread = Thread(target=forget)
        thread.start()
        return entered, done, errors, thread

    def test_the_logout_waits_for_the_lock_of_the_account(self):
        identifier = "an-identifier-of-a-session"
        digest = keyed_digest(identifier, second_factor.SESSION_CODE_FAILURE_PURPOSE)
        SessionCodeFailure.objects.create(account=self.user, session_digest=digest, failures=2)
        SessionCodeFailure.objects.create(account=self.user, session_digest="k" * 64, failures=1)

        with self.account_locked(self.user):
            entered, done, errors, thread = self.forget_in_a_thread(self.user, identifier)
            self.assertTrue(entered.wait(10), "The thread did not start.")
            self.assertFalse(done.wait(1.0), "It did not wait for the lock of the account.")
            self.assertEqual(SessionCodeFailure.objects.filter(account=self.user).count(), 2)
        thread.join(20)

        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])
        # Only the row of this session went.
        self.assertEqual(
            list(SessionCodeFailure.objects.values_list("session_digest", flat=True)), ["k" * 64]
        )

    def test_forgetting_inside_the_transaction_that_holds_the_lock_does_not_wait(self):
        identifier = "another-identifier-of-a-session"
        digest = keyed_digest(identifier, second_factor.SESSION_CODE_FAILURE_PURPOSE)
        SessionCodeFailure.objects.create(account=self.user, session_digest=digest, failures=2)
        request = SimpleNamespace(session={second_factor.SESSION_COUNTER_KEY: identifier})

        with transaction.atomic():
            User._base_manager.select_for_update().get(pk=self.user.pk)
            second_factor.reset_confirmation_failures(request, self.user)

        self.assertFalse(SessionCodeFailure.objects.exists())

    def test_forgetting_after_the_account_was_deleted_removes_nothing_and_does_not_fail(self):
        identifier = "an-identifier-of-a-deleted-account"
        request = SimpleNamespace(session={second_factor.SESSION_COUNTER_KEY: identifier})
        gone = User.objects.get(pk=self.other.pk)
        self.other.delete()

        second_factor.forget_confirmation_failures(request, gone)

        self.assertFalse(SessionCodeFailure.objects.exists())
