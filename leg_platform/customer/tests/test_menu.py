"""The menu and the bottom bar of a customer (MENU-STANDARD.md, START-SEITE.md section 7; D-057): the entries and their order,
the active state, the entries that are "later", the unchanged menu of a staff account."""

import re

from django.test import TestCase, tag
from django.urls import NoReverseMatch, Resolver404, resolve, reverse

from leg_platform.community.tests.helpers import staff_user
from leg_platform.customer.tests.helpers import (
    BUILDINGS,
    HOME,
    customer,
    draft_with_building,
    login,
    publish_contract,
)


def part(html, start, end):
    begin = html.index(start)
    return html[begin : html.index(end, begin)]


def menu_of(response):
    return part(response.content.decode(), '<nav class="frame-menu', "</nav>")


def bar_of(response):
    return part(response.content.decode(), '<nav class="frame-bottombar', "</nav>")


@tag("mariadb")
class CustomerMenuTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.menu@example.org")
        cls.staff = staff_user("staff.menu@example.org")
        publish_contract(cls.staff)

    def setUp(self):
        self.client = login(self.mine)

    def test_the_entries_and_their_order(self):
        menu = menu_of(self.client.get(HOME))
        order = [
            "Mein Bereich", "Start", "Gebäude", "Energie", "Rechnung", "Mitteilung", "Nachbarschaft", "Profil",
            "Information", "Gesellschaftsvertrag", "Frage und Antwort", "Veranstaltung", "Ausloggen", "Datenschutz",
            "Impressum",
        ]
        positions = [menu.index(label) for label in order]
        self.assertEqual(positions, sorted(positions))
        # D-130: no group "Coming next" and no group "Participation" in the customer menu; the names are singular.
        for gone in ("Demnächst", "Teilnahme", "Rechnungen", "Mitteilungen", "Für Mitarbeitende"):
            self.assertNotIn(gone, menu)

    def test_entries_without_a_page_are_no_links_and_say_later(self):
        menu = menu_of(self.client.get(HOME))
        later = re.findall(r'<a class="frame-(?:item|sub-item) is-later"[^>]*>', menu)
        # Energy, invoice, message, neighbourhood, questions, event; the contract page is built.
        self.assertEqual(len(later), 6)
        for tag_text in later:
            self.assertIn('aria-disabled="true"', tag_text)
            self.assertNotIn("href", tag_text)
        self.assertIn("später", menu)
        self.assertIn("noch nicht verfügbar", menu)

    def test_the_neighbourhood_is_deferred_in_both_menus_and_its_route_is_removed(self):
        html = self.client.get(HOME).content.decode()
        entry = '<a class="frame-item is-later" role="link" aria-disabled="true">'
        pattern = re.escape(entry) + r'\s*<svg[^>]*><use href="#i-persons"/></svg><span>Nachbarschaft</span>'
        self.assertEqual(len(re.findall(pattern, html)), 2)
        self.assertNotIn('href="/participation/neighbourhood/"', html)
        self.assertNotIn("leaflet", html)
        with self.assertRaises(NoReverseMatch):
            reverse("customer:neighbourhood")
        with self.assertRaises(Resolver404):
            resolve("/participation/neighbourhood/")
        for client in (self.client, login(self.staff), self.client_class()):
            for method in ("get", "post"):
                with self.subTest(client=client, method=method):
                    response = getattr(client, method)("/participation/neighbourhood/")
                    self.assertEqual(response.status_code, 404)
                    self.assertIn("no-store", response["Cache-Control"])
                    self.assertIn("img-src 'self'", response["Content-Security-Policy"])
                    self.assertNotIn("wmts.geo.admin.ch", response["Content-Security-Policy"])

    def test_the_two_real_entries_are_links_to_the_pages(self):
        menu = menu_of(self.client.get(HOME))
        self.assertIn(f'href="{BUILDINGS}"', menu)
        # The contract is ONE entry, "Articles of association" under Information (D-108: the entry "Contract" under
        # Participation led to the same page and is gone); it is the current entry on the contract page.
        self.assertEqual(menu.count(f'href="{reverse("customer:contract")}"'), 1)
        self.assertRegex(
            menu, rf'<a class="frame-sub-item" href="{reverse("customer:contract")}">Gesellschaftsvertrag</a>'
        )
        participation = menu[menu.index("Gebäude"):menu.index("Information")]
        self.assertNotIn("Vertrag", participation)
        self.assertIn('<form method="post" action="/logout/" data-render-as-keep>', menu)
        self.assertNotIn("Verwaltung", menu)

    def test_exactly_one_entry_is_current_and_it_is_the_page_of_the_request(self):
        draft_with_building(self.mine)
        pages = (
            (HOME, 'href="/"'), (reverse("accounts:profile"), 'href="/profile/"'), (BUILDINGS, f'href="{BUILDINGS}"'),
            (reverse("customer:application_start"), f'href="{BUILDINGS}"'),
            (reverse("customer:contract"), f'href="{reverse("customer:contract")}"'),
            (reverse("customer:deregistration_start"), f'href="{BUILDINGS}"'),
        )
        for path, expected in pages:
            with self.subTest(path=path):
                menu = menu_of(self.client.get(path))
                current = re.findall(r'<a class="frame-(?:item|sub-item)[^"]*" (href="[^"]+") aria-current="page"', menu)
                self.assertEqual(current, [expected])

    def test_the_wizard_marks_buildings_as_current(self):
        application, _building = draft_with_building(self.mine)
        menu = menu_of(self.client.get(reverse("customer:application_metering_points", args=[application.pk])))
        self.assertIn(f'<a class="frame-item is-active" href="{BUILDINGS}" aria-current="page">', menu)
        self.assertNotIn("frame-parent is-active", menu)  # Information is not the current group

    def test_the_bottom_bar_has_start_buildings_information_and_profile(self):
        bar = bar_of(self.client.get(HOME))
        self.assertEqual(re.findall(r"<span>([^<]+)</span>", bar), ["Start", "Gebäude", "Information", "Profil"])
        self.assertIn('class="frame-tab is-active" href="/" aria-current="page"', bar)
        self.assertNotIn("is-later", bar)
        profile_bar = bar_of(self.client.get(reverse("accounts:profile")))
        self.assertEqual(profile_bar.count("is-active"), 1)
        self.assertIn(f'<a class="frame-tab is-active" href="{reverse("accounts:profile")}"', profile_bar)
        self.assertIn('is-active" href="%s"' % BUILDINGS, bar_of(self.client.get(BUILDINGS)))

    def test_the_menu_is_in_the_drawer_of_the_phone_too(self):
        html = self.client.get(BUILDINGS).content.decode()
        self.assertEqual(html.count("frame-parent"), 2)  # one parent (Information), desktop menu and drawer
        self.assertEqual(html.count('action="/logout/"'), 2)

    def test_a_staff_account_has_the_menu_of_a_customer_in_my_area_and_its_own_menu_in_the_staff_area(self):
        # D-113, D-131: two workspaces. The menu of My area is the same for staff and customers; the way into the staff
        # area is the button in the header.
        staff_client = login(self.staff)
        page = staff_client.get(HOME)
        menu = menu_of(page)
        for label in ("Gebäude", "Gesellschaftsvertrag", "Rechnung"):
            self.assertIn(label, menu)
        for gone in ("Rechnungen", "Mitteilungen", "Für Mitarbeitende", "Mitarbeitenden-Bereich", "Demnächst",
                     'frame-group">Verwaltung<'):
            self.assertNotIn(gone, menu)
        header = part(page.content.decode(), '<header class="frame-header', "</header>")
        self.assertIn('class="btn btn-secondary-action frame-staff-btn"', header)
        self.assertIn(f'href="{reverse("management:overview")}"', header)
        self.assertNotIn("frame-staff-btn", part(self.client.get(HOME).content.decode(), '<header class="frame-header', "</header>"))
        staff_menu = menu_of(staff_client.get(reverse("management:overview")))
        for label in ("Rechnungen", "Energie", "Mitteilungen", "Verwaltung", "Zurück zu Mein Bereich"):
            self.assertIn(label, staff_menu)
        for gone in ("frame-parent", "Gesellschaftsvertrag", "frame-sub"):
            self.assertNotIn(gone, staff_menu)
        # The bar follows the workspace: the one of a customer in My area, the staff bar in the staff area.
        self.assertEqual(
            re.findall(r"<span>([^<]+)</span>", bar_of(staff_client.get(HOME))),
            ["Start", "Gebäude", "Information", "Profil"],
        )
        self.assertEqual(
            re.findall(r"<span>([^<]+)</span>", bar_of(staff_client.get(reverse("management:overview")))),
            ["Mein Bereich", "Übersicht", "Vorgänge", "Menü"],
        )

    def test_the_menu_has_no_inline_script_or_style(self):
        html = self.client.get(BUILDINGS).content.decode()
        self.assertNotRegex(html, r"(?i)<script(?![^>]*\ssrc=\")")
        self.assertNotRegex(html, r'\sstyle="')
