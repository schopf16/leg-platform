"""The settings page of the management area (LEG-Design-Vorgaben/verwaltung/einstellungen; D-077):
the texts of the template, validation, saving with the history, the directory banner and the
rights. Users and the settings row are created once per class."""

import re
import uuid
from unittest.mock import patch

from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.addresses.models import Address, Directory
from leg_platform.community.addressing import AddressSuggestion
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import HistoryEntry, ManagementSettings
from leg_platform.community.tests.helpers import staff_user

URL = reverse("management:settings")
VALID = {
    "sender_street": "Musterstrasse", "sender_house_number": "1", "sender_address_addition": "",
    "sender_postal_code": "3063", "sender_town": "Ittigen",
    "country_code": "CH", "identifier": "1A2B3C4D5E6", "warning_threshold_percent": "10",
}
SUGGESTION = AddressSuggestion("Musterstrasse", "1", "3063", "Ittigen")


def suggest_unless_correct(street, house_number, postal_code, town):
    """A directory that knows only "Musterstrasse 1, 3063 Ittigen"."""
    typed = (street, house_number, postal_code, town)
    return None if typed == SUGGESTION.as_tuple() else SUGGESTION


@tag("mariadb")
class SettingsPageTests(TestCase):
    # The address check reads the (here empty) directory file.
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.editor = staff_user("editor.person@example.org", "view_managementsettings", "change_managementsettings")
        cls.viewer = staff_user("viewer.person@example.org", "view_managementsettings")

    def login(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client

    def page(self, client=None, **data):
        client = client or self.login(self.editor)
        return client.get(URL, data).content.decode()

    def test_the_page_has_the_texts_of_the_template(self):
        html = self.page()

        for text in (
            "<title>Einstellungen",
            ">Verwaltung</p>", ">Einstellungen</h1>",
            ">Absender der Rechnungen</h2>", ">Messpunkte</h2>", ">Produktionsquote</h2>",
            ">Strasse</label>", ">Hausnummer</label>", "Adresszusatz", "(optional)", ">PLZ</label>", ">Ort</label>",
            ">Land</label>", ">Identifikator</label>", "Warnschwelle",
            "Gilt für alle Messpunkte. Die BKW vergibt den Identifikator.",
            "Gilt für alle LEGs. Gesetzlich nötig sind mindestens 5 %.",
            ">Speichern</button>",
        ):
            with self.subTest(text=text):
                self.assertIn(text, html)
        # The sections are in the order of the specification.
        self.assertLess(html.index("Absender der Rechnungen"), html.index(">Messpunkte</h2>"))
        self.assertLess(html.index(">Messpunkte</h2>"), html.index(">Produktionsquote</h2>"))
        # One button saves everything; the first button of the form is that one (key Enter).
        form = html[html.index('<form method="post" novalidate'):]
        self.assertIn('value="save"', re.search(r"<button[^>]*>", form).group(0))
        self.assertEqual(html.count(">Speichern</button>"), 1)

    def test_the_defaults_come_from_the_row_and_the_page_needs_no_inline_code(self):
        html = self.page()

        self.assertIn('name="country_code" value="CH"', html)
        self.assertIn('name="warning_threshold_percent" value="10"', html)
        self.assertIn('name="identifier"', html)
        self.assertNotIn("1A2B3C4D5E6", html)  # nothing of the examples of the template
        self.assertIn("csrfmiddlewaretoken", html)
        self.assertIn('href="/static/management/css/management.css"', html)
        self.assertNotRegex(html, r"<script(?![^>]*\ssrc=\"/static/)")
        self.assertNotRegex(html, r"<style|\sstyle=")

    def test_saving_stores_everything_and_records_the_three_kinds_of_change(self):
        client = self.login(self.editor)

        response = client.post(URL, VALID, follow=True)

        self.assertRedirects(response, URL)
        self.assertContains(response, "Gespeichert.")
        row = ManagementSettings.load()
        self.assertEqual(
            (row.sender_street, row.sender_postal_code, row.country_code, row.identifier, row.warning_threshold_percent),
            ("Musterstrasse", "3063", "CH", "1A2B3C4D5E6", 10),
        )
        events = list(HistoryEntry.objects.filter(subject_type="settings").values_list("event", "actor"))
        self.assertEqual(
            events,
            [(HistoryEvent.SENDER_ADDRESS_CHANGED, self.editor.pk), (HistoryEvent.IDENTIFIER_CHANGED, self.editor.pk)],
        )
        # The values come back in the fields.
        self.assertIn('name="identifier" value="1A2B3C4D5E6"', response.content.decode())

    def test_the_country_and_the_identifier_are_capitalised_and_the_rest_is_trimmed(self):
        client = self.login(self.editor)
        client.post(URL, {**VALID, "country_code": " li ", "identifier": " 1a2b3c4d5e6 ", "sender_town": " Ittigen ",
                          "sender_street": " Musterstrasse "})

        row = ManagementSettings.load()
        self.assertEqual((row.country_code, row.identifier, row.sender_town, row.sender_street),
                         ("LI", "1A2B3C4D5E6", "Ittigen", "Musterstrasse"))

    def test_invalid_entries_are_refused_with_a_message_under_the_field_and_nothing_is_saved(self):
        client = self.login(self.editor)
        bad = {
            "sender_street": "", "sender_house_number": "", "sender_address_addition": "",
            "sender_postal_code": "123", "sender_town": "", "country_code": "1C",
            "identifier": "1A2B3C4D5E", "warning_threshold_percent": "0",
        }

        response = client.post(URL, bad)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Bitte prüfen Sie Ihre Angaben", html)
        for name in ("sender_street", "sender_house_number", "sender_postal_code", "sender_town",
                     "country_code", "identifier", "warning_threshold_percent"):
            with self.subTest(field=name):
                self.assertIn(f'id="id_{name}_error"', html)
        self.assertNotIn("id_sender_address_addition_error", html)  # the addition is optional
        self.assertIn("Geben Sie eine Postleitzahl mit 4 Ziffern von 1000 bis 9999 ein.", html)
        self.assertIn("Geben Sie zwei Grossbuchstaben ein, z. B. CH.", html)
        self.assertIn("Geben Sie 11 Zeichen ein: Grossbuchstaben und Ziffern.", html)
        self.assertFalse(HistoryEntry.objects.exists())
        self.assertEqual(ManagementSettings.load().identifier, "")

    def test_the_threshold_is_a_whole_percentage_up_to_100(self):
        client = self.login(self.editor)
        for value, saved in (("100", True), ("1", True), ("101", False), ("-1", False), ("9.5", False), ("x", False)):
            with self.subTest(value=value):
                response = client.post(URL, {**VALID, "warning_threshold_percent": value})
                self.assertEqual(response.status_code, 302 if saved else 200)

    def test_the_right_to_view_shows_the_values_without_a_way_to_change_them(self):
        ManagementSettings.objects.create(id=1, identifier="1A2B3C4D5E6")
        client = self.login(self.viewer)

        html = self.page(client)

        self.assertIn('name="identifier" value="1A2B3C4D5E6"', html)
        self.assertNotIn(">Speichern</button>", html)
        self.assertEqual(len(re.findall(r"<input[^>]* disabled", html)), 8)
        editor_html = self.page()
        self.assertEqual(len(re.findall(r"<input[^>]* disabled", editor_html)), 0)
        self.assertEqual(client.post(URL, VALID).status_code, 403)
        self.assertEqual(ManagementSettings.load().identifier, "1A2B3C4D5E6")
        self.assertFalse(HistoryEntry.objects.exists())

    def test_an_unknown_action_is_refused(self):
        self.assertEqual(self.login(self.editor).post(URL, {**VALID, "action": "delete"}).status_code, 400)

    def test_the_page_changes_nothing_on_a_get_request(self):
        self.page()
        self.assertEqual(HistoryEntry.objects.count(), 0)


@tag("mariadb")
class AddressBannerTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.editor = staff_user("editor.person@example.org", "view_managementsettings", "change_managementsettings")

    def setUp(self):
        self.client = VerifiedClient()
        self.client.force_login(self.editor)
        ManagementSettings.objects.create(
            id=1, sender_street="musterstrasse", sender_house_number="1", sender_postal_code="3063",
            sender_town="Ittigen", identifier="1A2B3C4D5E6",
        )
        patcher = patch("leg_platform.community.addressing.suggest_correction", side_effect=suggest_unless_correct)
        self.suggest = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_deviation_from_the_directory_shows_the_banner_with_the_two_buttons(self):
        html = self.client.get(URL).content.decode()

        self.assertIn("Die Adresse weicht vom Adressverzeichnis ab. Vorschlag: Musterstrasse 1, 3063 Ittigen.", html)
        self.assertIn('value="accept_suggestion"', html)
        self.assertIn(">Übernehmen</button>", html)
        self.assertIn('value="dismiss_suggestion"', html)
        self.assertIn(">Nicht übernehmen</button>", html)
        # The banner is at the top of the first card, above the fields.
        self.assertLess(html.index("Adressverzeichnis ab"), html.index('name="sender_street"'))
        self.assertGreater(html.index("Adressverzeichnis ab"), html.index("Absender der Rechnungen"))

    def test_accepting_fills_the_fields_and_saves_nothing(self):
        before = ManagementSettings.load().sender_street

        response = self.client.post(URL, {**VALID, "sender_street": "musterstrasse", "identifier": "ZZZZZZZZZZZ",
                                         "action": "accept_suggestion"})

        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="sender_street" value="Musterstrasse"', html)
        # The other fields keep what was typed; the banner is gone.
        self.assertIn('name="identifier" value="ZZZZZZZZZZZ"', html)
        self.assertNotIn("Adressverzeichnis ab", html)
        row = ManagementSettings.load()
        self.assertEqual((row.sender_street, row.identifier), (before, "1A2B3C4D5E6"))
        # Only the decision is recorded (L-5), with the actor; the address is not changed by it.
        entry = HistoryEntry.objects.get()
        self.assertEqual((entry.event, entry.actor), (HistoryEvent.SENDER_SUGGESTION_ACCEPTED, self.editor))
        self.assertEqual(entry.detail, {"suggested": list(SUGGESTION.as_tuple())})

    def test_not_accepting_remembers_the_address_until_it_changes(self):
        response = self.client.post(URL, {**VALID, "sender_street": "musterstrasse", "action": "dismiss_suggestion"})

        self.assertNotIn("Adressverzeichnis ab", response.content.decode())
        row = ManagementSettings.load()
        self.assertEqual(row.sender_street, "musterstrasse")  # the address is not changed
        self.assertTrue(row.sender_suggestion_dismissed_for)
        self.assertNotIn("Adressverzeichnis ab", self.client.get(URL).content.decode())
        entry = HistoryEntry.objects.get()  # the decision, with the actor (L-5)
        self.assertEqual((entry.event, entry.actor), (HistoryEvent.SENDER_SUGGESTION_DISMISSED, self.editor))
        # A changed address (here the town) is checked again.
        self.client.post(URL, {**VALID, "sender_street": "musterstrasse", "sender_town": "Ittigen Dorf"})
        self.assertIn("Adressverzeichnis ab", self.client.get(URL).content.decode())

    def test_an_invalid_address_gets_no_decision(self):
        self.client.post(URL, {**VALID, "sender_postal_code": "12", "action": "dismiss_suggestion"})
        self.assertEqual(ManagementSettings.load().sender_suggestion_dismissed_for, "")

    def test_the_notice_that_the_directory_does_not_know_the_address(self):
        self.suggest.side_effect = lambda *a: None
        with patch(
            "leg_platform.community.addressing.not_found_notice",
            return_value="Diese Adresse wurde nicht gefunden.",
        ):
            html = self.client.get(URL).content.decode()

        self.assertIn("Diese Adresse wurde nicht gefunden.", html)
        self.assertNotIn(">Übernehmen</button>", html)

    def test_the_banner_and_the_notice_are_for_an_account_that_can_change_the_settings_only(self):
        viewer = staff_user("viewer.person@example.org", "view_managementsettings")
        client = VerifiedClient()
        client.force_login(viewer)

        html = client.get(URL).content.decode()

        self.assertNotIn("Adressverzeichnis ab", html)
        self.assertNotIn(">Übernehmen</button>", html)

    def test_an_address_that_is_complete_and_known_shows_nothing(self):
        ManagementSettings.objects.filter(id=1).update(sender_street="Musterstrasse")
        self.assertNotIn("Adressverzeichnis ab", self.client.get(URL).content.decode())

    def test_a_save_does_not_depend_on_the_directory(self):
        # Saving an address as typed is never blocked; the check only advises (D-066).
        response = self.client.post(URL, {**VALID, "sender_street": "musterstrasse"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ManagementSettings.load().sender_street, "musterstrasse")


@tag("mariadb")
class RealDirectoryTests(TestCase):
    """The same banner, with the real check against a directory file (no patch)."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.editor = staff_user("editor.person@example.org", "view_managementsettings", "change_managementsettings")
        generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=generation, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=1,
        )
        Address.objects.create(
            generation=generation, street="Musterstrasse", house_number="1", postal_code="3063", locality="Ittigen"
        )

    def test_the_page_checks_the_saved_address_against_the_directory(self):
        client = VerifiedClient()
        client.force_login(self.editor)
        client.post(URL, {**VALID, "sender_street": "musterstrasse"})

        html = client.get(URL).content.decode()
        self.assertIn("Vorschlag: Musterstrasse 1, 3063 Ittigen.", html)

        client.post(URL, {**VALID, "sender_street": "Nirgendwo"})
        self.assertIn("nicht gefunden", client.get(URL).content.decode())
        client.post(URL, VALID)
        html = client.get(URL).content.decode()
        self.assertNotIn("Adressverzeichnis ab", html)
        self.assertNotIn("nicht gefunden", html)
