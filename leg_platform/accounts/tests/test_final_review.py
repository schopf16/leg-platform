"""Corrections of the final security review (block 5b): open address change requests do not
outlive a change of the hold on the account, one escaping rule for all mail texts, the stop links
after a confirmed login address, the session key of the link of the second factor reset, the code
that is used up last, and the error pages with a session whose database fails.
"""

from datetime import timedelta
from importlib import import_module
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.db import OperationalError
from django.test import Client, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import email_change, email_texts, second_factor_reset
from leg_platform.accounts.models import (
    PendingEmailChange,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    VerifiedClient,
    create_active_user,
    run_mail_queue,
)
from leg_platform.accounts.tests.test_password_reset import NEW_PASSWORD, SET_URL, request_reset, reset_path
from leg_platform.accounts.tests.test_profile import NEW, SECOND
from leg_platform.accounts.tests.test_second_factor import (
    RECOVERY_CODES,
    create_user_with_totp,
)
from leg_platform.accounts.tests.test_second_factor_reset import (
    administrator,
    link_in as reset_link_in,
    reset_url,
    with_second_factor,
)

Purpose = VerificationToken.Purpose
REQUEST_PURPOSES = (Purpose.EMAIL_CHANGE, Purpose.SECONDARY_EMAIL, Purpose.EMAIL_CHANGE_STOP)
PROFILE_URL = reverse("accounts:profile")
EMAIL_URL = reverse("accounts:email_change_request")
PASSWORD_URL = reverse("accounts:password_change")


def open_requests(user):
    """Give the account both kinds of request and the three kinds of link; return the raw links."""
    expires_at = timezone.now() + timedelta(hours=24)
    PendingEmailChange.objects.create(
        user=user, kind=PendingEmailChange.Kind.LOGIN, address=NEW, expires_at=expires_at
    )
    PendingEmailChange.objects.create(
        user=user, kind=PendingEmailChange.Kind.SECONDARY, address=SECOND, expires_at=expires_at
    )
    lifetime = timedelta(hours=24)
    return {
        Purpose.EMAIL_CHANGE: VerificationToken.objects.issue(
            user, Purpose.EMAIL_CHANGE, lifetime, target_email=NEW
        ),
        Purpose.SECONDARY_EMAIL: VerificationToken.objects.issue(
            user, Purpose.SECONDARY_EMAIL, lifetime, target_email=SECOND
        ),
        Purpose.EMAIL_CHANGE_STOP: VerificationToken.objects.issue(
            user, Purpose.EMAIL_CHANGE_STOP, lifetime
        ),
    }


def count_requests(user):
    return (
        PendingEmailChange.objects.filter(user=user).count(),
        VerificationToken.objects.filter(user=user, purpose__in=REQUEST_PURPOSES).count(),
    )


@tag("mariadb")
class RequestsEndWithTheHoldOnTheAccountTests(TestCase):
    """M-A: a request made before a reset, a new password, a block or a reset of the second factor
    cannot be confirmed afterwards. An account without a request is not affected."""

    def setUp(self):
        self.user = create_active_user("holder.person@example.org")
        self.other = create_active_user("other.person@example.org")
        open_requests(self.user)
        open_requests(self.other)
        self.assertEqual(count_requests(self.user), (2, 3))

    def assert_discarded(self):
        self.assertEqual(count_requests(self.user), (0, 0))
        # Nothing of another account is touched.
        self.assertEqual(count_requests(self.other), (2, 3))

    def test_a_password_reset_discards_the_requests(self):
        mail.outbox.clear()
        request_reset(self.user.email)
        client = Client()
        client.get(reset_path(mail.outbox[0]))

        response = client.post(
            SET_URL, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
        )

        self.assertEqual(response.status_code, 302)
        self.assert_discarded()

    def test_a_failed_reset_keeps_the_requests(self):
        mail.outbox.clear()
        request_reset(self.user.email)
        client = Client()
        client.get(reset_path(mail.outbox[0]))

        client.post(SET_URL, {"new_password1": "short", "new_password2": "short"})

        self.assertEqual(count_requests(self.user), (2, 3))

    def test_a_password_change_discards_the_requests(self):
        self.client.force_login(self.user)

        response = self.client.post(
            PASSWORD_URL,
            {
                "current_password": PASSWORD,
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assert_discarded()

    def test_a_rejected_password_change_keeps_the_requests(self):
        self.client.force_login(self.user)

        self.client.post(
            PASSWORD_URL,
            {"current_password": "wrong", "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
        )

        self.assertEqual(PendingEmailChange.objects.filter(user=self.user).count(), 2)

    def test_a_status_other_than_active_discards_the_requests(self):
        self.user.set_status(User.Status.BLOCKED)

        self.assert_discarded()

    def test_the_status_active_again_is_not_a_reason(self):
        self.user.set_status(User.Status.ACTIVE)

        self.assertEqual(count_requests(self.user), (2, 3))

    def test_an_account_without_a_request_is_not_affected(self):
        quiet = create_active_user("quiet.person@example.org")

        quiet.set_status(User.Status.BLOCKED)

        self.assertEqual(quiet.status, User.Status.BLOCKED)
        self.assertEqual(count_requests(quiet), (0, 0))

    def test_the_reset_of_the_second_factor_by_the_owner_discards_the_requests(self):
        with_second_factor(self.user)
        boss = administrator()
        client = VerifiedClient()
        client.force_login(boss)
        client.post(reset_url(self.user), {})
        run_mail_queue()
        owner = Client()
        owner.get(reset_link_in(mail.outbox[-1]))

        response = owner.post(reverse("accounts:two_factor_reset_confirm"), {})

        self.assertEqual(response.status_code, 302)
        self.assert_discarded()

    def test_the_reset_of_a_staff_account_by_an_administrator_discards_the_requests(self):
        self.user.set_privileges(is_staff=True, is_superuser=False)
        with_second_factor(self.user)
        boss = administrator()

        second_factor_reset.reset_privileged(boss, self.user)

        self.assert_discarded()

    def test_the_emergency_command_discards_the_requests(self):
        with_second_factor(self.user)
        with mock.patch("builtins.input", return_value="yes"):
            call_command(
                "emergency_access", "reset-second-factor", str(self.user.pk), stdout=StringIO()
            )

        self.assert_discarded()


@tag("mariadb")
class MailTextEscapingTests(TestCase):
    """L1: every mail text is plain text, so a "&" is never written as "&amp;"."""

    def test_every_mail_template_switches_the_escaping_off(self):
        folder = Path(__file__).resolve().parent.parent / "templates" / "accounts" / "email"
        files = sorted(folder.glob("*.txt"))

        self.assertGreaterEqual(len(files), 18)
        for path in files:
            with self.subTest(template=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("{% autoescape off %}", text)
                self.assertIn("{% endautoescape %}", text)

    def test_every_default_text_keeps_an_ampersand_in_every_placeholder(self):
        shown = 0
        for key in email_texts.DEFINITIONS:
            values = {
                placeholder.name: f"https://www.example.org/{placeholder.name}?a=1&b=2"
                for placeholder in email_texts.placeholders_of(key)
            }
            values["instance_name"] = "Energy & Light"
            with self.subTest(key=key):
                subject, body = email_texts.compose(key, "", "", values)
                self.assertNotIn("&amp;", body)
                self.assertNotIn("&amp;", subject)
                self.assertNotIn("&#", body)
                shown += "a=1&b=2" in body
        # The values do reach the texts (a test that finds no value would prove nothing).
        self.assertGreaterEqual(shown, 12)


@tag("mariadb")
class StopLinksOfAConfirmedLoginAddressTests(TestCase):
    """L2: after the login address was confirmed, no stop link of the account works any more, also
    while a request for the second address is still open."""

    def test_all_stop_links_end_while_the_second_request_stays(self):
        user = create_active_user("holder.person@example.org")
        links = open_requests(user)
        token = VerificationToken.objects.find_valid(
            links[Purpose.EMAIL_CHANGE], Purpose.EMAIL_CHANGE
        )

        confirmed = email_change.confirm_change(token)

        self.assertEqual(confirmed.pk, user.pk)
        self.assertFalse(
            VerificationToken.objects.filter(user=user, purpose=Purpose.EMAIL_CHANGE_STOP).exists()
        )
        self.assertTrue(
            PendingEmailChange.objects.filter(user=user, kind=PendingEmailChange.Kind.SECONDARY).exists()
        )
        self.assertTrue(email_change.has_open_request(user))

    def test_an_error_of_the_rules_of_the_account_is_not_a_server_error(self):
        user = create_active_user("holder.person@example.org")
        links = open_requests(user)
        token = VerificationToken.objects.find_valid(
            links[Purpose.EMAIL_CHANGE], Purpose.EMAIL_CHANGE
        )

        with mock.patch.object(User, "change_email", side_effect=ValueError("refused")):
            result = email_change.confirm_change(token)

        self.assertIsNone(result)
        user.refresh_from_db()
        self.assertEqual(user.email, "holder.person@example.org")


@tag("mariadb")
class ResetLinkSessionTests(TestCase):
    """L3: opening the link of the reset of the second factor renews the session key."""

    def test_the_session_key_changes_and_the_data_stays(self):
        user = create_active_user("member@example.org")
        with_second_factor(user)
        boss = administrator()
        client = VerifiedClient()
        client.force_login(boss)
        client.post(reset_url(user), {})
        run_mail_queue()
        path = reset_link_in(mail.outbox[-1])
        owner = Client()
        session = owner.session
        session["kept"] = "yes"
        session.save()
        before = owner.cookies[settings.SESSION_COOKIE_NAME].value

        response = owner.get(path)

        self.assertEqual(response.status_code, 302)
        after = owner.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertNotEqual(before, after)
        self.assertEqual(owner.session["kept"], "yes")


@tag("mariadb")
class CodeIsUsedUpLastTests(TestCase):
    """R2: a recovery code is not lost to a form that fails for another reason."""

    def setUp(self):
        self.user, self.device = create_user_with_totp()
        self.client.force_login(self.user)
        self.code = RECOVERY_CODES[0]

    def test_the_password_card_with_another_mistake_keeps_the_code(self):
        wrong = self.client.post(
            PASSWORD_URL,
            {
                "current_password": PASSWORD,
                "otp_token": self.code,
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD + "x",
            },
        )
        self.assertEqual(wrong.status_code, 200)

        right = self.client.post(
            PASSWORD_URL,
            {
                "current_password": PASSWORD,
                "otp_token": self.code,
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD,
            },
        )

        self.assertEqual(right.status_code, 302)
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(OTHER_PASSWORD))

    def test_the_email_card_that_changes_nothing_keeps_the_code(self):
        unchanged = self.client.post(
            EMAIL_URL,
            {
                "email": self.user.email,
                "secondary_email": "",
                "current_password": PASSWORD,
                "otp_token": self.code,
            },
        )
        self.assertEqual(unchanged.status_code, 200)

        changed = self.client.post(
            EMAIL_URL,
            {
                "email": NEW,
                "secondary_email": "",
                "current_password": PASSWORD,
                "otp_token": self.code,
            },
        )

        self.assertEqual(changed.status_code, 302)
        self.assertTrue(PendingEmailChange.objects.filter(user=self.user).exists())

    def test_a_second_save_with_the_same_app_code_shows_an_error(self):
        from leg_platform.accounts.tests.helpers import code_for

        data = {
            "email": NEW,
            "secondary_email": "",
            "current_password": PASSWORD,
            "otp_token": code_for(self.device),
        }
        first = self.client.post(EMAIL_URL, data)
        self.assertEqual(first.status_code, 302)

        second = self.client.post(EMAIL_URL, data)

        # Not silent: the card is shown again with the message about the code.
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "Der Code ist falsch, abgelaufen oder wurde schon verwendet.")
        self.assertContains(second, "Warten Sie auf den nächsten")

    def test_a_weak_new_password_keeps_the_recovery_code(self):
        # The policy of the password (length, common passwords) runs before the code is checked.
        for weak in ("short", "password1234"):
            with self.subTest(password=weak):
                response = self.client.post(
                    PASSWORD_URL,
                    {
                        "current_password": PASSWORD,
                        "otp_token": self.code,
                        "new_password1": weak,
                        "new_password2": weak,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(User.objects.get(pk=self.user.pk).check_password(PASSWORD))

        right = self.client.post(
            PASSWORD_URL,
            {
                "current_password": PASSWORD,
                "otp_token": self.code,
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD,
            },
        )
        self.assertEqual(right.status_code, 302)

    def test_a_password_in_the_code_field_never_reaches_the_check(self):
        # A password typed by habit has no shape of a code: refused before the check, so it is not
        # counted and a recovery code stays valid (D-062).
        response = self.client.post(
            PASSWORD_URL,
            {
                "otp_token": PASSWORD,
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Das ist kein Code.")
        right = self.client.post(
            PASSWORD_URL,
            {
                "otp_token": self.code,
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD,
            },
        )
        self.assertEqual(right.status_code, 302)


@tag("mariadb")
class ErrorPageWithAFailingSessionTests(TestCase):
    """R3: the 403 and 404 pages are rendered with the request; when the session cannot be read,
    the answer is the 500 page, without any detail."""

    def setUp(self):
        self.user = create_active_user("holder.person@example.org")

    def failing_session(self):
        store = import_module(settings.SESSION_ENGINE).SessionStore
        return mock.patch.object(store, "load", side_effect=OperationalError("db-detail-xyz"))

    def check(self, response):
        self.assertEqual(response.status_code, 500)
        text = response.content.decode()
        self.assertIn("Hoppla, da ist etwas schiefgelaufen.", text)
        for detail in ("db-detail-xyz", "OperationalError", "Traceback"):
            self.assertNotIn(detail, text)

    def test_the_not_found_page_with_a_session_cookie(self):
        client = Client(raise_request_exception=False)
        client.force_login(self.user)

        with self.failing_session():
            self.check(client.get("/there-is-nothing-here/"))

    def test_the_csrf_page_with_a_session_cookie(self):
        client = Client(enforce_csrf_checks=True, raise_request_exception=False)
        client.force_login(self.user)

        with self.failing_session():
            self.check(client.post(reverse("accounts:login"), {}))

    def test_the_pages_work_with_a_session_cookie_when_the_database_is_fine(self):
        client = Client()
        client.force_login(self.user)

        response = client.get("/there-is-nothing-here/")

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Diese Seite gibt es nicht mehr.", status_code=404)
