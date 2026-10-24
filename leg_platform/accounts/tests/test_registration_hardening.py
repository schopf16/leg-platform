"""Hardening of the forms that send a link and of the registration session (security review of
phase 3 (A1 to A5, B1 to B8), designer acceptance of phase 3, D-058).

The negative tests state what must NOT happen: a refused request stores nothing, a script gets the
page of a person, a stale link or a forged form changes nothing.
"""

import re
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core import mail, signing
from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts import bot_check, mail_queue, rate_limits
from leg_platform.accounts.forms import (
    NewPasswordForm,
    RegistrationPasswordForm,
    password_hint,
)
from leg_platform.accounts.models import (
    MailJob,
    PortalSettings,
    RateLimitBucket,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    choose_method,
    completion_data,
    create_active_user,
    password_data,
    registration_data,
    reset_request_data,
    run_mail_queue,
    verification_path,
)
from leg_platform.accounts.views import (
    REGISTRATION_PASSWORD_SESSION_KEY,
    REGISTRATION_PRIVACY_SESSION_KEY,
    REGISTRATION_SESSION_KEY,
)

REGISTRATION_URL = reverse("accounts:registration")
RESET_URL = reverse("accounts:password_reset")
PASSWORD_URL = reverse("accounts:registration_password")
COMPLETE_URL = reverse("accounts:registration_complete")
ADDRESS_DATABASES = {"default", "addresses"}
HOME_URL = reverse("home")


def post_form(url, data, client=None, address="10.71.0.1"):
    return (client or Client()).post(url, data, REMOTE_ADDR=address)


def strip_token(response):
    return re.sub(rb'name="csrfmiddlewaretoken" value="[^"]+"', b"", response.content)


@tag("mariadb")
class RefusedRequestTests(TestCase):
    """A1: the limits of the client address and of the instance are read before anything is stored."""

    def test_a_refused_request_stores_no_bucket_no_job_and_no_session(self):
        with patch.object(rate_limits, "MAIL_IP_LIMITS", ((1, timedelta(hours=1)),)):
            first = post_form(REGISTRATION_URL, registration_data("first.person@example.org"))
            buckets = RateLimitBucket.objects.count()
            jobs = MailJob.objects.count()

            refused = post_form(REGISTRATION_URL, registration_data("second.person@example.org"))

        self.assertEqual(first.status_code, 302)
        self.assertEqual(refused.status_code, 429)
        self.assertContains(
            refused, "Zu viele Anfragen, bitte versuchen Sie es später erneut.", status_code=429
        )
        self.assertEqual(RateLimitBucket.objects.count(), buckets)
        self.assertEqual(MailJob.objects.count(), jobs)
        # No session for a client that is refused (a bot without cookies must not make rows).
        self.assertNotIn(settings.SESSION_COOKIE_NAME, refused.cookies)
        self.assertFalse(User.objects.filter(email="second.person@example.org").exists())

    def test_the_password_reset_request_is_refused_in_the_same_way(self):
        with patch.object(rate_limits, "MAIL_IP_LIMITS", ((1, timedelta(hours=1)),)):
            post_form(RESET_URL, reset_request_data("first.person@example.org"))
            refused = post_form(RESET_URL, reset_request_data("second.person@example.org"))

        self.assertEqual(refused.status_code, 429)
        self.assertEqual(MailJob.objects.count(), 1)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, refused.cookies)

    def test_the_limit_of_an_address_is_never_shown(self):
        # Only the limits that do not depend on an account are shown (no account oracle).
        with patch.object(rate_limits, "MAIL_ADDRESS_LIMITS", ((1, timedelta(hours=1)),)):
            first = post_form(REGISTRATION_URL, registration_data("same.person@example.org"))
            second = post_form(REGISTRATION_URL, registration_data("same.person@example.org"))

        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(MailJob.objects.filter(suppressed=True).count(), 1)

    def test_the_log_of_refusals_is_limited(self):
        with patch.object(rate_limits, "MAIL_IP_LIMITS", ((1, timedelta(hours=1)),)):
            post_form(REGISTRATION_URL, registration_data("first.person@example.org"))
            with patch.object(mail_queue, "_last_rejection_log", float("-inf")):
                with self.assertLogs("leg_platform.accounts.mail_queue", "WARNING") as logs:
                    for number in range(5):
                        post_form(REGISTRATION_URL, registration_data(f"bot{number}@example.org"))

        self.assertEqual(len(logs.output), 1)


@tag("mariadb")
class PauseAndBurstTests(TestCase):
    """A2: no new job within the pause for the same address; a burst window next to the hour."""

    def test_the_same_browser_and_address_within_the_pause_creates_no_new_job(self):
        client = Client()
        first = post_form(REGISTRATION_URL, registration_data("same.person@example.org"), client)
        variant = post_form(REGISTRATION_URL, registration_data(" SAME.person@example.org"), client)

        self.assertEqual(first["Location"], variant["Location"])
        self.assertEqual(MailJob.objects.count(), 1)
        run_mail_queue()
        self.assertEqual(len(mail.outbox), 1)

    def test_another_address_in_the_same_browser_is_a_new_request(self):
        client = Client()
        post_form(REGISTRATION_URL, registration_data("first.person@example.org"), client)
        post_form(REGISTRATION_URL, registration_data("second.person@example.org"), client)

        self.assertEqual(MailJob.objects.count(), 2)

    def test_the_password_reset_has_the_same_pause(self):
        create_active_user("known.person@example.org")
        client = Client()
        post_form(RESET_URL, reset_request_data("known.person@example.org"), client)
        post_form(RESET_URL, reset_request_data("known.person@example.org"), client)

        self.assertEqual(MailJob.objects.count(), 1)

    def test_after_the_pause_a_request_is_made_again(self):
        client = Client()
        post_form(REGISTRATION_URL, registration_data("same.person@example.org"), client)
        session = client.session
        session["mail_request_registration"]["available_at"] = 0
        session.save()

        post_form(REGISTRATION_URL, registration_data("same.person@example.org"), client)

        self.assertEqual(MailJob.objects.count(), 2)

    def test_a_script_that_asks_four_times_in_ten_minutes_gets_three_mails(self):
        # Without cookies the pause does not apply; the burst window (3 per 10 minutes) does.
        responses = [
            post_form(REGISTRATION_URL, registration_data("target.person@example.org"))
            for _ in range(4)
        ]
        run_mail_queue()

        self.assertEqual({(r.status_code, r["Location"]) for r in responses}, {(302, reverse("accounts:registration_sent"))})
        self.assertEqual(len(mail.outbox), 3)

    def test_the_burst_window_is_one_of_the_address_limits(self):
        windows = {(count, window) for count, window in rate_limits.MAIL_ADDRESS_LIMITS}

        self.assertIn((3, timedelta(minutes=10)), windows)
        self.assertIn((12, timedelta(hours=1)), windows)


@tag("mariadb")
class BotCheckTests(TestCase):
    """Stage 0: the hidden field and the signed time stamp; a hit gets the page of a person."""

    def normal(self, url, address="new.person@example.org"):
        data = registration_data(address) if url == REGISTRATION_URL else reset_request_data(address)
        return post_form(url, data, address="10.72.0.1")

    def test_the_forms_carry_the_hidden_field_and_the_stamp(self):
        for url in (REGISTRATION_URL, RESET_URL):
            with self.subTest(url=url):
                html = Client().get(url).content.decode()
                self.assertIn('class="hp-field" aria-hidden="true"', html)
                self.assertRegex(html, r'name="contact_fax_number"[^>]*tabindex="-1"')
                self.assertRegex(html, r'name="contact_fax_number"[^>]*autocomplete="off"')
                self.assertIn('name="form_started"', html)

    def test_a_filled_hidden_field_gets_the_same_page_and_no_job(self):
        for url, make in ((REGISTRATION_URL, registration_data), (RESET_URL, reset_request_data)):
            with self.subTest(url=url):
                MailJob.objects.all().delete()
                normal = post_form(url, make("a.person@example.org"), address="10.72.0.2")
                MailJob.objects.all().delete()

                caught = post_form(
                    url, make("a.person@example.org", contact_fax_number="https://spam.example"), address="10.72.0.3"
                )

                self.assertEqual((caught.status_code, caught["Location"]), (normal.status_code, normal["Location"]))
                self.assertEqual(MailJob.objects.count(), 0)

    def test_a_missing_forged_or_too_fresh_stamp_gets_the_same_page_and_no_job(self):
        normal = self.normal(REGISTRATION_URL)
        MailJob.objects.all().delete()
        cases = {
            "missing": {"email": "a.person@example.org"},
            "forged": {"email": "a.person@example.org", "form_started": "forged"},
            "other salt": {"email": "a.person@example.org", "form_started": signing.dumps(1, salt="other")},
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                caught = post_form(REGISTRATION_URL, data, address="10.72.0.4")
                self.assertEqual(caught["Location"], normal["Location"])
        with override_settings(MAIL_FORM_MINIMUM_SECONDS=300):
            fresh = post_form(REGISTRATION_URL, registration_data("a.person@example.org"), address="10.72.0.5")
        self.assertEqual(fresh["Location"], normal["Location"])
        self.assertEqual(MailJob.objects.count(), 0)

    def test_an_expired_stamp_is_a_hit(self):
        with patch.object(bot_check, "MAXIMUM_SECONDS", -1):
            response = post_form(REGISTRATION_URL, registration_data("a.person@example.org"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_a_hit_is_never_shown_as_a_refusal(self):
        response = post_form(
            REGISTRATION_URL, registration_data("a.person@example.org", contact_fax_number="x")
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotContains(self.client.get(response["Location"]), "Zu viele Anfragen")

    def test_the_pages_after_a_hit_and_after_a_request_are_the_same(self):
        pages = []
        for extra in ({}, {"contact_fax_number": "x"}):
            client = Client()
            response = post_form(
                REGISTRATION_URL, registration_data("a.person@example.org", **extra), client, "10.72.0.6"
            )
            pages.append(strip_token(client.get(response["Location"])))

        self.assertEqual(pages[0], pages[1])

    def test_the_stamp_of_a_person_is_accepted(self):
        response = self.normal(REGISTRATION_URL)

        self.assertEqual(MailJob.objects.count(), 1)
        self.assertEqual(response.status_code, 302)


@tag("mariadb")
class SessionHardeningTests(TestCase):
    """B1, B2, B3, B6, B8 and the forms (A4, A5)."""

    databases = ADDRESS_DATABASES

    def setUp(self):
        self.client = Client()
        post_form(REGISTRATION_URL, registration_data("new.person@example.org"), self.client)
        run_mail_queue()
        self.path = verification_path(mail.outbox[-1])
        self.token = VerificationToken.objects.get()

    def open_link(self):
        response = self.client.get(self.path)
        choose_method(self.client, "password", self.token.pk)
        return response

    def set_password(self):
        return self.client.post(PASSWORD_URL, password_data(registration=self.token.pk))

    def complete(self, **overrides):
        return self.client.post(
            COMPLETE_URL, completion_data(registration=self.token.pk, **overrides)
        )

    def test_opening_the_link_renews_the_session_id_and_keeps_the_data(self):
        before = self.client.session.session_key
        self.assertIn("mail_request_registration", self.client.session)

        self.open_link()

        self.assertNotEqual(self.client.session.session_key, before)
        self.assertIn("mail_request_registration", self.client.session)
        self.assertIn(REGISTRATION_SESSION_KEY, self.client.session)

    def test_opening_the_link_of_a_reset_renews_the_session_id_too(self):
        create_active_user("known.person@example.org")
        client = Client()
        post_form(RESET_URL, reset_request_data("known.person@example.org"), client)
        run_mail_queue()
        path = re.search(r"/password-reset/verify/\S+", mail.outbox[-1].body).group(0)
        before = client.session.session_key

        client.get(path)

        self.assertNotEqual(client.session.session_key, before)
        self.assertIn("mail_request_password_reset", client.session)

    def test_the_keys_of_the_registration_leave_the_session_after_the_completion(self):
        self.open_link()
        self.set_password()
        self.client.get(COMPLETE_URL)
        self.assertIn(REGISTRATION_PASSWORD_SESSION_KEY, self.client.session)
        self.assertIn(REGISTRATION_PRIVACY_SESSION_KEY, self.client.session)

        response = self.complete()

        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        session = self.client.session
        for key in (
            REGISTRATION_SESSION_KEY,
            REGISTRATION_PASSWORD_SESSION_KEY,
            REGISTRATION_PRIVACY_SESSION_KEY,
            "mail_request_registration",
        ):
            self.assertNotIn(key, session)

    def test_the_keys_leave_the_session_when_the_link_is_invalid(self):
        self.open_link()
        self.set_password()
        VerificationToken.objects.all().delete()

        response = self.client.get(COMPLETE_URL)

        self.assertRedirects(response, reverse("accounts:registration_invalid"), fetch_redirect_response=False)
        session = self.client.session
        for key in (REGISTRATION_SESSION_KEY, REGISTRATION_PASSWORD_SESSION_KEY, "mail_request_registration"):
            self.assertNotIn(key, session)

    def test_a_person_who_is_logged_in_goes_to_the_start_page_from_every_step(self):
        self.open_link()
        self.set_password()
        self.complete()

        for url in (PASSWORD_URL, COMPLETE_URL):
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), HOME_URL, fetch_redirect_response=False)
                self.assertRedirects(
                    self.client.post(url, {"registration": self.token.pk}), HOME_URL, fetch_redirect_response=False
                )

    def test_the_submit_button_of_the_details_step_is_disabled_by_an_external_script(self):
        self.open_link()
        self.set_password()

        html = self.client.get(COMPLETE_URL).content.decode()

        self.assertIn("data-submit-once", html)
        self.assertIn("js/submit-once.js", html)
        self.assertNotRegex(html, r"<script(?![^>]*\ssrc=)")

    def test_the_consent_is_stored_with_the_address_that_was_shown_when_the_form_was_opened(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"privacy_url": "https://example.org/shown"})
        self.open_link()
        self.set_password()
        self.client.get(COMPLETE_URL)
        PortalSettings.objects.update(privacy_url="https://example.org/changed")

        self.complete()

        self.assertEqual(User.objects.get().privacy_accepted_url, "https://example.org/shown")

    def test_the_event_names_the_account_by_its_number_only(self):
        self.open_link()
        self.set_password()

        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                self.complete()

        user = User.objects.get()
        entry = SecurityEvent.objects.get(event=SecurityEvent.Event.REGISTRATION_COMPLETED)
        self.assertEqual((entry.account_id, entry.actor_id), (user.pk, None))
        text = "\n".join(logs.output)
        self.assertNotIn("new.person@example.org", text)
        self.assertNotIn("Example", text)

    def test_the_username_is_set_again_to_the_verified_address(self):
        user = User.objects.get()
        data = {"username": "evil@example.org", "password1": "x", "password2": "y"}

        form = RegistrationPasswordForm(data, user=user)

        self.assertFalse(form.is_valid())
        self.assertEqual(form["username"].value(), "new.person@example.org")
        self.assertNotIn("evil@example.org", str(form["username"]))

    def test_the_username_of_the_new_password_form_is_set_again_too(self):
        user = create_active_user("known.person@example.org")
        data = {"username": "evil@example.org", "new_password1": "x", "new_password2": "y"}

        form = NewPasswordForm(user, data)

        self.assertFalse(form.is_valid())
        self.assertEqual(form["username"].value(), "known.person@example.org")

    def test_the_page_with_a_forged_username_shows_the_verified_address(self):
        self.open_link()

        response = self.client.post(
            PASSWORD_URL,
            password_data(registration=self.token.pk, username="evil@example.org", password1="x", password2="y"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "new.person@example.org")
        self.assertNotContains(response, "evil@example.org")

    def test_the_hint_has_no_example_password(self):
        hint = password_hint()

        self.assertIn("15", hint)
        for word in ("Beispiel", "example", "z. B.", "«", "\""):
            self.assertNotIn(word, hint)
        self.open_link()
        html = self.client.get(PASSWORD_URL).content.decode()
        self.assertNotIn("Beispiel", html)

    def test_every_field_of_the_details_step_has_exactly_one_message(self):
        self.open_link()
        self.set_password()

        response = self.client.post(
            COMPLETE_URL,
            {"registration": self.token.pk, "first_name": "", "last_name": "", "street": "", "house_number": "",
             "postal_code": "", "town": ""},
        )

        form = response.context["form"]
        self.assertTrue(form.errors)
        for name, errors in form.errors.items():
            with self.subTest(field=name):
                self.assertEqual(len(errors), 1, errors)
        html = response.content.decode()
        for word in ("Firmenname", "Postfach", "zwingend erforderlich"):
            self.assertNotIn(word, html)
        # The summary names the box by a short word (the checkbox has a long sentence as label).
        self.assertIn("Datenschutzerklärung:", html)

    def test_no_address_directory_note_appears_on_any_step(self):
        # D-066: nothing is said about an unavailable directory, on no page of the registration.
        self.open_link()
        unavailable = ({"status": "unavailable", "warning": "Operator text."}, None)
        with patch("leg_platform.addresses.services.directory_status", return_value=unavailable):
            password = self.client.get(PASSWORD_URL)
            self.set_password()
            details = self.client.get(COMPLETE_URL)
            self.complete()
            start = self.client.get(HOME_URL)

        text = "Ihre Adresse konnte nicht geprüft werden und wird so gespeichert, wie Sie sie eingegeben haben."
        for page in (password, details, start):
            self.assertNotContains(page, text)
            self.assertNotContains(page, "Adressverzeichnis")
            self.assertNotContains(page, "Operator text.")


def input_tag(html, field_id):
    """The whole tag of the field, so that the order of its attributes does not matter."""
    match = re.search(rf'<(?:input|select|textarea)[^>]*\bid="{field_id}"[^>]*>', html)
    assert match, f"No field {field_id}."
    return match.group(0)


@tag("mariadb")
class FormMarkupTests(TestCase):
    """Designer acceptance of phase 3: hints, descriptions and the error summary of single-field pages."""

    def test_the_hint_is_linked_with_the_field_and_with_its_error(self):
        page = Client().get(REGISTRATION_URL).content.decode()
        self.assertIn('aria-describedby="id_email_hint"', input_tag(page, "id_email"))
        self.assertIn('id="id_email_hint"', page)

        invalid = post_form(REGISTRATION_URL, registration_data("not-an-email")).content.decode()

        self.assertIn('aria-describedby="id_email_hint id_email_error"', input_tag(invalid, "id_email"))
        self.assertIn('id="id_email_error"', invalid)

    def test_the_rules_are_linked_with_the_password_field(self):
        user = create_active_user("known.person@example.org")
        client = Client()
        client.force_login(user)
        html = client.get(reverse("accounts:password_change") + "?edit=password").content.decode()
        self.assertIn("id_new_password1_rules", input_tag(html, "id_new_password1"))
        self.assertIn('id="id_new_password1_rules"', html)

    def test_the_single_field_pages_show_and_focus_the_error_summary(self):
        for url, data in (
            (REGISTRATION_URL, registration_data("not-an-email")),
            (RESET_URL, reset_request_data("not-an-email")),
        ):
            with self.subTest(url=url):
                response = post_form(url, data)
                html = response.content.decode()
                self.assertIn('class="alert alert-danger error-summary"', html)
                self.assertIn("autofocus", html)
                self.assertIn("Bitte prüfen Sie Ihre Angaben", html)
                self.assertRegex(html, r'href="#id_email"')

    def test_the_css_has_the_red_border_the_hint_size_and_no_resting_shadow(self):
        css = (settings.BASE_DIR / "leg_platform" / "static" / "css" / "portal.css").read_text(encoding="utf-8")

        self.assertRegex(css, r'\[aria-invalid="true"\][^{]*\{[^}]*border[^}]*2px')
        self.assertRegex(css, r"\.entry-card \.form-hint\s*\{[^}]*font-size:\s*14px")
        self.assertRegex(css, r"\.hp-field\s*\{[^}]*left:\s*-10000px")
        for gone in (".plain-box", ".member-menu", ".portal-header", ".tip-card", ".step-indicator"):
            self.assertNotIn(gone, css)


@tag("mariadb")
class RegistrationMailTextTests(TestCase):
    def test_the_mail_has_exactly_the_text_of_the_package_and_claims_no_single_use(self):
        with override_settings(INSTANCE_NAME="LEG-Portal"):
            post_form(REGISTRATION_URL, registration_data("new.person@example.org"))
            run_mail_queue()

        body = mail.outbox[0].body
        self.assertIn("Bitte bestätigen Sie Ihre E-Mail-Adresse mit diesem Link. Der Link ist 24 Stunden gültig.", body)
        for word in ("einmal", "Einmal", "once"):
            self.assertNotIn(word, body)

    def test_no_page_of_the_registration_claims_a_single_use_link(self):
        client = Client()
        pages = [client.get(REGISTRATION_URL), client.get(reverse("accounts:registration_invalid"))]
        post_form(REGISTRATION_URL, registration_data("new.person@example.org"), client)
        pages.append(client.get(reverse("accounts:registration_sent")))

        for page in pages:
            self.assertNotContains(page, "einmal verwendbar")
            self.assertNotContains(page, "nur einmal")
