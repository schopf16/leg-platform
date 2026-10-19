"""Idle and absolute session timeouts (docs/SECURITY.md, sections 3 and 11; D-041)."""

from unittest import mock

from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts import session_timeout
from leg_platform.accounts.models import User
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    VerifiedClient,
    create_active_user,
)
from leg_platform.tests.test_instance_configuration import token_routes

START = 1_000_000.0
MINUTE = 60
HOUR = 3600
PROFILE_URL = reverse("accounts:profile")
LOGIN_URL = reverse("accounts:login")


@tag("mariadb")
class SessionTimeoutTests(TestCase):
    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()
        cls.staff = create_active_user("staff@example.org")
        cls.staff.set_privileges(is_staff=True, is_superuser=False)

    def open_session(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[session_timeout.STARTED_KEY] = START
        session[session_timeout.LAST_ACTIVITY_KEY] = START
        session.save()

    def get(self, at, url=PROFILE_URL, method="get", data=None):
        with mock.patch.object(session_timeout, "time") as clock:
            clock.time.return_value = START + at
            return getattr(self.client, method)(url, data)

    def assertLoggedIn(self, response):
        self.assertEqual(response.status_code, 200)

    def assertEnded(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(LOGIN_URL))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_starts_the_timeouts(self):
        self.client.post(LOGIN_URL, {"username": self.user.email, "password": PASSWORD})
        session = self.client.session
        self.assertIn("_auth_user_id", session)
        self.assertIsInstance(session[session_timeout.STARTED_KEY], float)
        self.assertEqual(
            session[session_timeout.STARTED_KEY], session[session_timeout.LAST_ACTIVITY_KEY]
        )

    def test_a_user_session_ends_after_thirty_idle_minutes(self):
        self.open_session(self.user)
        self.assertLoggedIn(self.get(29 * MINUTE))
        self.assertEnded(self.get(29 * MINUTE + 31 * MINUTE))

    def test_activity_keeps_a_session_open_without_an_absolute_limit(self):
        # Owner rule (D-088): whoever logged in stays logged in until the idle timeout or the logout.
        self.open_session(self.user)
        for minute in range(20, 12 * 60, 20):
            with self.subTest(minute=minute):
                self.assertLoggedIn(self.get(minute * MINUTE))
        self.assertLoggedIn(self.get(12 * HOUR + MINUTE))

    def test_staff_users_have_the_shorter_idle_timeout_and_no_absolute_limit(self):
        self.open_session(self.staff)
        self.assertLoggedIn(self.get(14 * MINUTE))
        self.assertEnded(self.get(14 * MINUTE + 16 * MINUTE))
        self.open_session(self.staff)
        # Active use for longer than the former four hours never forces a new login.
        for minute in range(10, 6 * 60, 10):
            self.assertLoggedIn(self.get(minute * MINUTE))
        self.assertLoggedIn(self.get(6 * HOUR + 5 * MINUTE))
        # Idle for more than 15 minutes ends it.
        self.assertEnded(self.get(6 * HOUR + 5 * MINUTE + 16 * MINUTE))

    def test_a_staff_user_active_every_minute_for_a_day_is_never_asked_again(self):
        self.open_session(self.staff)
        for minute in range(1, 24 * 60, 7):
            self.assertLoggedIn(self.get(minute * MINUTE))

    def test_a_user_is_not_ended_after_sixteen_idle_minutes(self):
        self.open_session(self.user)
        self.assertLoggedIn(self.get(16 * MINUTE))

    def test_a_polling_request_does_not_extend_the_idle_timeout(self):
        # A background poll (import status, live log) must not keep an abandoned session alive.
        self.open_session(self.staff)
        passive = mock.patch.object(session_timeout, "is_passive", return_value=True)
        with passive:
            self.assertLoggedIn(self.get(10 * MINUTE))
            self.assertEqual(self.client.session[session_timeout.LAST_ACTIVITY_KEY], START)
            # The poll after the idle timeout is answered 401 and the session is gone.
            response = self.get(16 * MINUTE)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_ended_session_cannot_be_used_again(self):
        self.open_session(self.user)
        old_key = self.client.session.session_key
        self.assertEnded(self.get(31 * MINUTE))
        self.assertFalse(Session.objects.filter(session_key=old_key).exists())
        self.assertEnded(self.get(31 * MINUTE + 1))

    def test_an_ended_session_leads_to_the_login_page_with_a_message(self):
        self.open_session(self.user)
        response = self.get(31 * MINUTE)
        self.assertEqual(response["Location"], f"{LOGIN_URL}?next={PROFILE_URL}")
        page = self.client.get(response["Location"])
        self.assertContains(page, "Sie wurden aus Sicherheitsgründen ausgeloggt")

    def test_a_post_after_the_timeout_changes_nothing(self):
        self.open_session(self.user)
        response = self.get(
            31 * MINUTE,
            reverse("accounts:password_change"),
            method="post",
            data={
                "current_password": PASSWORD,
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD,
            },
        )
        self.assertEnded(response)
        self.assertNotIn("next=", response["Location"])
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(PASSWORD))

    def test_a_link_with_a_token_is_not_put_into_the_login_url(self):
        # Every route with a token of the URL configuration (the five links of an email), not
        # a list that has to be kept up to date (security review of unit A, L-3).
        routes = token_routes()
        self.assertGreaterEqual(len(routes), 5, routes)
        self.open_session(self.user)
        for route, path in routes:
            with self.subTest(route=route):
                response = self.get(31 * MINUTE, path)
                self.assertEnded(response)
                self.assertNotIn("next=", response["Location"])
                self.open_session(self.user)

    def test_a_logged_in_session_without_the_times_is_ended(self):
        self.open_session(self.user)
        session = self.client.session
        del session[session_timeout.STARTED_KEY]
        session.save()
        self.assertEnded(self.client.get(PROFILE_URL))

    def test_a_session_started_in_the_future_is_ended(self):
        self.open_session(self.user)
        self.assertEnded(self.get(-MINUTE))

    def test_the_activity_is_written_only_once_per_interval(self):
        self.open_session(self.user)
        self.get(10)
        self.assertEqual(self.client.session[session_timeout.LAST_ACTIVITY_KEY], START)
        self.get(2 * MINUTE)
        self.assertEqual(
            self.client.session[session_timeout.LAST_ACTIVITY_KEY], START + 2 * MINUTE
        )

    def test_the_stored_session_lives_longer_than_the_longest_idle_timeout(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 8 * HOUR)
        self.assertGreater(settings.SESSION_COOKIE_AGE, settings.SESSION_IDLE_TIMEOUT.total_seconds())

    def test_anonymous_requests_are_not_affected(self):
        self.assertEqual(self.client.get(LOGIN_URL).status_code, 200)

    def test_the_idle_timeout_of_staff_users_is_shorter_than_that_of_users(self):
        self.assertLess(
            settings.SESSION_PRIVILEGED_IDLE_TIMEOUT, settings.SESSION_IDLE_TIMEOUT
        )

    def test_there_is_no_absolute_timeout_setting_for_ordinary_sessions(self):
        self.assertFalse(hasattr(settings, "SESSION_ABSOLUTE_TIMEOUT"))
        self.assertFalse(hasattr(settings, "SESSION_PRIVILEGED_ABSOLUTE_TIMEOUT"))
        self.assertEqual(session_timeout.timeouts_for(self.user, {})[1], None)
        self.assertEqual(session_timeout.timeouts_for(self.staff, {})[1], None)


@tag("mariadb")
class PollRoutesTests(TestCase):
    """The background polls on their REAL routes (``PASSIVE_VIEWS`` is resolved against the URL configuration): a
    poll neither extends the idle timeout nor survives it (401, no redirect, session gone), and a normal page does
    extend it (D-088)."""

    client_class = VerifiedClient
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("poll.administrator@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)

    def test_a_poll_on_a_real_route_is_no_activity_and_gets_a_401_after_the_idle_timeout(self):
        routes = (
            reverse("admin:logs_tail", kwargs={"log": "application"}),
            reverse("admin:addresses_importrequest_status"),
        )
        for route in routes:
            with self.subTest(route=route):
                self.client.force_login(self.administrator)
                session = self.client.session
                session[session_timeout.STARTED_KEY] = START
                session[session_timeout.LAST_ACTIVITY_KEY] = START
                session.save()

                def at(seconds, url):
                    with mock.patch.object(session_timeout, "time") as clock:
                        clock.time.return_value = START + seconds
                        return self.client.get(url)

                self.assertEqual(at(10 * MINUTE, route).status_code, 200)
                self.assertEqual(self.client.session[session_timeout.LAST_ACTIVITY_KEY], START)
                # A normal page is an activity; the idle time counts from it.
                self.assertEqual(at(10 * MINUTE + 1, reverse("admin:index")).status_code, 200)
                self.assertEqual(self.client.session[session_timeout.LAST_ACTIVITY_KEY], START + 10 * MINUTE + 1)
                # Fifteen minutes and more after the last activity (the polls in between changed nothing).
                answer = at(10 * MINUTE + 1 + 16 * MINUTE, route)
                self.assertEqual(answer.status_code, 401)
                self.assertNotIn("Location", answer)
                self.assertEqual(answer["Cache-Control"], "no-store")
                self.assertNotIn("_auth_user_id", self.client.session)
