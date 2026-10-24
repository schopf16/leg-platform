"""The entry shell of the pages without a session and the corner buttons (D-056;
LEG-Design-Vorgaben/login/VORGABEN.md and register/VORGABEN.md)."""

import re
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import translation

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user

STATIC = Path(settings.BASE_DIR) / "leg_platform" / "static"
LOGIN_URL = reverse("accounts:login")
ENTRY_PAGES = {
    "login": LOGIN_URL,
    "registration": reverse("accounts:registration"),
    "registration sent": reverse("accounts:registration_sent"),
    "password reset": reverse("accounts:password_reset"),
    "password reset sent": reverse("accounts:password_reset_sent"),
}
LOGIN_PANEL = (
    "Willkommen im Portal der LEG",
    "Hier verwalten Sie Ihr Konto und später Ihre Standorte, Energiedaten und Rechnungen.",
    "Solarstrom aus der Nachbarschaft",
    "Lokal erzeugt, gemeinsam genutzt",
)
REGISTER_PANEL = (
    "Konto erstellen",
    "Zuerst bestätigen wir Ihre E-Mail-Adresse. Danach wählen Sie, wie Sie sich anmelden, und ergänzen Ihre Angaben.",
    "Eine Anmeldung für alle Standorte",
    "Rechnungen und Mitteilungen jederzeit abrufbar",
)


def corner_controls(html):
    return all(marker in html for marker in ('id="lang-btn"', 'id="theme-btn"', 'name="language" value="fr"'))


@tag("mariadb")
class EntryShellTests(TestCase):
    def test_every_page_without_a_session_uses_the_shell_with_logo_corner_buttons_and_footer(self):
        for name, url in ENTRY_PAGES.items():
            with self.subTest(page=name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn('class="entry-split"', html)
                self.assertIn('class="entry-warm"', html)
                self.assertIn('class="entry-form"', html)
                self.assertTrue(corner_controls(html))
                self.assertIn('class="legal-links"', html)
                self.assertIn("Datenschutz", html)
                self.assertIn("Impressum", html)
                # One level-one heading: the one of the form; the panel title is not a heading.
                self.assertEqual(html.count("<h1"), 1)
                # The old Tabler card layout and the old colour mode form are gone.
                self.assertNotIn("card-body", html)
                self.assertNotIn("colour-mode", html)

    def test_the_login_panel_has_the_texts_of_the_design_package(self):
        html = self.client.get(LOGIN_URL).content.decode()
        for text in LOGIN_PANEL:
            self.assertIn(text, html)
        for text in ("Anmelden", "E-Mail-Adresse", "Passwort", "Probleme beim Anmelden?",
                     "Angemeldet bleiben", "Mit Passkey anmelden", "Neu registrieren"):
            self.assertIn(text, html)

    def test_the_registration_pages_have_the_registration_panel(self):
        for name in ("registration", "registration sent"):
            with self.subTest(page=name):
                html = self.client.get(ENTRY_PAGES[name]).content.decode()
                for text in REGISTER_PANEL:
                    self.assertIn(text, html)
        # The same page after a password reset request shows the login texts instead.
        html = self.client.get(ENTRY_PAGES["password reset sent"]).content.decode()
        self.assertIn("Willkommen im Portal der LEG", html)
        self.assertNotIn("Konto erstellen", html)

    def test_an_invalid_registration_link_uses_the_shell_and_the_registration_panel(self):
        link = reverse("accounts:registration_verify", args=["x" * 43])
        response = self.client.get(link)
        # The link only redirects (and sends no referrer); the page after it is a page of its own.
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        html = self.client.get(link, follow=True).content.decode()
        self.assertIn('class="entry-split"', html)
        self.assertIn("Konto erstellen", html)

    def test_the_lockout_page_uses_the_shell(self):
        request = RequestFactory().get("/")
        with translation.override("de-ch"):
            html = render_to_string("accounts/lockout.html", {"login_cooloff_minutes": 15}, request=request)
        self.assertIn('class="entry-split"', html)
        self.assertIn("Anmeldung vorübergehend gesperrt", html)

    def test_the_shell_is_not_used_by_the_pages_after_the_login(self):
        user = create_active_user()
        client = VerifiedClient()
        client.force_login(user)
        html = client.get(reverse("accounts:profile")).content.decode()
        self.assertNotIn('class="entry-split"', html)
        # The corner buttons are on every page, also in the signed-in part.
        self.assertTrue(corner_controls(html))

    def test_the_error_pages_have_the_corner_buttons_too(self):
        html = self.client.get("/does-not-exist/").content.decode()
        self.assertTrue(corner_controls(html))
        self.assertIn('<script src="/static/js/theme-init.js"></script>', html)

    def test_the_logo_is_an_image_with_the_instance_name_and_both_files_are_embedded(self):
        from leg_platform.branding import load_branding

        branding = load_branding({
            "INSTANCE_LOGO_LIGHT": "instance/pos.svg", "INSTANCE_LOGO_DARK": "instance/neg.svg"
        })
        with override_settings(INSTANCE_BRANDING=branding, INSTANCE_NAME="Beispiel"):
            html = self.client.get(LOGIN_URL).content.decode()
        self.assertIn('<img class="portal-logo portal-logo-light" src="/static/instance/pos.svg" alt="Beispiel">', html)
        self.assertIn('<img class="portal-logo portal-logo-dark" src="/static/instance/neg.svg" alt="Beispiel">', html)

    def test_the_login_has_no_client_side_dictionary_and_the_texts_are_rendered_by_the_server(self):
        for url in ENTRY_PAGES.values():
            html = self.client.get(url).content.decode()
            self.assertNotIn("login-texts.js", html)
            self.assertNotIn("data-i18n", html)

    def test_no_page_has_an_inline_script_or_style(self):
        for name, url in ENTRY_PAGES.items():
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                self.assertNotRegex(html, r"<script(?![^>]*\ssrc=\"/static/)")
                self.assertNotRegex(html, r"<style|\sstyle=")
                self.assertNotIn("data:image", html)

    def test_the_password_toggle_is_an_eye_button_inside_the_field_and_hidden_without_a_script(self):
        html = self.client.get(LOGIN_URL).content.decode()
        self.assertRegex(html, r'<div class="password-field">\s*<input type="password"[^>]*>\s*<button type="button" class="password-toggle[^"]*" data-password-toggle hidden')
        self.assertIn('aria-label="Passwort anzeigen"', html)


class FlagTests(SimpleTestCase):
    def test_the_flags_are_plain_svg_files_of_the_own_origin(self):
        for name in ("de", "fr", "it", "gb"):
            with self.subTest(flag=name):
                text = (STATIC / "img" / "flags" / f"{name}.svg").read_text(encoding="utf-8")
                self.assertIn("<svg", text)
                for forbidden in ("<script", "javascript:", "onload", "href=", "<image", "<foreignObject", "data:"):
                    self.assertNotIn(forbidden, text)

    def test_the_french_and_italian_flags_have_three_coloured_vertical_stripes(self):
        for name, colours in (("fr", ("#0055a4", "#ffffff", "#ef4135")), ("it", ("#009246", "#ffffff", "#ce2b37"))):
            text = (STATIC / "img" / "flags" / f"{name}.svg").read_text(encoding="utf-8")
            for colour in colours:
                self.assertIn(colour, text)


class DesignRefinementTests(SimpleTestCase):
    """The points of the design acceptance of phase 1 (D-056)."""

    CSS = (STATIC / "css" / "portal.css").read_text(encoding="utf-8")
    SCRIPT = (STATIC / "js" / "corner-controls.js").read_text(encoding="utf-8")

    def test_the_language_menu_is_a_form_and_the_script_keeps_no_language(self):
        # The language is chosen on the server: a POST form with one submit button per language.
        # The script only opens the menu; it neither stores nor translates anything.
        self.assertNotIn("leg-lang", self.SCRIPT)
        self.assertNotIn("LEG_PAGE_TEXTS", self.SCRIPT)
        self.assertNotIn("data-lang", self.SCRIPT)
        self.assertIn('".lang-menu button"', self.SCRIPT)
        self.assertIn('"Escape"', self.SCRIPT)
        self.assertRegex(self.CSS, r"\.js \.corner \.theme-btn \{[^}]*display: grid")
        # Without JavaScript the language menu is shown, the light/dark button is not.
        self.assertRegex(self.CSS, r"\.corner \{[^}]*display: flex")
        self.assertRegex(self.CSS, r"\.corner \.theme-btn \{[^}]*display: none")

    def test_the_benefit_dot_has_a_ring_in_the_panel_colour(self):
        block = re.search(r"\.entry-benefits li::before \{(.*?)\}", self.CSS, re.S).group(1)
        self.assertIn("box-shadow: 0 0 0 3px var(--panel);", block)

    def test_tablers_resting_shadows_are_switched_off_and_the_text_settings_are_neutral(self):
        block = re.search(r"\.register-clear \{(.*?)\n\}", self.CSS, re.S).group(1)
        for setting in (
            "--tblr-btn-box-shadow: none;",
            "--tblr-shadow-input: none;",
            "--tblr-body-line-height: 1.5;",
            "font-feature-settings: normal;",
            "font-variant-ligatures: normal;",
            "text-rendering: auto;",
            "-webkit-font-smoothing: auto;",
        ):
            with self.subTest(setting=setting):
                self.assertIn(setting, block)
        # The cards have no shadow either; the focus ring of a field is the one of the design.
        self.assertRegex(self.CSS, r"\.register-clear \.card \{[^}]*box-shadow: none;")
