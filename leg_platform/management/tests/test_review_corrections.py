"""Corrections after the acceptance and the security review (D-097, D-098): the round trip of "Person missing?", the
next-step banners without a second message, the touch rules and a hostile ``next``. The person search and its budget
are tested in ``test_metering_point_pages.py``, the scripts in ``test_dialog_scripts.py``."""

import html
import re
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.models import Permission
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import leg_services, numbers, transformer_circuits
from leg_platform.community.models import Leg, ManagementSettings, TransformerCircuit
from leg_platform.community.tests.helpers import (
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)
from leg_platform.management import leg_views, returns

ROOT = Path(settings.BASE_DIR) / "leg_platform"

CREATE = {
    "email": "round.trip@example.org", "salutation": "mrs", "organisation_name": "", "first_name": "Rita",
    "last_name": "Rundweg", "address_line_1": "", "street": "Neuweg", "house_number": "3", "postal_code": "3000",
    "town": "Bern", "country": "CH", "phone_number": "",
}


@tag("mariadb")
class RoundTripTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.roundtrip@example.org")
        row = ManagementSettings.load()
        row.country_code, row.identifier = "CH", "1A2B3C4D5E6"
        row.save()
        cls.point = make_metering_point(1, location=make_location("Musterweg", "12", "3063", "Ittigen"))

    def setUp(self):
        self.client = VerifiedClient()
        self.client.force_login(self.staff)

    def test_a_person_created_from_the_assign_dialog_can_go_back_to_the_metering_point(self):
        # UX-004: an invited person cannot be assigned yet (D-081); the banner leads back instead of to a dead end.
        dialog = reverse("management:metering_points_person", args=[self.point.pk])
        page = self.client.get(dialog).content.decode()
        self.assertIn("Person fehlt? Neue Person einladen", page)
        self.assertIn("Keine Person gefunden. Zugewiesen werden können nur aktive Personen mit Kundennummer.", page)
        link = html.unescape(re.search(r'href="([^"]*persons/new/[^"]*)"', page).group(1))
        response = self.client.post(link, {**CREATE, "next": dialog})
        self.assertEqual(response.status_code, 302)
        landing = self.client.get(response["Location"])
        text = landing.content.decode()
        back = reverse("management:metering_points_detail", args=[self.point.pk])
        self.assertIn(f'<a class="btn btn-accent-outline" href="{back}">Zurück zum Messpunkt</a>', text)
        self.assertIn('class="mgmt-banner is-neutral"', text)  # a hint, no warning (UX-016)
        self.assertIn("Person angelegt. Die Einladung wurde gesendet.", text)
        # D-081 stays: the text does not suggest that the person can be assigned now (UX-004, D-102).
        self.assertIn("sobald sich die Person registriert hat und eine Kundennummer besitzt", text)
        self.assertNotIn("Ordnen Sie der Person", text)
        # The plain message is not queued as well (UX-016): one statement, not two.
        self.assertNotIn("Die Person wurde angelegt und die Einladung wurde gesendet.", text)

    def test_the_back_link_is_neutral_when_it_leads_to_the_dialog_and_named_when_it_leads_to_the_list(self):
        # S-1: with a safe next the top link goes to the page the person was invited from, so it just says "Zurück".
        dialog = reverse("management:metering_points_person", args=[self.point.pk])
        created = self.client.post(reverse("management:persons_new"), {**CREATE, "next": dialog})
        detail = created["Location"].split("?")[0]
        opened = self.client.get(created["Location"]).content.decode()
        self.assertIn(f'<a class="mgmt-back" href="{dialog}">← Zurück</a>', opened)
        self.assertNotIn("Zurück zu Personen", opened)
        plain = self.client.get(detail).content.decode()
        self.assertIn('<a class="mgmt-back" href="/management/persons/">← Zurück zu Personen</a>', plain)
        # An unsafe next falls back to the list and keeps the named label.
        hostile = self.client.get(detail, {"next": "//evil.example/x"}).content.decode()
        self.assertIn("← Zurück zu Personen</a>", hostile)

    def test_a_person_created_from_the_list_has_no_button_to_a_metering_point(self):
        response = self.client.post(reverse("management:persons_new"), {**CREATE, "next": reverse("management:persons")})
        text = self.client.get(response["Location"]).content.decode()
        self.assertIn("Person angelegt.", text)
        self.assertNotIn("Zurück zum Messpunkt", text)

    def test_a_next_that_is_no_metering_point_dialog_or_not_of_this_site_gives_no_button(self):
        person = self.client.post(reverse("management:persons_new"), {**CREATE, "next": reverse("management:persons")})
        detail = person["Location"].split("?")[0]
        for origin in (
            "https://evil.example/management/metering-points/1/person/", "//evil.example/x", "/\\evil.example",
            "javascript:alert(1)", reverse("management:locations"), "/management/metering-points/abc/person/",
            "/nothing/here/", "",
        ):
            with self.subTest(origin=origin):
                text = self.client.get(detail, {"new": "1", "next": origin}).content.decode()
                self.assertNotIn("Zurück zum Messpunkt", text)
                self.assertNotRegex(text, r'href="(https?:)?(//|\\)[^"]*evil')  # never a link to another site

    def test_the_origin_is_built_from_the_view_and_its_number_never_taken_over(self):
        factory = RequestFactory()
        request = factory.get("/management/persons/5/", {"next": f"/management/metering-points/{self.point.pk}/person/?x=https://evil.example"})
        self.assertEqual(
            returns.origin_point_url(request), reverse("management:metering_points_detail", args=[self.point.pk])
        )
        self.assertEqual(returns.origin_point_url(factory.get("/x/", {"next": "//evil.example/"})), "")
        self.assertEqual(returns.origin_point_url(factory.get("/x/")), "")

    def test_the_next_step_banner_after_a_create_is_neutral_and_has_no_second_message(self):
        response = self.client.post(
            reverse("management:legs_new"), {"name": "LEG Rundweg", "quota": "", "checked_on": "", "next": reverse("management:legs")},
            follow=True,
        )
        text = response.content.decode()
        self.assertIn("LEG angelegt. Weiter: Messpunkte der LEG zuordnen.", text)
        self.assertIn('class="mgmt-banner is-neutral"', text)
        self.assertNotIn("Gespeichert.", text)  # UX-016
        # A return to another page (the form that waited for the new object) keeps the plain message.
        point_dialog = reverse("management:metering_points_new")
        back = self.client.post(
            reverse("management:legs_new") + "?next=" + quote(point_dialog),
            {"name": "LEG Rückweg", "quota": "", "checked_on": "", "next": point_dialog},
        )
        self.assertTrue(back["Location"].startswith(point_dialog))

    def test_the_banner_of_a_warning_stays_in_the_warning_colour(self):
        text = self.client.get(reverse("management:metering_points")).content.decode()
        self.assertNotIn("mgmt-banner is-neutral", text)


@tag("mariadb")
class GoneObjectTests(TestCase):
    """L-C: an object that was deleted between the form and the save is the normal refusal in every dialog (one place:
    ``access.gone``), never an error page. The services raise ``DoesNotExist`` from their row lock; here they are made
    to do so for two different services, a function-like view and a GET."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.gone@example.org")
        cls.leg = make_leg("LEG Gone")
        cls.circuit = make_circuit("TRA777", "Gone")

    def setUp(self):
        self.client = VerifiedClient(raise_request_exception=False)
        self.client.force_login(self.staff)
        self.back = reverse("management:legs")

    def test_a_leg_that_is_gone_when_the_service_locks_it_is_a_refusal_with_a_message(self):
        url = reverse("management:legs_edit", args=[self.leg.pk])
        with mock.patch.object(leg_services, "update_master_data", side_effect=Leg.DoesNotExist), \
                self.assertLogs("leg_platform.management.access", "WARNING"):
            response = self.client.post(url, {"name": "LEG Neu", "remark": "", "next": self.back}, follow=True)
        self.assertEqual(response.redirect_chain, [(self.back, 302)])
        self.assertContains(response, "Diesen Eintrag gibt es nicht mehr.")
        self.assertEqual(Leg.objects.get(pk=self.leg.pk).name, "LEG Gone")  # nothing changed

    def test_a_circuit_that_is_gone_is_a_refusal_too_and_a_hostile_next_goes_to_the_overview(self):
        url = reverse("management:circuits_edit", args=[self.circuit.pk])
        with mock.patch.object(transformer_circuits, "update", side_effect=TransformerCircuit.DoesNotExist), \
                self.assertLogs("leg_platform.management.access", "WARNING"):
            response = self.client.post(url, {"grid_number": "TRA778", "name": "x", "next": "//evil.example/x"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("management:overview"))
        self.assertEqual(TransformerCircuit.objects.get(pk=self.circuit.pk).grid_number, "TRA777")

    def test_a_get_for_an_object_that_is_gone_is_a_404_and_the_access_check_comes_first(self):
        url = reverse("management:legs_edit", args=[self.leg.pk])
        with mock.patch.object(leg_views.LegMasterDataView, "get", side_effect=Leg.DoesNotExist), \
                self.assertLogs("leg_platform.management.access", "WARNING"):
            self.assertEqual(self.client.get(url).status_code, 404)
        # Without a login, without staff rights: the answer of the access check, whatever the service would say.
        plain = create_active_user("plain.gone@example.org")
        for client, expected in ((Client(), 302), (self._client_for(plain), 404)):
            with mock.patch.object(leg_services, "update_master_data", side_effect=Leg.DoesNotExist):
                response = client.post(url, {"name": "LEG Neu", "remark": ""})
            self.assertEqual(response.status_code, expected)

    def _client_for(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client

    def test_the_services_themselves_still_raise_so_that_the_view_decides(self):
        gone_circuit = make_circuit("TRA779", "Weg")
        TransformerCircuit.objects.filter(pk=gone_circuit.pk).delete()
        with self.assertRaises(TransformerCircuit.DoesNotExist):
            transformer_circuits.update(gone_circuit, "TRA780", "x")


@tag("mariadb")
class SystemAdministrationMenuTests(TestCase):
    """D-102: the entry "Systemverwaltung" is shown only to an account that can open something in the Django
    administration (Django's own app list), in the menu of the desktop and of the phone (one template). Hiding is not
    security: the administration checks every request itself."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = create_active_user("customer.menu@example.org")
        cls.bare = staff_user("bare.staff.menu@example.org")  # staff without any right of the administration
        cls.one = staff_user("one.staff.menu@example.org")
        cls.one.user_permissions.add(Permission.objects.get(codename="view_leg", content_type__app_label="community"))
        cls.mail = staff_user("mail.staff.menu@example.org")
        cls.mail.user_permissions.add(Permission.objects.get(codename="change_emailtext"))
        cls.superuser = staff_user("super.menu@example.org", superuser=True)

    def menu(self, user, url="/management/"):
        client = VerifiedClient()
        client.force_login(user)
        response = client.get(url)
        return response.status_code, response.content.decode()

    def test_the_entry_is_shown_twice_for_a_superuser_and_for_staff_with_one_right(self):
        for user in (self.superuser, self.one, self.mail):
            with self.subTest(user=user.email):
                status, text = self.menu(user)
                self.assertEqual(status, 200)
                self.assertEqual(text.count("Systemverwaltung"), 2)  # the left menu and the menu of the phone
                self.assertEqual(text.count('href="/admin/"'), 2)
                self.assertEqual(text.count('<p class="frame-group">System</p>'), 2)

    def test_the_entry_is_not_shown_to_a_customer_or_to_staff_without_a_right(self):
        for user, url in ((self.customer, "/profile/"), (self.bare, "/management/"), (self.bare, "/profile/")):
            with self.subTest(user=user.email, url=url):
                status, text = self.menu(user, url)
                self.assertEqual(status, 200)
                self.assertNotIn("Systemverwaltung", text)
                self.assertNotIn('href="/admin/"', text)
                self.assertNotIn('<p class="frame-group">System</p>', text)
        # The rest of the menu is unchanged for staff without a right.
        _status, text = self.menu(self.bare)
        for entry in ("Übersicht", "Personen", "Messpunkte", "Ausloggen"):
            self.assertIn(entry, text)

    def test_the_mail_texts_entry_follows_the_same_rule_and_the_administration_still_refuses(self):
        _status, text = self.menu(self.mail)
        self.assertEqual(text.count("Mailtexte"), 2)
        for user in (self.bare, self.one):
            self.assertNotIn(">Mailtexte<", self.menu(user)[1])
        # Hiding is not security: the pages that the account has no right for answer 403 whatever the menu says.
        client = VerifiedClient()
        client.force_login(self.bare)
        for url in ("/admin/accounts/user/", "/admin/accounts/emailtext/", "/admin/community/leg/", "/admin/logs/"):
            self.assertEqual(client.get(url).status_code, 403, url)
        one = VerifiedClient()
        one.force_login(self.one)
        self.assertEqual(one.get("/admin/community/leg/").status_code, 200)
        self.assertEqual(one.get("/admin/accounts/user/").status_code, 403)
        self.assertEqual(Client().get("/admin/").status_code, 302)  # not logged in: the login

    def test_the_menu_template_is_the_one_of_the_desktop_and_the_phone(self):
        template = (ROOT / "templates" / "_frame_menu.html").read_text(encoding="utf-8")
        self.assertEqual(template.count("system_administration as show_system"), 1)
        # D-102: the entry "System administration" is decided by the tag alone, never by ``user.is_staff``. The
        # customer menu (D-105) separates a customer from a staff account above this block, and the contact link of the
        # customer (W1-03) is in the foot below it; neither is the entry. So the block is read up to the foot.
        block = template.split("system_administration as show_system", 1)[1].split("frame-menu-foot", 1)[0]
        self.assertIn("href=\"{% url 'admin:index' %}\"", block)
        self.assertNotIn("is_staff", block)


@tag("mariadb")
class MissingLocationWarningTests(TestCase):
    """The location stays optional (D-095); a metering point without one is a problem that the list and the page of the
    point both show, with the way to set it (owner decision, work package E)."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.nolocation@example.org")
        cls.point = make_metering_point(41)
        cls.placed = make_metering_point(42, location=make_location("Musterweg", "12", "3063", "Ittigen"))

    def setUp(self):
        self.client = VerifiedClient()
        self.client.force_login(self.staff)

    def test_the_list_and_the_page_of_a_point_without_a_location_show_the_warning(self):
        warning = "Dieser Messpunkt ist keinem Standort zugeordnet."
        listing = self.client.get(reverse("management:metering_points")).content.decode()
        # The table and the card of a phone both carry the warning (the title and the hidden text of each).
        self.assertGreaterEqual(listing.count(warning), 4)
        detail = self.client.get(reverse("management:metering_points_detail", args=[self.point.pk])).content.decode()
        self.assertIn(f"<p>{warning}</p>", detail)
        self.assertIn(f'href="{reverse("management:metering_points_edit", args=[self.point.pk])}', detail)
        self.assertIn("Standort festlegen", detail)
        placed = self.client.get(reverse("management:metering_points_detail", args=[self.placed.pk])).content.decode()
        self.assertNotIn(warning, placed)
        # The filter "problems" lists it; the person page text does not offer an assignment before registration.
        filtered = self.client.get(reverse("management:metering_points"), {"problems": "1"}).content.decode()
        self.assertIn(numbers.format_metering_point_number(self.point.number), filtered)


class TouchRuleTests(SimpleTestCase):
    def test_links_chips_and_the_pager_are_44_px_high_on_a_touch_screen(self):
        css = (
            Path(settings.BASE_DIR) / "leg_platform" / "management" / "static" / "management" / "css" / "management.css"
        ).read_text(encoding="utf-8")
        touch = css.split("@media (pointer: coarse), (max-width: 760px) {", 1)[1]
        for needle in (".mgmt-hint a", ".mgmt-fact-value a", ".mgmt-circuit-links a", ".mgmt-pager .btn", "min-height: 44px"):
            self.assertIn(needle, touch, needle)  # UX-008
        # S-2: facts and notes with a link, and the swap button; the small icon targets stay as documented.
        self.assertIn(".mgmt-fact a:not(.mgmt-goto)", touch)
        self.assertIn(".mgmt-note a:not(.mgmt-goto)", touch)
        self.assertRegex(touch, r"\.mgmt-swap \{\s*width: 44px;\s*height: 44px;")
        self.assertRegex(css, r"\.mgmt-chip \{\s*flex-shrink: 0;\s*min-height: 44px;")
        self.assertIn("padding-right: 36px;", touch)  # UX-011: the arrow of a select does not overlap the text
        self.assertIn("max-height: 336px;", css)  # UX-012: five and a half rows
