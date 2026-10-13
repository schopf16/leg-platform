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

START = 1_000_000.0
MINUTE = 60
HOUR = 3600
PROFILE_URL = reverse("accounts:profile")
LOGIN_URL = reverse("accounts:login")


@tag("mariadb")
class SessionTimeoutTests(TestCase):
    client_class = VerifiedClient

    def setUp(self):
        self.user = create_active_user()
        self.staff = create_active_user("staff@example.org")
        self.staff.set_privileges(is_staff=True, is_superuser=False)

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

    def test_activity_keeps_the_session_open_until_the_absolute_timeout(self):
        self.open_session(self.user)
        for minute in range(20, 8 * 60, 20):
            with self.subTest(minute=minute):
                self.assertLoggedIn(self.get(minute * MINUTE))
        self.assertEnded(self.get(8 * HOUR + MINUTE))

    def test_staff_users_have_the_shorter_timeouts(self):
        self.open_session(self.staff)
        self.assertLoggedIn(self.get(14 * MINUTE))
        self.assertEnded(self.get(14 * MINUTE + 16 * MINUTE))
        self.open_session(self.staff)
        for minute in range(10, 4 * 60, 10):
            self.assertLoggedIn(self.get(minute * MINUTE))
        self.assertEnded(self.get(4 * HOUR + MINUTE))

    def test_a_user_is_not_ended_after_sixteen_idle_minutes(self):
        self.open_session(self.user)
        self.assertLoggedIn(self.get(16 * MINUTE))

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
        self.assertContains(page, "Sie wurden aus Sicherheitsgründen abgemeldet")

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
        self.open_session(self.user)
        url = reverse("accounts:two_factor_reset_verify", args=["x" * 43])
        response = self.get(31 * MINUTE, url)
        self.assertEnded(response)
        self.assertNotIn("next=", response["Location"])

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

    def test_the_stored_session_lives_as_long_as_the_longest_absolute_timeout(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 8 * HOUR)

    def test_anonymous_requests_are_not_affected(self):
        self.assertEqual(self.client.get(LOGIN_URL).status_code, 200)

    def test_the_timeouts_of_staff_users_are_shorter_than_those_of_users(self):
        self.assertLess(
            settings.SESSION_PRIVILEGED_IDLE_TIMEOUT, settings.SESSION_IDLE_TIMEOUT
        )
        self.assertLessEqual(
            settings.SESSION_PRIVILEGED_ABSOLUTE_TIMEOUT, settings.SESSION_ABSOLUTE_TIMEOUT
        )
