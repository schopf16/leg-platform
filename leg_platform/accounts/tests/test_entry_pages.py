"""The registration in three steps and the other entry pages (D-058): the step bar, the resend after
the pause, the pages of the password reset, the second step of the login, the result pages and the
emails of the registration. The texts are those of the design package of the owner
(LEG-Design-Vorgaben/register, profil), asserted in Swiss German."""

import re
import time

from django.core import mail
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts import resend
from leg_platform.accounts.models import MailJob, User, VerificationToken
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    code_for,
    create_active_user,
    run_mail_queue,
    verification_path,
)
from leg_platform.accounts.tests.test_password_reset import NEW_PASSWORD, request_reset, reset_path
from leg_platform.accounts.tests.test_registration import register
from leg_platform.accounts.tests.test_second_factor import (
    CODE_URL,
    create_user_with_totp,
    log_in,
)

SENT_URL = reverse("accounts:registration_sent")
RESEND_URL = reverse("accounts:registration_resend")
START_URL = reverse("accounts:registration")
RESET_SENT_URL = reverse("accounts:password_reset_sent")
RESET_RESEND_URL = reverse("accounts:password_reset_resend")
RESET_URL = reverse("accounts:password_reset")


def open_the_pause(client, purpose):
    """Let the pause of this browser be over (the time of the session is the server's decision)."""
    session = client.session
    key = f"mail_request_{purpose}"
    session[key] = {**session[key], "available_at": time.time() - 1}
    session.save()


@tag("mariadb")
class ResendTests(TestCase):
    def test_nothing_is_sent_during_the_pause(self):
        register(self.client)
        self.assertEqual(len(mail.outbox), 1)

        response = self.client.post(RESEND_URL)
        run_mail_queue()

        self.assertRedirects(response, SENT_URL, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_after_the_pause_the_mail_goes_to_the_address_of_this_browser_once_more(self):
        register(self.client)
        open_the_pause(self.client, resend.REGISTRATION)

        response = self.client.post(RESEND_URL)
        run_mail_queue()

        self.assertRedirects(response, SENT_URL, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[1].to, ["new.person@example.org"])
        verification_path(mail.outbox[1])
        # The new link replaces the first one (D-037) and a new pause starts.
        self.assertEqual(VerificationToken.objects.count(), 1)
        self.assertGreater(resend.pending(self.client, resend.REGISTRATION).wait, 50)
        again = self.client.post(RESEND_URL)
        run_mail_queue()
        self.assertEqual(len(mail.outbox), 2)
        self.assertRedirects(again, SENT_URL, fetch_redirect_response=False)

    def test_the_address_cannot_be_chosen_in_the_request(self):
        register(self.client)
        open_the_pause(self.client, resend.REGISTRATION)

        self.client.post(RESEND_URL, {"email": "someone.else@example.org", "address": "x@example.org"})
        run_mail_queue()

        self.assertEqual([message.to for message in mail.outbox], [["new.person@example.org"]] * 2)

    def test_without_a_pending_request_the_resend_leads_back_to_the_start(self):
        for url, start in ((RESEND_URL, START_URL), (RESET_RESEND_URL, RESET_URL)):
            with self.subTest(url=url):
                self.assertRedirects(Client().post(url), start, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_resend_is_a_post_with_a_csrf_token(self):
        register(self.client)
        for url in (RESEND_URL, RESET_RESEND_URL):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)
        browser = Client(enforce_csrf_checks=True)
        self.assertEqual(browser.post(RESEND_URL).status_code, 403)

    def test_the_page_shows_the_pause_and_a_disabled_button_until_it_is_over(self):
        register(self.client)

        waiting = self.client.get(SENT_URL).content.decode()
        seconds = int(re.search(r'data-cooldown="(\d+)"', waiting).group(1))
        self.assertTrue(55 <= seconds <= 60)
        self.assertRegex(waiting, r'<button type="submit" class="btn btn-secondary-action" data-resend-button disabled>')
        self.assertIn("js/resend-cooldown.js", waiting)
        self.assertNotRegex(waiting, r"<script(?![^>]*\ssrc=)")

        open_the_pause(self.client, resend.REGISTRATION)
        ready = self.client.get(SENT_URL).content.decode()
        self.assertIn('data-cooldown="0"', ready)
        self.assertNotIn("data-resend-button disabled", ready)
        self.assertRegex(ready, r"data-resend-note hidden")

    def test_a_tampered_session_value_names_no_address_and_sends_nothing(self):
        register(self.client)
        for bad in ("text", ["a"], {"address": 5, "available_at": 0}, {"address": "", "available_at": 0},
                    {"address": "a@example.org", "available_at": "soon"}, {"address": "a@example.org", "available_at": True}):
            with self.subTest(value=bad):
                session = self.client.session
                session["mail_request_registration"] = bad
                session.save()
                page = self.client.get(SENT_URL)
                self.assertContains(page, "Wir haben eine E-Mail gesendet.")
                self.assertRedirects(self.client.post(RESEND_URL), START_URL, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_password_reset_has_the_same_pause(self):
        user = create_active_user("known.person@example.org")
        request_reset(user.email, client=self.client)
        self.assertEqual(len(mail.outbox), 1)

        self.client.post(RESET_RESEND_URL)
        run_mail_queue()
        self.assertEqual(len(mail.outbox), 1)

        open_the_pause(self.client, resend.PASSWORD_RESET)
        response = self.client.post(RESET_RESEND_URL)
        run_mail_queue()

        self.assertRedirects(response, RESET_SENT_URL, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 2)
        reset_path(mail.outbox[1])

    def test_the_server_limits_apply_to_every_resend(self):
        register(self.client)
        for _ in range(15):
            open_the_pause(self.client, resend.REGISTRATION)
            self.client.post(RESEND_URL, REMOTE_ADDR="10.20.0.1")
        run_mail_queue()
        # The limits per address (12 per hour, D-038) stop the mails; the page stays the same.
        self.assertLessEqual(len(mail.outbox), 12)
        self.assertEqual(self.client.get(SENT_URL).status_code, 200)


@tag("mariadb")
class StepBarTests(TestCase):
    databases = {"default", "addresses"}

    def bar(self, html):
        match = re.search(r'role="progressbar"[^>]*aria-valuenow="(\d)".*?step-fill step-fill-(\d)', html, re.S)
        self.assertTrue(match, "No step bar.")
        return int(match.group(1)), int(match.group(2))

    def test_the_bar_fills_one_third_two_thirds_and_all_without_a_style_attribute(self):
        pages = [self.client.get(START_URL)]
        register(self.client)
        pages.append(self.client.get(SENT_URL))
        self.client.get(verification_path(mail.outbox[0]))
        pages.append(self.client.get(reverse("accounts:registration_password")))
        token = VerificationToken.objects.get()
        self.client.post(
            reverse("accounts:registration_password"),
            {"username": "", "password1": PASSWORD, "password2": PASSWORD, "registration": token.pk},
        )
        pages.append(self.client.get(reverse("accounts:registration_complete")))

        steps = []
        for page in pages:
            html = page.content.decode()
            steps.append(self.bar(html))
            self.assertNotRegex(html, r"(?i)\sstyle\s*=")
        # The two screens of step 1 (the form and "check your email") share the first third.
        self.assertEqual(steps, [(1, 1), (1, 1), (2, 2), (3, 3)])
        for page, text in zip(pages, ("Schritt 1 von 3", "Schritt 1 von 3", "Schritt 2 von 3", "Schritt 3 von 3")):
            self.assertContains(page, text)

    def test_the_css_has_the_three_fills_as_classes(self):
        from pathlib import Path

        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "leg_platform" / "static" / "css" / "portal.css").read_text(encoding="utf-8")
        for name, width in (("1", "33.333%"), ("2", "66.667%"), ("3", "100%")):
            self.assertRegex(css, rf"\.step-fill-{name} \{{ width: {re.escape(width)}; \}}")
        self.assertRegex(css, r"\.step-bar \{[^}]*height: 4px")


@tag("mariadb")
class PasswordResetPagesTests(TestCase):
    def test_the_request_page_has_the_texts_of_the_package(self):
        html = self.client.get(RESET_URL).content.decode()

        self.assertIn("Passwort zurücksetzen", html)
        self.assertIn("Wir senden Ihnen einen Link. Er ist 24 Stunden gültig.", html)
        self.assertIn(">Link senden</button>", html)
        self.assertIn("Zur Anmeldung", html)

    def test_the_new_password_page_follows_the_pattern_of_the_registration(self):
        user = create_active_user("known.person@example.org")
        request_reset(user.email)
        self.client.get(reset_path(mail.outbox[0]))

        page = self.client.get(reverse("accounts:password_reset_set"))
        html = page.content.decode()

        self.assertContains(page, "Passwort festlegen")
        self.assertRegex(html, r'<input[^>]*name="username"[^>]*autocomplete="username"|<input[^>]*autocomplete="username"[^>]*name="username"')
        self.assertIn("known.person@example.org", html)
        self.assertIn('class="password-rules"', html)
        self.assertIn("Mindestens 15 Zeichen.", html)
        self.assertIn("Passwort wiederholen", html)
        self.assertNotIn("Bitte das selbe Passwort", html)
        self.assertNotIn("Schritt", html)

    def test_the_new_password_is_saved_and_the_person_goes_to_the_login(self):
        user = create_active_user("known.person@example.org")
        request_reset(user.email)
        self.client.get(reset_path(mail.outbox[0]))

        response = self.client.post(
            reverse("accounts:password_reset_set"),
            {"username": "changed@example.org", "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        )

        self.assertRedirects(response, reverse("accounts:login"))
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertEqual(user.email, "known.person@example.org")

    def test_an_invalid_link_leads_to_a_page_with_the_address_field(self):
        page = self.client.get(reverse("accounts:password_reset_verify", args=["x" * 43]), follow=True)

        self.assertContains(page, "Link nicht mehr gültig")
        self.assertContains(page, "Neuen Link senden")
        self.assertContains(page, f'<form method="post" action="{RESET_URL}"')
        self.assertContains(page, "Zur Anmeldung")


@tag("mariadb")
class SecondStepOfTheLoginTests(TestCase):
    def test_the_page_has_the_texts_of_the_package(self):
        user, _device = create_user_with_totp("holder.person@example.org")
        log_in(self.client, user.email, address="10.30.0.1")

        page = self.client.get(CODE_URL)

        self.assertContains(page, "Anmeldung bestätigen")
        self.assertContains(page, "Code aus der App")
        self.assertContains(page, "Haben Sie die App nicht zur Hand? Geben Sie einen Wiederherstellungscode ein.")
        self.assertContains(page, 'class="entry-split"')
        self.assertContains(page, 'autocomplete="one-time-code"')

    def test_a_wrong_code_keeps_the_page_and_the_message(self):
        user, _device = create_user_with_totp("holder.person@example.org")
        log_in(self.client, user.email, address="10.30.0.2")

        page = self.client.post(CODE_URL, {"otp_token": "000000"}, REMOTE_ADDR="10.30.0.2")

        self.assertContains(page, "Der Code ist falsch, abgelaufen oder wurde schon verwendet.")

    def test_the_right_code_logs_in_and_leads_to_the_start_page(self):
        user, device = create_user_with_totp("holder.person@example.org")
        log_in(self.client, user.email, address="10.30.0.3")

        response = self.client.post(CODE_URL, {"otp_token": code_for(device)}, REMOTE_ADDR="10.30.0.3")

        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)


@tag("mariadb")
class ResultPagesTests(TestCase):
    def test_the_invalid_email_change_link_says_nothing_changed(self):
        page = self.client.get(reverse("accounts:email_change_verify", args=["x" * 43]), follow=True)

        self.assertContains(page, "Link nicht mehr gültig")
        self.assertContains(page, "Dieser Link kann nicht mehr verwendet werden. Falls Sie die Änderung soeben bestätigt haben, melden Sie sich mit der neuen Adresse an.")
        self.assertNotContains(page, "wurde nicht geändert")
        self.assertContains(page, f'href="{reverse("accounts:login")}">Zur Anmeldung</a>')
        self.assertContains(page, "Mein Konto")
        self.assertContains(page, "Ihre Angaben im LEG-Portal.")
        # The fixed page is a normal page: no token in its URL, the project default policy.
        self.assertEqual(page.redirect_chain, [(reverse("accounts:email_change_invalid"), 302)])
        self.assertEqual(page["Referrer-Policy"], "same-origin")

    def test_the_prepared_pages_render_with_the_texts_of_the_package(self):
        request = RequestFactory().get("/")
        from django.contrib.auth.models import AnonymousUser

        request.user = AnonymousUser()
        for template, address_text in (
            ("accounts/email_change_done.html", "Ihre neue E-Mail-Adresse"),
            ("accounts/email_second_confirmed.html", "Ihre zweite E-Mail-Adresse"),
            ("accounts/email_change_stopped.html", None),
        ):
            with self.subTest(template=template):
                html = render_to_string(template, {"new_email": "new.person@example.org"}, request)
                self.assertIn("Mein Konto", html)
                if address_text:
                    self.assertIn(address_text, html)
                    self.assertIn("new.person@example.org", html)
        stopped = render_to_string("accounts/email_change_stopped.html", {}, request)
        self.assertIn("Änderung gestoppt", stopped)
        self.assertIn("Die Änderung Ihrer E-Mail-Adresse wurde gestoppt. Ihre bisherigen Adressen bleiben gültig.", stopped)

    @override_settings(INSTANCE_WEBSITE_URL="https://www.example.org/")
    def test_the_removal_of_the_second_factor_pages_use_the_entry_shell(self):
        page = self.client.get(reverse("accounts:two_factor_reset_verify", args=["x" * 43]), follow=True)

        self.assertContains(page, 'class="entry-split"')
        self.assertContains(page, "Link nicht mehr gültig")
        self.assertNotContains(page, "Link ungültig")
        # A real way to the operator: the website of the instance, not the name of the portal.
        self.assertContains(page, 'href="https://www.example.org/"')
        self.assertContains(page, "wenden Sie sich an die Betreiber")
        self.assertNotContains(page, "wenden Sie sich an LEG-Portal")
        # The same term as the emails of the portal ("die Betreiber"), not "die LEG".
        self.assertNotContains(page, "an die LEG")

    @override_settings(INSTANCE_WEBSITE_URL="")
    def test_the_invalid_removal_link_without_a_website_still_names_the_operator(self):
        page = self.client.get(reverse("accounts:two_factor_reset_verify", args=["x" * 43]), follow=True)

        self.assertContains(page, "wenden Sie sich an die Betreiber")


@tag("mariadb")
class RegistrationEmailTests(TestCase):
    @override_settings(INSTANCE_NAME="LEG-Portal")
    def test_a_new_address_gets_the_text_of_the_package_with_the_link(self):
        register(self.client)

        message = mail.outbox[0]
        self.assertEqual(message.subject, "Ihre Registrierung beim LEG-Portal")
        self.assertIn("Bitte bestätigen Sie Ihre E-Mail-Adresse mit diesem Link. Der Link ist 24 Stunden gültig.", message.body)
        self.assertRegex(message.body, r"Registrierung fortsetzen:\n\nhttps?://\S+/register/verify/\S+")
        self.assertNotIn("ß", message.subject + message.body)

    @override_settings(INSTANCE_NAME="LEG-Portal")
    def test_an_address_with_an_account_gets_the_text_of_the_package_with_the_login(self):
        create_active_user("taken.person@example.org")

        register(self.client, "taken.person@example.org")

        message = mail.outbox[0]
        self.assertEqual(message.subject, "Ihr Konto beim LEG-Portal")
        self.assertIn(
            "Für diese Adresse besteht bereits ein Konto. Sie können sich hier anmelden. "
            "Falls Sie kein Konto erstellt haben, können Sie diese E-Mail ignorieren.",
            message.body,
        )
        self.assertRegex(message.body, r"Zur Anmeldung:\n\nhttps?://\S+/login/")
        self.assertNotIn("/register/verify/", message.body)

    def test_the_two_mails_can_still_be_edited_in_the_administration(self):
        from leg_platform.accounts import email_texts

        for key in (email_texts.Key.REGISTRATION_VERIFICATION, email_texts.Key.REGISTRATION_EXISTING_ACCOUNT):
            with self.subTest(key=key):
                definition = email_texts.DEFINITIONS[key]
                self.assertIn("%(instance_name)s", definition.default_subject())
