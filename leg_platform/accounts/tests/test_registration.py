"""Registration and email verification (docs/PROJECT.md, "Registration"; docs/SECURITY.md,
section 11). Emails are never sent: Django's test runner replaces the backend with the
in-memory backend, so the messages are read from ``mail.outbox``.
"""

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib.messages import get_messages
from django.db import IntegrityError, OperationalError, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import mail_queue, registration
from leg_platform.accounts.models import MailJob, User, VerificationToken
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    completion_data,
    create_active_user,
    registration_data,
    verification_path,
)
from leg_platform.accounts.views import REGISTRATION_SESSION_KEY, RegistrationCompleteView

INVALID_PAGE = "accounts/registration_invalid.html"


def retry_now():
    """Make failed jobs due and let the worker try them again."""
    MailJob.objects.update(next_attempt_at=timezone.now())
    mail_queue.process_pending()


def register(client, email="new.person@example.org", **overrides):
    """Submit the registration form and let the background worker process the request."""
    response = client.post(reverse("accounts:registration"), registration_data(email, **overrides))
    mail_queue.process_pending()
    return response


@tag("mariadb")
class RegistrationTests(TestCase):
    def test_registration_creates_an_unverified_account_and_sends_one_link(self):
        response = register(self.client)

        self.assertRedirects(response, reverse("accounts:registration_sent"))
        user = User.objects.get(normalized_email="new.person@example.org")
        self.assertEqual(user.status, User.Status.UNVERIFIED)
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertIsNone(user.customer_number_id)
        # Only the address is stored until it is verified (project owner, 2026-10-13).
        self.assertEqual((user.first_name, user.last_name, user.street, user.town), ("", "", "", ""))
        self.assertIsNone(user.swiss_postal_code)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["new.person@example.org"])
        verification_path(mail.outbox[0])

    def test_emails_go_to_the_canonical_address_the_account_stores(self):
        # Django encodes a domain with the IDNA 2003 codec ("ß" becomes "ss"), the account is
        # keyed by the UTS #46 form: the mail must not follow the entered spelling.
        for entered in ("Max.Muster@EXAMPLE.org ", "max@fa\u00df.example.org"):
            with self.subTest(entered=entered):
                mail.outbox.clear()
                register(Client(), entered)

                user = User.objects.latest("pk")
                self.assertEqual(mail.outbox[0].to, [user.email])
                self.assertTrue(mail.outbox[0].to[0].isascii())
                self.assertEqual(mail.outbox[0].to[0], mail.outbox[0].to[0].lower())

    def test_the_information_email_goes_to_the_stored_address_too(self):
        create_active_user("taken.person@example.org")

        register(self.client, " Taken.Person@EXAMPLE.org")

        self.assertEqual(mail.outbox[0].to, ["taken.person@example.org"])

    def test_the_link_is_built_from_the_configuration_not_from_the_request(self):
        with override_settings(INSTANCE_BASE_URL="https://portal.example.org"):
            register(self.client)

        self.assertEqual(
            mail.outbox[0].body.count("https://portal.example.org/register/verify/"), 1
        )
        self.assertNotIn("testserver", mail.outbox[0].body)

    def test_the_response_is_the_same_for_every_state_of_the_address(self):
        blocked = create_active_user("blocked.person@example.org")
        blocked.set_status(User.Status.BLOCKED)
        deactivated = create_active_user("former.person@example.org")
        deactivated.set_status(User.Status.DEACTIVATED)
        create_active_user("taken.person@example.org")
        register(Client(), "pending.person@example.org")
        locked = create_active_user("locked.person@example.org")
        for attempt in range(5):
            Client().post(
                reverse("accounts:login"),
                {"username": locked.email, "password": "wrong password here"},
                REMOTE_ADDR=f"10.8.0.{attempt}",
            )

        def comparable(response):
            # "Date" and "Expires" carry the current time and change with the clock.
            headers = {
                k: v for k, v in response.headers.items() if k.lower() not in {"date", "expires"}
            }
            return (response.status_code, response.content, sorted(response.cookies), headers)

        reference = comparable(register(Client(), "free.person@example.org"))
        for address in (
            "taken.person@example.org",
            "blocked.person@example.org",
            "locked.person@example.org",
            "former.person@example.org",
            "pending.person@example.org",
        ):
            with self.subTest(address=address):
                self.assertEqual(comparable(register(Client(), address)), reference)

    def test_the_confirmation_page_does_not_claim_that_a_link_was_sent(self):
        response = self.client.get(reverse("accounts:registration_sent"))

        # Conditional and the same for every address (D-054): it says what to expect if the
        # address can be used, never that an account or a message exists.
        self.assertContains(response, "Wenn wir die angegebene Adresse verwenden können")
        self.assertNotContains(response, "Wir haben eine Nachricht an Ihre Adresse")

    def test_emails_are_marked_as_automatic_messages(self):
        create_active_user("taken.person@example.org")

        register(self.client)
        register(self.client, "taken.person@example.org")

        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertEqual(message.extra_headers["Auto-Submitted"], "auto-generated")

    def test_a_failing_information_email_keeps_the_generic_response(self):
        create_active_user("taken.person@example.org")

        with patch("leg_platform.accounts.emails.EmailMessage.send", side_effect=OSError("x")):
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR") as logs:
                response = register(self.client, "taken.person@example.org")

        self.assertRedirects(response, reverse("accounts:registration_sent"))
        self.assertNotIn("taken.person", "\n".join(logs.output))

    def test_the_response_is_the_same_for_new_and_existing_addresses(self):
        create_active_user("taken.person@example.org")

        def comparable(response):
            return (
                response.status_code,
                response["Location"],
                response.content,
                sorted(response.cookies),
                sorted(name for name in response.headers if name.lower() not in {"date"}),
            )

        new = register(Client(), "free.person@example.org")
        existing = register(Client(), "taken.person@example.org")

        self.assertEqual(comparable(new), comparable(existing))
        self.assertEqual(User.objects.filter(normalized_email="taken.person@example.org").count(), 1)

    def test_an_address_of_an_active_account_gets_an_information_email_without_a_link(self):
        create_active_user("taken.person@example.org")

        register(self.client, "taken.person@example.org")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["taken.person@example.org"])
        self.assertNotIn("/register/verify/", mail.outbox[0].body)
        self.assertEqual(VerificationToken.objects.count(), 0)

    def test_an_address_of_a_blocked_account_creates_no_account(self):
        user = create_active_user("blocked.person@example.org")
        user.set_status(User.Status.BLOCKED)

        register(self.client, "Blocked.Person@example.org")

        self.assertEqual(User.objects.count(), 1)
        self.assertNotIn("/register/verify/", mail.outbox[0].body)

    def test_an_address_of_a_temporarily_locked_account_creates_no_account(self):
        user = create_active_user("locked.person@example.org")
        for attempt in range(5):
            Client().post(
                reverse("accounts:login"),
                {"username": user.email, "password": "wrong password here"},
                REMOTE_ADDR=f"10.9.0.{attempt}",
            )
        locked = Client().post(
            reverse("accounts:login"),
            {"username": user.email, "password": PASSWORD},
            REMOTE_ADDR="10.9.0.99",
        )
        self.assertEqual(locked.status_code, 429)

        register(self.client, "locked.person@example.org")

        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(VerificationToken.objects.count(), 0)
        self.assertNotIn("/register/verify/", mail.outbox[0].body)

    def test_an_address_of_a_deactivated_account_can_register_again(self):
        user = create_active_user("former.person@example.org")
        user.set_status(User.Status.DEACTIVATED)

        register(self.client, "former.person@example.org")

        statuses = sorted(User.objects.values_list("status", flat=True))
        self.assertEqual(statuses, ["deactivated", "unverified"])
        verification_path(mail.outbox[0])

    def test_a_new_registration_replaces_the_pending_one(self):
        register(self.client)
        first = User.objects.get()
        register(self.client, "NEW.person@example.org ")

        users = User.objects.all()
        self.assertEqual(users.count(), 1)
        self.assertNotEqual(users[0].pk, first.pk)
        self.assertEqual(VerificationToken.objects.count(), 1)
        first_link, second_link = (verification_path(message) for message in mail.outbox)
        self.assertTemplateUsed(self.client.get(first_link), INVALID_PAGE)
        self.assertNotIn(REGISTRATION_SESSION_KEY, self.client.session)
        self.assertRedirects(
            self.client.get(second_link),
            reverse("accounts:registration_complete"),
            fetch_redirect_response=False,
        )

    def test_submitted_privileges_and_controlled_fields_are_ignored(self):
        register(
            self.client,
            is_staff="on",
            is_superuser="on",
            groups="1",
            user_permissions="1",
            status="active",
            is_active="on",
            customer_number="123456",
            retention_obligation="on",
            normalized_email="other.person@example.org",
            password="copper kettle hums beside quiet orchard",
        )

        user = User.objects.get()
        self.assertEqual(user.status, User.Status.UNVERIFIED)
        self.assertEqual(user.normalized_email, "new.person@example.org")
        self.assertFalse(user.has_usable_password())
        self.assertIsNone(user.customer_number_id)
        self.assertFalse(user.retention_obligation)
        self.assertEqual((user.is_staff, user.is_superuser, user.is_active), (False, False, False))
        self.assertEqual(user.groups.count() + user.user_permissions.count(), 0)

    def test_an_invalid_address_creates_nothing_and_sends_nothing(self):
        for value in ("", "not-an-email", "a@b", "x" * 300 + "@example.org", "a b@example.org"):
            with self.subTest(value=value[:20]):
                response = register(self.client, value)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(User.objects.count(), 0)
                self.assertEqual(len(mail.outbox), 0)

    def test_profile_fields_sent_with_the_registration_are_ignored(self):
        register(self.client, first_name="Anna", last_name="Example", town="Bern", street="Weg")

        user = User.objects.get()
        self.assertEqual((user.first_name, user.last_name, user.town, user.street), ("", "", "", ""))

    def test_the_post_needs_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(reverse("accounts:registration"), registration_data())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(User.objects.count(), 0)

    def test_a_failing_email_keeps_the_generic_response_and_logs_no_address_or_link(self):
        with patch(
            "leg_platform.accounts.emails.EmailMessage.send", side_effect=OSError("secret-detail")
        ):
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR") as logs:
                response = register(self.client)

        self.assertRedirects(response, reverse("accounts:registration_sent"))
        output = "\n".join(logs.output)
        self.assertIn("OSError", output)
        for text in ("new.person@example.org", "secret-detail", "/register/verify/"):
            self.assertNotIn(text, output)
        # The pending account of the failed attempt is left; the repeated job replaces it.
        self.assertEqual(MailJob.objects.get().attempts, 1)
        self.assertEqual(User.objects.get().status, User.Status.UNVERIFIED)

    def test_a_concurrent_registration_is_repeated_and_finds_the_winner(self):
        create_active_user("taken.person@example.org")
        real = registration._register_once
        calls = []

        def conflict_once(*args):
            calls.append(args)
            if len(calls) == 1:
                raise IntegrityError("duplicate")
            return real(*args)

        with patch.object(registration, "_register_once", side_effect=conflict_once):
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR"):
                response = register(self.client, "taken.person@example.org")
            self.assertEqual(len(mail.outbox), 0)
            retry_now()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("/register/verify/", mail.outbox[0].body)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_a_deadlock_is_repeated_like_a_conflict(self):
        real = registration._register_once
        calls = []

        def deadlock_once(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OperationalError(1213, "Deadlock found")
            return real(*args)

        with patch.object(registration, "_register_once", side_effect=deadlock_once):
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR"):
                register(self.client)
            retry_now()

        self.assertEqual(len(calls), 2)
        verification_path(mail.outbox[0])

    def test_a_job_that_keeps_failing_is_given_up_after_three_attempts(self):
        with patch.object(registration, "_register_once", side_effect=IntegrityError("duplicate")):
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR"):
                response = register(self.client)
                for _attempt in range(mail_queue.MAX_ATTEMPTS):
                    retry_now()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(MailJob.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)


@tag("mariadb")
class VerificationTests(TestCase):
    # Completing the registration reads the address directory (advisory check, D-033).
    databases = {"default", "addresses", "address_import"}
    def setUp(self):
        register(self.client)
        self.path = verification_path(mail.outbox[0])
        self.user = User.objects.get()
        self.token = VerificationToken.objects.get()
        self.complete_url = reverse("accounts:registration_complete")

    def open_link(self, client=None):
        return (client or self.client).get(self.path)

    def complete(self, password=PASSWORD, **overrides):
        data = completion_data(password, registration=self.token.pk, **overrides)
        return self.client.post(self.complete_url, data)

    def test_opening_the_link_changes_nothing_and_leaves_the_url(self):
        first = self.open_link()
        second = self.open_link()

        self.assertRedirects(first, self.complete_url, fetch_redirect_response=False)
        self.assertEqual(second.status_code, 302)
        self.assertNotIn("/register/verify/", first["Location"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.UNVERIFIED)
        self.assertEqual(VerificationToken.objects.count(), 1)

    def test_the_form_shows_the_verified_address_without_the_token(self):
        self.open_link()

        response = self.client.get(self.complete_url)

        self.assertContains(response, "new.person@example.org")
        self.assertNotContains(response, self.path.rsplit("/", 2)[-2])
        self.assertContains(response, "data-address-directory")
        self.assertContains(response, "/addresses/lookup/")
        self.assertContains(response, "js/address-directory.js")

    def test_completing_activates_the_account_and_issues_the_customer_number(self):
        self.open_link()

        response = self.complete()

        self.assertRedirects(response, reverse("accounts:welcome"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.ACTIVE)
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertRegex(self.user.customer_number_id, r"\A[0-9]{6}\Z")
        self.assertEqual(VerificationToken.objects.count(), 0)
        self.assertNotIn(REGISTRATION_SESSION_KEY, self.client.session)

    def test_address_matching_warning_does_not_block_registration(self):
        self.open_link()
        with patch(
            "leg_platform.accounts.views.check_address",
            return_value={"warning": "Address could not be confirmed."},
        ) as check:
            response = self.complete()

        self.assertRedirects(response, reverse("accounts:welcome"))
        check.assert_called_once_with(
            street="Examplestrasse",
            house_number="12a",
            postal_code="3000",
            locality="Bern",
            country="CH",
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.ACTIVE)
        self.assertTrue(
            any(
                "Address could not be confirmed." in str(message)
                for message in get_messages(response.wsgi_request)
            )
        )

    def test_the_link_can_be_used_only_once(self):
        self.open_link()
        self.complete()

        again = self.open_link()
        direct = self.client.get(self.complete_url)

        for response in (again, direct):
            self.assertTemplateUsed(response, INVALID_PAGE)

    def test_the_form_is_not_available_without_the_link_in_the_same_session(self):
        for client in (Client(), self.client):
            self.assertTemplateUsed(client.get(self.complete_url), INVALID_PAGE)
        response = Client().post(
            self.complete_url, completion_data(registration=self.token.pk)
        )
        self.assertTemplateUsed(response, INVALID_PAGE)
        self.assertFalse(User.objects.get().has_usable_password())

    def test_a_form_rendered_for_another_registration_is_not_saved(self):
        # The victim opens link A, then the attacker's link B in the same browser replaces the
        # token of the session; the victim's form still carries the reference of A.
        other = Client()
        register(other, "attacker.person@example.org")
        attacker_path = verification_path(mail.outbox[-1])
        self.open_link()
        victim_form = self.client.get(self.complete_url)
        self.assertContains(victim_form, f'name="registration" value="{self.token.pk}"')

        self.client.get(attacker_path)
        response = self.complete(first_name="Victim")

        self.assertTemplateUsed(response, INVALID_PAGE)
        attacker = User.objects.get(normalized_email="attacker.person@example.org")
        self.assertEqual(attacker.status, User.Status.UNVERIFIED)
        self.assertEqual(attacker.first_name, "")
        self.assertFalse(attacker.has_usable_password())

    def test_a_form_without_the_registration_reference_is_refused(self):
        self.open_link()

        response = self.client.post(self.complete_url, completion_data())

        self.assertTemplateUsed(response, INVALID_PAGE)
        self.assertFalse(User.objects.get().has_usable_password())

    def test_an_expired_link_is_invalid(self):
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        response = self.open_link()

        self.assertTemplateUsed(response, INVALID_PAGE)
        self.assertNotIn(REGISTRATION_SESSION_KEY, self.client.session)

    def test_a_link_that_expires_before_the_form_is_sent_completes_nothing(self):
        self.open_link()
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        response = self.complete()

        self.assertTemplateUsed(response, INVALID_PAGE)
        self.assertFalse(User.objects.get().has_usable_password())

    def test_a_token_consumed_by_a_concurrent_submission_completes_nothing(self):
        self.open_link()
        token = VerificationToken.objects.get()
        # Seen as valid at the start of the request, consumed before the form is saved.
        with patch.object(RegistrationCompleteView, "find_token", return_value=token):
            token.delete()
            response = self.complete()

        self.assertTemplateUsed(response, INVALID_PAGE)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.UNVERIFIED)
        self.assertFalse(self.user.has_usable_password())

    def test_weak_passwords_are_rejected_and_the_link_stays_valid(self):
        weak = {
            "too short": "kettle orchard",
            "numeric": "1234567890123456",
            "common": "1q2w3e4r5t6y7u8i9o0p",
            "similar to the address": "new.person@example.org",
        }
        for name, password in weak.items():
            with self.subTest(name):
                self.open_link()
                response = self.complete(password)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors.get("password1"))
                self.assertFalse(User.objects.get().has_usable_password())
                self.assertEqual(VerificationToken.objects.count(), 1)

    def test_different_passwords_are_rejected(self):
        self.open_link()

        response = self.complete(password2=OTHER_PASSWORD)

        self.assertTrue(response.context["form"].errors.get("password2"))
        self.assertFalse(User.objects.get().has_usable_password())

    def test_a_long_passphrase_is_accepted(self):
        self.open_link()
        passphrase = "correct horse battery staple " * 4

        self.complete(passphrase)

        self.assertTrue(User.objects.get().check_password(passphrase))

    def test_the_password_fields_are_masked_in_error_reports(self):
        self.open_link()

        response = self.complete()

        self.assertEqual(
            response.wsgi_request.sensitive_post_parameters, ("password1", "password2")
        )

    def test_the_profile_is_saved_at_completion_but_not_the_address_or_privileges(self):
        self.open_link()

        self.complete(
            town="Zürich",
            postal_code="8001",
            email="other.person@example.org",
            is_staff="on",
            is_superuser="on",
            status="blocked",
        )

        user = User.objects.get()
        self.assertEqual((user.town, user.swiss_postal_code), ("Zürich", 8001))
        self.assertEqual(user.normalized_email, "new.person@example.org")
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertFalse(user.is_staff or user.is_superuser)

    def test_the_token_is_stored_only_as_a_keyed_digest(self):
        raw = self.path.rstrip("/").rsplit("/", 1)[-1]

        self.assertNotEqual(self.token.digest, raw)
        self.assertRegex(self.token.digest, r"\A[0-9a-f]{64}\Z")
        self.assertFalse(VerificationToken.objects.filter(digest=raw).exists())

    def test_malformed_tokens_are_invalid_and_cause_no_error(self):
        for token in ("x", "A" * 43, "A" * 5000, "-" * 43):
            with self.subTest(length=len(token)):
                response = self.client.get(reverse("accounts:registration_verify", args=[token]))
                self.assertTemplateUsed(response, INVALID_PAGE)

    def test_a_token_with_other_characters_matches_no_url(self):
        response = self.client.get("/register/verify/abc%20def/")

        self.assertEqual(response.status_code, 404)

    def test_an_unverified_account_cannot_log_in(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "new.person@example.org", "password": PASSWORD},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_every_page_of_the_flow_is_not_cached_including_the_invalid_page(self):
        valid = [self.open_link(), self.client.get(self.complete_url)]
        invalid = [
            Client().get(self.complete_url),
            Client().get(reverse("accounts:registration_verify", args=["x"])),
            Client().post(self.complete_url, completion_data()),
        ]
        for response in (*valid, *invalid):
            self.assertIn("no-store", response["Cache-Control"])

    def test_the_token_is_removed_from_the_real_request_log(self):
        # The verification path accepts GET only; the 405 is logged by django.request with the
        # path, through the handler of the project's logging configuration.
        handlers = [
            handler
            for handler in logging.getLogger("django").handlers
            if any(type(f).__name__ == "TokenRedactionFilter" for f in handler.filters)
        ]
        self.assertTrue(handlers)
        stream = io.StringIO()
        previous = handlers[0].setStream(stream)
        try:
            self.client.post(self.path)
        finally:
            handlers[0].setStream(previous)

        raw = self.path.rstrip("/").rsplit("/", 1)[-1]
        self.assertIn("/register/verify/<redacted>", stream.getvalue())
        self.assertNotIn(raw, stream.getvalue())


@tag("mariadb")
class ConcurrentCompletionTests(TransactionTestCase):
    databases = {"default", "addresses", "address_import"}
    def test_two_simultaneous_submissions_activate_the_account_once(self):
        register(Client())
        path = verification_path(mail.outbox[0])
        token = VerificationToken.objects.get()
        clients = [Client(), Client()]
        for client in clients:
            client.get(path)
        barrier = Barrier(2)

        def submit(client):
            try:
                barrier.wait()
                data = completion_data(registration=token.pk)
                return client.post(reverse("accounts:registration_complete"), data).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = sorted(executor.map(submit, clients))

        self.assertEqual(statuses, [200, 302])
        user = User.objects.get()
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertEqual(VerificationToken.objects.count(), 0)

    def test_a_replacement_and_a_completion_at_the_same_time_leave_one_consistent_state(self):
        register(Client())
        path = verification_path(mail.outbox[0])
        token = VerificationToken.objects.get()
        completing = Client()
        completing.get(path)
        barrier = Barrier(2)

        def complete(_):
            try:
                barrier.wait()
                data = completion_data(registration=token.pk)
                return completing.post(reverse("accounts:registration_complete"), data).status_code
            finally:
                connections.close_all()

        def replace(_):
            try:
                barrier.wait()
                return register(Client()).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(complete, 0), executor.submit(replace, 0)]
            [result.result() for result in results]

        users = list(User.objects.all())
        self.assertEqual(len(users), 1)
        self.assertIn(users[0].status, (User.Status.ACTIVE, User.Status.UNVERIFIED))
        if users[0].status == User.Status.ACTIVE:
            self.assertTrue(users[0].has_usable_password())
            self.assertIsNotNone(users[0].customer_number_id)
        else:
            self.assertFalse(users[0].has_usable_password())
            self.assertEqual(VerificationToken.objects.count(), 1)


@tag("mariadb")
class ExpiredRegistrationCleanupTests(TestCase):
    def test_expired_unverified_accounts_are_deleted_with_their_tokens(self):
        register(Client(), "expired.person@example.org")
        register(Client(), "pending.person@example.org")
        VerificationToken.objects.filter(
            user__normalized_email="expired.person@example.org"
        ).update(expires_at=timezone.now() - timedelta(seconds=1))

        output = io.StringIO()
        call_command("delete_expired_registrations", stdout=output)

        self.assertEqual(
            list(User.objects.values_list("normalized_email", flat=True)),
            ["pending.person@example.org"],
        )
        self.assertEqual(VerificationToken.objects.count(), 1)
        self.assertIn("Deleted 1 expired", output.getvalue())

    def test_an_unverified_account_without_any_token_is_deleted(self):
        User.objects.create_user("orphan.person@example.org")

        self.assertEqual(registration.delete_expired_registrations(), (1, 0))
        self.assertEqual(User.objects.count(), 0)

    def test_an_active_account_is_never_deleted_by_the_cleanup(self):
        user = create_active_user("active.person@example.org")
        VerificationToken.objects.issue(
            user, VerificationToken.Purpose.REGISTRATION, timedelta(seconds=-1)
        )

        self.assertEqual(registration.delete_expired_registrations(), (0, 0))

        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_nothing_is_deleted_while_the_link_is_valid(self):
        register(Client(), "pending.person@example.org")

        self.assertEqual(registration.delete_expired_registrations(), (0, 0))
        self.assertEqual(User.objects.count(), 1)

    def test_one_failing_account_does_not_stop_the_others(self):
        for name in ("first", "second"):
            User.objects.create_user(f"{name}.person@example.org")
        real = User.delete
        calls = []

        def failing_once(self, *args, **kwargs):
            calls.append(self.pk)
            if len(calls) == 1:
                raise registration.AccountIntegrityError("not deletable")
            return real(self, *args, **kwargs)

        with patch.object(User, "delete", failing_once):
            with self.assertLogs("leg_platform.accounts.registration", "ERROR") as logs:
                result = registration.delete_expired_registrations()

        self.assertEqual(result, (1, 1))
        self.assertEqual(User.objects.count(), 1)
        self.assertNotIn("person@example.org", "\n".join(logs.output))

    def test_the_command_fails_when_an_account_could_not_be_deleted(self):
        with patch(
            "leg_platform.accounts.management.commands.delete_expired_registrations"
            ".delete_expired_registrations",
            return_value=(0, 2),
        ):
            with self.assertRaises(CommandError):
                call_command("delete_expired_registrations", stdout=io.StringIO())
