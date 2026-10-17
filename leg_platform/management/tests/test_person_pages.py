"""The pages of the persons (D-081): the list (columns, states, filters, search, sort, the phone cards), the detail page (cards,
the table of the metering points with its sorting and the display of a termination, the history of the shares), the
three dialogs and the correction of the address. Every staff account sees every person and has every action
(D-086). The rules themselves are tested at service level (``community/tests/test_persons.py``); here only what the
views add. The negative authorization of every URL for anonymous, non-staff and second-factor-less accounts is in
``test_access.py`` (the route walk)."""

import uuid
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses.models import Address, Directory
from leg_platform.community import history, person_list, persons
from leg_platform.community.models import Direction, GridOperator, MeteringPoint, PersonRecord, ShareChange
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)
from leg_platform.management import person_views

User = get_user_model()
TODAY = timezone.localdate()
EARLIER = TODAY - timedelta(days=30)
LATER = TODAY + timedelta(days=10)
LIST = reverse("management:persons")
HOSTILE = ("https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)")


def fmt_date(day):
    """A day as the pages show it."""
    return day.strftime("%d.%m.%Y")


@tag("mariadb")
class PersonPageTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=generation, data_date=TODAY, imported_at=timezone.now(), checksum="0" * 64, row_count=1
        )
        Address.objects.create(
            generation=generation, street="Musterweg", house_number="12", postal_code="3063", locality="Ittigen"
        )

        cls.staff = staff_user("staff.person@example.org")
        operator = GridOperator.load()
        operator.name = "Netzbetreiber Beispiel"
        operator.save()

        cls.anna = create_active_user(
            "anna.person@example.org", first_name="Anna", last_name="Muster", salutation="mrs", street="Dorfstrasse",
            house_number="1", town="Ittigen", swiss_postal_code=3063, phone_number="+41791234567",
        )
        cls.company = create_active_user(
            "verwaltung.person@example.org", organisation_name="Beispiel Verwaltung AG", first_name="Lea",
            last_name="Probe", street="Verwaltungsstrasse", house_number="4", town="Bern", swiss_postal_code=3011,
            address_line_1="c/o Treuhand",
        )
        cls.blocked = create_active_user("blocked.person@example.org", first_name="Ruth", last_name="Gesperrt")
        cls.blocked.set_status(User.Status.BLOCKED)
        cls.gone = create_active_user("gone.person@example.org", first_name="Max", last_name="Inaktiv")
        cls.gone.set_status(User.Status.DEACTIVATED)
        cls.fixable = create_active_user(
            "fixable.person@example.org", first_name="Emil", last_name="Probe", street="musterweg", house_number="12",
            town="Bern", swiss_postal_code=3063,
        )
        cls.unverified = User.objects.create_user("unverified.person@example.org")
        cls.record = PersonRecord.objects.create(
            person=cls.anna, staff_note="Telefonat am 12.09.", operator_customer_number="OP-4711",
            iban="CH9300762011623852957",
        )
        # The history of the shares of Anna: two entries, entered with the latest date first.
        persons.add_share_change(cls.anna, 5, EARLIER)
        persons.add_share_change(cls.anna, 2, EARLIER - timedelta(days=100))

        cls.place = make_location("Musterweg", "12", town="Ittigen", postal_code="3063")
        cls.other_place = make_location("Werkstrasse", "5", town="Ittigen", postal_code="3063")
        cls.leg = make_leg("LEG Musterquartier")
        cls.registered = make_metering_point(1, Direction.CONSUMPTION, cls.place)
        cls.confirmed = make_metering_point(
            2, Direction.FEED_IN, cls.other_place, registered_with_operator_on=EARLIER, confirmed_on=EARLIER
        )
        cls.unnumbered = MeteringPoint.objects.create(number=None, direction=Direction.CONSUMPTION)
        assign_person(cls.registered, cls.anna, EARLIER)
        assign_person(cls.confirmed, cls.anna, EARLIER, valid_to=LATER)
        assign_person(cls.unnumbered, cls.anna, EARLIER)
        assign_leg(cls.registered, cls.leg, EARLIER)
        for index in (7, 8):
            assign_person(make_metering_point(index, registered_with_operator_on=EARLIER), cls.company, EARLIER)

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def detail(self, person, *names):
        return reverse(f"management:persons_{names[0] if names else 'detail'}", args=[person.pk])

    @staticmethod
    def shown_count():
        """Every account with a customer number that is not deactivated is a person, also the staff accounts of
        these tests."""
        return User.objects.filter(customer_number__isnull=False).exclude(status=User.Status.DEACTIVATED).count()

    # The list ---------------------------------------------------------------------------------------

    def test_the_list_shows_the_columns_the_states_the_words_and_the_two_lines_of_a_company(self):
        page = self.login(self.staff).get(LIST)
        for text in (
            "Personen", "Alle Kundinnen und Kunden der LEG.", "Name, Kundennummer oder E-Mail", "Anna Muster",
            "Beispiel Verwaltung AG", self.anna.customer_number_display, "Dorfstrasse 1", "3 · Aufnahme",
            "2 · angemeldet", "Ruth Gesperrt", "Gesperrt", "Aktiv", "Kundennummer", "Adresse", "Messpunkte", "Konto",
            "Die sechsstellige Nummer, unter der die Person im Portal geführt wird.", "Strasse und Hausnummer der Rechnungsadresse.",
            "Name der Person, oder der Firma bei Verwaltungen und Firmen.", "Information: Konto", "Information: Messpunkte",
            "angemeldet, wenn alle bei der Netzbetreiber Beispiel angemeldet sind.",
            "Gesperrt: vorübergehend gesperrt, zum Beispiel nach Fehlversuchen.", f"1–{self.shown_count()} von {self.shown_count()} Personen",
            "mit Messpunkt", "ohne Messpunkt", "archiviert",
        ):
            self.assertContains(page, text)
        # The contact person of a company stands below its name; the name is plain text, the icon is the link.
        self.assertContains(page, 'mgmt-person-contact" title="Lea Probe"')
        self.assertContains(page, f'href="{self.detail(self.anna)}"')
        self.assertContains(page, 'class="mgmt-grip"')
        # Nothing that has no specified effect: no button "Start registration", no triangle; no inactive account,
        # no unverified one, no note, no customer number at the grid operator and no shares (the shares are on the
        # page of the person only). The button "Print" is there (a desktop only, D-082), and left of it nothing: the
        # button "Create person" (D-087) is the one on its right.
        for text in ("Anmeldung starten", "mgmt-warning", "Max Inaktiv", "Emil Probe " + "x",
                     "unverified", "Telefonat", "OP-4711", "CH93 0076", "Anteile", "BKW"):
            self.assertNotContains(page, text)
        self.assertContains(page, ">Drucken</a>")
        self.assertContains(page, ">Person anlegen</a>")
        content = page.content.decode()
        self.assertLess(content.index(">Drucken</a>"), content.index(">Person anlegen</a>"))
        # On a phone one card per person, the dot has the word as its accessible name.
        self.assertContains(page, 'role="img" aria-label="Gesperrt"')
        self.assertContains(page, 'class="mgmt-cards-view"')
        self.assertContains(page, 'class="frame-item is-active" href="/management/persons/" aria-current="page"')

    def test_the_filters_the_search_and_the_sort_come_from_the_address(self):
        client = self.login(self.staff)

        def shown(**params):
            content = client.get(LIST, params).content.decode()
            found = [name for name in ("Anna Muster", "Beispiel Verwaltung AG", "Ruth Gesperrt", "Max Inaktiv", "Emil Probe")
                     if f'title="{name}"' in content]
            return sorted(found, key=lambda name: content.index(f'title="{name}"'))

        self.assertEqual(shown(), ["Anna Muster", "Beispiel Verwaltung AG", "Emil Probe", "Ruth Gesperrt"])
        self.assertEqual(shown(status="active"), ["Anna Muster", "Beispiel Verwaltung AG", "Emil Probe"])
        self.assertEqual(shown(status="blocked"), ["Ruth Gesperrt"])
        self.assertEqual(shown(status="archived"), ["Max Inaktiv"])  # the archive only through its filter
        self.assertEqual(shown(points="with"), ["Anna Muster", "Beispiel Verwaltung AG"])
        self.assertEqual(shown(points="without"), ["Emil Probe", "Ruth Gesperrt"])
        self.assertEqual(shown(points="with", status="blocked"), [])  # the filters act together
        self.assertEqual(shown(status="x", points="x"), shown())  # an unknown value is the default
        # Name, customer number (with or without the space), address, town, e-mail; the case and the accent do not matter.
        for text in ("MUSTER", "anna.person@", self.anna.customer_number_display, "dorfstrasse 1", "ittigen"):
            self.assertIn("Anna Muster", shown(q=text), text)
        self.assertEqual(shown(q="lea probe"), ["Beispiel Verwaltung AG"])
        self.assertEqual(shown(q="Telefonat"), [])  # the note is never searched
        self.assertEqual(shown(q="OP-4711"), [])
        self.assertEqual(shown(q="CH9300762011623852957"), [])
        # Sorting: by default the name; every column, up and down.
        self.assertEqual(shown(sort="name", dir="desc")[0], "Ruth Gesperrt")
        self.assertEqual(shown(sort="points", dir="desc")[:2], ["Anna Muster", "Beispiel Verwaltung AG"])
        self.assertEqual(shown(sort="account", dir="desc")[0], "Ruth Gesperrt")
        self.assertEqual(shown(sort="address")[0], "Anna Muster")
        self.assertEqual(shown(sort="nonsense"), shown())
        self.assertContains(client.get(LIST, {"q": "no such person"}), "Keine Personen gefunden.")
        self.assertContains(client.get(LIST, {"per": "all"}), f"1–{self.shown_count()} von {self.shown_count()} Personen")

    # The detail page ---------------------------------------------------------------------------------

    def test_the_detail_page_shows_the_header_the_cards_and_the_table_of_the_metering_points(self):
        page = self.login(self.staff).get(self.detail(self.anna))
        for text in (
            "← Zurück zu Personen", "Anna Muster", "Kundennummer", self.anna.customer_number_display, "Aktiv",
            "Details", "Anrede", "Frau", "Firma", "Vorname", "Nachname", "Zweite Person",
            "Kontakt", "E-Mail", "anna.person@example.org", "Zweite E-Mail", "Telefon", "079 123 45 67",
            "Rechnungsadresse", "Dorfstrasse 1", "3063 Ittigen", "Land", "Schweiz",
            "Zahlung und Kundennummern", "CH93 0076 2011 6238 5295 7", "Papierrechnung", "Nein",
            "Netzbetreiber Beispiel-Kundennummer", "OP-4711",
            "Genossenschaft", "Anteile", "Notiz", "Telefonat am 12.09.",
            "Messpunkte und Zuordnungen", "Zuordnungen ändern Sie auf der Seite des Messpunkts.",
        ):
            self.assertContains(page, text)
        # The table: the number with a space after four characters, the direction, the LEG, "Zugeordnet" with both
        # lines for an assignment that ends, the words of the registration at the grid operator.
        for text in (
            "CH 1A2B 3C4D 5E60 0000 0000 0000 0000 001", "Bezug", "Einspeisung", "LEG Musterquartier",
            f"ab {fmt_date(EARLIER)}", f"bis {fmt_date(LATER)}", "registriert", "Netzbetreiber Beispiel-Anmeldung",
            f"bestätigt am {fmt_date(EARLIER)}",
        ):
            self.assertContains(page, text)
        # The hints (information buttons) of the template, without the sentence about 30 days.
        for text in (
            "Der Standort ist die Adresse, an der der Messpunkt liegt.",
            "Nummer des Smart Meters bei der Netzbetreiber Beispiel.",
            "Die Person speist Strom ins Netz ein, zum Beispiel aus einer PV-Anlage.",
            "Die Zuordnung endet an diesem Datum.", "Die Anmeldung ist bei der Netzbetreiber Beispiel eingereicht.",
            "Das Datum ist der Tag, an dem die LEG die Bestätigung erfasst hat.",
        ):
            self.assertContains(page, text)
        self.assertNotContains(page, "30 Tage")
        self.assertNotContains(page, "BKW")
        # No button without an effect and nothing for the customer's own login data. The dialogs of the staff are
        # offered: the four cards of the customer's data and the note (all "Edit", D-087) and the shares ("Add change").
        for text in ("Anmeldung starten", "Person anlegen", "Konto sperren"):
            self.assertNotContains(page, text)
        self.assertEqual(page.content.decode().count("Bearbeiten"), 5)
        self.assertContains(page, "Änderung hinzufügen")
        for name in ("details", "contact", "address", "payment", "shares", "note"):
            self.assertContains(page, self.detail(self.anna, name))
        # Links: the location, the metering point and the LEG.
        self.assertContains(page, f'href="/management/locations/{self.place.pk}/"')
        self.assertContains(page, f'href="/management/metering-points/{self.registered.pk}/"')
        self.assertContains(page, f'href="/management/legs/{self.leg.pk}/"')

    def test_the_table_sorts_by_every_column_with_the_number_as_the_default(self):
        client = self.login(self.staff)
        url = self.detail(self.anna)

        def order(**params):
            content = client.get(url, params).content.decode()
            return sorted(("0001", "0002"), key=lambda end: content.index(f"0000 0000 0000 0000 00{end[-1]}"))

        self.assertEqual(order(), ["0001", "0002"])
        self.assertEqual(order(sort="number", dir="desc"), ["0002", "0001"])
        self.assertEqual(order(sort="direction", dir="desc"), ["0002", "0001"])  # feed_in after consumption
        self.assertEqual(order(sort="registration", dir="desc"), ["0002", "0001"])  # confirmed before registered
        self.assertEqual(order(sort="location", dir="desc"), ["0002", "0001"])  # Werkstrasse before Musterweg
        self.assertEqual(order(sort="nonsense"), ["0001", "0002"])
        page = client.get(url, {"sort": "registration"})
        self.assertContains(page, 'aria-sort="ascending"')
        self.assertContains(page, "sort=registration&amp;dir=desc")
        # A metering point without a number is shown, with a dash.
        self.assertContains(page, 'class="mgmt-muted">–</span>')

    def test_a_company_shows_the_company_name_in_the_header_and_its_contact_in_the_card(self):
        client = self.login(self.staff)
        page = client.get(self.detail(self.company))
        self.assertContains(page, "<h1 class=\"start-title\">Beispiel Verwaltung AG</h1>")
        for text in ("Lea", "Probe", "c/o Treuhand", "CH 1A2B 3C4D 5E60 0000 0000 0000 0000 007", "angemeldet am"):
            self.assertContains(page, text)
        # A person without a side record shows dashes and "no shares"; nothing was created by looking.
        page = client.get(self.detail(self.blocked))
        self.assertContains(page, "Gesperrt")
        self.assertFalse(PersonRecord.objects.filter(person=self.blocked).exists())
        self.assertContains(page, "Keine Messpunkte zugeordnet.")

    def test_only_a_person_with_a_customer_number_has_a_page(self):
        client = self.login(self.staff)
        self.assertEqual(client.get(self.detail(self.unverified)).status_code, 404)
        self.assertEqual(client.get(reverse("management:persons_detail", args=[999999])).status_code, 404)
        self.assertContains(client.get(self.detail(self.gone)), "Inaktiv")  # an archived account has its page
        self.assertEqual(client.get(self.detail(self.unverified, "note")).status_code, 404)

    # The display of a termination in the table of the metering points ---------------------------------

    def test_a_terminated_metering_point_shows_the_word_and_a_future_termination_adds_a_line(self):
        client = self.login(self.staff)
        url = self.detail(self.anna)
        # A termination in the future: the step stays and a second line names the day.
        MeteringPoint.objects.filter(pk=self.confirmed.pk).update(terminated_on=LATER)
        page = client.get(url)
        self.assertContains(page, f"bestätigt am {fmt_date(EARLIER)}")
        self.assertContains(page, f"Kündigung zum {fmt_date(LATER)}")
        self.assertNotContains(page, "gekündigt")
        # Once the day is reached the column says "terminated" in the muted tone and nothing else.
        for day in (TODAY - timedelta(days=1), TODAY):
            MeteringPoint.objects.filter(pk=self.confirmed.pk).update(terminated_on=day)
            page = client.get(url)
            self.assertContains(page, '<span class="mgmt-person-registration is-muted">gekündigt</span>', msg_prefix=str(day))
            self.assertNotContains(page, "Kündigung zum", msg_prefix=str(day))
            self.assertNotContains(page, f"bestätigt am {fmt_date(EARLIER)}", msg_prefix=str(day))

    # The history of the shares ------------------------------------------------------------------------

    def test_the_card_cooperative_is_empty_without_an_entry_and_lists_the_history_latest_date_first(self):
        client = self.login(self.staff)
        page = client.get(self.detail(self.blocked))
        self.assertContains(page, "Genossenschaft")
        self.assertContains(page, "Keine Anteile erfasst.")
        self.assertContains(page, "Änderung hinzufügen")
        self.assertNotContains(page, '<dt>Anteile</dt>')
        self.assertFalse(PersonRecord.objects.filter(person=self.blocked).exists())  # nothing created by looking
        # Anna: the current number and the table, the latest date first.
        content = client.get(self.detail(self.anna)).content.decode()
        self.assertIn('<dt>Anteile</dt><dd class="tnum">5</dd>', content)
        latest = f'<tr><td>{fmt_date(EARLIER)}</td><td class="tnum">5</td></tr>'
        oldest = f'<tr><td>{fmt_date(EARLIER - timedelta(days=100))}</td><td class="tnum">2</td></tr>'
        self.assertIn(latest, content)
        self.assertIn(oldest, content)
        self.assertLess(content.index(latest), content.index(oldest))
        self.assertNotIn("Keine Anteile erfasst.", content)

    def test_a_future_entry_is_listed_but_is_not_the_current_number(self):
        persons.add_share_change(self.fixable, 3, EARLIER, self.staff)
        persons.add_share_change(self.fixable, 7, LATER, self.staff)
        persons.add_share_change(self.blocked, 4, LATER, self.staff)
        client = self.login(self.staff)
        content = client.get(self.detail(self.fixable)).content.decode()
        self.assertIn('<dt>Anteile</dt><dd class="tnum">3</dd>', content)
        future = f'<tr><td>{fmt_date(LATER)}</td><td class="tnum">7</td></tr>'
        past = f'<tr><td>{fmt_date(EARLIER)}</td><td class="tnum">3</td></tr>'
        self.assertLess(content.index(future), content.index(past))
        # No entry holds yet: there is no current number, but the entry is listed.
        content = client.get(self.detail(self.blocked)).content.decode()
        self.assertIn('<dt>Anteile</dt><dd class="tnum">–</dd>', content)
        self.assertIn(f'<tr><td>{fmt_date(LATER)}</td><td class="tnum">4</td></tr>', content)

    # The dialogs -------------------------------------------------------------------------------------

    def test_the_note_and_the_operator_customer_number_are_saved_logged_and_shown(self):
        client = self.login(self.staff)
        url = self.detail(self.fixable)
        # Nothing is created by opening a dialog.
        for name, title in (("note", "Notiz bearbeiten"), ("payment", "Zahlung und Kundennummern bearbeiten")):
            self.assertContains(client.get(self.detail(self.fixable, name)), title)
        self.assertFalse(PersonRecord.objects.filter(person=self.fixable).exists())

        for name, data in (
            ("note", {"note": "Rückruf vereinbart."}),
            ("payment", {"operator_customer_number": "OP 99", "paper_invoice": "0", "iban": ""}),
        ):
            response = client.post(self.detail(self.fixable, name), {**data, "next": url})
            self.assertRedirects(response, url, fetch_redirect_response=False)
        record = PersonRecord.objects.get(person=self.fixable)
        self.assertEqual((record.staff_note, record.operator_customer_number), ("Rückruf vereinbart.", "OP 99"))
        page = client.get(url)
        for text in ("Gespeichert", "Rückruf vereinbart.", "OP 99", "Notiz geändert",
                     "Netzbetreiber Beispiel-Kundennummer geändert", "Änderungsverlauf"):
            self.assertContains(page, text)
        # The history names the actor and the time, never a value (the entry of the note holds no text).
        for entry in history.entries_for(self.fixable):
            self.assertEqual((entry.actor, entry.detail), (self.staff, {}))
        # The form is filled with what is saved.
        self.assertContains(client.get(self.detail(self.fixable, "payment")), 'value="OP 99"')

    def test_a_change_of_the_shares_is_added_to_the_history_and_the_latest_date_is_the_current_number(self):
        client = self.login(self.staff)
        url = self.detail(self.fixable, "shares")
        page = client.get(url)
        self.assertEqual(page.status_code, 200)
        for text in ("Änderung hinzufügen", 'name="quantity"', 'name="valid_from"', f'value="{TODAY.isoformat()}"'):
            self.assertContains(page, text)
        self.assertFalse(PersonRecord.objects.filter(person=self.fixable).exists())  # nothing created by opening
        back = self.detail(self.fixable)
        response = client.post(url, {"quantity": "3", "valid_from": EARLIER.isoformat(), "next": back})
        self.assertRedirects(response, back, fetch_redirect_response=False)
        entry = ShareChange.objects.get(record__person=self.fixable)
        self.assertEqual((entry.quantity, entry.valid_from, entry.actor), (3, EARLIER, self.staff))
        # A second entry with an earlier date is added; the current number is still the one of the latest date.
        earlier = EARLIER - timedelta(days=20)
        client.post(url, {"quantity": "9", "valid_from": earlier.isoformat()})
        self.assertEqual(ShareChange.objects.filter(record__person=self.fixable).count(), 2)
        self.assertEqual(persons.current_shares(persons.share_history(self.fixable), TODAY), 3)
        page = client.get(back)
        for text in ("Gespeichert", '<dt>Anteile</dt><dd class="tnum">3</dd>', "Anteile geändert",
                     f'<tr><td>{fmt_date(earlier)}</td><td class="tnum">9</td></tr>'):
            self.assertContains(page, text)
        events = list(history.entries_for(self.fixable))
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertEqual((event.event, event.actor, event.detail), ("person_shares_changed", self.staff, {}))
        # No shares any more is the number 0 from a day on.
        client.post(url, {"quantity": "0", "valid_from": TODAY.isoformat()})
        self.assertContains(client.get(back), '<dt>Anteile</dt><dd class="tnum">0</dd>')

    def test_the_dialogs_check_their_fields_and_never_return_to_a_foreign_address(self):
        client = self.login(self.staff)
        shares = self.detail(self.fixable, "shares")
        day = EARLIER.isoformat()
        for data in (
            {"quantity": "-1", "valid_from": day}, {"quantity": "100001", "valid_from": day},
            {"quantity": "abc", "valid_from": day}, {"quantity": "", "valid_from": day},
            {"quantity": "3", "valid_from": ""}, {"quantity": "3"}, {"quantity": "3", "valid_from": "2026-02-30"},
        ):
            page = client.post(shares, data)
            self.assertEqual(page.status_code, 200, data)
            self.assertContains(page, "mgmt-field-error", msg_prefix=str(data))
        self.assertFalse(ShareChange.objects.filter(record__person=self.fixable).exists())
        self.assertFalse(PersonRecord.objects.filter(person=self.fixable).exists())
        page = client.post(
            self.detail(self.fixable, "payment"), {"operator_customer_number": "1" * 31, "paper_invoice": "0"}
        )
        self.assertEqual(page.status_code, 200)
        self.assertFalse(PersonRecord.objects.filter(person=self.fixable).exists())
        for hostile in HOSTILE:
            response = client.post(self.detail(self.fixable, "note"), {"note": "x", "next": hostile})
            self.assertRedirects(response, self.detail(self.fixable), fetch_redirect_response=False)

    # The correction of the address -----------------------------------------------------------------------

    def test_the_banner_is_shown_and_accepting_changes_only_the_address(self):
        url = self.detail(self.fixable)
        client = self.login(self.staff)
        page = client.get(url)
        self.assertContains(page, "Die Adresse weicht vom Adressverzeichnis ab. Vorschlag: Musterweg 12, 3063 Ittigen.")
        self.assertContains(page, "Übernehmen")
        self.assertContains(page, "Nicht übernehmen")
        token = persons.suggestion_token(persons.address_suggestion(self.fixable))
        post = reverse("management:persons_address_suggestion", args=[self.fixable.pk])
        response = client.post(post, {"action": "accept", "token": token, "next": url})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        fixed = User.objects.get(pk=self.fixable.pk)
        self.assertEqual((fixed.street, fixed.town, fixed.swiss_postal_code), ("Musterweg", "Ittigen", 3063))
        page = client.get(url)
        self.assertNotContains(page, "Die Adresse weicht vom Adressverzeichnis ab.")
        # The history names the event, the account of the actor and the time.
        for text in ("Adresskorrektur übernommen", self.staff.display_name, "Änderungsverlauf", "Musterweg 12"):
            self.assertContains(page, text)
        self.assertContains(page, timezone.localtime().strftime("%d.%m.%Y"))

    def test_do_not_accept_keeps_the_address_and_a_stale_or_unknown_decision_is_refused(self):
        client = self.login(self.staff)
        post = reverse("management:persons_address_suggestion", args=[self.fixable.pk])
        stale = client.post(post, {"action": "accept", "token": "0" * 64}, follow=True)
        self.assertContains(stale, "Es gibt keinen Vorschlag, über den zu entscheiden wäre.")
        self.assertEqual(client.post(post, {"action": "bogus", "token": "x"}).status_code, 400)
        self.assertEqual(client.get(post).status_code, 405)
        self.assertEqual(User.objects.get(pk=self.fixable.pk).town, "Bern")
        token = persons.suggestion_token(persons.address_suggestion(self.fixable))
        client.post(post, {"action": "dismiss", "token": token})
        page = client.get(self.detail(self.fixable))
        self.assertNotContains(page, "Die Adresse weicht vom Adressverzeichnis ab.")
        self.assertContains(page, "Adresskorrektur nicht übernommen")
        self.assertEqual(User.objects.get(pk=self.fixable.pk).street, "musterweg")
        # Every POST needs the CSRF token.
        strict = self.login(self.staff, enforce_csrf_checks=True)
        self.assertEqual(strict.post(post, {"action": "dismiss", "token": token}).status_code, 403)
        self.assertEqual(strict.post(self.detail(self.fixable, "note"), {"note": "x"}).status_code, 403)
        self.assertEqual(strict.post(self.detail(self.fixable, "shares"), {"quantity": "1", "valid_from": TODAY.isoformat()}).status_code, 403)
        self.assertFalse(ShareChange.objects.filter(record__person=self.fixable).exists())

    # The other pages -----------------------------------------------------------------------------------

    def test_the_other_pages_name_the_person_and_link_to_the_page_of_the_person(self):
        client = self.login(self.staff)
        link = f'href="{self.detail(self.anna)}"'
        for path in (
            reverse("management:locations_detail", args=[self.place.pk]),
            reverse("management:metering_points_detail", args=[self.registered.pk]),
        ):
            page = client.get(path)
            self.assertContains(page, link, msg_prefix=path)
            self.assertContains(page, "Anna Muster", msg_prefix=path)
        # A metering point without a number or a location is no reason for the page to fail.
        self.assertEqual(client.get(reverse("management:metering_points_detail", args=[self.unnumbered.pk])).status_code, 200)


class RegistrationDisplayTests(SimpleTestCase):
    """The column "Registration at the grid operator" (D-086): the step of a metering point, the word of a
    termination that is reached and the second line of a termination that lies in the future."""

    TODAY = date(2026, 6, 15)
    NOTIFIED_ON, CONFIRMED_ON = date(2026, 5, 1), date(2026, 5, 20)

    def point(self, **dates):
        return MeteringPoint(**dates)

    def row(self, state, registration_date=None, termination_date=None):
        return person_list.PointRow(
            assignment=None, point=None, location_id=None, street="", house_number="", leg_id=None, leg_name="",
            registration=state, registration_date=registration_date, termination_date=termination_date,
        )

    def test_the_state_is_the_furthest_step_and_terminated_only_once_the_day_is_reached(self):
        notified = {"registered_with_operator_on": self.NOTIFIED_ON}
        confirmed = {**notified, "confirmed_on": self.CONFIRMED_ON}
        yesterday, tomorrow = self.TODAY - timedelta(days=1), self.TODAY + timedelta(days=1)
        for dates, expected in (
            ({}, (person_list.REGISTRATION_REGISTERED, None)),
            (notified, (person_list.REGISTRATION_NOTIFIED, self.NOTIFIED_ON)),
            (confirmed, (person_list.REGISTRATION_CONFIRMED, self.CONFIRMED_ON)),
            ({**confirmed, "terminated_on": tomorrow}, (person_list.REGISTRATION_CONFIRMED, self.CONFIRMED_ON)),
            ({**confirmed, "terminated_on": self.TODAY}, (person_list.REGISTRATION_TERMINATED, self.TODAY)),
            ({**confirmed, "terminated_on": yesterday}, (person_list.REGISTRATION_TERMINATED, yesterday)),
        ):
            with self.subTest(dates=dates):
                self.assertEqual(person_list.registration_state(self.point(**dates), self.TODAY), expected)

    def test_the_text_names_the_step_and_the_tone_and_a_termination_in_the_future_adds_a_line(self):
        future = date(2026, 7, 31)
        with translation.override("de-ch"):
            for row, expected in (
                (self.row(person_list.REGISTRATION_REGISTERED), (["registriert"], "warn")),
                (self.row(person_list.REGISTRATION_NOTIFIED, self.NOTIFIED_ON), (["angemeldet am 01.05.2026"], "ok")),
                (self.row(person_list.REGISTRATION_CONFIRMED, self.CONFIRMED_ON), (["bestätigt am 20.05.2026"], "ok")),
                (self.row(person_list.REGISTRATION_TERMINATED, self.TODAY), (["gekündigt"], "muted")),
                (
                    self.row(person_list.REGISTRATION_CONFIRMED, self.CONFIRMED_ON, future),
                    (["bestätigt am 20.05.2026", "Kündigung zum 31.07.2026"], "ok"),
                ),
                (
                    self.row(person_list.REGISTRATION_REGISTERED, None, future),
                    (["registriert", "Kündigung zum 31.07.2026"], "warn"),
                ),
            ):
                with self.subTest(state=row.registration, termination=row.termination_date):
                    lines, tone = person_views.registration_text(row)
                    self.assertEqual(([str(line) for line in lines], tone), expected)
