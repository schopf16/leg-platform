"""The menu and the bottom bar of a customer (MENU-STANDARD.md, START-SEITE.md section 7; D-057): the entries and their order,
the active state, the entries that are "later", the unchanged menu of a staff account."""

import re

from django.test import TestCase, tag
from django.urls import reverse

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
            "Mein Bereich", "Start", "Profil", "Teilnahme", "Gebäude", "Startdatum", "Nachbarschaft",
            "Information", "Gesellschaftsvertrag", "Frage und Antwort", "Veranstaltung", "Demnächst", "Rechnung",
            "Energie", "Mitteilung", "Ausloggen", "Datenschutz", "Impressum",
        ]
        positions = [menu.index(label) for label in order]
        self.assertEqual(positions, sorted(positions))
        # The names of the group "Coming next" are singular (owner, 2026-10-19).
        for plural in ("Rechnungen", "Mitteilungen"):
            self.assertNotIn(plural, menu)

    def test_entries_without_a_page_are_no_links_and_say_later(self):
        menu = menu_of(self.client.get(HOME))
        later = re.findall(r'<a class="frame-(?:item|sub-item) is-later"[^>]*>', menu)
        # Start date, neighbourhood, questions, event, invoice, energy, message (the contract page is built).
        self.assertEqual(len(later), 7)
        for tag_text in later:
            self.assertIn('aria-disabled="true"', tag_text)
            self.assertNotIn("href", tag_text)
        self.assertIn("später", menu)
        self.assertIn("noch nicht verfügbar", menu)

    def test_the_two_real_entries_are_links_to_the_pages(self):
        menu = menu_of(self.client.get(HOME))
        self.assertIn(f'href="{BUILDINGS}"', menu)
        # The contract is ONE entry, "Articles of association" under Information (D-108: the entry "Contract" under
        # Participation led to the same page and is gone); it is the current entry on the contract page.
        self.assertEqual(menu.count(f'href="{reverse("customer:contract")}"'), 1)
        self.assertRegex(
            menu, rf'<a class="frame-sub-item" href="{reverse("customer:contract")}">Gesellschaftsvertrag</a>'
        )
        participation = menu[menu.index("Teilnahme"):menu.index("Information")]
        self.assertNotIn("Vertrag", participation)
        self.assertIn('<form method="post" action="/logout/">', menu)
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

    def test_the_wizard_marks_participation_as_current(self):
        application, _building = draft_with_building(self.mine)
        menu = menu_of(self.client.get(reverse("customer:application_metering_points", args=[application.pk])))
        self.assertIn(f'<a class="frame-sub-item is-active" href="{BUILDINGS}" aria-current="page">', menu)
        self.assertIn('class="frame-item frame-parent is-active"', menu)

    def test_the_bottom_bar_has_start_participation_information_and_profile(self):
        bar = bar_of(self.client.get(HOME))
        self.assertEqual(re.findall(r"<span>([^<]+)</span>", bar), ["Start", "Teilnahme", "Information", "Profil"])
        self.assertIn('class="frame-tab is-active" href="/" aria-current="page"', bar)
        self.assertNotIn("is-later", bar)
        profile_bar = bar_of(self.client.get(reverse("accounts:profile")))
        self.assertEqual(profile_bar.count("is-active"), 1)
        self.assertIn(f'<a class="frame-tab is-active" href="{reverse("accounts:profile")}"', profile_bar)
        self.assertIn('is-active" href="%s"' % BUILDINGS, bar_of(self.client.get(BUILDINGS)))

    def test_the_menu_is_in_the_drawer_of_the_phone_too(self):
        html = self.client.get(BUILDINGS).content.decode()
        self.assertEqual(html.count("frame-parent"), 4)  # two parents, desktop menu and drawer
        self.assertEqual(html.count('action="/logout/"'), 2)

    def test_a_staff_account_keeps_its_menu_and_its_bottom_bar(self):
        staff_client = login(self.staff)
        menu = menu_of(staff_client.get(HOME))
        for label in ("Rechnungen", "Energie", "Mitteilungen", "Verwaltung"):
            self.assertIn(label, menu)
        for gone in ("Teilnahme", "Gesellschaftsvertrag", "frame-sub"):
            self.assertNotIn(gone, menu)
        bar = bar_of(staff_client.get(HOME))
        # The bar of the staff changed with the staff walkthrough (W5-11): Start, Overview, Processes and the menu.
        self.assertEqual(re.findall(r"<span>([^<]+)</span>", bar), ["Start", "Übersicht", "Vorgänge", "Menü"])

    def test_the_menu_has_no_inline_script_or_style(self):
        html = self.client.get(BUILDINGS).content.decode()
        self.assertNotRegex(html, r"(?i)<script(?![^>]*\ssrc=\")")
        self.assertNotRegex(html, r'\sstyle="')
