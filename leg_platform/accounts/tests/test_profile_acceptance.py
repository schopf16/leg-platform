"""Acceptance of the designer for phase 4 (block 4c): the details of the profile cards that the
images and the decisions of the team lead fix (D-059)."""

import re
from datetime import timedelta

from django.conf import settings
from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts import emails
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    create_active_user,
    run_mail_queue,
)
from leg_platform.accounts.tests.test_profile import NEW, PROFILE_URL, SECOND, link_in, mails_to
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp

CSS = (settings.BASE_DIR / "leg_platform" / "static" / "css" / "frame.css").read_text(encoding="utf-8")
EMAIL_URL = reverse("accounts:email_change_request")


def card(html, name):
    match = re.search(rf'<section[^>]*id="card-{name}".*?</section>', html, re.S)
    assert match, f"No card {name}."
    return match.group(0)


@tag("mariadb")
class CardLayoutTests(TestCase):
    databases = {"default", "addresses", "address_import"}

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def page(self, query=""):
        return self.client.get(f"{PROFILE_URL}{query}").content.decode()

    def test_the_buttons_of_a_card_are_44_px_high_on_a_desktop_and_48_on_a_phone(self):
        self.assertRegex(CSS, r"\.profile-card \.btn \{\s*min-height: 44px;")
        self.assertRegex(CSS, r"@media \(max-width: 760px\)[^@]*\.profile-card \.btn,\s*\.profile-actions > \* \{\s*min-height: 48px;")

    def test_the_form_rows_are_one_column_on_a_phone_and_the_registration_keeps_its_pairs(self):
        self.assertRegex(CSS, r"\.profile-form \.field-pair-street,\s*\.profile-form \.field-pair-locality \{\s*grid-template-columns: minmax\(0, 1fr\);")
        # The rule is bound to the profile form: the pairs of the registration are not touched.
        self.assertNotRegex(CSS, r"(?m)^\s*\.field-pair-street \{")

    def test_the_button_to_set_up_2fa_is_wide_on_a_phone_and_primary_only_when_no_card_is_open(self):
        view = card(self.page(), "two-factor")
        self.assertRegex(view, r'<div class="profile-actions"><button type="submit" class="btn btn-primary">2FA einrichten')
        for query in ("?edit=personal", "?edit=email", "?edit=password", "?edit=delete"):
            with self.subTest(query=query):
                self.assertRegex(
                    card(self.page(query), "two-factor"),
                    r'class="btn btn-secondary-action">2FA einrichten',
                )

    def test_the_postal_code_is_called_plz_in_the_profile_only(self):
        self.assertRegex(self.page("?edit=personal"), r'<label[^>]*for="id_postal_code"[^>]*>PLZ</label>')

    def test_the_destructive_submit_buttons_are_red_outlines(self):
        user, _device = create_user_with_totp("coded.person@example.org")
        client = Client()
        client.force_login(user)
        disable = client.get(f"{PROFILE_URL}?edit=two_factor&step=disable").content.decode()
        delete = self.page("?edit=delete")

        self.assertIn('class="btn btn-danger-outline">2FA deaktivieren</button>', disable)
        self.assertIn('class="btn btn-danger-outline" data-gate="id_confirm">Konto endgültig löschen</button>', delete)
        self.assertNotIn("btn btn-danger\"", disable + delete)

    def test_the_code_field_says_2fa_authentication_code_and_has_a_placeholder(self):
        user, _device = create_user_with_totp("coded.person@example.org")
        client = Client()
        client.force_login(user)

        for query in ("?edit=email", "?edit=password", "?edit=delete", "?edit=two_factor&step=disable"):
            html = client.get(f"{PROFILE_URL}{query}").content.decode()
            with self.subTest(query=query):
                self.assertRegex(html, r'<label[^>]*for="id_otp_token"[^>]*>2FA-Authentifizierungscode</label>')
                self.assertRegex(html, r'<input[^>]*name="otp_token"[^>]*placeholder="000000"')

    def test_the_error_of_a_field_is_directly_under_it_before_the_hint(self):
        response = self.client.post(
            EMAIL_URL, {"email": NEW, "secondary_email": "", "current_password": "wrong"}
        )

        html = response.content.decode()
        field = html[html.index('id="id_current_password"'):]
        self.assertLess(field.index("invalid-feedback"), field.index("form-hint"))

    def test_the_summary_names_the_box_of_the_deletion_by_a_short_word(self):
        response = self.client.post(reverse("accounts:account_delete"), {"current_password": PASSWORD})

        self.assertContains(response, "Bestätigung: Bitte setzen Sie das Häkchen")
        self.assertNotContains(response, "Ich weiss, dass mein Konto und meine Angaben gelöscht werden und das nicht rückgängig gemacht werden kann.:")

    def test_the_password_card_says_aendern_and_the_second_address_says_what_it_is_for(self):
        html = self.page()

        self.assertRegex(card(html, "password"), r'class="btn btn-secondary-action"[^>]*>Ändern</a>')
        self.assertIn("Erhält Informationen, nicht für die Anmeldung.", card(html, "email"))
        self.user.set_secondary_email(SECOND)
        self.assertIn("Erhält Informationen, nicht für die Anmeldung.", card(self.page(), "email"))

    def test_the_two_factor_status_sits_in_the_head_of_the_card_and_the_codes_have_no_extra_box(self):
        self.assertRegex(CSS, r"\.profile-card-head \.profile-status \{\s*margin-left: auto;")
        self.assertNotRegex(CSS, r"@media \(max-width: 760px\)[^@]*\.recovery-codes \{\s*grid-template-columns: minmax\(0, 1fr\)")
        self.assertRegex(CSS, r"\.profile-card \.qr-tile-wrap \{\s*text-align: left;")
        self.assertRegex(CSS, r"\.profile-done-form \{\s*display: grid;")


@tag("mariadb")
class CardMessageTests(TestCase):
    databases = {"default", "addresses", "address_import"}

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def test_the_message_is_inside_the_card_under_its_title_and_not_above_the_page_title(self):
        page = self.client.post(
            reverse("accounts:profile_edit"),
            {
                "first_name": "Berta", "last_name": "Muster", "street": "Neue Strasse",
                "house_number": "3", "postal_code": "3063", "town": "Ittigen", "country": "CH",
            },
            follow=True,
        ).content.decode()

        personal = card(page, "personal")
        self.assertIn('role="status" tabindex="-1" data-profile-message>Gespeichert.</div>', personal)
        self.assertLess(personal.index("Persönliche Angaben"), personal.index("Gespeichert."))
        # Nothing of it above the title block, and no other card shows it.
        self.assertNotIn("Gespeichert.", page[: page.index('class="page-label"')])
        self.assertEqual(page.count("Gespeichert."), 1)

    def test_every_card_gets_its_own_message(self):
        self.client.post(
            EMAIL_URL, {"email": NEW, "secondary_email": "", "current_password": PASSWORD}, REMOTE_ADDR="10.90.0.1"
        )
        page = self.client.get(PROFILE_URL).content.decode()
        self.assertIn("Wir haben einen Bestätigungslink", card(page, "email"))

        self.client.post(reverse("accounts:email_change_cancel", args=["login"]))
        page = self.client.get(PROFILE_URL).content.decode()
        self.assertIn("Die Anfrage wurde abgebrochen.", card(page, "email"))

    def test_the_focus_script_is_loaded_and_the_cards_in_the_edit_state_are_marked(self):
        page = self.client.get(f"{PROFILE_URL}?edit=email").content.decode()

        self.assertIn("js/profile-focus.js", page)
        self.assertIn('id="card-email" aria-labelledby="card-email-title" data-state="edit"', page)
        self.assertNotIn('data-state="edit"', card(page, "personal"))
        script = (settings.BASE_DIR / "leg_platform" / "static" / "js" / "profile-focus.js").read_text(encoding="utf-8")
        for part in ("data-profile-message", 'data-state="edit"', "#card-", ".focus("):
            self.assertIn(part, script)
        self.assertNotRegex(script, r"fetch\(|XMLHttpRequest|localStorage|sessionStorage")


@tag("mariadb")
class AccessibleNameAndMailTests(TestCase):
    def test_the_cancel_buttons_name_their_address(self):
        user = create_active_user("holder.person@example.org")
        client = Client()
        client.force_login(user)
        client.post(
            EMAIL_URL, {"email": NEW, "secondary_email": SECOND, "current_password": PASSWORD}, REMOTE_ADDR="10.90.0.2"
        )

        email = card(client.get(PROFILE_URL).content.decode(), "email")

        self.assertIn(f'aria-label="Anfrage abbrechen: {NEW}"', email)
        self.assertIn(f'aria-label="Anfrage abbrechen: {SECOND}"', email)

    def test_the_mails_round_the_validity_up(self):
        for lifetime in (timedelta(hours=23, minutes=59, seconds=59), timedelta(hours=24), timedelta(hours=23, minutes=1)):
            mail.outbox.clear()
            with self.subTest(lifetime=lifetime):
                emails.send_email_change_verification("new@example.org", "token-token-token", lifetime)
                self.assertIn("24 Stunden", mail.outbox[0].body)
                self.assertNotIn("23 Stunden", mail.outbox[0].body)

    def test_a_result_page_leads_a_person_who_is_logged_in_to_the_profile(self):
        user = create_active_user("holder.person@example.org")
        client = Client()
        client.force_login(user)
        # The link is read from the mail of a request, as a person would.
        client.post(
            EMAIL_URL, {"email": user.email, "secondary_email": SECOND, "current_password": PASSWORD}, REMOTE_ADDR="10.90.0.3"
        )
        run_mail_queue()
        path = link_in(mails_to(SECOND)[0], "email-change/verify")
        client.get(path)

        done = client.post(reverse("accounts:email_change_confirm"), {})

        self.assertContains(done, "Zweite Adresse bestätigt")
        self.assertContains(done, f'href="{PROFILE_URL}"')
        self.assertContains(done, "Zum Profil")
        self.assertNotContains(done, "Zur Anmeldung")
        anonymous = Client().get(path, follow=True)
        self.assertContains(anonymous, "Zur Anmeldung")
