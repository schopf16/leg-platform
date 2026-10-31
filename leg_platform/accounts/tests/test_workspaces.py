"""The two workspaces of the logged-in portal (D-113): "My area" for everybody (staff and system administrators take part
like every other account) and the "Staff area" (the management area and the system administration) that opens in a
second browser tab. Covers the workspace rule, the menu per category and workspace, the header chip and the title of the
tab, the bottom bars, the start page, the administration shell and the catalogues. The rules of access themselves are
tested elsewhere (``management/tests/test_access.py``, ``customer/tests/test_access.py``)."""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import translation

from leg_platform.accounts.tests.helpers import instance_mail_addresses
from leg_platform.accounts.models import PortalSettings
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.context_processors import frame_workspace

STATIC = Path(settings.BASE_DIR) / "leg_platform" / "static"
TEMPLATES = Path(settings.BASE_DIR) / "leg_platform" / "templates"
HOME = reverse("home")
OVERVIEW = reverse("management:overview")
PERSONS = reverse("management:persons")
PROCESSES = reverse("management:processes")
ADMIN_INDEX = reverse("admin:index")
PROFILE = reverse("accounts:profile")
BUILDINGS = reverse("customer:buildings")
CHIP = '<span class="frame-workspace">Mitarbeitenden-Bereich</span>'
BUTTON = re.compile(r'<a class="btn btn-secondary-action frame-staff-btn".*?</a>', re.S)
BACK = re.compile(r'<div class="frame-back">\s*<a class="frame-item frame-item-two-line" href="/">.*?</a>\s*</div>', re.S)


def client_of(user):
    client = VerifiedClient()
    client.force_login(user)
    return client


def part(html, start, end):
    begin = html.index(start)
    return html[begin : html.index(end, begin)]


def menu_of(response):
    return part(response.content.decode(), '<nav class="frame-menu', "</nav>")


def bar_of(response):
    return part(response.content.decode(), '<nav class="frame-bottombar', "</nav>")


def labels(html):
    return re.findall(r"<span>([^<]+)</span>", html)


def request_for(view_name, user):
    request = RequestFactory().get("/")
    request.user = user
    request.resolver_match = SimpleNamespace(view_name=view_name) if view_name is not None else None
    return request


class AccountOf(SimpleNamespace):
    """An account as the workspace rule sees it (no database)."""

    def __init__(self, staff=False, active=True, authenticated=True):
        super().__init__(is_staff=staff, is_active=active, is_authenticated=authenticated)


class WorkspaceRuleTests(SimpleTestCase):
    """``frame_workspace``: derived from the resolved view name; the staff workspace only for a management user."""

    def test_management_and_administration_pages_are_the_staff_workspace(self):
        for name in ("management:overview", "management:persons_detail", "management:processes_application", "admin:index",
                     "admin:accounts_user_changelist", "admin:logs"):
            with self.subTest(name=name):
                self.assertEqual(frame_workspace(request_for(name, AccountOf(staff=True))), "staff")

    def test_every_other_page_is_my_area(self):
        for name in ("home", "accounts:profile", "accounts:passkeys", "accounts:password_change", "customer:buildings",
                     "customer:deregistration_start", "accounts:account_delete", "", None):
            with self.subTest(name=name):
                self.assertEqual(frame_workspace(request_for(name, AccountOf(staff=True))), "participant")

    def test_an_account_that_may_not_use_the_staff_area_never_gets_the_staff_workspace(self):
        for user in (AccountOf(), AccountOf(staff=True, active=False), AccountOf(authenticated=False)):
            with self.subTest(user=vars(user)):
                self.assertEqual(frame_workspace(request_for("management:overview", user)), "participant")
                self.assertEqual(frame_workspace(request_for("admin:index", user)), "participant")

    def test_a_namespace_that_only_looks_alike_is_no_staff_page(self):
        for name in ("management", "managementx:overview", "administration:index", "customer:management:x"):
            with self.subTest(name=name):
                self.assertEqual(frame_workspace(request_for(name, AccountOf(staff=True))), "participant")


@tag("mariadb")
class MenuTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = create_active_user("ws.customer@example.org")
        cls.staff = create_active_user("ws.staff@example.org")
        cls.staff.set_privileges(is_staff=True, is_superuser=False)
        cls.administrator = create_active_user("ws.administrator@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)

    # ---- My area -------------------------------------------------------------------------------------------------

    def test_the_menu_of_a_customer_has_no_staff_entry_and_no_staff_group(self):
        for url in (HOME, PROFILE, BUILDINGS):
            with self.subTest(url=url):
                response = client_of(self.customer).get(url)
                menu = menu_of(response)
                self.assertNotIn("Für Mitarbeitende", menu)
                self.assertNotIn("Mitarbeitenden-Bereich", menu)
                self.assertNotIn("/management/", menu)
                self.assertNotIn("/admin/", menu)
                self.assertNotIn("frame-item-two-line", menu)
                # The groups of the staff workspace are not here for anybody.
                self.assertNotIn('<p class="frame-group">Verwaltung</p>', menu)
                self.assertNotIn('<p class="frame-group">System</p>', menu)
                self.assertIn("Gebäude", menu)
                self.assertIn("Information", menu)
                self.assertNotIn("frame-staff-btn", response.content.decode())
                self.assertNotIn("Mitarbeitenden-Bereich", response.content.decode())

    def test_my_area_has_the_staff_button_in_the_header_for_every_management_user_in_a_new_tab(self):
        # D-131: the block "For staff" is gone from the menu; the way into the staff area is a button in the header.
        for user in (self.staff, self.administrator):
            for url in (HOME, PROFILE, BUILDINGS, reverse("customer:contract")):
                with self.subTest(user=user.pk, url=url):
                    html = client_of(user).get(url).content.decode()
                    header = part(html, '<header class="frame-header', "</header>")
                    button = BUTTON.findall(header)
                    self.assertEqual(len(button), 1, "the button of the header")
                    button = button[0]
                    self.assertIn('target="_blank" rel="noopener noreferrer"', button)
                    self.assertIn('aria-label="Mitarbeitenden-Bereich (öffnet in neuem Tab)"', button)
                    self.assertIn('<span class="frame-staff-text">Mitarbeitenden-Bereich</span>', button)
                    self.assertIn('href="#i-overview"', button)
                    self.assertIn('href="#i-external"', button)
                    # Before the profile link, which shows the name of the account.
                    self.assertLess(header.index("frame-staff-btn"), header.index("frame-name-link"))
                    # Not in the menu, the drawer or the bottom bar: the menu is the one of a customer.
                    menu = menu_of(client_of(user).get(url))
                    for gone in ("Für Mitarbeitende", "Mitarbeitenden-Bereich", "frame-staff-block", "/management/"):
                        self.assertNotIn(gone, menu)
                    self.assertEqual(html.count("frame-staff-btn"), 1)
                    self.assertNotIn("/management/", bar_of(client_of(user).get(url)))

    def test_the_header_of_my_area_links_to_the_profile_with_the_name(self):
        for user in (self.customer, self.staff):
            with self.subTest(user=user.pk):
                html = client_of(user).get(HOME).content.decode()
                header = part(html, '<header class="frame-header', "</header>")
                link = re.search(r'<a class="frame-name frame-name-link" href="/profile/"[^>]*>(.*?)</a>', header, re.S)
                self.assertIsNotNone(link)
                self.assertIn('<span class="visually-hidden">Profil: </span>', link.group(1))
                self.assertIn(user.display_name, link.group(1))
        # On the profile page the link is the current page.
        profile = part(client_of(self.customer).get(PROFILE).content.decode(), '<header class="frame-header', "</header>")
        self.assertIn('href="/profile/" aria-current="page"', profile)
        # In the staff area the name is plain text (the chip names the workspace).
        staff_header = part(client_of(self.staff).get(OVERVIEW).content.decode(), '<header class="frame-header', "</header>")
        self.assertNotIn("frame-name-link", staff_header)
        self.assertNotIn("frame-staff-btn", staff_header)
        self.assertIn('<span class="frame-name">', staff_header)

    def test_the_menu_of_my_area_is_the_same_for_staff_and_customer(self):
        def flat(menu):
            return re.sub(r"\s+", " ", re.sub(r"<form.*?</form>", "", menu, flags=re.S)).strip()

        mine = flat(menu_of(client_of(self.customer).get(HOME)))
        for user in (self.staff, self.administrator):
            with self.subTest(user=user.pk):
                self.assertEqual(flat(menu_of(client_of(user).get(HOME))), mine)

    def test_the_menu_of_my_area_has_no_group_coming_next_and_the_neighbourhood_above_the_profile(self):
        menu = menu_of(client_of(self.customer).get(HOME))
        self.assertNotIn("Demnächst", menu)
        self.assertNotIn("Teilnahme", menu)
        self.assertLess(menu.index("<span>Nachbarschaft</span>"), menu.index('href="/profile/"'))
        self.assertLess(menu.index('href="/profile/"'), menu.index("frame-parent"))

    def test_the_drawer_has_the_same_menu_and_the_page_no_other_staff_link(self):
        html = client_of(self.staff).get(HOME).content.decode()
        self.assertEqual(html.count('href="/management/"'), 1)  # the button of the header only
        self.assertNotIn("Systemverwaltung", menu_of(client_of(self.administrator).get(HOME)))
        self.assertNotIn('href="/admin/"', menu_of(client_of(self.administrator).get(HOME)))

    def test_the_placeholders_of_my_area_are_singular_for_everybody(self):
        for user in (self.customer, self.staff):
            with self.subTest(user=user.pk):
                menu = menu_of(client_of(user).get(HOME))
                for word in ("Rechnung", "Energie", "Mitteilung"):
                    self.assertIn(f"<span>{word}</span>", menu)
                self.assertNotIn("<span>Rechnungen</span>", menu)
                self.assertNotIn("<span>Mitteilungen</span>", menu)

    def test_my_area_pages_never_wear_the_staff_look(self):
        for url in (HOME, PROFILE, BUILDINGS):
            with self.subTest(url=url):
                html = client_of(self.administrator).get(url).content.decode()
                self.assertNotIn("frame--staff", html)
                self.assertNotIn('class="frame-workspace"', html)
                self.assertNotRegex(html, r"<title>Mitarbeitenden-Bereich")

    def test_the_staff_entry_is_not_authorisation(self):
        # A customer who forges the address gets the 404 of a page that does not exist; an anonymous visitor the login.
        self.assertEqual(client_of(self.customer).get(OVERVIEW).status_code, 404)
        self.assertEqual(client_of(self.customer).get(PERSONS).status_code, 404)
        self.assertRedirects(
            client_of(self.customer).get(ADMIN_INDEX), f"{reverse('admin:login')}?next={ADMIN_INDEX}",
            fetch_redirect_response=False,
        )
        self.assertEqual(Client().get(OVERVIEW).status_code, 302)
        self.assertNotIn("Mitarbeitenden-Bereich", Client().get(reverse("accounts:login")).content.decode())

    # ---- Staff area ----------------------------------------------------------------------------------------------

    def test_the_menu_of_the_staff_area_starts_with_the_way_back_in_the_same_tab(self):
        for user in (self.staff, self.administrator):
            for url in (OVERVIEW, PERSONS, PROCESSES):
                with self.subTest(user=user.pk, url=url):
                    menu = menu_of(client_of(user).get(url))
                    back = BACK.findall(menu)
                    self.assertEqual(len(back), 1)
                    self.assertNotIn("target=", back[0])
                    self.assertNotIn("rel=", back[0])
                    self.assertIn('href="#i-arrow-left"', back[0])
                    self.assertIn("<span>Zurück zu Mein Bereich</span>", back[0])
                    self.assertIn('<span class="frame-item-sub">Gebäude, Information und Profil</span>', back[0])
                    # The first entry, then the groups in the agreed order.
                    self.assertLess(menu.index("frame-back"), menu.index('<p class="frame-group">Verwaltung</p>'))
                    self.assertLess(menu.index('<p class="frame-group">Verwaltung</p>'), menu.index('<p class="frame-group">Demnächst</p>'))

    def test_the_menu_of_the_staff_area_has_management_coming_next_plural_and_system_for_administrators(self):
        staff_menu = menu_of(client_of(self.staff).get(OVERVIEW))
        for entry in ("Übersicht", "Vorgänge", "Personen", "Standorte", "Messpunkte", "Einstellungen", "Verträge"):
            self.assertIn(f"<span>{entry}</span>", staff_menu)
        for word in ("Rechnungen", "Energie", "Mitteilungen"):
            self.assertIn(f"<span>{word}</span>", staff_menu)
        self.assertNotIn("<span>Rechnung</span>", staff_menu)
        self.assertNotIn('<p class="frame-group">System</p>', staff_menu)
        self.assertNotIn("Systemverwaltung", staff_menu)
        admin_menu = menu_of(client_of(self.administrator).get(OVERVIEW))
        self.assertIn('<p class="frame-group">System</p>', admin_menu)
        self.assertIn('href="/admin/"', admin_menu)
        # DA-1: "System" stands before "Coming next" (the sticky foot would hide it at the bottom at a low window).
        self.assertLess(admin_menu.index('<p class="frame-group">Verwaltung</p>'), admin_menu.index('<p class="frame-group">System</p>'))
        self.assertLess(admin_menu.index('<p class="frame-group">System</p>'), admin_menu.index('<p class="frame-group">Demnächst</p>'))

    def test_the_menu_of_the_staff_area_has_no_participant_entries(self):
        menu = menu_of(client_of(self.administrator).get(OVERVIEW))
        for absent in ("<span>Start</span>", "<span>Profil</span>", "Gebäude</span>", "<span>Information</span>",
                       "/participation/", 'href="/profile/"', "Für Mitarbeitende", "frame-parent", "Kontakt zur LEG"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, menu)
        self.assertNotIn("Mein Bereich</p>", menu)
        self.assertNotIn('target="_blank"', BACK.sub("", part(menu, 'class="frame-menu', "frame-menu-foot")))

    def test_the_way_back_for_an_account_that_does_not_take_part_names_what_exists(self):
        with patch("leg_platform.customer.access.is_customer", return_value=False):
            menu = menu_of(client_of(self.staff).get(OVERVIEW))
        self.assertIn('<span class="frame-item-sub">Start und Profil</span>', menu)

    def test_the_header_the_drawer_and_the_title_of_the_staff_area(self):
        for user in (self.staff, self.administrator):
            for url in (OVERVIEW, PERSONS, PROCESSES):
                with self.subTest(user=user.pk, url=url):
                    html = client_of(user).get(url).content.decode()
                    self.assertIn('<div class="frame frame--staff">', html)
                    header = part(html, '<header class="frame-header', "</header>")
                    self.assertIn(CHIP, header)
                    self.assertLess(header.index('class="frame-logo"'), header.index(CHIP))
                    drawer = part(html, '<div class="frame-drawer-head">', "<nav aria-label=")
                    self.assertIn(CHIP, drawer)
                    # DA-2: the head holds the logo and the close button only; the chip has its own row below it.
                    head = part(drawer, '<div class="frame-drawer-head">', "</div>")
                    self.assertNotIn("frame-workspace", head)
                    self.assertRegex(drawer, r'</div>\s*(\{#.*?#\}\s*)?<div class="frame-drawer-brand">' + re.escape(CHIP) + "</div>")
                    self.assertEqual(html.count(CHIP), 2)
                    self.assertRegex(html, r"<title>Mitarbeitenden-Bereich: [^<]+ – [^<]+</title>")
        # The chip is text, not a control.
        self.assertNotRegex(html, r"<(a|button)[^>]*frame-workspace")

    def test_the_title_of_my_area_stays_as_it_is(self):
        html = client_of(self.staff).get(HOME).content.decode()
        self.assertRegex(html, r"<title>Start – [^<]+</title>")
        html = client_of(self.staff).get(PROFILE).content.decode()
        self.assertNotRegex(html, r"<title>Mitarbeitenden-Bereich")

    def test_base_html_renders_without_the_context_processor(self):
        # N-2: the title reads the workspace with a default, so a template that is rendered without the context
        # processor (a test, an error page in a changed project) never raises VariableDoesNotExist.
        from django.template import engines

        template = engines["django"].from_string(
            '{% extends "base.html" %}{% block title %}Seite{% endblock %}{% block content %}x{% endblock %}'
        )
        with translation.override("de-ch"):
            html = template.render({})
        self.assertRegex(html, r"<title>Seite – ")

    def test_the_title_prefix_is_escaped(self):
        from django.template import Context, Template
        from django.utils.safestring import mark_safe

        from leg_platform.management.templatetags.management_ui import staff_page_title

        # N-2: a raw string is escaped by the filter itself; a safe one is left alone; another workspace changes nothing.
        with translation.override("de-ch"):
            self.assertEqual(
                staff_page_title("<script>alert(1)</script>", "staff"),
                "Mitarbeitenden-Bereich: &lt;script&gt;alert(1)&lt;/script&gt;",
            )
            self.assertEqual(staff_page_title(mark_safe("A &amp; B"), "staff"), "Mitarbeitenden-Bereich: A &amp; B")
            self.assertEqual(staff_page_title("<b>", "participant"), "<b>")
        template = Template("{% load management_ui %}{% filter staff_page_title:workspace %}A &amp; <b>x</b>{% endfilter %}")
        with translation.override("de-ch"):
            self.assertEqual(
                template.render(Context({"workspace": "staff"})), "Mitarbeitenden-Bereich: A &amp; <b>x</b>"
            )
            self.assertEqual(template.render(Context({"workspace": "participant"})), "A &amp; <b>x</b>")
        # A title is escaped by the template before it reaches the filter; the filter adds the text of the catalogue only.
        template = Template("{% load management_ui %}{% filter staff_page_title:workspace %}{{ page }}{% endfilter %}")
        with translation.override("de-ch"):
            self.assertEqual(
                template.render(Context({"workspace": "staff", "page": "<script>alert(1)</script>"})),
                "Mitarbeitenden-Bereich: &lt;script&gt;alert(1)&lt;/script&gt;",
            )

    # ---- Bottom bars ---------------------------------------------------------------------------------------------

    def test_the_bottom_bar_follows_the_workspace_not_the_account(self):
        for user in (self.customer, self.staff, self.administrator):
            with self.subTest(user=user.pk, area="my area"):
                self.assertEqual(labels(bar_of(client_of(user).get(HOME))), ["Start", "Gebäude", "Information", "Profil"])
        for user in (self.staff, self.administrator):
            with self.subTest(user=user.pk, area="staff"):
                bar = bar_of(client_of(user).get(PERSONS))
                self.assertEqual(labels(bar), ["Mein Bereich", "Übersicht", "Vorgänge", "Menü"])
                self.assertIn('<a class="frame-tab" href="/">', bar)
                self.assertIn('href="#i-arrow-left"', bar)
                self.assertNotIn("is-later", bar)

    def test_a_customer_bar_has_no_staff_link(self):
        self.assertNotIn("/management/", bar_of(client_of(self.customer).get(HOME)))
        self.assertNotIn("/management/", bar_of(client_of(self.staff).get(HOME)))

    def test_an_account_that_does_not_take_part_gets_start_profile_and_the_menu_without_a_dead_link(self):
        with patch("leg_platform.customer.access.is_customer", return_value=False):
            html = client_of(self.staff).get(HOME).content.decode()
        bar = part(html, '<nav class="frame-bottombar', "</nav>")
        self.assertEqual(labels(bar), ["Start", "Profil", "Menü"])
        self.assertNotIn("/participation/", html)

    # ---- The start page ------------------------------------------------------------------------------------------

    def test_the_start_page_of_a_management_user_has_no_staff_button_of_its_own(self):
        # D-131: the button of the start page (phone) is gone; the button of the header is on every page of My area.
        for user in (self.staff, self.administrator):
            with self.subTest(user=user.pk):
                html = client_of(user).get(HOME).content.decode()
                self.assertNotIn("start-staff-link", html)
                self.assertNotIn("Mitarbeitenden-Bereich öffnen", html)
                self.assertEqual(len(BUTTON.findall(html)), 1)

    def test_the_start_page_of_a_customer_and_the_staff_pages_have_no_such_button(self):
        self.assertNotIn("frame-staff-btn", client_of(self.customer).get(HOME).content.decode())
        self.assertNotIn("frame-staff-btn", client_of(self.staff).get(OVERVIEW).content.decode())

    def test_the_button_of_the_header_is_an_icon_on_the_phone(self):
        css = (STATIC / "css" / "frame.css").read_text(encoding="utf-8")
        self.assertNotIn("start-staff-link", css)
        self.assertRegex(
            css,
            r"@media \(max-width: 760px\) \{\s*\.frame-staff-btn \{[^}]*width: 44px;[^}]*height: 44px;[^}]*border-radius: 50%;",
        )
        self.assertRegex(css, r"\.frame-staff-btn \.frame-staff-text \{\s*position: absolute;")  # text for screen readers
    def test_a_staff_account_with_a_customer_number_sees_the_whole_start_page_of_a_participant(self):
        for user in (self.staff, self.administrator):
            with self.subTest(user=user.pk):
                html = client_of(user).get(HOME).content.decode()
                self.assertIn("Kundennummer", html)
                self.assertIn(user.customer_number_display, html)
                self.assertIn('id="task-register_building-title"', html)  # the tile of a participant without a building

    def test_the_start_page_of_an_account_without_a_customer_number_does_not_crash_and_shows_no_participation(self):
        with patch("leg_platform.customer.access.is_customer", return_value=False):
            response = client_of(self.staff).get(HOME)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn('id="task-register_building-title"', html)
        self.assertNotIn("Ihr Weg in die LEG", html)

    def test_the_login_of_a_staff_account_still_ends_on_the_start_page(self):
        self.assertEqual(settings.LOGIN_REDIRECT_URL, "home")

    # ---- The sign of a new tab ----------------------------------------------------------------------------------

    @instance_mail_addresses(contact_address="leg@example.org")
    def test_the_contact_link_is_a_page_of_the_portal_not_a_new_tab(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"website_url": "https://leg.example.org/"})
        menu = menu_of(client_of(self.customer).get(HOME))
        self.assertIn('href="/participation/contact/"', menu)
        self.assertNotIn('target="_blank"', menu[menu.index("/participation/contact/") - 200 : menu.index("/participation/contact/") + 200])

    def test_the_icons_exist(self):
        sprite = (TEMPLATES / "_frame_icons.html").read_text(encoding="utf-8")
        for name in ("i-external", "i-arrow-left"):
            self.assertIn(f'<symbol id="{name}" viewBox="0 0 24 24">', sprite)
        html = client_of(self.staff).get(HOME).content.decode()
        self.assertIn('id="i-external"', html)
        self.assertIn('id="i-arrow-left"', html)


class WorkspaceStyleTests(SimpleTestCase):
    CSS = (STATIC / "css" / "frame.css").read_text(encoding="utf-8")

    def block(self, selector):
        found = re.search(r"(?m)^" + re.escape(selector) + r" \{(.*?)\}", self.CSS, re.S)
        self.assertIsNotNone(found, selector)
        return found.group(1)

    def test_the_header_of_the_staff_area_keeps_its_height_and_uses_existing_tokens(self):
        rule = self.block(".frame--staff .frame-header")
        for line in ("background: var(--warm);", "box-sizing: border-box;"):
            self.assertIn(line, rule)
        # DA-7: the accent bar is a pseudo-element, not a border: the content of the header does not move down.
        self.assertNotRegex(rule, r"border(?!-box)")
        self.assertNotIn("height", rule)  # the 64 px of ``.frame-header`` stay, the menu at top: 64 px does not move
        bar = self.block(".frame--staff .frame-header::before")
        for line in ("position: absolute;", "top: 0;", "left: 0;", "right: 0;", "height: 4px;", "background: var(--accent);"):
            self.assertIn(line, bar)
        self.assertRegex(self.block(".frame-header"), r"height: 64px")
        self.assertRegex(self.block(".frame-header"), r"position: fixed;")
        self.assertRegex(self.block(".frame-menu"), r"top: 64px")

    def test_the_chip_follows_the_specification(self):
        rule = self.block(".frame-workspace")
        for line in ("padding: 4px 12px;", "border: 1px solid var(--accent);", "border-radius: 8px;", "font-size: 14px;",
                     "font-weight: 600;", "color: var(--accent);", "white-space: nowrap;"):
            self.assertIn(line, rule)
        self.assertRegex(self.CSS, r"@media \(max-width: 399px\) \{\s*\.frame-header \.frame-workspace \{\s*display: none;")
        phone = re.search(r"@media \(max-width: 760px\) \{\s*\.frame-header \.frame-workspace \{(.*?)\}", self.CSS, re.S).group(1)
        self.assertIn("min-width: 0;", phone)
        self.assertIn("text-overflow: ellipsis;", phone)
        # DA-2: in the drawer the chip may shrink with an ellipsis in its own row.
        drawer = self.block(".frame-drawer-brand .frame-workspace")
        for line in ("flex: 0 1 auto;", "min-width: 0;", "overflow: hidden;", "text-overflow: ellipsis;"):
            self.assertIn(line, drawer)

    def test_the_menu_scrolls_clear_of_the_sticky_foot(self):
        # DA-1 (WCAG 2.4.11): the entry that gets the focus is never hidden by the foot (99 px high plus the 24 px of
        # padding of the menu below it: 123 px, and a little room).
        rule = re.search(r"\.frame-menu,\s*\.frame-drawer-panel \{(.*?)\}", self.CSS, re.S).group(1)
        self.assertIn("scroll-padding-bottom: 136px;", rule)
        self.assertRegex(self.block(".frame-menu-foot"), r"position: sticky;")

    def test_the_foot_covers_the_strip_below_it_with_the_panel_colour(self):
        # D4: the padding of the menu below the sticky foot must not show the entries that scroll past.
        after = self.block(".frame-menu-foot::after")
        for line in ("position: absolute;", "top: 100%;", "height: 24px;", "background: var(--panel);"):
            self.assertIn(line, after)
        self.assertIn("height: 16px;", self.block(".frame-drawer-nav .frame-menu-foot::after"))  # the padding of the drawer
        self.assertRegex(self.block(".frame-menu"), r"padding: 32px 12px 24px;")  # the measure of D-057 is unchanged
        self.assertRegex(self.block(".frame-menu-foot"), r"padding: 12px 0 8px;")  # and so is the height of the foot

    def test_the_staff_entry_keeps_its_title_on_one_line(self):
        # D1: 14 px title, 8 px gap and 4 px right padding for the two-line entry only; one line of the title and of the
        # second line, about 52 px high.
        rule = self.block(".frame-item.frame-item-two-line")
        for line in ("min-height: 52px;", "gap: 8px;", "padding-right: 4px;"):
            self.assertIn(line, rule)
        self.assertIn("font-size: 14px;", self.block(".frame-item-two-line .frame-item-text > span:first-child"))

    def test_the_two_line_entry_and_the_back_block(self):
        self.assertIn("min-height: 52px;", self.block(".frame-item.frame-item-two-line"))
        self.assertIn("font-size: 12px;", self.block(".frame-item-sub"))
        self.assertIn("color: var(--muted);", self.block(".frame-item-sub"))
        back = re.search(r"(?m)^\.frame-back \{(.*?)\}", self.CSS, re.S).group(1)
        self.assertIn("border-bottom: 1px solid var(--line);", back)
        self.assertIn("padding-bottom: 12px;", back)
        # DA-5: the first line of the way back breaks in balance, not after "Mein".
        self.assertIn("text-wrap: balance;", self.block(".frame-back .frame-item-text > span:first-child"))
        # DA-8 (the phone button below the start title) is gone with the button (D-131).
        self.assertNotIn("start-staff-link", self.CSS)

    def test_no_new_colour_shadow_or_gradient(self):
        start = self.CSS.index("/* ---- Two workspaces")
        part_of_css = self.CSS[start : self.CSS.index("/* ---- Start page ---- */")]
        self.assertNotRegex(part_of_css, r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(")
        for forbidden in ("box-shadow", "gradient("):
            self.assertNotIn(forbidden, part_of_css)


@tag("mariadb")
class AdministrationShellTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("ws.admin.shell@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)
        cls.staff = create_active_user("ws.staff.shell@example.org")
        cls.staff.set_privileges(is_staff=True, is_superuser=False)

    def user_tools(self, html):
        return part(html, '<div id="user-tools">', "</div>")

    def test_the_administration_shows_the_chip_the_title_and_the_two_ways_back(self):
        for user in (self.administrator, self.staff):
            with self.subTest(user=user.pk):
                html = client_of(user).get(ADMIN_INDEX).content.decode()
                self.assertIn('<span class="admin-workspace">Mitarbeitenden-Bereich</span>', html)
                self.assertRegex(html, r"<title>Mitarbeitenden-Bereich: [^<]+ \| [^<]+</title>")
                tools = self.user_tools(html)
                self.assertIn('<a href="/management/">Verwaltung</a>', tools)
                self.assertIn('<a href="/">Mein Bereich</a>', tools)
                self.assertNotIn("target=", tools)  # the same tab
                # Change password and log out stay.
                self.assertIn(reverse("admin:password_change"), tools)
                self.assertIn('id="logout-form"', tools)
                self.assertIn(reverse("admin:logout"), tools)

    def test_view_site_is_gone_from_every_administration_page(self):
        client = client_of(self.administrator)
        for url in (ADMIN_INDEX, reverse("admin:accounts_user_changelist"), reverse("admin:logs")):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                tools = self.user_tools(response.content.decode())
                self.assertEqual(re.findall(r'<a href="([^"]*)"', tools),
                                 ["/management/", "/", reverse("admin:password_change")])
                with translation.override("en"):
                    self.assertNotIn("View site", tools)
                self.assertNotIn("Website anzeigen", tools)
                self.assertNotIn("Zum Portal", tools)  # the translation of "View site" in this project

    def test_the_title_of_a_page_with_a_subtitle_is_prefixed_once(self):
        html = client_of(self.administrator).get(reverse("admin:accounts_user_changelist")).content.decode()
        title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
        self.assertTrue(title.startswith("Mitarbeitenden-Bereich: "), title)
        self.assertEqual(title.count("Mitarbeitenden-Bereich"), 1)

    def test_the_chip_and_the_links_have_touch_targets(self):
        css = (STATIC / "css" / "admin-portal.css").read_text(encoding="utf-8")
        self.assertRegex(
            css,
            r"@media \(pointer: coarse\), \(max-width: 767px\) \{\s*#user-tools a,\s*#user-tools #logout-form button \{[^}]*min-height: 44px;",
        )
        touch = part(css, "@media (pointer: coarse), (max-width: 767px) {\n  #user-tools a,", "\n}\n")
        # DA-9: the links are underlined (the faint border of the admin is no sign of a link) and the theme button is 44 px.
        self.assertRegex(touch, r"#user-tools a \{[^}]*border-bottom: 0;[^}]*text-decoration: underline;[^}]*text-underline-offset: 3px;")
        self.assertRegex(touch, r"#user-tools #logout-form button \{[^}]*border-bottom: 0;[^}]*text-decoration: underline;[^}]*text-underline-offset: 3px;")
        # The selector is the real one of Django's admin/base.html: a button in the form ``logout-form`` in ``user-tools``.
        from django.contrib.admin import __file__ as admin_init

        base = (Path(admin_init).parent / "templates" / "admin" / "base.html").read_text(encoding="utf-8")
        self.assertIn('<div id="user-tools">', base)
        self.assertRegex(base, r'(?s)<form id="logout-form"[^>]*>.*?<button type="submit">')
        self.assertRegex(touch, r"#header \.theme-toggle \{[^}]*min-width: 44px;[^}]*min-height: 44px;")

    def test_the_chip_is_readable_on_the_header_of_the_administration(self):
        # DA-3: the chip uses the header colour of the admin (the yellow of the site name had 3.65:1), at least 4.5:1.
        from django.contrib.admin import __file__ as admin_init

        from leg_platform.branding import contrast

        css = (STATIC / "css" / "admin-portal.css").read_text(encoding="utf-8")
        chip = part(css, ".admin-workspace {", "}")
        self.assertIn("color: var(--header-color);", chip)
        self.assertIn("border: 1px solid var(--header-color);", chip)
        self.assertNotIn("header-branding-color", chip)
        base = (Path(admin_init).parent / "static" / "admin" / "css" / "base.css").read_text(encoding="utf-8")

        def value(name):
            found = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{3,6}})", base).group(1)
            return "#" + "".join(c * 2 for c in found[1:]) if len(found) == 4 else found

        self.assertGreaterEqual(contrast(value("header-color"), value("secondary")), 4.5)

    def test_the_login_page_of_the_administration_has_no_chip(self):
        html = Client().get(reverse("admin:login"), follow=True).content.decode()
        self.assertNotIn("admin-workspace", html)
        self.assertNotIn("Mitarbeitenden-Bereich", html)

    def test_a_customer_does_not_get_the_administration(self):
        customer = create_active_user("ws.customer.shell@example.org")
        response = client_of(customer).get(ADMIN_INDEX, follow=True)
        self.assertNotIn("admin-workspace", response.content.decode())

    def test_an_administration_page_is_closed_to_a_blocked_deactivated_or_password_bound_account(self):
        # D-113 review N-3. (Without a completed second factor and without staff status: ``test_admin_exposure``.)
        login_location = f"{reverse('admin:login')}?next={ADMIN_INDEX}"
        for state in ("blocked", "deactivated"):
            user = create_active_user(f"ws.{state}.admin@example.org")
            user.set_privileges(is_staff=True, is_superuser=False)
            client = client_of(user)
            self.assertEqual(client.get(ADMIN_INDEX).status_code, 200)
            if state == "blocked":
                user.set_status(type(user).Status.BLOCKED)
            else:
                user.set_retention_obligation()
                user.delete_or_deactivate()
            for url in (ADMIN_INDEX, reverse("admin:accounts_user_changelist")):
                with self.subTest(state=state, url=url):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith(reverse("admin:login")), response["Location"])
            self.assertRedirects(client.get(ADMIN_INDEX), login_location, fetch_redirect_response=False)
        bound = create_active_user("ws.password.admin@example.org")
        bound.set_privileges(is_staff=True, is_superuser=True)
        client = client_of(bound)
        bound.password_change_required = True
        bound.save()
        for url in (ADMIN_INDEX, reverse("admin:accounts_user_changelist")):
            with self.subTest(state="password_change_required", url=url):
                self.assertRedirects(
                    client.get(url), reverse("accounts:password_change"), fetch_redirect_response=False
                )


class CatalogueTests(SimpleTestCase):
    """The new texts exist in every language (the three catalogues are also compared by ``LanguageCatalogueTests``)."""

    EXPECTED = {
        "de-ch": {"Staff area": "Mitarbeitenden-Bereich", "Back to My area": "Zurück zu Mein Bereich",
                  "Buildings, information and profile": "Gebäude, Information und Profil",
                  "Start and profile": "Start und Profil"},
        "fr": {"Staff area": "Espace collaborateurs"},
        "it": {"Staff area": "Area collaboratori"},
    }

    def test_the_texts_are_translated_and_never_use_the_forbidden_letter(self):
        for language, texts in self.EXPECTED.items():
            with translation.override(language):
                for source, expected in texts.items():
                    with self.subTest(language=language, source=source):
                        self.assertEqual(translation.gettext(source), expected)
                        self.assertNotIn("ß", expected)

    def test_the_title_pattern_keeps_its_placeholder_in_every_language(self):
        for language, expected in (("de-ch", "Mitarbeitenden-Bereich: Seite"), ("fr", "Espace collaborateurs : Seite"),
                                   ("it", "Area collaboratori: Seite")):
            with translation.override(language):
                self.assertEqual(translation.gettext("Staff area: %(page)s") % {"page": "Seite"}, expected)
