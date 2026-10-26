"""The overview and the printing of the lists (D-082, D-086): the rows and the button of the overview (the problem
rows of all three lists, for every staff account), the dialog "Print list", the PDF (the table like the list, the lines
above it, the detail pages in list order, with everything: names of persons, LEGs, the table of metering points),
hostile texts, the bounds, a forged selection and the access of the dialog. The PDF is checked at the seam of the
document (``areas``: texts only) and by its bytes (page count, headers); a text cannot be read back from a compressed
PDF without another package, so the escaping is proven with ReportLab's own paragraph parser (``getPlainText``: what
the paragraph would print)."""

import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Paragraph

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import persons
from leg_platform.community.models import Direction, GridOperator, Leg, Location, MeteringPoint, PersonRecord
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)
from leg_platform.management import navigation
from leg_platform.management.printing import areas, pdf
from leg_platform.management.printing.document import Column, DetailPage, PrintDocument, Section, Table

TODAY = timezone.localdate()
EARLIER = TODAY - timedelta(days=30)
HOSTILE = '<b>x</b> & <img src="http://127.0.0.1:9/a.png"/> <font color="eval(1)">y</font> &amp; &#60;'
ALPHABETS = "äöüÄÖÜéèàçßñ ćčđšžłőűńśźżğıİşĞ€–·→✓● ΑΒΓ АБВ"


def pages(data):
    """The number of pages of a PDF (the page objects are not compressed)."""
    return len(re.findall(rb"/Type /Page\b", data))


class PdfRenderTests(SimpleTestCase):
    def test_a_text_is_never_markup(self):
        style = ParagraphStyle("t")
        for text in (HOSTILE, "<para><b>", "a < b > c & d", "x" * 50 + "<", "</b></font>"):
            with self.subTest(text=text):
                self.assertEqual(Paragraph(pdf.markup(text), style).getPlainText(), text)
        self.assertEqual(pdf.markup("a\nb"), "a<br/>b")

    def test_control_characters_go_and_a_long_text_is_cut(self):
        self.assertEqual(pdf.plain("a\x00b\x07c\u0085d", 20), "abcd")
        cut = pdf.plain("x" * 400, pdf.MAX_CELL_CHARS)
        self.assertEqual(len(cut), pdf.MAX_CELL_CHARS)
        self.assertTrue(cut.endswith("…"))
        self.assertEqual(pdf.plain(None, 5), "")

    def test_the_font_has_the_letters_of_names(self):
        pdf.register_fonts()
        for face in (pdf.REGULAR, pdf.BOLD):
            glyphs = pdfmetrics.getFont(face).face.charToGlyph
            missing = [char for char in ALPHABETS if char != " " and ord(char) not in glyphs]
            self.assertEqual(missing, [], face)

    def document(self, rows=3, **extra):
        columns = [Column("Name", 3), Column("Nr", 1), Column("Ort", 2)]
        cells = [[HOSTILE + " " + ALPHABETS, "1", "Zürich\nGenève"], ["W" * 400, "2", "Bern"], ["a\x00b", "3", "x"]]
        cells += [[f"Name {index}", str(index), "Bern"] for index in range(rows)]
        detail = DetailPage(
            "<i>Titel</i> " + "T" * 300, "sub", [HOSTILE],
            [Section("Abschnitt", [("Bemerkung", "Zeile\n" + "y" * 5000), (HOSTILE, "ß")], Table(columns, cells[:5]), "Hinweis")],
        )
        return PrintDocument(
            "Verwaltung · <LEGs>", [("Datum", "17.10.2026"), ("Suche", HOSTILE)], Table(columns, cells), [detail] * 2, **extra
        )

    def test_hostile_texts_a_very_long_name_and_all_alphabets_make_a_pdf(self):
        data = pdf.render(self.document())
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreaterEqual(pages(data), 3)  # the list and one new page per detail page
        self.assertEqual(pages(pdf.render(self.document(), compress=False)), pages(data))
        # The markup of a text was never read: the address is printed as text, never fetched or linked.
        raw = pdf.render(self.document(), compress=False)
        self.assertNotIn(b"/URI", raw)
        self.assertNotIn(b"/Link", raw)

    def test_a_long_list_takes_many_pages_in_a_moment(self):
        data = pdf.render(self.document(rows=1200))
        self.assertGreater(pages(data), 20)

    def test_the_bound_of_the_rows_counts_facts_and_tables(self):
        self.assertEqual(self.document(rows=10).row_count(), 13 + 2 * (2 + 5))


class Fixture(TestCase):
    """The data and the accounts shared by the tests of the overview and of the printing."""

    @classmethod
    def setUpTestData(cls):
        cls.plain = create_active_user("plain.person@example.org")
        cls.staff = staff_user("staff.person@example.org")
        operator = GridOperator.load()
        operator.name = "Netzbetreiber Beispiel"
        operator.save()

        cls.circuit = make_circuit("TRA21359", "Nord")
        cls.place = make_location(
            "Musterweg", "12", transformer_circuit=cls.circuit, address_correction_open=True,
            remark="Schlüssel beim Hauswart", needs_confirmation=True,
        )
        make_location("Lindenweg", "7", transformer_circuit=cls.circuit)
        cls.anna = create_active_user(
            "anna.person@example.org", first_name="<b>Anna</b>", last_name="Mu&ster", street="Dorfstrasse",
            house_number="1", town="Ittigen", swiss_postal_code=3063,
        )
        cls.long_name = create_active_user(
            "long.person@example.org", first_name="L" * 30, last_name="W" * 30, organisation_name="O" * 60,
            street="S" * 80,
        )
        PersonRecord.objects.create(person=cls.anna, staff_note="Notiz <u>geheim</u>", iban="CH9300762011623852957")

        # One LEG with a problem (the quota is below the legal minimum), one without, one archived.
        cls.leg = make_leg(
            '<b>LEG</b> & <img src="http://127.0.0.1:9/a.png"/>', production_quota_percent=Decimal("3"),
            quota_checked_on=TODAY, remark="Geheime <i>Bemerkung</i>",
        )
        cls.fine = make_leg("LEG Ruhig", production_quota_percent=Decimal("40"), quota_checked_on=TODAY)
        cls.old = make_leg("LEG Alt", is_archived=True)
        cls.point_one = make_metering_point(1, Direction.CONSUMPTION, cls.place)
        cls.point_two = make_metering_point(2, Direction.FEED_IN, cls.place)
        make_metering_point(3, Direction.CONSUMPTION)  # nothing: three problems
        for point in (cls.point_one, cls.point_two):
            assign_leg(point, cls.leg, valid_from=EARLIER)
        assign_leg(cls.point_one, cls.fine, valid_from=EARLIER - timedelta(days=60), valid_to=EARLIER - timedelta(days=1))
        assign_person(cls.point_one, cls.anna, EARLIER)

    def setUp(self):
        self.client = self.login(self.staff)

    def login(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client


@tag("mariadb")
class OverviewTests(Fixture):
    URL = reverse("management:overview")

    def test_the_button_and_the_rows_count_the_lists_with_problems(self):
        page = self.client.get(self.URL)
        # One location (address correction), one LEG (quota), two metering points (one without a person,
        # one without anything): 1 + 1 + 2; the persons have no problems and no row.
        for text in (
            "Probleme filtern (4)", "1 Standort mit Problem", "1 LEG mit Problem", "2 Messpunkte mit Problem",
            "Zur Liste Standorte", "Zur Liste LEGs", "Zur Liste Messpunkte", "erscheint nur, wenn eine Liste ein Problem hat",
        ):
            self.assertContains(page, text)
        # The button opens the first affected list of the menu with the filter on; every row does the same for its list.
        self.assertContains(page, 'class="btn btn-warning-outline" href="/management/locations/?problems=1"')
        self.assertContains(page, 'href="/management/legs/?problems=1"')
        self.assertContains(page, 'href="/management/metering-points/?problems=1"')
        self.assertNotContains(page, "Personen mit Problem")
        # The sum is the count of the buttons of the lists themselves.
        self.assertContains(self.client.get(reverse("management:legs")), "Probleme filtern (1)")
        self.assertContains(self.client.get(reverse("management:metering_points")), "Probleme filtern (2)")

    def test_without_problems_there_is_no_button_and_a_calm_text(self):
        # Archived entries have no problems: nothing is left that could have one.
        Location.objects.update(is_archived=True)
        Leg.objects.update(is_archived=True)
        MeteringPoint.objects.update(is_archived=True)
        page = self.client.get(self.URL)
        self.assertContains(page, "Keine Probleme.")
        for text in ("Probleme filtern", "mit Problem", "mgmt-overview-row"):
            self.assertNotContains(page, text)

    def test_the_overview_and_the_list_of_persons_have_no_print_button_and_no_triangle(self):
        self.assertNotContains(self.client.get(self.URL), "Drucken")
        page = self.client.get(reverse("management:persons"))
        self.assertNotContains(page, "mgmt-warning")
        self.assertContains(page, "Drucken")  # the lists have it; the overview does not

    def test_the_menu_has_the_entries_in_the_order_of_the_specification_and_the_link_to_the_mail_texts(self):
        keys = [entry["key"] for entry in navigation.menu_entries(self.staff)]
        self.assertEqual(
            keys, ["overview", "processes", "persons", "locations", "legs", "metering_points", "settings", "contracts"]
        )
        # The link to the Django administration keeps its permission model (D-086); "Contracts" follows it (owner design).
        editor = staff_user("staff.mail@example.org")
        editor.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="change_emailtext"))
        editor = type(editor).objects.get(pk=editor.pk)
        entries = navigation.menu_entries(editor)
        last = entries[-2]
        self.assertEqual((last["key"], last["url"]), ("email_texts", "/admin/accounts/emailtext/"))
        self.assertEqual(entries[-1]["key"], "contracts")
        home = self.login(editor).get(reverse("management:overview")).content.decode()
        self.assertIn('href="/admin/accounts/emailtext/"', home)
        self.assertIn("Mailtexte", home)

    def test_the_small_buttons_have_one_height_everywhere(self):
        from pathlib import Path

        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "leg_platform/management/static/management/css/management.css").read_text(encoding="utf-8")
        self.assertIn(".register-clear .btn.btn-xs {\n  min-height: 36px;", css)
        self.assertNotIn(".register-clear .mgmt-person .btn.btn-xs", css)


@tag("mariadb")
class PrintDocumentTests(Fixture):
    """What a list prints, at the seam of the document."""

    def document(self, key, query="", *, details=True, ids=None):
        area = areas.AREAS[key]
        prepared = area.prepare(QueryDict(query))
        items = prepared.load()
        chosen = [item for item in items if ids is None or item.pk in ids] if details else []
        return prepared, area.document(prepared, items, chosen, TODAY)

    @staticmethod
    def texts(document):
        found = [document.title]
        found += [text for pair in document.info for text in pair]
        found += [column.label for column in document.table.columns]
        found += [cell for row in document.table.rows for cell in row]
        for page in document.details:
            found += [page.title, page.subtitle, *page.problems]
            for section in page.sections:
                found += [section.title, section.note]
                found += [text for pair in section.facts for text in pair]
                if section.table:
                    found += [column.label for column in section.table.columns]
                    found += [cell for row in section.table.rows for cell in row]
        return "\n".join(found)

    def test_the_lines_above_the_list_name_the_title_the_date_the_search_the_filters_and_the_sorting(self):
        _prepared, document = self.document("legs", "q=Ruhig&display=all&sort=quota&dir=desc", details=False)
        self.assertEqual(document.title, "Verwaltung · LEGs")
        self.assertEqual(
            document.info,
            [("Datum", TODAY.strftime("%d.%m.%Y")), ("Suche", "Ruhig"), ("Sortierung", "Produktionsquote, absteigend")],
        )
        _prepared, document = self.document("legs", "display=archived&problems=1", details=False)
        self.assertEqual(
            [line for line in document.info if line[0] in ("Anzeige", "Filter", "Suche")],
            [("Anzeige", "archiviert"), ("Filter", "Nur Probleme")],
        )
        _prepared, document = self.document("metering_points", "step=notify&leg=%d" % self.fine.pk, details=False)
        self.assertEqual(
            [line for line in document.info if line[0] in ("Schnellfilter", "LEG")],
            [("Schnellfilter", "Bei Netzbetreiber Beispiel anmelden"), ("LEG", "LEG Ruhig")],
        )
        # An unknown value is the default, as on the page; nothing of the address is printed that was not parsed.
        _prepared, document = self.document("legs", "sort=password&dir=x&display=zzz&evil=1", details=False)
        self.assertEqual(document.info[-1], ("Sortierung", "LEG, aufsteigend"))
        self.assertNotIn("zzz", self.texts(document))

    def test_the_columns_are_those_of_the_list_and_a_problem_is_text_in_the_cell(self):
        _prepared, legs = self.document("legs", details=False)
        self.assertEqual(
            [c.label for c in legs.table.columns], ["LEG", "Produktionsquote", "Teilnehmer", "Rabattstufe", "Messpunkte"]
        )
        row = next(row for row in legs.table.rows if row[0].startswith("<b>LEG</b>"))
        self.assertIn("Problem:", row[1])
        self.assertIn("3 %", row[1])
        self.assertEqual(len(legs.table.rows), 2)  # the archived LEG is not in this view
        _prepared, places = self.document("locations", details=False)
        self.assertEqual(places.table.rows[0][0], "Lindenweg 7")  # sorted by the address
        self.assertEqual(places.table.rows[1][0], "Musterweg 12 (neu)\nProblem: Adresskorrektur offen")
        _prepared, points = self.document("metering_points", "sort=number&dir=desc", details=False)
        self.assertEqual([c.label for c in points.table.columns], ["Messpunkt", "Standort", "LEG", "Status"])
        self.assertIn("Dieser Messpunkt ist keiner LEG zugeordnet.", points.table.rows[0][0])
        self.assertIn("Dieser Messpunkt ist keiner Person zugeordnet.", points.table.rows[1][0])
        self.assertEqual(points.table.rows[0][1:3], ["–", "–"])
        _prepared, people = self.document("persons", details=False)
        self.assertEqual([c.label for c in people.table.columns][:3], ["Name", "Kundennummer", "Adresse"])
        self.assertNotIn("Problem", self.texts(people))

    def test_detail_pages_follow_the_order_of_the_list_and_may_be_chosen(self):
        _prepared, everything = self.document("legs", "sort=name&dir=desc")
        self.assertEqual([page.title for page in everything.details], ["LEG Ruhig", self.leg.name])
        _prepared, chosen = self.document("legs", "sort=name&dir=desc", ids={self.leg.pk})
        self.assertEqual([page.title for page in chosen.details], [self.leg.name])
        self.assertEqual(len(chosen.table.rows), 2)  # the list is always complete
        page = chosen.details[0]
        self.assertEqual([section.title for section in page.sections][:3], ["Produktionsquote", "Stammdaten", "Zusammensetzung"])
        self.assertIn("Geheime <i>Bemerkung</i>", self.texts(chosen))
        self.assertTrue(any("Produktionsquote unter 5 %" in problem for problem in page.problems))

    def test_every_area_prints_everything_of_its_page(self):
        _prepared, point = self.document("metering_points", "q=%s" % self.point_one.number)
        text = self.texts(point)
        for expected in ("Bezug", "Musterweg 12", "TRA21359", "<b>Anna</b> Mu&ster", "registriert"):
            self.assertIn(expected, text)
        _prepared, place = self.document("locations", "q=Musterweg")
        text = self.texts(place)
        for expected in ("Schlüssel beim Hauswart", "TRA21359 · Nord", "Zuordnungen", "<b>Anna</b> Mu&ster", "Problem: Adresskorrektur offen"):
            self.assertIn(expected, text)
        # The page of a LEG has the table of its metering points with the names of the occupants.
        _prepared, leg = self.document("legs", "sort=name&dir=desc", ids={self.leg.pk})
        text = self.texts(leg)
        for expected in ("Messpunkte dieser LEG", "Musterweg 12", "<b>Anna</b> Mu&ster"):
            self.assertIn(expected, text)
        _prepared, person = self.document("persons", "q=Anna")
        text = self.texts(person)
        for expected in (
            "Notiz <u>geheim</u>", "CH93 0076 2011 6238 5295 7", "anna.person@example.org", "Messpunkte und Zuordnungen",
            "Netzbetreiber Beispiel-Anmeldung", "Netzbetreiber Beispiel-Kundennummer", "Musterweg 12",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("BKW", text)

    def test_the_page_of_a_person_prints_the_shares_as_a_history(self):
        persons.add_share_change(self.anna, 5, EARLIER - timedelta(days=10))
        persons.add_share_change(self.anna, 8, EARLIER)
        persons.add_share_change(self.anna, 99, TODAY + timedelta(days=5))  # not valid yet: not the current number
        _prepared, document = self.document("persons", "q=Anna")
        section = next(section for section in document.details[0].sections if section.title == "Genossenschaft")
        self.assertEqual(list(section.facts), [("Anteile", "8")])
        self.assertEqual([column.label for column in section.table.columns], ["Gilt ab", "Anzahl Anteile"])
        self.assertEqual(
            [list(row) for row in section.table.rows],
            [
                [(TODAY + timedelta(days=5)).strftime("%d.%m.%Y"), "99"], [EARLIER.strftime("%d.%m.%Y"), "8"],
                [(EARLIER - timedelta(days=10)).strftime("%d.%m.%Y"), "5"],
            ],
        )
        self.assertNotIn("Zeitraum", self.texts(document))

    def test_the_search_never_finds_a_remark_in_the_printout(self):
        _prepared, document = self.document("locations", "q=Hauswart", details=False)
        self.assertEqual(document.table.rows, [])


@tag("mariadb")
class PrintViewTests(Fixture):
    ROUTES = {
        "persons": "management:persons_print", "locations": "management:locations_print",
        "legs": "management:legs_print", "metering_points": "management:metering_points_print",
    }

    def test_the_dialog_has_the_three_options_the_entries_and_a_way_back(self):
        client = self.client
        for key, name in self.ROUTES.items():
            with self.subTest(key=key):
                page = client.get(reverse(name))
                for text in (
                    "Liste drucken", "Nur die Liste", "Liste und Detailseiten aller angezeigten Einträge",
                    "Liste und Detailseiten ausgewählter Einträge", "PDF erstellen", "Abbrechen",
                    "in der Reihenfolge der Liste, je auf einer neuen Seite.", 'role="dialog"', 'method="post"',
                ):
                    self.assertContains(page, text)
                self.assertContains(page, 'name="ids"')
                self.assertContains(page, f'href="{reverse(f"management:{key}")}"')
        page = client.get(reverse("management:legs_print"))
        self.assertContains(page, "Es werden 2 Einträge gedruckt.")
        self.assertContains(page, "&lt;b&gt;LEG&lt;/b&gt;")  # the names of the entries are escaped
        self.assertNotContains(page, "<b>LEG</b>")

    def test_the_dialog_reads_the_state_of_the_list_from_the_address(self):
        client = self.client
        page = client.get(reverse("management:legs_print") + "?q=Ruhig&sort=quota&dir=desc&evil=1")
        self.assertContains(page, "Es wird 1 Eintrag gedruckt.")
        self.assertContains(page, 'href="/management/legs/?q=Ruhig&amp;sort=quota&amp;dir=desc"')
        page = client.get(reverse("management:legs_print") + "?problems=1")
        self.assertContains(page, "Es wird 1 Eintrag gedruckt.")
        self.assertContains(client.get(reverse("management:legs_print") + "?q=zzz"), "Es werden 0 Einträge gedruckt.")

    def test_the_button_is_on_each_list_left_of_the_create_button_and_never_on_the_phone_layout(self):
        client = self.client
        for key, create in (("legs", "LEG anlegen"), ("locations", "Standort anlegen"), ("metering_points", "Messpunkt anlegen")):
            with self.subTest(key=key):
                html = client.get(reverse(f"management:{key}") + "?sort=" + {"legs": "quota", "locations": "circuit", "metering_points": "leg"}[key]).content.decode()
                link = re.search(r'<a class="btn btn-secondary-action mgmt-desktop-only" href="([^"]+)">Drucken</a>', html)
                self.assertIsNotNone(link)
                self.assertTrue(link.group(1).startswith(reverse(self.ROUTES[key])))
                self.assertIn("sort=", link.group(1))
                self.assertLess(html.index(">Drucken<"), html.index(create))
        persons = client.get(reverse("management:persons")).content.decode()
        self.assertIn('mgmt-desktop-only" href="/management/persons/print/">Drucken', persons)
        # The class that hides the button on a phone is on the button; no phone layout (the cards) has a print link.
        self.assertNotIn("print/", persons[persons.index("mgmt-cards-view"):])

    def test_the_pdf_of_every_list(self):
        client = self.client
        for key, name in self.ROUTES.items():
            with self.subTest(key=key):
                url = reverse(name)
                only = client.post(url, {"scope": "list"})
                self.assertEqual(only.status_code, 200)
                self.assertEqual(only["Content-Type"], "application/pdf")
                self.assertTrue(only.content.startswith(b"%PDF"))
                self.assertIn("no-store", only["Cache-Control"])
                self.assertEqual(only["Content-Disposition"], f'attachment; filename="{areas.AREAS[key].filename}"')
                everything = client.post(url, {"scope": "all"})
                self.assertTrue(everything.content.startswith(b"%PDF"))
                self.assertGreater(pages(everything.content), pages(only.content))
        # One list page and one page for each of the two LEGs.
        self.assertEqual(pages(client.post(reverse("management:legs_print"), {"scope": "all"}).content), 3)

    def test_the_state_of_the_address_decides_what_is_printed_not_the_form(self):
        client = self.client
        url = reverse("management:legs_print") + "?q=Ruhig"
        # A form field that tries to widen the list does nothing: the list is parsed from the address only.
        data = {"scope": "all", "q": "", "display": "archived", "problems": "1"}
        self.assertEqual(pages(client.post(url, data).content), 2)

    def test_a_forged_selection_prints_only_entries_of_the_list(self):
        client = self.client
        url = reverse("management:legs_print")
        forged = ["999999", "abc", "1; DROP", "-1", "", str(self.old.pk)]  # the archived LEG is not in this list
        only_foreign = client.post(url, {"scope": "selected", "ids": forged})
        self.assertEqual(only_foreign["Content-Type"], "text/html; charset=utf-8")
        self.assertContains(only_foreign, "Wählen Sie mindestens einen Eintrag aus.")
        mixed = client.post(url, {"scope": "selected", "ids": [*forged, str(self.fine.pk)]})
        self.assertEqual(mixed["Content-Type"], "application/pdf")
        self.assertEqual(pages(mixed.content), 2)  # the list and the one page of the one valid entry
        # The same entry twice is one page; an unknown scope and a flood of ids are refused.
        self.assertEqual(pages(client.post(url, {"scope": "selected", "ids": [str(self.fine.pk)] * 3}).content), 2)
        self.assertEqual(client.post(url, {"scope": "everything"}).status_code, 400)
        self.assertEqual(client.post(url, {"scope": "selected", "ids": ["1"] * 2001}).status_code, 400)
        self.assertEqual(client.post(url, {}).status_code, 400)

    def test_the_bounds_refuse_with_a_message(self):
        client = self.client
        url = reverse("management:legs_print")
        with mock.patch.object(areas, "MAX_LIST_ENTRIES", 1):
            dialog = client.get(url)
            self.assertContains(dialog, "Die Liste ist zu lang zum Drucken. Sie hat mehr als 1 Einträge.")
            self.assertNotContains(dialog, "PDF erstellen")
            self.assertNotContains(dialog, 'type="radio"')
            refused = client.post(url, {"scope": "list"})
            self.assertEqual(refused["Content-Type"], "text/html; charset=utf-8")
            self.assertContains(refused, "zu lang zum Drucken")
        with mock.patch.object(areas, "MAX_DETAIL_PAGES", 1):
            dialog = client.get(url)
            self.assertContains(dialog, 'value="all" disabled')  # "all" cannot be chosen
            self.assertEqual(client.post(url, {"scope": "list"})["Content-Type"], "application/pdf")
            for data in ({"scope": "all"}, {"scope": "selected", "ids": [str(self.leg.pk), str(self.fine.pk)]}):
                refused = client.post(url, data)
                self.assertContains(refused, "Es können höchstens 1 Detailseiten auf einmal gedruckt werden.")
            self.assertEqual(
                client.post(url, {"scope": "selected", "ids": [str(self.fine.pk)]})["Content-Type"], "application/pdf"
            )
        with mock.patch.object(areas, "MAX_ROWS", 5):
            self.assertContains(client.post(url, {"scope": "all"}), "Der Ausdruck ist zu gross.")

    def test_a_pdf_that_cannot_be_drawn_is_refused_without_a_text_in_the_log(self):
        client = self.client
        with mock.patch.object(pdf, "render", side_effect=pdf.PdfError("LayoutError")):
            with self.assertLogs("leg_platform.management.printing.views", "ERROR") as logged:
                response = client.post(reverse("management:legs_print"), {"scope": "all"})
        self.assertContains(response, "Das PDF konnte nicht erstellt werden.")
        self.assertEqual(logged.output, ["ERROR:leg_platform.management.printing.views:The PDF of a list could not be created (LayoutError)."])

    def test_a_normal_account_gets_no_print_dialog_and_no_pdf(self):
        client = self.login(self.plain)
        for name in self.ROUTES.values():
            for method in ("get", "post"):
                self.assertEqual(getattr(client, method)(reverse(name), {"scope": "list"}).status_code, 404, (name, method))

    def test_the_pdf_is_not_cached_and_the_file_name_is_fixed(self):
        response = self.client.post(reverse("management:persons_print") + "?q=<script>", {"scope": "list"})
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="persons.pdf"')
        self.assertNotIn(b"<script>", response.content)
