"""Stay signed in (D-056, D-088): a persistent session for everyone who asks for it.

Off by default; a session of ``STAY_SIGNED_IN_DURATION`` that does not slide, for users as well as
staff users and system administrators (owner decision D-088). The second factor is asked once, at the
login; the persistent session exists only after the code was accepted. A privileged account
without a second factor gets no persistent session.
"""

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core import mail
from django.test import TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import mail_queue, session_timeout
from leg_platform.accounts.models import MailJob, SecurityEvent
from leg_platform.accounts.second_factor import PENDING_SESSION_KEY
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    code_for,
    create_active_user,
    finish_registration,
    run_mail_queue,
    confirmation_code,
)
from leg_platform.accounts.tests.test_registration import register
from leg_platform.accounts.tests.test_second_factor import (
    CODE_URL,
    create_user_with_totp,
    promote,
    send_code,
)

LOGIN_URL = reverse("accounts:login")
PROFILE_URL = reverse("accounts:profile")
HOME_URL = reverse("home")
DAY = 86400
START = 1_000_000.0
DURATION = settings.STAY_SIGNED_IN_DURATION.total_seconds()


def log_in(client, email, remember=False, address="10.9.0.1"):
    data = {"username": email, "password": PASSWORD}
    if remember:
        data["remember_me"] = "on"
    return client.post(LOGIN_URL, data, REMOTE_ADDR=address)


def cookie_max_age(response):
    return int(response.cookies[settings.SESSION_COOKIE_NAME]["max-age"])


@tag("mariadb")
class StaySignedInTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()

    def test_the_login_page_has_the_box_and_it_is_not_ticked(self):
        response = self.client.get(LOGIN_URL)
        html = response.content.decode()
        self.assertContains(response, 'name="remember_me"')
        self.assertContains(response, "Angemeldet bleiben")
        box = html[html.index('name="remember_me"') - 80 : html.index('name="remember_me"') + 120]
        self.assertNotIn("checked", box)
        # A native checkbox of the design package, not Tabler's drawn one.
        self.assertIn('class="entry-check-input"', html)

    def test_the_default_is_off_the_session_has_the_normal_length(self):
        response = log_in(self.client, self.user.email)
        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        session = self.client.session
        self.assertNotIn(session_timeout.STAYING_KEY, session)
        self.assertEqual(session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
        self.assertEqual(cookie_max_age(response), settings.SESSION_COOKIE_AGE)

    def test_the_session_of_an_unticked_login_ends_after_thirty_idle_minutes(self):
        log_in(self.client, self.user.email)
        with mock.patch.object(session_timeout, "time") as clock:
            clock.time.return_value = self.client.session[session_timeout.STARTED_KEY] + 31 * 60
            response = self.client.get(PROFILE_URL)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(LOGIN_URL))

    def test_ticked_a_user_gets_a_persistent_session_of_fourteen_days(self):
        self.assertEqual(DURATION, 14 * DAY)
        response = log_in(self.client, self.user.email, remember=True)
        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        session = self.client.session
        self.assertIs(session[session_timeout.STAYING_KEY], True)
        # The wish is a value of the request, never of the session (security review L1).
        self.assertFalse([key for key in session.keys() if "stay" in key and key != session_timeout.STAYING_KEY])
        self.assertAlmostEqual(session.get_expiry_age(), DURATION, delta=10)
        self.assertAlmostEqual(cookie_max_age(response), DURATION, delta=10)

    def test_a_staying_session_survives_days_of_idle_time_but_not_the_duration(self):
        log_in(self.client, self.user.email, remember=True)
        started = self.client.session[session_timeout.STARTED_KEY]
        with mock.patch.object(session_timeout, "time") as clock:
            clock.time.return_value = started + 13 * DAY
            self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)
            # The duration does not slide: it counts from the login, however active the person is.
            clock.time.return_value = started + 14 * DAY + 60
            response = self.client.get(PROFILE_URL)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(LOGIN_URL))

    def test_the_end_of_a_staying_session_does_not_slide_with_activity(self):
        # Security review L3: an integer expiry would mean "that many seconds of inactivity" and
        # move the end at every save; a timedelta is stored as an absolute end time.
        log_in(self.client, self.user.email, remember=True)
        key = self.client.session.session_key
        ends = Session.objects.get(session_key=key).expire_date
        started = self.client.session[session_timeout.STARTED_KEY]
        later = timezone.now() + timedelta(days=3)
        with mock.patch.object(session_timeout, "time") as clock, mock.patch(
            "django.contrib.sessions.backends.base.timezone.now", return_value=later
        ), mock.patch("django.contrib.sessions.backends.db.timezone.now", return_value=later):
            clock.time.return_value = started + 3 * DAY
            response = self.client.get(PROFILE_URL)
        self.assertEqual(response.status_code, 200)
        # The activity was written, so the session was saved and the cookie sent again ...
        self.assertEqual(
            self.client.session[session_timeout.LAST_ACTIVITY_KEY], started + 3 * DAY
        )
        # ... with the remaining time, and the stored end did not move.
        self.assertAlmostEqual(cookie_max_age(response), DURATION - 3 * DAY, delta=10)
        self.assertEqual(Session.objects.get(session_key=key).expire_date, ends)

    def test_a_staff_user_without_a_second_factor_gets_no_persistent_session(self):
        # The wish is ignored until the account has its second factor: the restricted session that
        # can only set the factor up never becomes a 14-day session (D-088).
        promote(self.user)
        response = log_in(self.client, self.user.email, remember=True)
        # The login worked; the account still has to set up the second factor.
        self.assertEqual(response.status_code, 302)
        session = self.client.session
        self.assertIn("_auth_user_id", session)
        self.assertNotIn(session_timeout.STAYING_KEY, session)
        self.assertEqual(session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
        self.assertEqual(
            session_timeout.timeouts_for(self.user, session),
            (settings.SESSION_PRIVILEGED_IDLE_TIMEOUT.total_seconds(), None),
        )

    def test_the_timeouts_of_a_staying_session_are_the_duration_for_everyone(self):
        session = {session_timeout.STAYING_KEY: True}
        self.assertEqual(session_timeout.timeouts_for(self.user, session), (DURATION, DURATION))
        self.assertEqual(
            session_timeout.timeouts_for(self.user, {}),
            (settings.SESSION_IDLE_TIMEOUT.total_seconds(), None),
        )
        promote(self.user)
        # A staying session of a staff user keeps its own limit; without the mark the idle timeout is the short one.
        self.assertEqual(session_timeout.timeouts_for(self.user, session), (DURATION, DURATION))
        self.assertEqual(
            session_timeout.timeouts_for(self.user, {}),
            (settings.SESSION_PRIVILEGED_IDLE_TIMEOUT.total_seconds(), None),
        )

    def test_a_wrong_password_starts_no_session_and_keeps_no_wish(self):
        response = self.client.post(
            LOGIN_URL, {"username": self.user.email, "password": "wrong", "remember_me": "on"},
            REMOTE_ADDR="10.9.0.2",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn(session_timeout.STAYING_KEY, self.client.session)
        self.assertFalse([key for key in self.client.session.keys() if "stay" in key])

    def test_the_wish_of_an_earlier_login_does_not_carry_over(self):
        log_in(self.client, self.user.email, remember=True)
        self.client.post(reverse("accounts:logout"))
        response = log_in(self.client, self.user.email, remember=False)
        self.assertNotIn(session_timeout.STAYING_KEY, self.client.session)
        self.assertEqual(cookie_max_age(response), settings.SESSION_COOKIE_AGE)

    def test_a_login_without_the_box_ends_the_persistence_of_the_same_session(self):
        # Security review L2: a second login in the same session, without a logout in between,
        # must not keep the long expiry of the first one.
        log_in(self.client, self.user.email, remember=True)
        self.assertIs(self.client.session[session_timeout.STAYING_KEY], True)
        response = log_in(self.client, self.user.email, remember=False, address="10.9.0.3")
        session = self.client.session
        self.assertNotIn(session_timeout.STAYING_KEY, session)
        self.assertEqual(session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
        self.assertEqual(cookie_max_age(response), settings.SESSION_COOKIE_AGE)

    def test_a_value_in_the_session_of_the_client_cannot_ask_for_a_persistent_session(self):
        session = self.client.session
        session["stay_signed_in_requested"] = True
        session[session_timeout.STAYING_KEY] = True
        session.save()
        response = log_in(self.client, self.user.email, remember=False)
        self.assertNotIn(session_timeout.STAYING_KEY, self.client.session)
        self.assertEqual(cookie_max_age(response), settings.SESSION_COOKIE_AGE)


@tag("mariadb")
class StaySignedInWithSecondFactorTests(TestCase):
    """The wish survives the second step, and only there is the session persistent."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def test_ticked_the_session_is_persistent_after_the_code(self):
        response = log_in(self.client, self.user.email, remember=True)
        self.assertRedirects(response, CODE_URL)
        # Nothing is persistent before the code: the session is not logged in, and the wish is
        # held by the pending login only.
        session = self.client.session
        self.assertNotIn("_auth_user_id", session)
        self.assertIs(session[PENDING_SESSION_KEY]["stay"], True)
        response = send_code(self.client, code_for(self.device), address="10.9.0.1")
        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        session = self.client.session
        self.assertIs(session[session_timeout.STAYING_KEY], True)
        self.assertNotIn(PENDING_SESSION_KEY, session)
        self.assertAlmostEqual(session.get_expiry_age(), DURATION, delta=10)
        self.assertAlmostEqual(cookie_max_age(response), DURATION, delta=10)

    def test_unticked_the_session_is_normal_after_the_code(self):
        log_in(self.client, self.user.email, remember=False)
        response = send_code(self.client, code_for(self.device), address="10.9.0.1")
        self.assertNotIn(session_timeout.STAYING_KEY, self.client.session)
        self.assertEqual(cookie_max_age(response), settings.SESSION_COOKIE_AGE)

    def test_a_staff_user_with_the_second_factor_can_stay_signed_in_after_the_code(self):
        promote(self.user)
        response = log_in(self.client, self.user.email, remember=True)
        self.assertRedirects(response, CODE_URL)
        # Nothing is persistent before the code: no login, the wish waits in the pending login.
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertIs(self.client.session[PENDING_SESSION_KEY]["stay"], True)
        response = send_code(self.client, code_for(self.device), address="10.9.0.1")
        session = self.client.session
        self.assertIs(session[session_timeout.STAYING_KEY], True)
        self.assertAlmostEqual(session.get_expiry_age(), DURATION, delta=10)
        self.assertAlmostEqual(cookie_max_age(response), DURATION, delta=10)
        # The session is verified (the factor is complete), so the privileges work, also days later.
        started = session[session_timeout.STARTED_KEY]
        with mock.patch.object(session_timeout, "time") as clock:
            clock.time.return_value = started + 5 * DAY
            self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)
            self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
            # Its own limit stays: after the duration the staying session of a staff user ends too.
            clock.time.return_value = started + 14 * DAY + 60
            ended = self.client.get(PROFILE_URL)
        self.assertEqual(ended.status_code, 302)
        self.assertTrue(ended["Location"].startswith(LOGIN_URL))

    def test_a_long_session_of_a_staff_user_is_logged_without_any_email_but_not_that_of_a_person(self):
        # The second factor is checked once per login; a long session of a privileged account stays in the
        # security log (long retention), without an email (owner decision 2026-10-19: normal security, D-100).
        promote(self.user)
        log_in(self.client, self.user.email, remember=False)
        send_code(self.client, code_for(self.device), address="10.9.0.1")
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.PERSISTENT_SESSION_STARTED).exists())
        self.client.post(reverse("accounts:logout"))
        log_in(self.client, self.user.email, remember=True)
        send_code(self.client, code_for(self.device), address="10.9.0.1")
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.PERSISTENT_SESSION_STARTED)
        self.assertEqual(event.account_id, self.user.pk)
        self.assertTrue(event.long_retention)
        # No job, nothing sent.
        self.assertFalse(MailJob.objects.exists())
        run_mail_queue()
        self.assertEqual(mail.outbox, [])

    def test_a_leftover_job_of_the_removed_long_session_mail_is_given_up_without_a_crash(self):
        job = MailJob.objects.create(
            kind="persistent_session_started", address=self.user.email, next_attempt_at=timezone.now(),
        )
        with self.assertLogs("leg_platform.accounts.mail_queue", level="ERROR"):
            self.assertEqual(mail_queue.process_pending(), (0, 1))
        self.assertEqual(mail.outbox, [])
        self.assertTrue(MailJob.objects.filter(pk=job.pk).exists())
        # Stale jobs of an unknown kind are deleted like any other request after an hour.
        MailJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(hours=2))
        mail_queue.delete_stale_jobs()
        self.assertFalse(MailJob.objects.filter(pk=job.pk).exists())

    def test_the_data_migration_deletes_the_jobs_and_the_text_of_the_removed_mail_and_nothing_else(self):
        import importlib

        from django.apps import apps

        from leg_platform.accounts.models import EmailText, EmailTextVersion

        migration = importlib.import_module("leg_platform.accounts.migrations.0033_remove_persistent_session_mail")
        now = timezone.now()
        MailJob.objects.create(kind="persistent_session_started", address="a@example.org", next_attempt_at=now)
        kept_job = MailJob.objects.create(kind=MailJob.Kind.PASSWORD_CHANGED, address="b@example.org", next_attempt_at=now)
        text = EmailText.objects.create(key="persistent_session_started", subject="Old")
        EmailTextVersion.objects.create(email_text=text, subject="Older", body="")
        kept_text = EmailText.objects.create(key=EmailText.Key.PASSWORD_RESET, subject="Kept")

        migration.delete_removed_mail(apps, None)

        self.assertEqual(list(MailJob.objects.values_list("pk", flat=True)), [kept_job.pk])
        self.assertEqual(list(EmailText.objects.values_list("pk", flat=True)), [kept_text.pk])
        self.assertFalse(EmailTextVersion.objects.exists())
        # Safe to run again.
        migration.delete_removed_mail(apps, None)

    def test_a_person_without_staff_rights_gets_no_notice_for_a_long_session(self):
        log_in(self.client, self.user.email, remember=True)
        send_code(self.client, code_for(self.device), address="10.9.0.1")
        self.assertIs(self.client.session[session_timeout.STAYING_KEY], True)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.PERSISTENT_SESSION_STARTED).exists())
        self.assertFalse(MailJob.objects.exists())

    def test_a_staff_user_without_a_second_factor_gets_no_long_session_and_no_notice(self):
        user = create_active_user("nofactor.staff@example.org")
        promote(user)
        log_in(self.client, user.email, remember=True)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.PERSISTENT_SESSION_STARTED).exists())

    def test_a_wrong_code_gives_a_staff_user_no_session_even_with_the_wish(self):
        promote(self.user)
        log_in(self.client, self.user.email, remember=True)
        response = send_code(self.client, "000000", address="10.9.0.1")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn(session_timeout.STAYING_KEY, self.client.session)

    def test_the_logout_ends_the_staying_session_of_a_staff_user(self):
        promote(self.user)
        log_in(self.client, self.user.email, remember=True)
        send_code(self.client, code_for(self.device), address="10.9.0.1")
        self.assertIs(self.client.session[session_timeout.STAYING_KEY], True)
        self.client.post(reverse("accounts:logout"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn(session_timeout.STAYING_KEY, self.client.session)

    def test_a_staff_user_unticked_gets_the_short_idle_timeout_and_no_persistence(self):
        promote(self.user)
        log_in(self.client, self.user.email, remember=False)
        response = send_code(self.client, code_for(self.device), address="10.9.0.1")
        self.assertNotIn(session_timeout.STAYING_KEY, self.client.session)
        self.assertEqual(cookie_max_age(response), settings.SESSION_COOKIE_AGE)
        with mock.patch.object(session_timeout, "time") as clock:
            clock.time.return_value = self.client.session[session_timeout.STARTED_KEY] + 16 * 60
            ended = self.client.get(PROFILE_URL)
        self.assertEqual(ended.status_code, 302)

    def test_removing_the_rights_of_a_staying_staff_user_ends_the_session(self):
        promote(self.user)
        log_in(self.client, self.user.email, remember=True)
        send_code(self.client, code_for(self.device), address="10.9.0.1")
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)
        self.user.refresh_from_db()
        self.user.end_all_sessions()
        response = self.client.get(PROFILE_URL)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(LOGIN_URL))


def restart_browser(client):
    """What a browser does when it is closed and opened again: cookies without Max-Age and Expires are dropped."""
    for name in list(client.cookies.keys()):
        morsel = client.cookies[name]
        if not (morsel["max-age"] or morsel["expires"]):
            del client.cookies[name]


def session_morsel(response):
    return response.cookies[settings.SESSION_COOKIE_NAME]


def assert_persistent_cookie(test, response, remaining=DURATION):
    """The session cookie of the response has Max-Age and Expires (a persistent cookie), as stage sends it."""
    morsel = session_morsel(response)
    test.assertTrue(morsel["expires"], "no Expires: the browser would drop the cookie when it is closed")
    test.assertAlmostEqual(int(morsel["max-age"]), remaining, delta=120)
    test.assertTrue(morsel["secure"])
    test.assertTrue(morsel["httponly"])


STAGE_COOKIES = {"SESSION_COOKIE_NAME": "__Host-sessionid", "SESSION_COOKIE_SECURE": True}


@tag("mariadb")
@override_settings(**STAGE_COOKIES)
class StaySignedInSurvivesTheBrowserTests(TestCase):
    """Regression (stage walkthrough): a ticked login must still be open after the browser was closed and
    opened again, with the cookie name and the Secure flag of stage (``__Host-sessionid``).

    The browser is simulated by dropping every cookie that has neither Max-Age nor Expires.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()
        cls.second_factor_user, cls.device = create_user_with_totp("second.factor@example.org")

    def _days_later(self, days, url=PROFILE_URL, secure=True):
        started = self.client.session[session_timeout.STARTED_KEY]
        with mock.patch.object(session_timeout, "time") as clock:
            clock.time.return_value = started + days * DAY
            return self.client.get(url, secure=secure)

    def test_the_login_response_sends_a_persistent_secure_cookie(self):
        response = self.client.post(
            LOGIN_URL, {"username": self.user.email, "password": PASSWORD, "remember_me": "on"},
            REMOTE_ADDR="10.9.1.1", secure=True,
        )
        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        assert_persistent_cookie(self, response)

    def test_a_ticked_login_is_still_open_after_the_browser_was_closed_and_days_later(self):
        self.client.post(
            LOGIN_URL, {"username": self.user.email, "password": PASSWORD, "remember_me": "on"},
            REMOTE_ADDR="10.9.1.1", secure=True,
        )
        for day in (0, 1, 7, 13):
            restart_browser(self.client)
            self.assertIn(settings.SESSION_COOKIE_NAME, self.client.cookies)
            response = self._days_later(day)
            self.assertEqual(response.status_code, 200, f"signed out on day {day}")
            if settings.SESSION_COOKIE_NAME in response.cookies:
                # A request that wrote the session sends the cookie again: still persistent, never a browser-session
                # cookie (the clock of Django's expiry is the real one here, so the remaining time is the full duration).
                assert_persistent_cookie(self, response)
        # The start page is open as well, not the login page.
        self.assertEqual(self._days_later(13, url=HOME_URL).status_code, 200)

    def test_a_ticked_login_with_the_second_factor_is_still_open_after_the_browser_was_closed(self):
        response = self.client.post(
            LOGIN_URL,
            {"username": self.second_factor_user.email, "password": PASSWORD, "remember_me": "on"},
            REMOTE_ADDR="10.9.1.2", secure=True,
        )
        self.assertRedirects(response, CODE_URL)
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(
            CODE_URL, {"otp_token": code_for(self.device)}, REMOTE_ADDR="10.9.1.2", secure=True
        )
        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        assert_persistent_cookie(self, response)
        for day in (0, 6, 13):
            restart_browser(self.client)
            self.assertEqual(self._days_later(day).status_code, 200, f"signed out on day {day}")
        # No new code was asked for, and the day after the duration the login page comes.
        self.assertEqual(self._days_later(14.01, url=HOME_URL).status_code, 302)

    def test_a_ticked_login_of_a_staff_user_with_the_second_factor_survives_the_browser(self):
        promote(self.second_factor_user)
        self.client.post(
            LOGIN_URL,
            {"username": self.second_factor_user.email, "password": PASSWORD, "remember_me": "on"},
            REMOTE_ADDR="10.9.1.3", secure=True,
        )
        response = self.client.post(
            CODE_URL, {"otp_token": code_for(self.device)}, REMOTE_ADDR="10.9.1.3", secure=True
        )
        assert_persistent_cookie(self, response)
        restart_browser(self.client)
        self.assertEqual(self._days_later(5, url=reverse("admin:index")).status_code, 200)

    def test_every_later_request_that_writes_the_session_keeps_the_expiry(self):
        self.client.post(
            LOGIN_URL, {"username": self.user.email, "password": PASSWORD, "remember_me": "on"},
            REMOTE_ADDR="10.9.1.1", secure=True,
        )
        key = self.client.session.session_key
        ends = Session.objects.get(session_key=key).expire_date
        started = self.client.session[session_timeout.STARTED_KEY]
        with mock.patch.object(session_timeout, "time") as clock:
            for minutes in (2, 4, 6):
                clock.time.return_value = started + minutes * 60
                response = self.client.get(PROFILE_URL, secure=True)
                self.assertEqual(response.status_code, 200)
                assert_persistent_cookie(self, response)
        self.assertEqual(Session.objects.get(session_key=key).expire_date, ends)
        self.assertAlmostEqual((ends - timezone.now()).total_seconds(), DURATION, delta=120)

    def test_an_unticked_login_is_ended_by_the_idle_timeout_and_keeps_its_row_bounded(self):
        # Documented policy (D-100): without the box there is no persistence beyond the idle timeout of 30 minutes;
        # the cookie age of 8 hours only bounds an abandoned cookie and row.
        response = self.client.post(
            LOGIN_URL, {"username": self.user.email, "password": PASSWORD}, REMOTE_ADDR="10.9.1.1", secure=True
        )
        self.assertEqual(int(session_morsel(response)["max-age"]), settings.SESSION_COOKIE_AGE)
        restart_browser(self.client)
        self.assertEqual(self._days_later(0).status_code, 200)
        self.assertEqual(self._days_later(1).status_code, 302)


@tag("mariadb")
class AbandonedSecondStepTests(TestCase):
    """Security review L1: an abandoned second step leaves no wish behind that a later login of
    the same browser could use."""

    databases = {"default", "addresses"}

    def test_a_registration_after_an_abandoned_ticked_login_is_not_persistent(self):
        user, _device = create_user_with_totp("holder.person@example.org")
        log_in(self.client, user.email, remember=True)
        # The code step is never completed; the same browser completes a registration.
        register(self.client)
        response = finish_registration(self.client, confirmation_code(mail.outbox[0]))
        self.assertEqual(response.status_code, 302)
        session = self.client.session
        self.assertIn("_auth_user_id", session)
        self.assertNotIn(session_timeout.STAYING_KEY, session)
        self.assertEqual(session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
        self.assertEqual(cookie_max_age(response), settings.SESSION_COOKIE_AGE)
        self.assertFalse([key for key in session.keys() if "stay" in key])
