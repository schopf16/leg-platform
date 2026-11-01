"""Password reset (docs/SECURITY.md, sections 3 and 11; OWASP Forgot Password Cheat Sheet)."""

import re
import threading
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

from axes.models import AccessAttempt
from django.conf import settings
from django.core import mail
from django.db import connection
from django.test import (
    Client,
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
    tag,
)
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import mail_addresses
from leg_platform.accounts import mail_queue, password_reset, rate_limits
from leg_platform.accounts.forms import NewPasswordForm
from leg_platform.accounts.models import LoginIpAttempt, MailJob, User, VerificationToken
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    create_active_user,
    hour_and_day_limits,
    registration_data,
    reset_request_data,
    run_mail_queue,
)

REQUEST_URL = reverse("accounts:password_reset")
SET_URL = reverse("accounts:password_reset_set")
LOGIN_URL = reverse("accounts:login")
NEW_PASSWORD = "silver lantern rests under quiet rain"
INVALID_PAGE = "accounts/password_reset_invalid.html"
INVALID_URL = reverse("accounts:password_reset_invalid")


def request_reset(address, client=None, remote_addr="10.40.0.1"):
    response = (client or Client()).post(
        REQUEST_URL, reset_request_data(address), REMOTE_ADDR=remote_addr
    )
    run_mail_queue()
    return response


def reset_path(message):
    match = re.search(r"^(https?://\S+/password-reset/verify/\S+)$", message.body, re.MULTILINE)
    assert match, "The email contains no reset link."
    link = match.group(1)
    assert link.startswith(settings.INSTANCE_BASE_URL + "/")
    return urlsplit(link).path


def login(client, address, password, remote_addr):
    return client.post(
        LOGIN_URL, {"username": address, "password": password}, REMOTE_ADDR=remote_addr
    )


@tag("mariadb")
@hour_and_day_limits
class ResetRequestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("active.person@example.org")

    def test_an_active_account_receives_a_link_built_from_the_configuration(self):
        response = request_reset(" Active.Person@EXAMPLE.org ")

        self.assertRedirects(response, reverse("accounts:password_reset_sent"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["active.person@example.org"])
        reset_path(mail.outbox[0])
        self.assertEqual(mail.outbox[0].extra_headers["Auto-Submitted"], "auto-generated")
        self.assertEqual(mail.outbox[0].from_email, mail_addresses.sender())

    def test_no_email_is_sent_to_other_addresses_and_the_response_is_the_same(self):
        blocked = create_active_user("blocked.person@example.org")
        blocked.set_status(User.Status.BLOCKED)
        deactivated = create_active_user("former.person@example.org")
        deactivated.set_status(User.Status.DEACTIVATED)
        User.objects.create_user("pending.person@example.org")

        def comparable(response):
            # "Date" and "Expires" carry the current time and change with the clock.
            headers = {
                k: v for k, v in response.headers.items() if k.lower() not in {"date", "expires"}
            }
            return (response.status_code, response.content, sorted(response.cookies), headers)

        reference = comparable(request_reset("active.person@example.org", remote_addr="10.40.1.1"))
        mail.outbox.clear()
        for number, address in enumerate(
            (
                "unknown.person@example.org",
                "blocked.person@example.org",
                "former.person@example.org",
                "pending.person@example.org",
            ),
            start=2,
        ):
            with self.subTest(address=address):
                response = request_reset(address, remote_addr=f"10.40.1.{number}")
                self.assertEqual(comparable(response), reference)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(VerificationToken.objects.filter(purpose="password_reset").count(), 1)

    def test_a_temporarily_locked_account_can_still_request_a_reset(self):
        for attempt in range(5):
            login(Client(), self.user.email, "wrong password here", f"10.40.2.{attempt}")
        self.assertEqual(login(Client(), self.user.email, PASSWORD, "10.40.2.99").status_code, 429)

        request_reset(self.user.email)

        self.assertEqual(len(mail.outbox), 1)

    def test_a_request_does_not_lock_the_account(self):
        for _ in range(3):
            request_reset(self.user.email)

        response = login(Client(), self.user.email, PASSWORD, "10.40.3.1")

        self.assertEqual(response.status_code, 302)

    def test_an_invalid_address_is_rejected_without_a_job(self):
        response = Client().post(REQUEST_URL, {"email": "not-an-email"})

        self.assertEqual(response.status_code, 200)
        run_mail_queue()
        self.assertEqual(len(mail.outbox), 0)

    def test_the_request_needs_a_csrf_token(self):
        response = Client(enforce_csrf_checks=True).post(
            REQUEST_URL, reset_request_data(self.user.email)
        )

        self.assertEqual(response.status_code, 403)

    def test_the_request_is_limited_per_address(self):
        limit = rate_limits.MAIL_ADDRESS_LIMITS[0][0]
        for _ in range(limit + 2):
            request_reset(self.user.email)

        self.assertEqual(len(mail.outbox), limit)

    def test_the_login_page_links_to_the_reset(self):
        help_url = reverse("accounts:login_help")
        self.assertContains(Client().get(LOGIN_URL), f'href="{help_url}"')
        self.assertContains(Client().get(help_url), f'href="{REQUEST_URL}"')


@tag("mariadb")
class ResetFlowTests(TestCase):
    def setUp(self):
        self.user = create_active_user("active.person@example.org")
        request_reset(self.user.email)
        self.path = reset_path(mail.outbox[0])
        self.client = Client()

    def open_link(self, client=None):
        return (client or self.client).get(self.path)

    def set_password(self, password=NEW_PASSWORD, **overrides):
        data = {"new_password1": password, "new_password2": password, **overrides}
        return self.client.post(SET_URL, data)

    def test_opening_the_link_changes_nothing_and_leaves_the_url(self):
        first = self.open_link()
        second = self.open_link()

        self.assertRedirects(first, SET_URL, fetch_redirect_response=False)
        self.assertEqual(second.status_code, 302)
        self.assertNotIn("/password-reset/verify/", first["Location"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertEqual(VerificationToken.objects.filter(purpose="password_reset").count(), 1)

    def test_the_new_password_is_saved_and_the_person_is_not_logged_in(self):
        self.open_link()

        response = self.set_password()

        self.assertRedirects(response, LOGIN_URL)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertFalse(self.user.check_password(PASSWORD))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(VerificationToken.objects.count(), 0)

    def test_the_owner_is_notified_without_a_link(self):
        self.open_link()
        mail.outbox.clear()

        self.set_password()
        self.assertEqual(len(mail.outbox), 0)  # queued, not sent in the request
        run_mail_queue()

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.user.email])
        self.assertNotIn("http", message.body)
        self.assertNotIn(NEW_PASSWORD, message.body)
        # Automatic emails do not invite a reply.
        self.assertNotIn("antworten Sie auf diese E-Mail", message.body)
        self.assertIn("wenden Sie sich sofort an die Betreiber", message.body)

    @override_settings(INSTANCE_WEBSITE_URL="https://www.example.org/")
    def test_the_notification_names_the_contact_information_of_the_instance(self):
        self.open_link()
        mail.outbox.clear()

        self.set_password()
        run_mail_queue()

        self.assertIn("https://www.example.org/", mail.outbox[0].body)

    def test_a_failing_notification_does_not_undo_the_change_and_is_repeated(self):
        self.open_link()
        mail.outbox.clear()

        with patch("leg_platform.accounts.emails.EmailMessage.send", side_effect=OSError("x")):
            response = self.set_password()
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR") as logs:
                mail_queue.process_pending()

        self.assertRedirects(response, LOGIN_URL)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertNotIn(self.user.email, "\n".join(logs.output))
        MailJob.objects.update(next_attempt_at=timezone.now())
        mail_queue.process_pending()
        self.assertEqual(len(mail.outbox), 1)

    def test_all_other_sessions_of_the_account_end(self):
        other = Client()
        login(other, self.user.email, PASSWORD, "10.41.0.1")
        self.assertEqual(other.get(reverse("accounts:profile")).status_code, 200)
        self.open_link()

        self.set_password()

        self.assertRedirects(
            other.get(reverse("accounts:profile")), f"{LOGIN_URL}?next=/profile/"
        )

    def test_the_link_can_be_used_only_once(self):
        self.open_link()
        self.set_password()

        self.assertRedirects(self.open_link(), INVALID_URL, fetch_redirect_response=False)
        self.assertRedirects(self.client.get(SET_URL), INVALID_URL, fetch_redirect_response=False)

    def test_a_new_request_makes_the_earlier_link_invalid_and_the_new_link_works(self):
        request_reset(self.user.email)
        second = reset_path(mail.outbox[-1])
        self.assertNotEqual(second, self.path)

        self.assertRedirects(Client().get(self.path), INVALID_URL, fetch_redirect_response=False)
        self.assertEqual(VerificationToken.objects.filter(purpose="password_reset").count(), 1)
        self.assertRedirects(self.client.get(second), SET_URL, fetch_redirect_response=False)
        self.assertRedirects(self.set_password(), LOGIN_URL)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))

    def test_a_session_that_opened_the_earlier_link_cannot_set_the_password(self):
        self.open_link()
        request_reset(self.user.email)

        response = self.set_password()

        self.assertRedirects(response, INVALID_URL, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_the_last_link_stays_valid_when_the_address_limit_is_reached(self):
        limit = rate_limits.MAIL_ADDRESS_LIMITS[0][0]
        # setUp sent the first email of the budget.
        for _ in range(limit - 1):
            request_reset(self.user.email)
        last = reset_path(mail.outbox[-1])
        sent = len(mail.outbox)

        request_reset(self.user.email)

        self.assertEqual(len(mail.outbox), sent)
        self.assertRedirects(Client().get(last), SET_URL, fetch_redirect_response=False)

    def test_every_outstanding_reset_link_of_the_account_is_removed(self):
        # A link that was never consumed (e.g. created before earlier links were replaced).
        stale = VerificationToken.objects.issue(
            self.user, VerificationToken.Purpose.PASSWORD_RESET, timedelta(hours=1)
        )
        self.open_link()

        self.set_password()

        self.assertRedirects(
            Client().get(reverse("accounts:password_reset_verify", args=(stale,))),
            INVALID_URL,
            fetch_redirect_response=False,
        )
        self.assertEqual(VerificationToken.objects.count(), 0)

    def test_an_expired_link_is_invalid(self):
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        self.assertRedirects(self.open_link(), INVALID_URL, fetch_redirect_response=False)
        self.assertTemplateUsed(self.client.get(INVALID_URL), INVALID_PAGE)

    def test_a_link_that_expires_before_the_form_is_sent_changes_nothing(self):
        self.open_link()
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        response = self.set_password()

        self.assertRedirects(response, INVALID_URL, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_the_form_is_not_available_without_the_link_in_the_same_session(self):
        for client in (Client(), self.client):
            self.assertRedirects(client.get(SET_URL), INVALID_URL, fetch_redirect_response=False)
        response = Client().post(
            SET_URL, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
        )
        self.assertRedirects(response, INVALID_URL, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_an_account_that_was_blocked_after_the_link_cannot_reset(self):
        self.open_link()
        self.user.set_status(User.Status.BLOCKED)

        response = self.set_password()

        self.assertRedirects(response, INVALID_URL, fetch_redirect_response=False)
        self.assertRedirects(self.open_link(), INVALID_URL, fetch_redirect_response=False)

    def test_the_password_policy_applies(self):
        weak = {
            "too short": "orchard",
            "numeric": "1234567890123456",
            "common": "1q2w3e4r5t6y7u8i9o0p",
            "similar to the address": "active.person@example.org",
        }
        for name, password in weak.items():
            with self.subTest(name):
                self.open_link()
                response = self.set_password(password)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors.get("new_password2"))
                self.user.refresh_from_db()
                self.assertTrue(self.user.check_password(PASSWORD))
        self.assertEqual(VerificationToken.objects.count(), 1)

    def test_different_passwords_are_rejected(self):
        self.open_link()

        response = self.set_password(new_password2=OTHER_PASSWORD)

        self.assertTrue(response.context["form"].errors.get("new_password2"))

    def test_a_long_passphrase_is_accepted(self):
        self.open_link()
        passphrase = "correct horse battery staple " * 4

        self.set_password(passphrase)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(passphrase))

    def test_a_reset_link_does_not_open_a_registration_and_the_registration_email_has_no_link(self):
        client = Client()
        client.post(reverse("accounts:registration"), registration_data("new.person@example.org"))
        run_mail_queue()

        self.assertRedirects(
            client.get(self.path.replace("password-reset", "register")),
            reverse("accounts:registration_invalid"),
            fetch_redirect_response=False,
        )
        # The registration email carries a code, no link that could be tried here (D-136).
        self.assertNotIn("/password-reset/", mail.outbox[-1].body)
        self.assertNotIn("/register/verify/", mail.outbox[-1].body)

    def test_the_token_pages_send_no_referrer_and_are_not_cached(self):
        # Only the pages whose URL contains the token send no referrer.
        pages = [
            self.open_link(),
            Client().get(reverse("accounts:password_reset_verify", args=["x"])),
        ]
        for response in pages:
            self.assertEqual(response["Referrer-Policy"], "no-referrer")
            self.assertIn("no-store", response["Cache-Control"])

    def test_the_pages_with_a_form_use_the_project_policy_and_are_not_cached(self):
        # The form of these pages is submitted with an Origin header only if the policy is not
        # "no-referrer"; the URL of these pages contains no token (D-054).
        self.open_link()
        pages = [
            self.client.get(SET_URL),
            self.set_password("short"),
            Client().get(SET_URL),
        ]
        for response in pages:
            self.assertEqual(response["Referrer-Policy"], "same-origin")
            self.assertIn("no-store", response["Cache-Control"])

    def test_the_password_fields_are_masked_in_error_reports(self):
        self.open_link()

        response = self.set_password()

        self.assertEqual(
            response.wsgi_request.sensitive_post_parameters,
            ("new_password1", "new_password2"),
        )

    def test_the_token_is_stored_only_as_a_keyed_digest(self):
        raw = self.path.rstrip("/").rsplit("/", 1)[-1]
        stored = VerificationToken.objects.get()

        self.assertNotEqual(stored.digest, raw)
        self.assertFalse(VerificationToken.objects.filter(digest=raw).exists())

    def test_the_reset_link_lifetime_matches_the_longest_request_window(self):
        token = VerificationToken.objects.get()

        remaining = token.expires_at - timezone.now()

        # A person whose budget someone else used up can still use a link already received.
        self.assertLess(remaining, timedelta(hours=24))
        self.assertGreater(remaining, timedelta(hours=23, minutes=59))
        self.assertEqual(password_reset.PASSWORD_RESET_LIFETIME, rate_limits.LONGEST_WINDOW)

    def test_the_reset_does_not_clear_other_accounts_when_the_digest_is_missing(self):
        other = create_active_user("other.person@example.org")
        for attempt in range(2):
            login(Client(), other.email, "wrong password here", f"10.44.0.{attempt}")
        self.assertEqual(AccessAttempt.objects.count(), 1)
        self.open_link()

        with patch.object(password_reset, "axes_username", return_value=None):
            self.set_password()

        self.assertEqual(AccessAttempt.objects.count(), 1)

    def test_expired_reset_links_are_deleted_by_the_cleanup(self):
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        self.assertEqual(password_reset.delete_expired_reset_tokens(), 1)
        self.assertEqual(VerificationToken.objects.count(), 0)

    def test_expired_email_change_links_and_their_target_address_are_deleted_by_the_cleanup(self):
        user = VerificationToken.objects.get().user
        VerificationToken.objects.issue(
            user,
            VerificationToken.Purpose.EMAIL_CHANGE,
            timedelta(hours=24),
            target_email="proposed.address@example.org",
        )
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        self.assertEqual(password_reset.delete_expired_reset_tokens(), 2)
        self.assertFalse(
            VerificationToken.objects.filter(target_email="proposed.address@example.org").exists()
        )

    def test_unexpired_email_change_links_are_kept_by_the_cleanup(self):
        user = VerificationToken.objects.get().user
        VerificationToken.objects.issue(
            user,
            VerificationToken.Purpose.EMAIL_CHANGE,
            timedelta(hours=24),
            target_email="proposed.address@example.org",
        )

        self.assertEqual(password_reset.delete_expired_reset_tokens(), 0)
        self.assertTrue(
            VerificationToken.objects.filter(
                purpose=VerificationToken.Purpose.EMAIL_CHANGE
            ).exists()
        )

    def test_the_pages_are_in_swiss_german_without_sharp_s(self):
        pages = [
            Client().get(REQUEST_URL),
            Client().get(reverse("accounts:password_reset_sent")),
            Client().get(reverse("accounts:password_reset_verify", args=["x"])),
            self.client.get(self.path, follow=True),
            self.set_password("short"),
        ]
        for response in pages:
            html = response.content.decode()
            self.assertNotIn("\u00df", html)
        self.assertContains(pages[0], "Passwort zurücksetzen")
        self.assertContains(pages[4], "Passwort festlegen")
        for message in mail.outbox:
            self.assertNotIn("\u00df", message.subject + message.body)
            self.assertIn("Guten Tag", message.body)


@tag("mariadb")
class ResetLockoutTests(TestCase):
    def test_a_reset_ends_only_the_lockout_of_the_account_not_the_ip_counters(self):
        user = create_active_user("active.person@example.org")
        for attempt in range(5):
            login(Client(), user.email, "wrong password here", f"10.42.0.{attempt}")
        self.assertEqual(AccessAttempt.objects.count(), 1)
        self.assertEqual(login(Client(), user.email, PASSWORD, "10.42.0.99").status_code, 429)
        ip_attempts = LoginIpAttempt.objects.count()
        self.assertGreaterEqual(ip_attempts, 5)
        request_reset(user.email, remote_addr="10.42.0.98")
        client = Client()
        client.get(reset_path(mail.outbox[0]))

        client.post(SET_URL, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})

        self.assertEqual(AccessAttempt.objects.count(), 0)
        self.assertEqual(LoginIpAttempt.objects.count(), ip_attempts)
        response = login(Client(), user.email, NEW_PASSWORD, "10.42.0.99")
        self.assertEqual(response.status_code, 302)

    def test_a_locked_ip_stays_locked_after_the_attacker_resets_the_password_of_their_own_account(self):
        attacker = create_active_user("attacker.person@example.org")
        for attempt in range(5):
            login(Client(), f"victim{attempt}@example.org", "wrong password", "10.43.0.1")
        self.assertEqual(login(Client(), attacker.email, PASSWORD, "10.43.0.1").status_code, 429)
        request_reset(attacker.email, remote_addr="10.43.0.2")
        client = Client()
        client.get(reset_path(mail.outbox[0]))
        client.post(SET_URL, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})

        response = login(Client(), attacker.email, NEW_PASSWORD, "10.43.0.1")

        self.assertEqual(response.status_code, 429)


@tag("mariadb")
class ResetRequestLockTests(TestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_the_account_row_is_locked_while_the_link_is_replaced(self):
        create_active_user("locked.request@example.org")

        with CaptureQueriesContext(connection) as context:
            self.assertIsNotNone(
                password_reset.process_reset_request("locked.request@example.org")
            )

        self.assertTrue(
            any(
                "FOR UPDATE" in query["sql"] and User._meta.db_table in query["sql"]
                for query in context.captured_queries
            )
        )

    def test_a_link_of_another_purpose_never_sets_the_password(self):
        user = create_active_user("other.purpose@example.org")
        for purpose in (
            VerificationToken.Purpose.REGISTRATION,
            VerificationToken.Purpose.TWO_FACTOR_RESET,
        ):
            with self.subTest(purpose=purpose):
                raw = VerificationToken.objects.issue(
                    user, purpose, password_reset.PASSWORD_RESET_LIFETIME
                )
                token = VerificationToken.objects.find_valid(raw, purpose)
                form = NewPasswordForm(
                    user=user,
                    data={"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
                )
                self.assertTrue(form.is_valid())

                self.assertIsNone(password_reset.complete_reset(token, form))

                user.refresh_from_db()
                self.assertTrue(user.check_password(PASSWORD))
                self.assertTrue(VerificationToken.objects.filter(pk=token.pk).exists())


@tag("mariadb")
class ResetRequestConcurrencyTests(TransactionTestCase):
    def test_parallel_requests_leave_exactly_one_valid_link(self):
        user = create_active_user("parallel.request@example.org")
        count = 4
        barrier = threading.Barrier(count)
        results = []

        def work():
            try:
                barrier.wait()
                results.append(
                    password_reset.process_reset_request("parallel.request@example.org")
                )
            finally:
                connection.close()

        threads = [threading.Thread(target=work) for _number in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), count)
        self.assertTrue(all(result is not None for result in results))
        self.assertEqual(
            VerificationToken.objects.filter(
                user=user, purpose=VerificationToken.Purpose.PASSWORD_RESET
            ).count(),
            1,
        )
