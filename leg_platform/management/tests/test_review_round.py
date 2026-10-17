"""The corrections of the review round of D-082 (D-084): the merge of locations (M-3), the security log of a printout
(M-2), the limits of the lists and the print dialog (L-1, L-8), the parsing of numbers (L-2), the lockout lookup of a
page (L-4), the fonts in the package (L-9), and the corrections of the designer's findings that can be tested without a
browser (the skip link, the tokens, the settings check, the links of the tables). The behaviour of the pages in a
browser was checked at 1440, 1280, 1024, 768 and 390 px. The rights that once gated what a list and a notice show of
another area (M-4) are gone (D-086): every staff account sees the data of every area, asserted once below."""

import re
import tomllib
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.http import QueryDict
from django.test import Client, SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import leg_list, location_list, person_list, persons
from leg_platform.community.models import REMARK_MAX_LENGTH, Direction, ManagementSettings, PersonRecord
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    number,
    staff_user,
)
from leg_platform.management import lists, person_views, summaries
from leg_platform.management.printing import areas, pdf
from leg_platform.management.printing.document import Column, DetailPage, PrintDocument, Section, Table

TODAY = timezone.localdate()
EARLIER = TODAY - timedelta(days=30)
BASE = Path(settings.BASE_DIR)


@tag("mariadb")
class ReviewRoundTests(TestCase):
    databases = {"default", "addresses"}  # the page of a person asks the address directory

    @classmethod
    def setUpTestData(cls):
        settings_row = ManagementSettings.load()
        settings_row.country_code, settings_row.identifier = "CH", "1A2B3C4D5E6"
        settings_row.save()
        cls.staff = staff_user("staff.person@example.org")  # no group, no permission

        cls.circuit = make_circuit("TRA21359")
        cls.place = make_location("Musterweg", "12", transformer_circuit=cls.circuit)
        cls.empty_place = make_location("Leerweg", "1")
        cls.anna = create_active_user("anna.person@example.org", first_name="Anna", last_name="Muster")
        cls.leg = make_leg("LEG Geheimname")
        cls.point = make_metering_point(1, Direction.CONSUMPTION, cls.place)
        assign_leg(cls.point, cls.leg, valid_from=EARLIER)
        assign_person(cls.point, cls.anna, EARLIER)

    def login(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client

    # M-4 (D-086): the data of every area is shown to every staff account --------------------------------

    def test_a_staff_account_sees_the_data_of_every_area_in_notices_searches_lists_and_printouts(self):
        # The notice of a number that exists names the metering point, its location and its LEG.
        summary = summaries.duplicate_summary(self.point)
        self.assertEqual((summary["direction"], summary["location"], summary["leg"]), ("Bezug", str(self.place), "LEG Geheimname"))
        self.assertTrue(summary["status"] and summary["url"])
        dialog = reverse("management:locations_metering_point_new", args=[self.empty_place.pk])
        html = self.login(self.staff).post(dialog, {"direction": "consumption", "number_digits": "1"}).content.decode()
        self.assertIn("Diese Messpunkt-Nummer gibt es schon.", html)
        for text in ("LEG Geheimname", "Zum bestehenden Messpunkt", "Musterweg"):
            self.assertIn(text, html)
        # The search of the location list looks at LEG names and numbers; the suggestions never at a LEG name.
        base = location_list.base_queryset(TODAY)
        names = lambda queryset: sorted(queryset.values_list("street", flat=True))  # noqa: E731
        self.assertEqual(names(location_list.apply_search(base, "geheimname")), ["Musterweg"])
        self.assertEqual(names(location_list.apply_search(base, "geheimname", include_assignments=False)), [])
        self.assertEqual(names(location_list.apply_search(base, number(1)[20:])), ["Musterweg"])
        self.assertEqual(location_list.location_suggestions("geheimname"), [])
        rows, _persons = location_list.details([self.place], TODAY)
        self.assertEqual((rows[self.place.pk][0].leg_id, rows[self.place.pk][0].leg_name), (self.leg.pk, "LEG Geheimname"))
        # The location list: the filter by occupancy and the name of the LEG in the expanded rows.
        client = self.login(self.staff)
        locations = reverse("management:locations")
        occupied = client.get(locations, {"occupancy": "occupied", "sort": "occupancy"})
        self.assertContains(occupied, "Musterweg 12")
        self.assertNotContains(occupied, "Leerweg 1")
        self.assertContains(client.get(locations), "LEG Geheimname")
        # The check of the number on leaving the field answers the same way.
        answer = client.get(reverse("management:metering_points_check_number"), {"digits": "1"}).json()["duplicate"]
        self.assertEqual((answer["leg"], answer["location"]), ("LEG Geheimname", str(self.place)))
        # The metering point list: the address and the LEG are columns, filter and search key.
        points = reverse("management:metering_points")
        html = client.get(points, {"sort": "location", "q": "geheimname"}).content.decode()
        self.assertIn("Musterweg 12", html)
        self.assertIn("LEG Geheimname", html)
        self.assertIn("sort=location", html)
        # The printout is the same list, with the address and the LEG name.
        prepared = areas.AREAS["metering_points"].prepare(QueryDict("q=geheimname"))
        self.assertEqual(prepared.total, 1)
        self.assertEqual(prepared.load()[0].cells[1:3], ["Musterweg 12", "LEG Geheimname"])
        items = areas.AREAS["locations"].prepare(QueryDict("")).load()
        self.assertTrue(any(item.cells[3] not in ("–", "leer") for item in items))

    # M-3: the merge of two locations ------------------------------------------------------------------

    def test_the_page_of_a_merge_binds_the_confirmation_to_the_shown_target(self):
        source = make_location("Alt", "3")
        target = make_location("Neu", "4")
        url = reverse("management:locations_address", args=[source.pk])
        data = {"street": "Neu", "house_number": "4", "address_addition": "", "postal_code": "3000", "town": "Bern"}
        page = self.login(self.staff).post(url, data)
        self.assertContains(page, f'name="merge_target" value="{target.pk}"')
        self.assertContains(page, 'name="confirm_merge" value="1"')

    # L-1, L-2: bounds and the parsing of numbers ---------------------------------------------------------

    def test_a_remark_has_a_maximum_in_the_forms_and_the_model(self):
        place = self.place
        too_long = "x" * (REMARK_MAX_LENGTH + 1)
        response = self.login(self.staff).post(
            reverse("management:locations_remark", args=[place.pk]), {"remark": too_long}
        )
        self.assertEqual(response.status_code, 200)
        place.refresh_from_db()
        self.assertEqual(place.remark, "")
        from django.core.exceptions import ValidationError

        for model, field in ((place, "remark"), (self.leg, "remark"), (self.point, "remark")):
            setattr(model, field, too_long)
            with self.assertRaises(ValidationError):
                model.full_clean()
        record = PersonRecord(person=self.anna, staff_note=too_long)
        with self.assertRaises(ValidationError):
            record.full_clean(exclude=["person"])
        self.assertEqual(REMARK_MAX_LENGTH, persons.NOTE_MAX_LENGTH)
        self.assertGreaterEqual(pdf.MAX_FACT_CHARS, REMARK_MAX_LENGTH)

    def test_all_rows_are_bounded_and_the_list_says_so(self):
        client = self.login(self.staff)
        # The list holds the staff account and Anna; a bound of 1 is smaller than that.
        with mock.patch.object(lists, "MAX_ALL_ROWS", 1):
            page = client.get(reverse("management:persons"), {"per": "all"})
        self.assertContains(page, "Die Liste hat mehr als 1 Einträge; sie können nicht alle auf einmal angezeigt werden.")
        self.assertNotContains(client.get(reverse("management:persons"), {"per": "all"}), "nicht alle auf einmal")

    def test_a_digit_that_is_not_ascii_is_no_number(self):
        client = self.login(self.staff)
        for value in ("²", "٣", "１２", "9" * 5000):
            self.assertEqual(client.get(reverse("management:locations_new"), {"circuit": value}).status_code, 200)
        self.assertEqual(leg_list._house_key("²b"), (0, "²b"))
        self.assertEqual(person_list._house_key("1²"), (1, "1²"))
        self.assertEqual(person_list._house_key("12a"), (12, "12a"))

    def test_the_lockout_is_looked_up_for_the_rows_of_the_page_unless_the_filter_needs_all(self):
        with mock.patch.object(person_list, "locked_account_ids", return_value={self.anna.pk}) as lookup:
            plain = person_views.build_list(QueryDict(""))
            self.assertIsNone(plain.locked)
            self.assertEqual(plain.locked_for([self.anna]), {self.anna.pk})
            lookup.assert_called_with(only=[self.anna.pk])
            lookup.reset_mock()
            blocked = person_views.build_list(QueryDict("status=blocked"))
            self.assertEqual(blocked.locked, {self.anna.pk})
            lookup.assert_called_once_with()
            sorted_by_account = person_views.build_list(QueryDict("sort=account"))
            self.assertEqual(sorted_by_account.locked, {self.anna.pk})
        self.assertEqual(person_list.locked_account_ids(only=[self.anna.pk]), set())  # no failed login: nothing

    # M-2, L-8: the printout ----------------------------------------------------------------------------

    def test_every_printout_is_in_the_security_log_without_its_content(self):
        client = self.login(self.staff)
        url = reverse("management:legs_print")
        self.assertEqual(SecurityEvent.objects.filter(event="list_printed").count(), 0)
        with self.assertLogs("leg_platform.security", "WARNING") as logged, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(client.post(url, {"scope": "all"})["Content-Type"], "application/pdf")
        entry = SecurityEvent.objects.get(event="list_printed")
        self.assertEqual((entry.actor_id, entry.account_id, entry.long_retention), (self.staff.pk, None, True))
        self.assertIn("list legs, scope all, 1 entries, 1 detail pages", logged.output[0])
        self.assertNotIn("Geheimname", logged.output[0])
        # A refusal prints nothing and logs nothing.
        client.post(url, {"scope": "selected"})
        with mock.patch.object(areas, "MAX_LIST_ENTRIES", 0):
            client.post(url, {"scope": "list"})
        self.assertEqual(SecurityEvent.objects.filter(event="list_printed").count(), 1)

    def test_the_printout_of_person_details_says_in_the_log_that_it_holds_the_iban(self):
        """L-9: the details of a person hold the IBAN; the log line says so (no value); the list alone does not."""
        client = self.login(self.staff)
        url = reverse("management:persons_print")
        with self.assertLogs("leg_platform.security", "WARNING") as logged, self.captureOnCommitCallbacks(execute=True):
            client.post(url, {"scope": "all"})
            client.post(url, {"scope": "list"})
        self.assertIn("detail pages, with IBAN", logged.output[0])
        self.assertNotIn("IBAN", logged.output[1])

    def test_a_list_with_too_many_entries_cannot_be_selected_from_and_never_ends_in_a_bare_400(self):
        client = self.login(self.staff)
        url = reverse("management:persons_print")
        with mock.patch.object(areas, "MAX_SELECTABLE", 1):
            dialog = client.get(url)
            self.assertContains(dialog, 'value="selected" disabled')
            self.assertContains(dialog, "Es gibt zu viele Einträge für eine Auswahl.")
            self.assertNotContains(dialog, 'name="ids"')
            refused = client.post(url, {"scope": "selected", "ids": [str(self.anna.pk)]})
            self.assertContains(refused, "Es gibt zu viele Einträge für eine Auswahl.")
            self.assertEqual(client.post(url, {"scope": "all"})["Content-Type"], "application/pdf")
            self.assertEqual(client.post(url, {"scope": "selected", "ids": ["1"] * 301}).status_code, 400)
        # The dialog offers at most MAX_SELECTABLE boxes, far below the 1000 fields that Django accepts.
        self.assertLess(areas.MAX_SELECTABLE + 10, settings.DATA_UPLOAD_MAX_NUMBER_FIELDS)

    def test_a_note_of_the_longest_size_is_printed_in_full(self):
        record = PersonRecord.objects.create(person=self.anna, staff_note="Zeile\n" + "n" * (persons.NOTE_MAX_LENGTH - 6))
        prepared = areas.AREAS["persons"].prepare(QueryDict("q=Anna"))
        items = [item for item in prepared.load() if item.pk == self.anna.pk]
        document = areas.AREAS["persons"].document(prepared, items, items, TODAY)
        notes = [value for page in document.details for s in page.sections for label, value in s.facts if label == "Notiz"]
        self.assertEqual([len(note) for note in notes], [persons.NOTE_MAX_LENGTH])  # nothing is cut at the stored maximum
        data = pdf.render(document)
        self.assertTrue(data.startswith(b"%PDF"))
        record.delete()

    # L-9: the fonts are in the package data -------------------------------------------------------------

    def test_the_fonts_of_the_printout_are_in_the_package_data_of_the_wheel(self):
        project = tomllib.loads((BASE / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = project["tool"]["setuptools"]["package-data"]["leg_platform.management.printing"]
        directory = BASE / "leg_platform" / "management" / "printing"
        listed = {path.relative_to(directory).as_posix() for pattern in patterns for path in directory.glob(pattern)}
        shipped = {path.relative_to(directory).as_posix() for path in (directory / "fonts").iterdir()}
        self.assertEqual(listed, shipped)
        self.assertTrue({"fonts/DejaVuSansCondensed.ttf", "fonts/DejaVuSansCondensed-Bold.ttf", "fonts/LICENSE-DejaVu.txt"} <= listed)

    # The designer's findings that a test can see ---------------------------------------------------------

    def test_the_pages_have_the_skip_link_the_main_target_and_the_aligned_wrappers(self):
        client = self.login(self.staff)
        home = client.get(reverse("home")).content.decode()
        self.assertIn('<a class="skip-link" href="#main-content">Zum Inhalt springen</a>', home)
        self.assertIn('<main class="frame-main" id="main-content" tabindex="-1">', home)
        self.assertLess(home.index("skip-link"), home.index("frame-header"))
        self.assertIn("mgmt-page mgmt-wide", client.get(reverse("management:overview")).content.decode())
        self.assertIn("mgmt-page mgmt-settings", client.get(reverse("management:settings")).content.decode())
        self.assertNotIn("skip-link", Client().get(reverse("accounts:login")).content.decode())

    def test_the_settings_are_checked_on_leaving_a_field_with_the_message_of_the_server(self):
        url = reverse("management:settings_check")
        client = self.login(self.staff)
        self.assertNotEqual(client.get(url, {"field": "sender_postal_code", "value": "12"}).json()["message"], "")
        self.assertEqual(client.get(url, {"field": "sender_postal_code", "value": "3063"}).json(), {"message": ""})
        self.assertNotEqual(client.get(url, {"field": "identifier", "value": ""}).json()["message"], "")
        for field in ("is_superuser", "", "id"):
            self.assertEqual(client.get(url, {"field": field, "value": "x"}).status_code, 404)
        self.assertEqual(client.post(url).status_code, 405)

    def test_the_links_of_the_tables_have_names_with_the_subject_and_lead_to_the_metering_point(self):
        client = self.login(self.staff)
        persons_page = client.get(reverse("management:persons")).content.decode()
        self.assertIn('aria-label="Zur Person: Anna Muster"', persons_page)
        legs_page = client.get(reverse("management:legs")).content.decode()
        self.assertIn('aria-label="Zur LEG-Seite: LEG Geheimname"', legs_page)
        point_url = reverse("management:metering_points_detail", args=[self.point.pk])
        detail = client.get(reverse("management:locations_detail", args=[self.place.pk])).content.decode()
        self.assertEqual(detail.count(f'href="{point_url}"'), 2)  # the link column and the card of a phone
        self.assertIn(f'aria-label="Zur Detailseite des Messpunkts: {self.point.number}"', detail)
        self.assertIn('class="mgmt-grip"', detail)
        leg = client.get(reverse("management:legs_detail", args=[self.leg.pk])).content.decode()
        self.assertEqual(leg.count(f'href="{point_url}"'), 2)

    def test_the_empty_history_has_a_hint_and_the_first_card_of_a_person_is_called_details_in_the_customers_words(self):
        client = self.login(self.staff)
        point_page = client.get(reverse("management:metering_points_detail", args=[self.empty_point().pk])).content.decode()
        self.assertIn("Noch keine Einträge.", point_page)
        person = client.get(reverse("management:persons_detail", args=[self.anna.pk])).content.decode()
        self.assertIn("<h2>Angaben</h2>", person)

    def empty_point(self):
        return make_metering_point(9, Direction.FEED_IN, self.empty_place)

    def test_the_stylesheets_have_the_tokens_and_rules_of_the_review(self):
        portal = (BASE / "leg_platform/static/css/portal.css").read_text(encoding="utf-8")
        management = (BASE / "leg_platform/management/static/management/css/management.css").read_text(encoding="utf-8")
        frame = (BASE / "leg_platform/static/css/frame.css").read_text(encoding="utf-8")
        for token in ("--ok-text:", "--warn-icon:"):
            self.assertEqual(portal.count(token), 2, token)  # the light and the dark values
        self.assertIn("#b45309", portal)
        self.assertIn("#f5a623", portal)
        self.assertIn("fill: var(--warn-icon);", management)
        self.assertIn("color: var(--ok-text);", management)
        self.assertNotIn(".mgmt-direction.is-feed-in {\n  background: color-mix(in srgb, var(--ok) 16%, var(--panel));\n  color: var(--ok);", management)
        self.assertIn(".mgmt-page .btn:focus-visible", management)
        self.assertIn(".mgmt-grip:focus-visible {\n  outline: 3px solid var(--accent);", management)
        self.assertIn("min-height: 44px;\n  color: var(--muted);", management)  # the sort button
        self.assertIn(".frame-item.is-active {\n  border-left-color: var(--accent);\n  background: var(--warm);", frame)
        # The scripts that the pages load exist.
        for script in ("table-fit.js", "settings-validate.js", "list.js", "info-bubble.js"):
            self.assertTrue((BASE / "leg_platform/management/static/management/js" / script).is_file(), script)


class PdfLongNoteTests(SimpleTestCase):
    def test_a_note_of_the_longest_size_runs_over_the_pages_instead_of_failing(self):
        columns = [Column("Name", 1)]
        text = "Zeile ohne Ende " * 400 + "\n" + "w" * 1500
        page = DetailPage("Titel", "", [], [Section("Notiz", [("Notiz", text[: persons.NOTE_MAX_LENGTH])])])
        data = pdf.render(PrintDocument("Titel", [("Datum", "x")], Table(columns, [["a"]]), [page]))
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreaterEqual(len(re.findall(rb"/Type /Page\b", data)), 2)  # the list and the page of the note
