"""The confirmation code of a new registration (D-136): binding to the session, limits, uniform answers, the mail."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from html.parser import HTMLParser
from threading import Barrier
from unittest import mock

from django.core import mail
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import mail_queue, rate_limits, registration
from leg_platform.accounts.models import MailJob, PortalSettings, RegistrationChallenge, SecurityEvent, User
from leg_platform.accounts.models.registration_codes import code_digest
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    confirm_code_of,
    confirmation_code,
    create_active_user,
    registration_data,
)
from leg_platform.accounts.views import REGISTRATION_CHALLENGE_SESSION_KEY

CODE_URL = reverse("accounts:registration_sent")
EMAIL = "new.person@example.org"
WRONG_TEXT = "Dieser Code ist nicht korrekt. Bitte prüfen Sie ihn und versuchen Sie es erneut."
INVALID_TEXT = "Dieser Code ist nicht mehr gültig. Fordern Sie einen neuen Code an."


class Collector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def ask(client, email=EMAIL, **extra):
    response = client.post(reverse("accounts:registration"), registration_data(email), **extra)
    mail_queue.process_pending()
    return response


def invited_user(email):
    """An address that staff invited (D-087): its account is pending and carries ``invited_at``."""
    from leg_platform.community import persons
    from leg_platform.community.tests.helpers import staff_user

    data = {
        "salutation": "mrs", "first_name": "Erika", "last_name": "Neu", "address_line_1": "c/o Treuhand",
        "street": "Neuweg", "house_number": "3", "swiss_postal_code": 3000, "town": "Bern", "country": "CH",
        "phone_number": "+41311234567",
    }
    persons.create_person(staff_user(f"staff.{email}"), email, data)
    mail_queue.process_pending()
    return User.objects.get(normalized_email=email)


def other_code(code):
    return f"{(int(code) + 1) % 1_000_000:06d}"


def enter(client, value, **extra):
    return client.post(CODE_URL, {"otp_token": value}, **extra)


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []

    def handle_starttag(self, tag_name, attrs):
        self.tags.append(tag_name)
        if tag_name == "a":
            self.links.append(dict(attrs).get("href"))


@tag("mariadb")
class CodeEntryTests(TestCase):
    def setUp(self):
        ask(self.client)
        self.message = mail.outbox[-1]
        self.code = confirmation_code(self.message)

    def test_the_code_confirms_once_and_only_once(self):
        first = enter(self.client, self.code)
        self.assertRedirects(first, reverse("accounts:registration_method"), fetch_redirect_response=False)
        self.assertTrue(RegistrationChallenge.objects.get().consumed)
        # A used code is dead, also in a second tab of the same session.
        again = enter(self.client, self.code)
        self.assertContains(again, INVALID_TEXT)

    def test_spaces_and_hyphens_in_the_entry_are_ignored(self):
        spaced = f" {self.code[:3]} - {self.code[3:]} "
        self.assertEqual(enter(self.client, spaced).status_code, 302)

    def test_a_code_from_another_session_is_refused(self):
        response = enter(Client(), self.code)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, INVALID_TEXT)
        self.assertEqual(User.objects.count(), 0)
        # The right session still works: the foreign attempt did not touch the challenge.
        self.assertEqual(enter(self.client, self.code).status_code, 302)

    def test_a_wrong_code_counts_and_the_fifth_wrong_entry_kills_the_code(self):
        wrong = other_code(self.code)
        for _attempt in range(4):
            self.assertContains(enter(self.client, wrong), WRONG_TEXT)
        fifth = enter(self.client, wrong)
        self.assertContains(fifth, INVALID_TEXT)
        self.assertEqual(RegistrationChallenge.objects.get().failed_attempts, 5)
        # Even the right code does not work any more.
        self.assertContains(enter(self.client, self.code), INVALID_TEXT)
        self.assertEqual(User.objects.count(), 0)

    def test_an_expired_code_is_refused(self):
        RegistrationChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertContains(enter(self.client, self.code), INVALID_TEXT)
        self.assertEqual(User.objects.count(), 0)

    def test_the_code_is_valid_for_fifteen_minutes(self):
        row = RegistrationChallenge.objects.get()
        self.assertAlmostEqual(
            (row.expires_at - timezone.now()).total_seconds(), registration.CODE_LIFETIME.total_seconds(), delta=30
        )
        self.assertEqual(registration.CODE_LIFETIME, timedelta(minutes=15))

    def test_garbage_and_empty_entries_get_neutral_answers(self):
        self.assertContains(enter(self.client, "abcdef"), WRONG_TEXT)
        self.assertEqual(enter(self.client, "").status_code, 200)

    def test_a_resend_inside_the_pause_sends_nothing_and_after_it_replaces_the_code(self):
        resend_url = reverse("accounts:registration_resend")
        self.client.post(resend_url, {})
        mail_queue.process_pending()
        self.assertEqual(len(mail.outbox), 1)  # the pause is enforced on the server

        session = self.client.session
        session["mail_request_registration"]["available_at"] = 0
        session.save()
        self.client.post(resend_url, {})
        mail_queue.process_pending()
        self.assertEqual(len(mail.outbox), 2)
        new_code = confirmation_code(mail.outbox[-1])

        old_code = self.code if self.code != new_code else other_code(new_code)
        self.assertContains(enter(self.client, old_code), WRONG_TEXT)  # a wrong code for the new challenge
        self.assertEqual(enter(self.client, new_code).status_code, 302)
        # The first challenge was discarded with the resend.
        self.assertEqual(RegistrationChallenge.objects.count(), 1)

    def test_the_code_page_without_a_request_goes_back_to_step_one(self):
        response = Client().get(CODE_URL)
        self.assertRedirects(response, reverse("accounts:registration"), fetch_redirect_response=False)

    def test_change_address_discards_the_challenge_and_the_staged_data(self):
        response = self.client.post(reverse("accounts:registration_change_address"))
        self.assertRedirects(response, reverse("accounts:registration"), fetch_redirect_response=False)
        self.assertNotIn(REGISTRATION_CHALLENGE_SESSION_KEY, self.client.session)
        self.assertEqual(RegistrationChallenge.objects.count(), 0)
        self.assertEqual(self.client.get(CODE_URL).status_code, 302)
        # The old code works nowhere any more.
        self.assertContains(enter(self.client, self.code), INVALID_TEXT)

    def test_change_address_is_post_only(self):
        self.assertEqual(self.client.get(reverse("accounts:registration_change_address")).status_code, 405)

    def test_pages_are_not_cached(self):
        response = self.client.get(CODE_URL)
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_form_has_the_one_time_code_attributes_and_no_pattern(self):
        html = self.client.get(CODE_URL).content.decode()
        self.assertIn('autocomplete="one-time-code"', html)
        self.assertIn('inputmode="numeric"', html)
        self.assertNotIn("maxlength", html.split('name="otp_token"')[1].split(">")[0])
        self.assertNotIn("pattern=", html)
        self.assertIn("novalidate", html)
        self.assertNotIn("<script>", html)


@tag("mariadb")
class UniformAnswerTests(TestCase):
    """The page and the answers are the same for every address (D-058): no account oracle."""

    def addresses(self):
        active = create_active_user("active.person@example.org")
        blocked = create_active_user("blocked.person@example.org")
        blocked.set_status(User.Status.BLOCKED)
        pending = User.objects.create_user("pending.person@example.org")
        invited = invited_user("invited.person@example.org")
        deactivated = create_active_user("gone.person@example.org")
        deactivated.set_status(User.Status.DEACTIVATED)
        return [
            "new.person@example.org",
            active.email,
            blocked.email,
            pending.email,
            invited.email,
            deactivated.email,
            "unknown@example.org",
        ]

    def test_only_a_new_and_a_pending_address_get_a_code(self):
        subjects = {}
        for address in self.addresses():
            mail.outbox.clear()
            client = Client()
            ask(client, address)
            row = RegistrationChallenge.objects.filter(address=address).latest("pk")
            subjects[address] = (bool(row.code_digest), [bool(re.match(r"^\d{6} ", m.subject)) for m in mail.outbox])
        with_code = {address for address, (stored, _sent) in subjects.items() if stored}
        # A deactivated account does not hold its address (D-016): it gets a code like a new address.
        self.assertEqual(
            with_code,
            {"new.person@example.org", "pending.person@example.org", "gone.person@example.org", "unknown@example.org"},
        )
        for address, (stored, sent) in subjects.items():
            self.assertEqual(sent, [True] if stored else [False], address)  # an invitation or a notice has no code

    def test_the_answers_of_step_one_and_the_code_page_are_identical(self):
        results = []
        for address in self.addresses():
            client = Client()
            first = client.post(reverse("accounts:registration"), registration_data(address))
            page = client.get(first["Location"])
            wrong = enter(client, "000000")
            results.append(
                (
                    first.status_code,
                    first["Location"],
                    page.status_code,
                    wrong.status_code,
                    re.sub(r"name=.csrfmiddlewaretoken. value=.[^\"']+.", "", re.sub(r"[\w.+-]+@example\.org", "ADDR", wrong.content.decode())),
                    re.sub(r"name=.csrfmiddlewaretoken. value=.[^\"']+.", "", re.sub(r"[\w.+-]+@example\.org", "ADDR", page.content.decode())),
                )
            )
        for index in range(6):
            self.assertEqual(len({row[index] for row in results}), 1, f"field {index} differs")
        self.assertEqual(len(set(results)), 1)

    def test_the_expiry_is_identical_for_every_address_kind(self):
        remaining = {}
        for address in self.addresses():
            ask(Client(), address)
            mail_queue.process_pending()
            row = RegistrationChallenge.objects.filter(address=address).latest("pk")
            remaining[address] = (row.expires_at - row.created_at).total_seconds()
        self.assertEqual(len({round(value) for value in remaining.values()}), 1, remaining)
        self.assertAlmostEqual(next(iter(remaining.values())), registration.CODE_LIFETIME.total_seconds(), delta=5)

    def test_the_dead_state_is_identical_for_every_address_kind(self):
        norm = lambda text: re.sub(  # noqa: E731
            r"name=.csrfmiddlewaretoken. value=.[^\"']+.", "", re.sub(r"[\w.+-]+@example\.org", "ADDR", text)
        )
        results = []
        for number, address in enumerate(self.addresses(), start=1):
            client = Client(REMOTE_ADDR=f"192.0.2.{number}")  # the per-IP limit of code entries is not the subject here
            ask(client, address)
            fifth = None
            for _attempt in range(5):
                fifth = enter(client, "000000")
            page = client.get(CODE_URL)
            session = client.session
            resend = client.post(reverse("accounts:registration_resend"), {}, follow=True)
            results.append(
                (
                    norm(fifth.content.decode()),
                    norm(page.content.decode()),
                    resend.status_code,
                    resend.redirect_chain,
                    norm(resend.content.decode()),
                    session.get("mail_request_registration", {}).get("available_at", 0) > 0,
                )
            )
        for index in range(6):
            values = [row[index] for row in results]
            if index == 0 and len({repr(v) for v in values}) > 1:
                import difflib

                self.fail("\n".join(difflib.unified_diff(values[0].splitlines(), next(v for v in values if v != values[0]).splitlines(), lineterm="", n=0)))
            self.assertEqual(len({repr(v) for v in values}), 1, f"field {index} differs")

    def test_every_entered_address_gets_a_challenge_row_but_a_taken_address_gets_no_code(self):
        taken = create_active_user("taken.person@example.org")
        ask(Client(), taken.email)
        ask(Client(), "free.person@example.org")
        self.assertEqual(RegistrationChallenge.objects.count(), 2)
        taken_row = RegistrationChallenge.objects.get(address=taken.email)
        self.assertEqual(taken_row.code_digest, "")
        # The information mail carries no code.
        notice = next(message for message in mail.outbox if message.to == [taken.email])
        self.assertFalse(re.match(r"^\d{6} ", notice.subject))

    def test_the_counter_works_without_a_code_row(self):
        taken = create_active_user("taken.person@example.org")
        client = Client()
        ask(client, taken.email)
        for _attempt in range(4):
            self.assertContains(enter(client, "123456"), WRONG_TEXT)
        self.assertContains(enter(client, "123456"), INVALID_TEXT)
        self.assertEqual(RegistrationChallenge.objects.get().failed_attempts, 5)

    def test_a_taken_address_never_gets_an_account_from_any_entry(self):
        taken = create_active_user("taken.person@example.org")
        client = Client()
        ask(client, taken.email)
        for value in ("000000", "123456", "999999"):
            enter(client, value)
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(User.objects.get().check_password(PASSWORD))


@tag("mariadb")
class QueueAndStorageTests(TestCase):
    def test_a_stale_registration_job_is_dropped_and_no_mail_is_sent(self):
        Client().post(reverse("accounts:registration"), registration_data())
        MailJob.objects.update(created_at=timezone.now() - registration.JOB_MAX_AGE - timedelta(seconds=30))
        mail_queue.process_pending()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_a_fresh_job_is_sent(self):
        Client().post(reverse("accounts:registration"), registration_data())
        mail_queue.process_pending()
        self.assertEqual(len(mail.outbox), 1)

    def test_the_code_is_stored_nowhere_in_clear_and_never_logged(self):
        collector = Collector()
        root = logging.getLogger()
        root.addHandler(collector)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            client = Client()
            ask(client)
            code = confirmation_code(mail.outbox[-1])
            enter(client, other_code(code))
            enter(client, code)
        finally:
            root.removeHandler(collector)
            root.setLevel(old_level)
        self.assertFalse([line for line in collector.lines if code in line])
        # No stored text carries the code: the job, the session, the challenge row and the security log.
        stored = json.dumps(list(MailJob.objects.values()), default=str)
        stored += json.dumps(list(SecurityEvent.objects.values()), default=str)
        stored += json.dumps(list(RegistrationChallenge.objects.values()), default=str)
        stored += json.dumps(dict(client.session.items()), default=str)
        self.assertNotIn(code, stored)

    def test_the_challenge_id_is_held_in_the_session_and_only_its_digest_is_stored(self):
        client = Client()
        ask(client)
        raw = client.session[REGISTRATION_CHALLENGE_SESSION_KEY]
        self.assertGreaterEqual(len(raw), 43)
        row = RegistrationChallenge.objects.get()
        self.assertNotEqual(row.challenge_digest, raw)
        self.assertNotIn(raw, json.dumps(list(RegistrationChallenge.objects.values()), default=str))

    def test_the_code_digest_depends_on_the_challenge(self):
        self.assertNotEqual(code_digest("challenge-a", "123456"), code_digest("challenge-b", "123456"))

    def test_two_requests_for_the_same_address_get_separate_codes(self):
        first, second = Client(), Client()
        ask(first)
        first_code = confirmation_code(mail.outbox[-1])
        ask(second)
        second_code = confirmation_code(mail.outbox[-1])
        # Each browser can only use the code that belongs to its own challenge.
        if first_code != second_code:
            self.assertContains(enter(first, second_code), WRONG_TEXT)
        self.assertEqual(enter(second, second_code).status_code, 302)


@tag("mariadb")
class CodeLimitTests(TestCase):
    def test_the_per_ip_limit_answers_429_after_the_allowed_entries(self):
        client = Client()
        ask(client)
        allowed = rate_limits.REGISTRATION_CODE_IP_LIMITS[0][0]
        statuses = []
        for _attempt in range(allowed + 2):
            statuses.append(enter(client, "000000", REMOTE_ADDR="203.0.113.7").status_code)
        self.assertEqual(statuses[0], 200)
        self.assertEqual(statuses[-1], 429)

    def test_the_limit_counts_per_address_of_the_client(self):
        client = Client()
        ask(client)
        for _attempt in range(rate_limits.REGISTRATION_CODE_IP_LIMITS[0][0] + 1):
            enter(client, "000000", REMOTE_ADDR="203.0.113.8")
        self.assertEqual(enter(client, "000000", REMOTE_ADDR="203.0.113.9").status_code, 200)


@tag("mariadb")
class CodeMailTests(TestCase):
    def setUp(self):
        PortalSettings.objects.update_or_create(
            pk=1,
            defaults={
                "name": 'LEG <b>"Tal"</b> & Co',
                "privacy_url": "https://www.example.org/privacy?a=1&b=2",
                "website_url": "https://leg.example.org/",
            },
        )
        ask(Client())
        self.message = mail.outbox[-1]
        self.code = confirmation_code(self.message)
        self.html = self.message.alternatives[0][0]

    def test_the_subject_contains_the_code_and_the_instance_name(self):
        self.assertRegex(self.message.subject, r"^\d{6} ist Ihr Code für ")
        self.assertIn("Tal", self.message.subject)

    def test_the_mail_is_html_with_a_text_variant_that_contains_the_code(self):
        self.assertEqual(self.message.alternatives[0][1], "text/html")
        self.assertIn(self.code, self.message.body)
        self.assertEqual(self.message.attachments, [])

    def test_the_instance_name_and_values_are_escaped(self):
        self.assertNotIn("<b>", self.html)
        self.assertIn("&lt;b&gt;", self.html)
        self.assertIn("&amp; Co", self.html)
        self.assertIn("a=1&amp;b=2", self.html)

    def test_no_image_script_or_link_but_the_privacy_notice(self):
        parser = LinkParser()
        parser.feed(self.html)
        for forbidden in ("img", "script", "link", "iframe", "form", "button", "style", "object", "video"):
            self.assertNotIn(forbidden, parser.tags)
        self.assertEqual(parser.links, ["https://www.example.org/privacy?a=1&b=2"])
        self.assertNotIn("url(", self.html)
        self.assertNotIn("src=", self.html)
        self.assertIn('name="color-scheme"', self.html)

    def test_the_code_box_holds_only_the_digits_and_it_is_the_only_big_number(self):
        preheader, box = re.findall(r"<div[^>]*>([^<]*)</div>", self.html)
        self.assertEqual(box, self.code)
        # The hidden preheader starts with the code (the preview of a mail list); it is never displayed.
        self.assertTrue(preheader.startswith(self.code + " "))
        self.assertIn("display:none", self.html)
        self.assertEqual(len(re.findall(r"font-size:40px", self.html)), 1)
        # No other run of six digits in the visible text.
        visible = re.sub(r"<div style=\"display:none.*?</div>", "", self.html)
        text = re.sub(r"<[^>]+>", " ", visible)
        self.assertEqual(re.findall(r"\d{6,}", text), [self.code])

    def test_a_purpose_line_stands_above_the_box_and_the_box_has_a_firm_edge(self):
        self.assertIn("Ihr Code zum Erstellen Ihres Kontos", self.html)
        self.assertLess(self.html.index("Ihr Code zum Erstellen Ihres Kontos", self.html.index("</div>")), self.html.index("border:2px solid #9ca3af"))
        self.assertIn("border:2px solid #9ca3af", self.html)

    def test_the_text_variant_has_no_link_but_the_privacy_notice(self):
        urls = re.findall(r"https?://\S+", self.message.body)
        # The optional contact line is plain text; the HTML variant has no link for it.
        self.assertEqual(urls, ["https://www.example.org/privacy?a=1&b=2", "https://leg.example.org/"])

    def test_the_small_print_is_there(self):
        self.assertIn("Falls Sie das nicht angefordert haben, ignorieren Sie diese E-Mail.", self.message.body)
        self.assertIn("Geben Sie diesen Code nie weiter.", self.message.body)

    def test_the_information_mail_for_a_taken_address_is_unchanged_and_has_no_code(self):
        user = create_active_user("taken.person@example.org")
        mail.outbox.clear()
        ask(Client(), user.email)
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(re.search(r"\b\d{6}\b", mail.outbox[0].subject + mail.outbox[0].body))
        self.assertEqual(getattr(mail.outbox[0], "alternatives", []), [])

    def test_the_graph_backend_sends_the_html_variant_as_html_without_attachments(self):
        from leg_platform.mail import _payload

        payload = _payload(self.message)
        self.assertEqual(payload["message"]["body"]["contentType"], "HTML")
        self.assertIn(self.code, payload["message"]["body"]["content"])
        self.assertNotIn("attachments", payload["message"])


@tag("mariadb")
class StepsAfterTheCodeTests(TestCase):
    def prove(self, client):
        ask(client)
        self.assertEqual(confirm_code_of(client, mail.outbox[-1]).status_code, 302)

    def test_the_steps_two_and_three_need_the_proof(self):
        for name in ("registration_method", "registration_password", "registration_complete"):
            response = Client().get(reverse(f"accounts:{name}"))
            self.assertEqual(response.status_code, 302, name)
            self.assertEqual(response["Location"], reverse("accounts:registration"), name)

    def test_the_proof_ends_after_about_two_hours(self):
        client = Client()
        self.prove(client)
        RegistrationChallenge.objects.update(proven_until=timezone.now() - timedelta(seconds=1))
        response = client.get(reverse("accounts:registration_method"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("accounts:registration"))

    def test_the_proof_page_works_after_the_code(self):
        client = Client()
        self.prove(client)
        self.assertEqual(client.get(reverse("accounts:registration_method")).status_code, 200)

    def test_a_proof_of_one_session_does_not_open_the_steps_of_another(self):
        self.prove(Client())
        other = Client()
        self.assertEqual(other.get(reverse("accounts:registration_method")).status_code, 302)

    def test_the_session_key_changes_when_the_code_is_confirmed(self):
        client = Client()
        ask(client)
        client.get(CODE_URL)
        before = client.cookies["sessionid"].value
        confirm_code_of(client, mail.outbox[-1])
        self.assertNotEqual(client.cookies["sessionid"].value, before)


@tag("mariadb")
class ConcurrentEntryTests(TransactionTestCase):
    databases = {"default", "addresses"}

    def test_simultaneous_wrong_entries_are_all_counted(self):
        client = Client()
        ask(client)
        code = confirmation_code(mail.outbox[-1])
        raw = client.session[REGISTRATION_CHALLENGE_SESSION_KEY]
        wrong = other_code(code)
        barrier = Barrier(4)

        def enter_wrong(_index):
            try:
                barrier.wait()
                return registration.confirm_code(raw, wrong)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(enter_wrong, range(4)))

        self.assertEqual(results, [registration.CodeResult.WRONG] * 4)
        self.assertEqual(RegistrationChallenge.objects.get().failed_attempts, 4)
        self.assertEqual(registration.confirm_code(raw, code), registration.CodeResult.CONFIRMED)

    def test_the_right_code_entered_twice_at_once_creates_one_account(self):
        client = Client()
        ask(client)
        code = confirmation_code(mail.outbox[-1])
        raw = client.session[REGISTRATION_CHALLENGE_SESSION_KEY]
        barrier = Barrier(2)

        def enter_right(_index):
            try:
                barrier.wait()
                return registration.confirm_code(raw, code)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(enter_right, range(2)))

        self.assertEqual(results.count(registration.CodeResult.CONFIRMED), 1)
        self.assertEqual(User.objects.count(), 1)


@override_settings()
class PasswordRuleNoteTests(TestCase):
    """The only link to the password rule here: the registration form shows the minimum of the validator (8)."""

    def test_the_password_step_shows_the_minimum_of_eight_characters(self):
        client = Client()
        ask(client)
        confirm_code_of(client, mail.outbox[-1])
        from leg_platform.accounts.tests.helpers import choose_method, registration_reference

        choose_method(client, "password", registration_reference(client))
        html = client.get(reverse("accounts:registration_password")).content.decode()
        self.assertIn("8", html)


@tag("mariadb")
class ReviewFollowUpTests(TestCase):
    """The pause after a dead code, the honest page text, the notice after a resend, the access of the field, the address of "Change address"."""

    def setUp(self):
        ask(self.client)
        self.message = mail.outbox[-1]
        self.code = confirmation_code(self.message)

    def cooldown(self):
        match = re.search(r'data-cooldown="(\d+)"', self.client.get(CODE_URL).content.decode())
        return int(match.group(1))

    def resend(self):
        response = self.client.post(reverse("accounts:registration_resend"))
        mail_queue.process_pending()
        return response

    def test_a_live_code_keeps_the_pause_and_a_dead_one_does_not(self):
        self.assertGreater(self.cooldown(), 0)
        self.resend()
        self.assertEqual(len(mail.outbox), 1)  # inside the pause nothing is sent
        wrong = other_code(self.code)
        for _attempt in range(5):
            enter(self.client, wrong)
        self.assertEqual(self.cooldown(), 0)
        self.resend()
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(enter(self.client, confirmation_code(mail.outbox[-1])).status_code, 302)

    def test_an_expired_code_can_be_replaced_at_once(self):
        RegistrationChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.cooldown(), 0)
        self.resend()
        self.assertEqual(len(mail.outbox), 2)

    def test_the_limits_of_the_requests_still_apply_to_a_dead_code(self):
        wrong = other_code(self.code)
        for _attempt in range(5):
            enter(self.client, wrong)
        with mock.patch.object(mail_queue, "requests_refused", return_value=True):
            self.resend()
        self.assertEqual(len(mail.outbox), 1)

    def test_the_page_says_that_an_email_was_sent_and_the_validity(self):
        html = self.client.get(CODE_URL).content.decode()
        self.assertIn("Wir haben eine E-Mail an", html)
        self.assertIn("Falls Sie stattdessen einen Link erhalten haben, öffnen Sie diesen Link.", html)
        self.assertIn("Der Code ist 15 Minuten gültig.", html)

    def test_the_invitation_mail_says_that_no_code_is_needed(self):
        person = invited_user("invited.person@example.org")
        mail.outbox.clear()
        ask(Client(), person.email)
        self.assertIn("Sie müssen keinen Code eingeben. Öffnen Sie den Link.", mail.outbox[-1].body)

    def test_a_new_code_is_announced_after_a_resend(self):
        RegistrationChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.post(reverse("accounts:registration_resend"), follow=True)
        self.assertContains(response, "Wir haben einen neuen Code gesendet. Der alte Code funktioniert nicht mehr.")

    def test_the_field_names_its_error_and_its_help_for_assistive_technology(self):
        html = self.client.get(CODE_URL).content.decode()
        self.assertIn("6 Ziffern aus der E-Mail", html)
        self.assertIn("id_otp_token_helptext", html)
        wrong = enter(self.client, other_code(self.code)).content.decode()
        field = re.search(r"<input[^>]*name=\"otp_token\"[^>]*>", wrong).group(0)
        self.assertIn('aria-invalid="true"', field)
        self.assertIn("id_otp_token_error", field)
        self.assertIn('id="id_otp_token_error"', wrong)

    def test_change_address_offers_the_address_again_once(self):
        self.client.post(reverse("accounts:registration_change_address"))
        first = self.client.get(reverse("accounts:registration"))
        self.assertContains(first, f'value="{EMAIL}"')
        self.assertNotIn(EMAIL, first["Location"] if first.has_header("Location") else "")
        second = self.client.get(reverse("accounts:registration"))
        self.assertNotContains(second, f'value="{EMAIL}"')

    def test_a_hit_of_the_bot_check_creates_no_challenge_row(self):
        before = RegistrationChallenge.objects.count()
        response = Client().post(
            reverse("accounts:registration"), registration_data("bot.person@example.org", contact_fax_number="x")
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RegistrationChallenge.objects.count(), before)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.REGISTRATION).exists())  # the setUp job was sent

    def test_the_code_field_is_hidden_from_error_reports(self):
        response = self.client.post(CODE_URL, {"otp_token": "123456"})
        self.assertEqual(tuple(response.wsgi_request.sensitive_post_parameters), ("otp_token",))
