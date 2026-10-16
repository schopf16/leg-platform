"""Stay signed in (D-056, team lead decision K21): a persistent session for users who ask for it.

Off by default; for a user a session of ``STAY_SIGNED_IN_DURATION`` that does not slide; ignored
(not hidden) for staff users and system administrators, whose shorter timeouts of D-041 stay.
"""

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core import mail
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import session_timeout
from leg_platform.accounts.models import VerificationToken
from leg_platform.accounts.second_factor import PENDING_SESSION_KEY
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    code_for,
    create_active_user,
    finish_registration,
    verification_path,
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

    def test_a_staff_user_gets_no_persistent_session_the_shorter_timeouts_stay(self):
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
            (
                settings.SESSION_PRIVILEGED_IDLE_TIMEOUT.total_seconds(),
                settings.SESSION_PRIVILEGED_ABSOLUTE_TIMEOUT.total_seconds(),
            ),
        )

    def test_the_timeouts_of_a_staying_session_are_the_duration_for_users_only(self):
        session = {session_timeout.STAYING_KEY: True}
        self.assertEqual(session_timeout.timeouts_for(self.user, session), (DURATION, DURATION))
        self.assertEqual(
            session_timeout.timeouts_for(self.user, {}),
            (
                settings.SESSION_IDLE_TIMEOUT.total_seconds(),
                settings.SESSION_ABSOLUTE_TIMEOUT.total_seconds(),
            ),
        )
        promote(self.user)
        self.assertEqual(
            session_timeout.timeouts_for(self.user, session)[0],
            settings.SESSION_PRIVILEGED_IDLE_TIMEOUT.total_seconds(),
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

    def test_a_staff_user_with_the_second_factor_is_not_persistent_either(self):
        promote(self.user)
        log_in(self.client, self.user.email, remember=True)
        response = send_code(self.client, code_for(self.device), address="10.9.0.1")
        self.assertNotIn(session_timeout.STAYING_KEY, self.client.session)
        self.assertEqual(cookie_max_age(response), settings.SESSION_COOKIE_AGE)


@tag("mariadb")
class AbandonedSecondStepTests(TestCase):
    """Security review L1: an abandoned second step leaves no wish behind that a later login of
    the same browser could use."""

    databases = {"default", "addresses", "address_import"}

    def test_a_registration_after_an_abandoned_ticked_login_is_not_persistent(self):
        user, _device = create_user_with_totp("holder.person@example.org")
        log_in(self.client, user.email, remember=True)
        # The code step is never completed; the same browser completes a registration.
        register(self.client)
        link = verification_path(mail.outbox[0])
        response = finish_registration(self.client, link)
        self.assertEqual(response.status_code, 302)
        session = self.client.session
        self.assertIn("_auth_user_id", session)
        self.assertNotIn(session_timeout.STAYING_KEY, session)
        self.assertEqual(session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
        self.assertEqual(cookie_max_age(response), settings.SESSION_COOKIE_AGE)
        self.assertFalse([key for key in session.keys() if "stay" in key])
