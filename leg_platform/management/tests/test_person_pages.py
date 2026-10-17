"""The pages of the persons (D-081): the list (columns, states, filters, search, sort, the phone cards), the detail page (cards,
the table of the metering points with its sorting, the rights that shape it), the three dialogs, the correction of the
address, and the rule that an account without the right to see persons sees no person anywhere. The rules themselves
are tested at service level (``community/tests/test_persons.py``); here only what the views add. The negative
authorization of every URL for anonymous, non-staff, rights-less and second-factor-less accounts is in
``test_access.py`` (the route walk)."""

import uuid
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses.models import Address, Directory
from leg_platform.community import history, persons
from leg_platform.community.models import Direction, MeteringPoint, PersonRecord
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)

User = get_user_model()
TODAY = timezone.localdate()
EARLIER = TODAY - timedelta(days=30)
LATER = TODAY + timedelta(days=10)
LIST = reverse("management:persons")
HOSTILE = ("https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)")


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

        cls.plain = create_active_user("plain.person@example.org")
        cls.none = staff_user("staff.none@example.org")
        cls.viewer = staff_user("staff.view@example.org", "view_personrecord")
        cls.points = staff_user("staff.points@example.org", "view_personrecord", "view_meteringpoint")
        cls.linked = staff_user(
            "staff.linked@example.org", "view_personrecord", "view_meteringpoint", "view_location", "view_leg"
        )
        cls.changer = staff_user("staff.change@example.org", "view_personrecord", "change_personrecord")
        cls.corrector = staff_user("staff.correct@example.org", "view_personrecord", "correct_person_address")
        cls.blind = staff_user(
            "staff.blind@example.org", "view_meteringpoint", "view_location", "view_leg", "view_managementsettings"
        )
        cls.full = staff_user("staff.all@example.org")
        cls.full.user_permissions.set(Permission.objects.filter(content_type__app_label="community"))

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
            person=cls.anna, staff_note="Telefonat am 12.09.", bkw_customer_number="BKW-4711",
            iban="CH9300762011623852957", shares=5, shares_from=date(2026, 3, 1),
        )

        cls.place = make_location("Musterweg", "12", town="Ittigen", postal_code="3063")
        cls.other_place = make_location("Werkstrasse", "5", town="Ittigen", postal_code="3063")
        cls.leg = make_leg("LEG Musterquartier")
        cls.registered = make_metering_point(1, Direction.CONSUMPTION, cls.place)
        cls.confirmed = make_metering_point(
            2, Direction.FEED_IN, cls.other_place, registered_with_bkw_on=EARLIER, confirmed_on=EARLIER
        )
        cls.unnumbered = MeteringPoint.objects.create(number=None, direction=Direction.CONSUMPTION)
        assign_person(cls.registered, cls.anna, EARLIER)
        assign_person(cls.confirmed, cls.anna, EARLIER, valid_to=LATER)
        assign_person(cls.unnumbered, cls.anna, EARLIER)
        assign_leg(cls.registered, cls.leg, EARLIER)
        for index in (7, 8):
            assign_person(make_metering_point(index, registered_with_bkw_on=EARLIER), cls.company, EARLIER)

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
        page = self.login(self.points).get(LIST)
        for text in (
            "Personen", "Alle Kundinnen und Kunden der LEG.", "Name, Kundennummer oder E-Mail", "Anna Muster",
            "Beispiel Verwaltung AG", self.anna.customer_number_display, "Dorfstrasse 1", "3 · Aufnahme",
            "2 · angemeldet", "Ruth Gesperrt", "Gesperrt", "Aktiv", "Kundennummer", "Adresse", "Messpunkte", "Konto",
            "Die sechsstellige Nummer, unter der die Person im Portal geführt wird.", "Strasse und Hausnummer der Rechnungsadresse.",
            "Name der Person, oder der Firma bei Verwaltungen und Firmen.", "Information: Konto",
            "Gesperrt: vorübergehend gesperrt, zum Beispiel nach Fehlversuchen.", f"1–{self.shown_count()} von {self.shown_count()} Personen",
            "mit Messpunkt", "ohne Messpunkt", "archiviert",
        ):
            self.assertContains(page, text)
        # The contact person of a company stands below its name; the name is plain text, the icon is the link.
        self.assertContains(page, 'mgmt-person-contact" title="Lea Probe"')
        self.assertContains(page, f'href="{self.detail(self.anna)}"')
        self.assertContains(page, 'class="mgmt-grip"')
        # Nothing that has no specified effect: no buttons "Create person" or "Start registration", no
        # triangle; no inactive account, no unverified one, no note and no BKW customer number. The button
        # "Print" is there (a desktop only, D-082).
        for text in ("Person anlegen", "Anmeldung starten", "mgmt-warning", "Max Inaktiv", "Emil Probe " + "x",
                     "unverified", "Telefonat", "BKW-4711", "CH93 0076"):
            self.assertNotContains(page, text)
        self.assertContains(page, ">Drucken</a>")
        # On a phone one card per person, the dot has the word as its accessible name.
        self.assertContains(page, 'role="img" aria-label="Gesperrt"')
        self.assertContains(page, 'class="mgmt-cards-view"')
        self.assertContains(page, 'class="frame-item is-active" href="/management/persons/" aria-current="page"')

    def test_the_filters_the_search_and_the_sort_come_from_the_address(self):
        client = self.login(self.points)

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
        self.assertEqual(shown(q="BKW-4711"), [])
        self.assertEqual(shown(q="CH9300762011623852957"), [])
        # Sorting: by default the name; every column, up and down.
        self.assertEqual(shown(sort="name", dir="desc")[0], "Ruth Gesperrt")
        self.assertEqual(shown(sort="points", dir="desc")[:2], ["Anna Muster", "Beispiel Verwaltung AG"])
        self.assertEqual(shown(sort="account", dir="desc")[0], "Ruth Gesperrt")
        self.assertEqual(shown(sort="address")[0], "Anna Muster")
        self.assertEqual(shown(sort="nonsense"), shown())
        self.assertContains(client.get(LIST, {"q": "no such person"}), "Keine Personen gefunden.")
        self.assertContains(client.get(LIST, {"per": "all"}), f"1–{self.shown_count()} von {self.shown_count()} Personen")

    def test_without_the_right_to_see_metering_points_the_list_has_no_column_and_no_filter_for_them(self):
        client = self.login(self.viewer)
        page = client.get(LIST)
        self.assertContains(page, "Anna Muster")
        for text in ("Aufnahme", "angemeldet", "ohne Messpunkt", 'name="points"', "Information: Messpunkte"):
            self.assertNotContains(page, text)
        # The filter is ignored, not obeyed.
        self.assertContains(client.get(LIST, {"points": "without"}), "Anna Muster")

    # The detail page ---------------------------------------------------------------------------------

    def test_the_detail_page_shows_the_header_the_cards_and_the_table_of_the_metering_points(self):
        page = self.login(self.linked).get(self.detail(self.anna))
        for text in (
            "← Zurück zu Personen", "Anna Muster", "Kundennummer", self.anna.customer_number_display, "Aktiv",
            "Details", "Anrede", "Frau", "Firma", "Vorname", "Nachname", "Zweite Person",
            "Kontakt", "E-Mail", "anna.person@example.org", "Zweite E-Mail", "Telefon", "079 123 45 67",
            "Rechnungsadresse", "Dorfstrasse 1", "3063 Ittigen", "Land", "Schweiz",
            "Zahlung und Kundennummern", "CH93 0076 2011 6238 5295 7", "Papierrechnung", "Nein", "BKW-4711",
            "Genossenschaft", "Anteile", "ab 01.03.2026", "Notiz", "Telefonat am 12.09.",
            "Messpunkte und Zuordnungen", "Zuordnungen ändern Sie auf der Seite des Messpunkts.",
        ):
            self.assertContains(page, text)
        # The table: the number with a space after four characters, the direction, the LEG, "Zugeordnet" with both
        # lines for an assignment that ends, the words of the BKW registration.
        for text in (
            "CH 1A2B 3C4D 5E60 0000 0000 0000 0000 001", "Bezug", "Einspeisung", "LEG Musterquartier",
            f"ab {EARLIER.strftime('%d.%m.%Y')}", f"bis {LATER.strftime('%d.%m.%Y')}", "registriert",
            f"bestätigt am {EARLIER.strftime('%d.%m.%Y')}",
        ):
            self.assertContains(page, text)
        # The hints (information buttons) of the template, without the sentence about 30 days.
        for text in (
            "Der Standort ist die Adresse, an der der Messpunkt liegt.", "Nummer des Smart Meters bei der BKW.",
            "Die Person speist Strom ins Netz ein, zum Beispiel aus einer PV-Anlage.",
            "Die Zuordnung endet an diesem Datum.", "Die Anmeldung ist bei der BKW eingereicht.",
            "Das Datum ist der Tag, an dem die LEG die Bestätigung erfasst hat.",
        ):
            self.assertContains(page, text)
        self.assertNotContains(page, "30 Tage")
        # No button without an effect and nothing for the customer's own data.
        for text in ("Anmeldung starten", "Person anlegen", "Bearbeiten", "Konto sperren"):
            self.assertNotContains(page, text)
        # Links: the location, the metering point and the LEG.
        self.assertContains(page, f'href="/management/locations/{self.place.pk}/"')
        self.assertContains(page, f'href="/management/metering-points/{self.registered.pk}/"')
        self.assertContains(page, f'href="/management/legs/{self.leg.pk}/"')

    def test_the_table_sorts_by_every_column_with_the_number_as_the_default(self):
        client = self.login(self.points)
        url = self.detail(self.anna)

        def order(**params):
            content = client.get(url, params).content.decode()
            return sorted(("0001", "0002"), key=lambda end: content.index(f"0000 0000 0000 0000 00{end[-1]}"))

        self.assertEqual(order(), ["0001", "0002"])
        self.assertEqual(order(sort="number", dir="desc"), ["0002", "0001"])
        self.assertEqual(order(sort="direction", dir="desc"), ["0002", "0001"])  # feed_in after consumption
        self.assertEqual(order(sort="bkw", dir="desc"), ["0002", "0001"])  # confirmed before registered
        self.assertEqual(order(sort="location", dir="desc"), ["0002", "0001"])  # Werkstrasse before Musterweg
        self.assertEqual(order(sort="nonsense"), ["0001", "0002"])
        page = client.get(url, {"sort": "bkw"})
        self.assertContains(page, 'aria-sort="ascending"')
        self.assertContains(page, "sort=bkw&amp;dir=desc")
        # A metering point without a number is shown, with a dash.
        self.assertContains(page, 'class="mgmt-muted">–</span>')

    def test_a_company_shows_the_company_name_in_the_header_and_its_contact_in_the_card(self):
        page = self.login(self.points).get(self.detail(self.company))
        self.assertContains(page, "<h1 class=\"start-title\">Beispiel Verwaltung AG</h1>")
        for text in ("Lea", "Probe", "c/o Treuhand", "CH 1A2B 3C4D 5E60 0000 0000 0000 0000 007", "angemeldet am"):
            self.assertContains(page, text)
        # A person without a side record shows dashes and "no shares"; nothing was created by looking.
        page = self.login(self.points).get(self.detail(self.blocked))
        self.assertContains(page, "Keine Anteile erfasst.")
        self.assertContains(page, "Gesperrt")
        self.assertFalse(PersonRecord.objects.filter(person=self.blocked).exists())
        self.assertContains(page, "Keine Messpunkte zugeordnet.")

    def test_only_a_person_with_a_customer_number_has_a_page(self):
        client = self.login(self.full)
        self.assertEqual(client.get(self.detail(self.unverified)).status_code, 404)
        self.assertEqual(client.get(reverse("management:persons_detail", args=[999999])).status_code, 404)
        self.assertContains(client.get(self.detail(self.gone)), "Inaktiv")  # an archived account has its page
        self.assertEqual(client.get(self.detail(self.unverified, "note")).status_code, 404)

    def test_the_rights_shape_the_detail_page(self):
        # Only view_personrecord: the cards, but no table of metering points and no button of a change.
        page = self.login(self.viewer).get(self.detail(self.anna))
        self.assertContains(page, "Telefonat am 12.09.")
        for text in ("Messpunkte und Zuordnungen", "CH 1A2B", "Bearbeiten", "Übernehmen", "Zuordnungen ändern"):
            self.assertNotContains(page, text)
        # With the metering points: the table, but no link to the location, the LEG, and none to change anything.
        page = self.login(self.points).get(self.detail(self.anna))
        self.assertContains(page, "Messpunkte und Zuordnungen")
        self.assertNotContains(page, "/management/locations/")
        self.assertNotContains(page, "/management/legs/")
        # The right to change shows the three buttons "Edit" (BKW customer number, shares, note).
        page = self.login(self.changer).get(self.detail(self.anna))
        self.assertEqual(page.content.decode().count("Bearbeiten"), 3)
        for name in ("bkw_number", "shares", "note"):
            self.assertContains(page, self.detail(self.anna, name))

    # The dialogs -------------------------------------------------------------------------------------

    def test_the_note_the_bkw_customer_number_and_the_shares_are_saved_logged_and_shown(self):
        client = self.login(self.changer)
        url = self.detail(self.fixable)
        # Nothing is created by opening a dialog.
        for name, title in (("note", "Notiz bearbeiten"), ("bkw_number", "BKW-Kundennummer bearbeiten"), ("shares", "Anteile bearbeiten")):
            self.assertContains(client.get(self.detail(self.fixable, name)), title)
        self.assertFalse(PersonRecord.objects.filter(person=self.fixable).exists())

        for name, data in (
            ("note", {"note": "Rückruf vereinbart."}),
            ("bkw_number", {"bkw_customer_number": "BKW 99"}),
            ("shares", {"shares": "3", "shares_from": "2026-01-01", "shares_to": "2026-12-31"}),
        ):
            response = client.post(self.detail(self.fixable, name), {**data, "next": url})
            self.assertRedirects(response, url, fetch_redirect_response=False)
        record = PersonRecord.objects.get(person=self.fixable)
        self.assertEqual(
            (record.staff_note, record.bkw_customer_number, record.shares, record.shares_to),
            ("Rückruf vereinbart.", "BKW 99", 3, date(2026, 12, 31)),
        )
        page = client.get(url)
        for text in ("Gespeichert", "Rückruf vereinbart.", "BKW 99", "ab 01.01.2026 bis 31.12.2026", "Notiz geändert",
                     "BKW-Kundennummer geändert", "Anteile geändert", "Änderungsverlauf"):
            self.assertContains(page, text)
        # The history names the actor and the time, never a value (the entry of the note holds no text).
        for entry in history.entries_for(self.fixable):
            self.assertEqual((entry.actor, entry.detail), (self.changer, {}))
        # The shares end with a date and go with an empty number; the form is filled with what is saved.
        self.assertContains(client.get(self.detail(self.fixable, "shares")), 'value="3"')
        client.post(self.detail(self.fixable, "shares"), {"shares": ""})
        self.assertIsNone(PersonRecord.objects.get(person=self.fixable).shares)

    def test_the_dialogs_check_their_fields_and_never_return_to_a_foreign_address(self):
        client = self.login(self.changer)
        shares = self.detail(self.fixable, "shares")
        for data, message in (
            ({"shares": "3"}, "Bitte füllen Sie dieses Feld aus."),
            ({"shares": "0", "shares_from": "2026-01-01"}, ""),
            ({"shares": "3", "shares_from": "2026-02-01", "shares_to": "2026-01-01"}, "Das Ende darf nicht vor dem Beginn liegen."),
        ):
            page = client.post(shares, data)
            self.assertEqual(page.status_code, 200)
            self.assertFalse(PersonRecord.objects.filter(person=self.fixable, shares__isnull=False).exists())
            if message:
                self.assertContains(page, message)
        page = client.post(self.detail(self.fixable, "bkw_number"), {"bkw_customer_number": "1" * 31})
        self.assertEqual(page.status_code, 200)
        self.assertFalse(PersonRecord.objects.filter(person=self.fixable).exists())
        for hostile in HOSTILE:
            response = client.post(self.detail(self.fixable, "note"), {"note": "x", "next": hostile})
            self.assertRedirects(response, self.detail(self.fixable), fetch_redirect_response=False)

    def test_a_view_only_account_cannot_change_anything(self):
        client = self.login(self.viewer)
        for name, data in (
            ("note", {"note": "x"}), ("bkw_number", {"bkw_customer_number": "1"}),
            ("shares", {"shares": "1", "shares_from": "2026-01-01"}),
        ):
            path = self.detail(self.fixable, name)
            for response in (client.get(path), client.post(path, data)):
                self.assertContains(response, "Hier haben Sie keinen Zugriff.", status_code=403)
        self.assertFalse(PersonRecord.objects.filter(person=self.fixable).exists())
        self.assertEqual(history.entries_for(self.fixable).count(), 0)

    # The correction of the address -----------------------------------------------------------------------

    def test_the_banner_is_shown_only_with_the_right_to_correct_and_accepting_changes_only_the_address(self):
        url = self.detail(self.fixable)
        for user in (self.viewer, self.changer):
            self.assertNotContains(self.login(user).get(url), "Die Adresse weicht vom Adressverzeichnis ab.")
        client = self.login(self.corrector)
        page = client.get(url)
        self.assertContains(page, "Die Adresse weicht vom Adressverzeichnis ab. Vorschlag: Musterweg 12, 3063 Ittigen.")
        self.assertContains(page, "Übernehmen")
        self.assertContains(page, "Nicht übernehmen")
        token = persons.suggestion_token(persons.address_suggestion(self.fixable))
        # A POST without the right of the correction (the right to change is not enough) is refused.
        refused = self.login(self.changer).post(
            reverse("management:persons_address_suggestion", args=[self.fixable.pk]), {"action": "accept", "token": token}
        )
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(User.objects.get(pk=self.fixable.pk).town, "Bern")
        post = reverse("management:persons_address_suggestion", args=[self.fixable.pk])
        response = client.post(post, {"action": "accept", "token": token, "next": url})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        fixed = User.objects.get(pk=self.fixable.pk)
        self.assertEqual((fixed.street, fixed.town, fixed.swiss_postal_code), ("Musterweg", "Ittigen", 3063))
        page = client.get(url)
        self.assertNotContains(page, "Die Adresse weicht vom Adressverzeichnis ab.")
        # The history names the event, the account of the actor and the time.
        for text in ("Adresskorrektur übernommen", self.corrector.display_name, "Änderungsverlauf", "Musterweg 12"):
            self.assertContains(page, text)
        self.assertContains(page, timezone.localtime().strftime("%d.%m.%Y"))

    def test_do_not_accept_keeps_the_address_and_a_stale_or_unknown_decision_is_refused(self):
        client = self.login(self.corrector)
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
        strict = self.login(self.corrector, enforce_csrf_checks=True)
        self.assertEqual(strict.post(post, {"action": "dismiss", "token": token}).status_code, 403)
        self.assertEqual(strict.post(self.detail(self.fixable, "note"), {"note": "x"}).status_code, 403)

    # The rights --------------------------------------------------------------------------------------------

    def test_an_account_without_the_right_to_see_persons_sees_no_person_anywhere(self):
        client = self.login(self.blind)
        # No menu entry, no list, no page of a person, no dialog; the other areas show no name, no address of an
        # account, no e-mail address and no customer number.
        self.assertNotContains(client.get(reverse("management:locations")), 'href="/management/persons/"')
        for path in (LIST, self.detail(self.anna), self.detail(self.anna, "note"), self.detail(self.anna, "shares"),
                     self.detail(self.anna, "bkw_number")):
            self.assertContains(client.get(path), "Hier haben Sie keinen Zugriff.", status_code=403)
        pages = [
            reverse("management:locations_detail", args=[self.place.pk]),
            reverse("management:locations"),
            reverse("management:metering_points"),
            reverse("management:metering_points_detail", args=[self.registered.pk]),
            reverse("management:metering_points_detail", args=[self.unnumbered.pk]),
            reverse("management:legs_detail", args=[self.leg.pk]),
        ]
        for path in pages:
            page = client.get(path)
            self.assertEqual(page.status_code, 200, path)
            for text in ("Anna Muster", self.anna.customer_number_display, "anna.person@", "/management/persons/"):
                self.assertNotContains(page, text, msg_prefix=path)
        # Searching for the name finds nothing there either.
        self.assertNotContains(client.get(reverse("management:metering_points"), {"q": "Anna"}), "CH1A2B3C4D5E6")

    def test_the_other_pages_link_to_the_page_of_the_person_for_an_account_that_may_see_persons(self):
        client = self.login(self.full)
        link = f'href="{self.detail(self.anna)}"'
        self.assertContains(client.get(reverse("management:locations_detail", args=[self.place.pk])), link)
        self.assertContains(client.get(reverse("management:metering_points_detail", args=[self.registered.pk])), link)
        # A metering point without a number or a location is no reason for the page to fail.
        self.assertEqual(client.get(reverse("management:metering_points_detail", args=[self.unnumbered.pk])).status_code, 200)

    def test_the_menu_entry_needs_the_right_to_see_persons(self):
        self.assertContains(self.login(self.viewer).get(LIST), 'href="/management/persons/"')
        self.assertNotContains(self.login(self.blind).get(reverse("management:locations")), 'href="/management/persons/"')
