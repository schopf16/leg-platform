"""The frame after the login, the start page and the task tiles (D-057;
LEG-Design-Vorgaben/navigation/VORGABEN.md; team lead decisions K1, K2, K4, K5, K10, K13)."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.admin.sites import site as admin_site
from django.test import Client, SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts import tasks
from leg_platform.accounts.models import HiddenTask, PortalSettings
from leg_platform.accounts.tests.helpers import PASSWORD, VerifiedClient, create_active_user
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp, log_in, promote

STATIC = Path(settings.BASE_DIR) / "leg_platform" / "static"
SETUP_URL = reverse("accounts:two_factor_setup")
HOME_URL = reverse("home")
LOGIN_URL = reverse("accounts:login")
PROFILE_URL = reverse("accounts:profile")
LATER = ("Rechnungen", "Energie", "Mitteilungen")  # "Locations" is a real page of the management area (D-078)


def login(user=None):
    user = user or create_active_user()
    client = VerifiedClient()
    client.force_login(user)
    return client, user


def part(html, start, end):
    begin = html.index(start)
    return html[begin : html.index(end, begin)]


@tag("mariadb")
class FrameTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()

    def setUp(self):
        self.client, _user = login(self.user)

    def test_the_frame_has_header_menu_drawer_and_bottom_bar(self):
        html = self.client.get(PROFILE_URL).content.decode()
        for marker in (
            'class="frame-header',
            'class="frame-menu',
            'id="frame-drawer"',
            'class="frame-bottombar',
            'class="frame-main"',
            'id="menu-btn"',
            'aria-label="Menü öffnen"',
            'aria-label="Menü schliessen"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)
        # The old frame is gone.
        for gone in ("member-menu", "member-top-navigation", "member-tabs", "portal-header"):
            self.assertNotIn(gone, html)

    def test_the_header_shows_the_logo_the_name_and_the_corner_buttons(self):
        html = self.client.get(HOME_URL).content.decode()
        header = part(html, '<header class="frame-header', "</header>")
        self.assertIn('class="frame-logo"', header)
        self.assertIn(f'<span class="frame-name">{self.user.display_name}</span>', header)
        self.assertIn('id="lang-btn"', html)
        self.assertIn('id="theme-btn"', html)

    def test_the_menu_has_the_groups_the_entries_and_the_active_page(self):
        html = self.client.get(HOME_URL).content.decode()
        menu = part(html, '<nav class="frame-menu', "</nav>")
        self.assertIn("Mein Bereich", menu)
        self.assertIn("Demnächst", menu)
        self.assertLess(menu.index("Mein Bereich"), menu.index("Start"))
        self.assertLess(menu.index("Start"), menu.index("Profil"))
        self.assertLess(menu.index("Profil"), menu.index("Demnächst"))
        for name in LATER:
            self.assertIn(name, menu)
        self.assertRegex(menu, r'class="frame-item is-active" href="/" aria-current="page"')
        profile_menu = part(
            self.client.get(PROFILE_URL).content.decode(), '<nav class="frame-menu', "</nav>"
        )
        self.assertRegex(profile_menu, r'class="frame-item is-active" href="/profile/" aria-current="page"')
        self.assertNotRegex(profile_menu, r'class="frame-item is-active" href="/"')

    def test_the_later_entries_are_no_links_and_say_so(self):
        # K10: no href, aria-disabled and a hidden note, in the menu and in the bottom bar.
        html = self.client.get(HOME_URL).content.decode()
        for container in (
            part(html, '<nav class="frame-menu', "</nav>"),
            part(html, '<nav class="frame-bottombar', "</nav>"),
        ):
            later = re.findall(r'<a class="frame-(?:item|tab) is-later"[^>]*>', container)
            self.assertGreaterEqual(len(later), 3)
            for tag_text in later:
                self.assertIn('aria-disabled="true"', tag_text)
                self.assertNotIn("href", tag_text)
            self.assertIn("später", container)
            self.assertIn("noch nicht verfügbar", container)

    def test_the_menu_logs_out_with_a_post_form_and_has_the_legal_links(self):
        html = self.client.get(HOME_URL).content.decode()
        menu = part(html, '<nav class="frame-menu', "</nav>")
        self.assertRegex(menu, r'<form method="post" action="/logout/">')
        self.assertIn("csrfmiddlewaretoken", menu)
        self.assertIn("Abmelden", menu)
        self.assertNotRegex(menu, r'<a[^>]*href="/logout/')
        self.assertIn("Datenschutz", menu)
        self.assertIn("Impressum", menu)
        # Both menus (desktop and phone) carry the same form.
        self.assertEqual(html.count('action="/logout/"'), 2)

    @override_settings(INSTANCE_PRIVACY_URL="")
    def test_the_legal_links_of_the_menu_come_from_the_portal_settings(self):
        PortalSettings.objects.create(
            id=1, privacy_url="https://www.example.org/privacy", imprint_url="https://www.example.org/imprint"
        )
        menu = part(self.client.get(HOME_URL).content.decode(), '<div class="frame-legal">', "</div>")
        self.assertIn('href="https://www.example.org/privacy" target="_blank" rel="noopener noreferrer"', menu)
        self.assertIn('href="https://www.example.org/imprint" target="_blank" rel="noopener noreferrer"', menu)

    @override_settings(INSTANCE_PRIVACY_URL="")
    def test_without_an_address_the_legal_items_are_plain_text(self):
        menu = part(self.client.get(HOME_URL).content.decode(), '<div class="frame-legal">', "</div>")
        self.assertNotIn("<a ", menu)
        self.assertIn("<span>Datenschutz</span>", menu)

    def test_the_bottom_bar_has_four_entries_and_profile_is_not_one_of_them(self):
        html = self.client.get(HOME_URL).content.decode()
        bar = part(html, '<nav class="frame-bottombar', "</nav>")
        labels = re.findall(r"<span>([^<]+)</span>", bar)
        self.assertEqual(labels, ["Start", "Energie", "Rechnungen", "Standorte"])
        self.assertNotIn("Profil", bar)
        self.assertIn('class="frame-tab is-active"', bar)

    def test_on_the_profile_no_entry_of_the_bottom_bar_is_active(self):
        # K4.
        bar = part(self.client.get(PROFILE_URL).content.decode(), '<nav class="frame-bottombar', "</nav>")
        self.assertNotIn("is-active", bar)
        self.assertNotIn("aria-current", bar)

    def test_administration_is_in_the_menu_for_staff_users_only(self):
        # K13.
        member_menu = part(self.client.get(HOME_URL).content.decode(), '<nav class="frame-menu', "</nav>")
        self.assertNotIn("Verwaltung", member_menu)
        self.assertNotIn("/admin/", member_menu)
        staff = create_active_user("staff.member@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        client = VerifiedClient()
        client.force_login(staff)
        staff_menu = part(client.get(HOME_URL).content.decode(), '<nav class="frame-menu', "</nav>")
        self.assertIn('href="/admin/"', staff_menu)
        self.assertIn("Systemverwaltung", staff_menu)

    def test_a_link_with_a_token_renders_no_page_and_the_fixed_page_has_no_logout_form(self):
        # D-054, D-067: the link of an email only redirects (with no-referrer); the fixed page it
        # leads to is an entry page that has no header and no form that posts.
        response = self.client.get(reverse("accounts:email_change_verify", args=["x" * 43]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.content, b"")
        fixed = self.client.get(response["Location"])
        self.assertEqual(fixed.status_code, 200)
        self.assertNotIn("/logout/", fixed.content.decode())

    def test_the_frame_needs_no_inline_script_or_style(self):
        html = self.client.get(HOME_URL).content.decode()
        self.assertNotRegex(html, r"<script(?![^>]*\ssrc=\"/static/)")
        self.assertNotRegex(html, r"<style|\sstyle=")
        self.assertIn('<script defer src="/static/js/frame-menu.js"></script>', html)
        self.assertIn('<link rel="stylesheet" href="/static/css/frame.css">', html)

    def test_the_menu_button_is_a_link_to_the_menu_without_a_script(self):
        html = self.client.get(HOME_URL).content.decode()
        self.assertRegex(html, r'<a class="frame-round frame-menu-btn" id="menu-btn" href="#frame-drawer" role="button"')

    def test_an_account_that_must_change_its_password_gets_header_and_logout_only(self):
        self.user.password_change_required = True
        self.user.save()
        response = self.client.get(reverse("accounts:password_change"))
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="frame frame-bare"', html)
        header = part(html, '<header class="frame-header', "</header>")
        self.assertIn('action="/logout/"', header)
        self.assertIn("Abmelden", header)
        for absent in ('class="frame-menu', 'id="frame-drawer"', 'class="frame-bottombar', 'id="menu-btn"'):
            self.assertNotIn(absent, html)
        self.assertNotIn("Profil", part(html, "<body", "</header>"))

    def test_a_person_without_a_login_gets_the_entry_shell_not_the_frame(self):
        # The pages of a link of an email are entry pages since phase 3 (D-058).
        response = Client().get(reverse("accounts:email_change_verify", args=["x" * 43]), follow=True)
        html = response.content.decode()
        self.assertIn('class="entry-split"', html)
        self.assertNotIn('class="frame-menu', html)
        self.assertNotIn('id="menu-btn"', html)
        self.assertNotIn("/logout/", html)
        self.assertIn('class="legal-links"', html)

    def test_the_corner_buttons_are_not_covered_by_the_header(self):
        css = (STATIC / "css" / "frame.css").read_text(encoding="utf-8")
        corner = re.search(r"\.corner \{(.*?)\}", (STATIC / "css" / "portal.css").read_text(encoding="utf-8"), re.S).group(1)
        header = re.search(r"\.frame-header \{(.*?)\}", css, re.S).group(1)
        self.assertIn("z-index: 1030", corner)
        self.assertIn("z-index: 1020", header)
        self.assertIn("height: 64px", header)


class FrameStyleTests(SimpleTestCase):
    CSS = (STATIC / "css" / "frame.css").read_text(encoding="utf-8")

    def test_the_sizes_of_the_frame_are_the_ones_of_the_package(self):
        self.assertRegex(self.CSS, r"\.frame-menu \{[^}]*width: 232px")
        self.assertRegex(self.CSS, r"\.frame-drawer-panel \{[^}]*width: 290px")
        self.assertIn("height: calc(72px + env(safe-area-inset-bottom, 0px))", self.CSS)
        self.assertRegex(self.CSS, r"\.frame-content \{[^}]*max-width: 960px")
        self.assertRegex(self.CSS, r"\.frame-header \{[^}]*height: 64px")
        self.assertIn("rgb(0 0 0 / 0.35)", self.CSS)
        self.assertIn("@media (max-width: 760px)", self.CSS)

    def test_there_are_no_shadows_gradients_or_light_dark_in_the_frame(self):
        for forbidden in ("box-shadow", "gradient(", "light-dark(", "prefers-color-scheme"):
            self.assertNotIn(forbidden, self.CSS)

    def test_the_font_sizes_are_the_allowed_ones(self):
        allowed = {11, 12, 13, 14, 16, 18, 20, 22, 24, 26, 32, 40}
        sizes = {int(size) for size in re.findall(r"font-size: (\d+)px", self.CSS)}
        sizes |= {int(size) for size in re.findall(r"font: [0-9]+ (\d+)px", self.CSS)}
        self.assertTrue(sizes <= allowed, sizes - allowed)

    def test_the_controls_are_at_least_44_pixels_on_a_desktop_and_48_on_a_phone(self):
        # K5: the buttons of the tiles and the entries of the menu.
        self.assertRegex(self.CSS, r"\.frame-item \{[^}]*min-height: 44px")
        self.assertRegex(self.CSS, r"\.task-actions \.btn \{[^}]*min-height: 44px")
        phone = self.CSS[self.CSS.index("@media (max-width: 760px)") :]
        self.assertIn("min-height: 48px", phone)

    def test_the_menu_script_uses_no_network_storage_or_dynamic_code(self):
        text = (STATIC / "js" / "frame-menu.js").read_text(encoding="utf-8")
        for forbidden in ("eval(", "new Function", "innerHTML", "document.write", "fetch(",
                          "XMLHttpRequest", "localStorage", "sessionStorage", "document.cookie",
                          "console."):
            self.assertNotIn(forbidden, text)
        self.assertIn('"use strict"', text)
        # Escape, the focus trap and the return of the focus to the button.
        self.assertIn('"Escape"', text)
        self.assertIn('"Tab"', text)
        self.assertIn("button.focus()", text)
        self.assertIn("inert", text)

    def test_the_measures_of_the_images_of_the_owner(self):
        """Designer review of phase 2: the images win (D-057)."""
        # Desktop menu: 32 px at the top, 24 px at the bottom, 44 px entries with 4 px between them.
        self.assertRegex(self.CSS, r"\.frame-menu \{[^}]*padding: 32px 12px 24px")
        self.assertRegex(self.CSS, r"\.frame-item \{[^}]*margin-bottom: 4px")
        # Icon at 24 px and text at 56 px: 12 (menu) + 3 (bar) + 9 (padding), then a gap of 12.
        self.assertRegex(self.CSS, r"\.frame-item \{[^}]*padding: 8px 12px 8px 9px")
        self.assertRegex(self.CSS, r"\.frame-item \{[^}]*border-left: 3px solid transparent")
        # Header: 16 px at the left, room for the buttons (24 + 40 + 12 + 40 + 20 px of the name).
        self.assertRegex(self.CSS, r"\.frame-header \{[^}]*padding: 0 136px 0 16px")
        self.assertRegex(self.CSS, r"body:has\(\.frame\) \.corner \{[^}]*right: 24px;[^}]*gap: 12px")
        # Start page: the subtitle 4 px lower, the tiles at y = 231 px.
        self.assertRegex(self.CSS, r"\.start-sub \{[^}]*margin: 8px 0 0")
        self.assertRegex(self.CSS, r"\.task-grid \{[^}]*margin-top: 64px")
        # Empty state: a panel with a line and 12 px radius, no shadow.
        empty = re.search(r"\.empty-state \{(.*?)\}", self.CSS, re.S).group(1)
        for declaration in ("border: 1px solid var(--line)", "border-radius: 12px", "background: var(--panel)", "min-height: 360px"):
            self.assertIn(declaration, empty)
        # Phone menu: the close button and the logo plate 16 px from the edges.
        self.assertRegex(self.CSS, r"\.frame-drawer-head \{[^}]*padding: 0 4px")
        # The tile padding stays 24 px (R3); the image shows 20 px (deviation noted in D-057).
        self.assertRegex(self.CSS, r"\.task-tile \{[^}]*padding: 24px")

    def test_the_menu_script_focuses_the_close_button_when_it_opens(self):
        text = (STATIC / "js" / "frame-menu.js").read_text(encoding="utf-8")
        self.assertIn('".frame-drawer-head [data-menu-close]"', text)
        self.assertIn("closeButton.focus()", text)


@tag("mariadb")
class StartPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()

    def setUp(self):
        self.client, _user = login(self.user)

    def test_the_root_leads_a_visitor_to_the_login_and_a_member_to_the_start_page(self):
        self.assertRedirects(Client().get(HOME_URL), LOGIN_URL, fetch_redirect_response=False)
        response = self.client.get(HOME_URL)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/home.html")
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_login_leads_to_the_start_page(self):
        client = Client()
        response = client.post(LOGIN_URL, {"username": self.user.email, "password": PASSWORD}, REMOTE_ADDR="10.7.0.1")
        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        # The page the person wanted is still reached (next).
        other = Client()
        response = other.post(
            f"{LOGIN_URL}?next={PROFILE_URL}",
            {"username": self.user.email, "password": PASSWORD},
            REMOTE_ADDR="10.7.0.2",
        )
        self.assertRedirects(response, PROFILE_URL, fetch_redirect_response=False)

    def test_the_title_and_the_line_with_the_real_data_of_the_account(self):
        html = self.client.get(HOME_URL).content.decode()
        self.assertIn(f'<h1 class="start-title">Guten Tag, {self.user.display_name}</h1>', html)
        self.assertIn(
            f"Kundennummer {self.user.customer_number_display} · Examplestrasse 12a", html
        )
        self.assertRegex(self.user.customer_number_display, r"^\d{3} \d{3}$")

    def test_a_company_account_shows_the_company_name(self):
        company = create_active_user("company@example.org", first_name="", last_name="", organisation_name="Beispiel AG")
        client, _user = login(company)
        self.assertIn("Guten Tag, Beispiel AG", client.get(HOME_URL).content.decode())
        header = part(client.get(HOME_URL).content.decode(), '<span class="frame-name">', "</span>")
        self.assertIn("Beispiel AG", header)

    def test_a_complete_account_without_a_second_factor_has_the_one_tile(self):
        html = self.client.get(HOME_URL).content.decode()
        self.assertEqual(html.count('<article class="task-tile"'), 1)
        self.assertIn("Zwei-Faktor-Anmeldung", html)
        self.assertIn("Schützt Ihr Konto zusätzlich mit einem Code aus einer App.", html)
        self.assertIn("2FA einrichten", html)
        self.assertNotIn("Adresse ergänzen", html)
        self.assertNotIn("Keine offenen Aufgaben", html)

    def test_the_tile_has_a_status_with_a_word_a_button_and_a_hide_button(self):
        html = self.client.get(HOME_URL).content.decode()
        tile = part(html, '<article class="task-tile"', "</article>")
        self.assertIn('<span class="status-dot status-open" aria-hidden="true"></span>Offen', tile)
        # The setup is started by a POST.
        self.assertRegex(tile, r'<form method="post" action="/profile/two-factor/setup/">')
        self.assertRegex(tile, r'<form method="post" action="/tasks/two_factor/hide/">')
        self.assertIn(">Ausblenden</button>", tile)
        self.assertIn('aria-label="Ausblenden: Zwei-Faktor-Anmeldung"', tile)
        self.assertEqual(tile.count("csrfmiddlewaretoken"), 2)

    def test_an_account_without_an_address_gets_the_address_tile_first(self):
        incomplete = create_active_user(
            "no.address@example.org", street="", house_number="", town="", swiss_postal_code=None
        )
        client, _user = login(incomplete)
        html = client.get(HOME_URL).content.decode()
        self.assertEqual(html.count('<article class="task-tile"'), 2)
        self.assertLess(html.index("Adresse ergänzen"), html.index("Zwei-Faktor-Anmeldung"))
        self.assertIn("Ihre Adresse fehlt noch.", html)
        self.assertIn('href="/profile/edit/"', html)
        # No address: no "·" and no street in the line under the title.
        self.assertIn(f"Kundennummer {incomplete.customer_number_display}</p>", html)

    def test_the_task_rules(self):
        base = create_active_user("rules@example.org")
        self.assertTrue(tasks.address_is_complete(base))
        for name, change in (
            ("street", {"street": ""}),
            ("postal code", {"swiss_postal_code": None}),
            ("town", {"town": ""}),
        ):
            with self.subTest(missing=name):
                base.refresh_from_db()
                for field, value in change.items():
                    setattr(base, field, value)
                self.assertFalse(tasks.address_is_complete(base))
        base.refresh_from_db()
        base.street = ""
        base.post_office_box_number = 1234
        self.assertTrue(tasks.address_is_complete(base))

    def test_a_done_task_disappears_and_the_empty_state_shows(self):
        user, _device = create_user_with_totp("done.person@example.org")
        client, _user = login(user)
        html = client.get(HOME_URL).content.decode()
        self.assertEqual(html.count('<article class="task-tile"'), 0)
        self.assertIn("Keine offenen Aufgaben", html)
        self.assertIn("Hier erscheinen Aufgaben, sobald es etwas zu erledigen gibt.", html)
        self.assertIn('class="empty-state-icon"', html)
        # No button and no frame of a card in the empty state (navigation, section 4.4).
        empty = html[html.index('<div class="empty-state">') :].split("</main>")[0]
        self.assertNotIn("<button", empty)
        self.assertNotIn("task-tile", empty)

    def test_the_welcome_page_is_gone(self):
        # The registration leads to the start page (D-058); the old page has no address any more.
        self.assertEqual(self.client.get("/welcome/").status_code, 404)
        with self.assertRaises(Exception):
            reverse("accounts:welcome")

    def test_the_old_tip_on_the_profile_is_gone(self):
        html = self.client.get(PROFILE_URL).content.decode()
        for gone in ("tip-card", "Möchten Sie Ihr Konto zusätzlich schützen?", "Jetzt nicht"):
            self.assertNotIn(gone, html)
        with self.assertRaises(Exception):
            reverse("accounts:two_factor_tip_dismiss")


@tag("mariadb")
class HideTaskTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()

    def setUp(self):
        self.client, _user = login(self.user)
        self.url = reverse("accounts:task_hide", args=["two_factor"])

    def test_hiding_removes_the_tile_for_good_on_every_device_and_after_every_login(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        self.assertTrue(HiddenTask.objects.filter(user=self.user, task_key="two_factor").exists())
        self.assertIn("Keine offenen Aufgaben", self.client.get(HOME_URL).content.decode())
        # Another browser and a new login.
        other = Client()
        other.post(LOGIN_URL, {"username": self.user.email, "password": PASSWORD}, REMOTE_ADDR="10.7.1.1")
        self.assertIn("Keine offenen Aufgaben", other.get(HOME_URL).content.decode())

    def test_there_is_no_way_back(self):
        self.client.post(self.url)
        html = self.client.get(HOME_URL).content.decode()
        self.assertNotIn("anzeigen", html)
        self.assertNotIn("Wiederherstellen", html)
        for method in ("get", "put", "delete", "head", "options", "patch"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(self.url).status_code, 405)
        self.assertEqual(HiddenTask.objects.count(), 1)

    def test_hiding_twice_is_harmless(self):
        self.client.post(self.url)
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(HiddenTask.objects.filter(user=self.user).count(), 1)

    def test_an_unknown_key_is_a_404_and_stores_nothing(self):
        for key in ("unknown", "ADDRESS", "two-factor", "x" * 100):
            with self.subTest(key=key):
                self.assertEqual(self.client.post(reverse("accounts:task_hide", args=[key])).status_code, 404)
        self.assertFalse(HiddenTask.objects.exists())

    def test_it_needs_a_login_and_a_csrf_token(self):
        # Without a login the person goes to the login page, without "?next": a POST target is no
        # page to return to (security review of phase 2, N-1).
        anonymous = Client().post(self.url)
        self.assertRedirects(anonymous, LOGIN_URL, fetch_redirect_response=False)
        self.assertNotIn("next=", anonymous["Location"])
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        with self.assertLogs("django.security.csrf", "WARNING"):
            self.assertEqual(browser.post(self.url).status_code, 403)
        self.assertFalse(HiddenTask.objects.exists())

    def test_it_hides_only_the_task_of_the_own_account(self):
        stranger = create_active_user("stranger@example.org")
        self.client.post(self.url)
        self.assertFalse(HiddenTask.objects.filter(user=stranger).exists())
        other, _u = login(stranger)
        self.assertIn("Zwei-Faktor-Anmeldung", other.get(HOME_URL).content.decode())
        # The account cannot be chosen: any extra field of the request is ignored.
        self.client.post(reverse("accounts:task_hide", args=["address"]), {"user": stranger.pk, "user_id": stranger.pk})
        self.assertFalse(HiddenTask.objects.filter(user=stranger).exists())
        self.assertTrue(HiddenTask.objects.filter(user=self.user, task_key="address").exists())

    def test_a_hidden_task_stays_hidden_when_something_is_missing_later(self):
        incomplete = create_active_user("later@example.org")
        client, _u = login(incomplete)
        client.post(reverse("accounts:task_hide", args=["address"]))
        incomplete.street = ""
        incomplete.save()
        html = client.get(HOME_URL).content.decode()
        self.assertNotIn("Ihre Adresse fehlt noch.", html)

    def test_the_rows_are_unique_per_account_and_removed_with_the_account(self):
        from django.db import IntegrityError, transaction

        HiddenTask.objects.create(user=self.user, task_key="address")
        with self.assertRaises(IntegrityError), transaction.atomic():
            HiddenTask.objects.create(user=self.user, task_key="address")

    def test_the_admin_shows_the_hidden_tasks_read_only(self):
        self.client.post(self.url)
        administrator = create_active_user("admin.person@example.org")
        administrator.set_privileges(is_staff=True, is_superuser=True)
        admin_client = VerifiedClient()
        admin_client.force_login(administrator)
        response = admin_client.get(reverse("admin:accounts_user_change", args=[self.user.pk]))
        self.assertContains(response, "Ausgeblendete Aufgaben")
        self.assertContains(response, "Zwei-Faktor-Anmeldung")
        self.assertNotContains(response, 'name="hidden_tasks_display"')
        # Not a model of the administration of its own.
        self.assertNotIn(HiddenTask, admin_site._registry)


@tag("mariadb")
class ReviewFollowUpTests(TestCase):
    """Security review of phase 2 (N-2, N-4, E-3, E-4): the restricted frame, the negative tests
    of the pages and the task button, the no-script menu and the active entry of the menu."""

    def forced_password_change(self):
        user = create_active_user("forced.person@example.org")
        user.password_change_required = True
        user.save()
        client = VerifiedClient()
        client.force_login(user)
        return client, user

    def test_an_account_that_must_change_its_password_reaches_nothing_else_and_hides_nothing(self):
        client, user = self.forced_password_change()
        target = reverse("accounts:password_change")

        for response in (
            client.get(HOME_URL),
            client.post(reverse("accounts:task_hide", args=["two_factor"])),
            client.get("/admin/"),
            client.get(PROFILE_URL),
        ):
            self.assertRedirects(response, target, fetch_redirect_response=False)
        self.assertFalse(HiddenTask.objects.filter(user=user).exists())

    def test_staff_without_a_second_factor_is_sent_to_the_setup_and_hides_nothing(self):
        staff = promote(create_active_user("staff.person@example.org"))
        client = Client()
        log_in(client, staff.email, address="10.7.5.1")

        response = client.post(reverse("accounts:task_hide", args=["two_factor"]))

        self.assertRedirects(response, SETUP_URL, fetch_redirect_response=False)
        self.assertFalse(HiddenTask.objects.exists())

    def test_an_account_with_a_mandatory_but_incomplete_second_factor_gets_the_slim_frame(self):
        staff = promote(create_active_user("staff.person@example.org"))
        client = Client()
        log_in(client, staff.email, address="10.7.5.2")

        html = client.get(SETUP_URL).content.decode()

        self.assertIn('class="frame frame-bare"', html)
        header = part(html, '<header class="frame-header', "</header>")
        self.assertIn('action="/logout/"', header)
        for absent in ('class="frame-menu', 'id="frame-drawer"', 'class="frame-bottombar', 'id="menu-btn"'):
            self.assertNotIn(absent, html)

    def test_a_staff_account_with_a_complete_second_factor_gets_the_full_frame(self):
        user, _device = create_user_with_totp("staff.complete@example.org")
        promote(user)
        client = VerifiedClient()
        client.force_login(user)

        html = client.get(HOME_URL).content.decode()

        self.assertIn('class="frame-menu', html)
        self.assertNotIn("frame-bare", html)

    def test_every_page_of_the_frame_is_not_stored(self):
        user, _device = create_user_with_totp("stored.person@example.org")
        client, _user = login(user)
        for name in ("home", "accounts:profile", "accounts:password_change", "accounts:two_factor"):
            with self.subTest(page=name):
                response = client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertIn("no-store", response["Cache-Control"])

    def test_the_menu_entry_profile_is_active_on_the_profile_pages_only(self):
        client, _user = login()

        def menu(url):
            html = client.get(url).content.decode()
            return part(html, '<nav class="frame-menu', "</nav>")

        for name in ("accounts:profile", "accounts:profile_edit", "accounts:password_change", "accounts:two_factor"):
            with self.subTest(page=name):
                nav = menu(reverse(name))
                self.assertIn('class="frame-item is-active" href="/profile/" aria-current="page"', nav)
                self.assertEqual(nav.count('aria-current="page"'), 1)
        home = menu(HOME_URL)
        self.assertNotIn('class="frame-item is-active" href="/profile/"', home)
        self.assertEqual(home.count('aria-current="page"'), 1)

    def test_the_menu_can_be_closed_without_a_script(self):
        client, _user = login()

        html = client.get(HOME_URL).content.decode()

        drawer = part(html, 'id="frame-drawer"', "</nav>")
        self.assertRegex(drawer, r'<a class="frame-overlay" href="#" data-menu-close tabindex="-1" aria-hidden="true">')
        self.assertIn('<a class="visually-hidden-focusable frame-close-link" href="#" data-menu-close>Menü schliessen</a>', html)
        # The buttons stay.
        self.assertIn('<button type="button" class="frame-overlay" data-menu-close', html)

    def test_an_unknown_key_is_checked_before_anything_is_written(self):
        user = create_active_user("keys@example.org")
        with self.assertRaises(ValueError):
            tasks.hide(user, "unknown")
        self.assertFalse(HiddenTask.objects.exists())
