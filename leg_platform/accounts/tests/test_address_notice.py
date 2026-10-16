"""The address suggestions and the address notice (D-054, D-065, D-066).

The server renders the places for the suggestions and the notice without any script, only where an
address can be edited (the edit form of the profile and the details step of the registration), and
never in a read-only view. Nothing is said about a directory that cannot answer, and no source of
the directory is shown. The behaviour of the script (mouse-down, keys, timers) is DOM-level and is
not tested here.
"""

import re
import uuid
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import profile_views
from leg_platform.accounts.tests.helpers import PROFILE, create_active_user
from leg_platform.addresses.models import Address, Directory

PROFILE_URL = reverse("accounts:profile")
PROFILE_EDIT_URL = reverse("accounts:profile_edit")
ADDRESS_DATABASES = {"default", "addresses", "address_import"}
NOT_FOUND_DE = "Diese Adresse wurde nicht gefunden."
# Texts that the pages must never show (D-066): the source of the directory, and the texts for a
# directory that cannot answer or for a browser without a script.
FORBIDDEN_TEXTS = (
    "swisstopo", "Landestopografie", "Official address directory", "official address directory",
    "Amtliches Adressverzeichnis", "amtlichen Adressverzeichnis", "Address suggestions need JavaScript",
    "Adressvorschläge benötigen JavaScript", "address directory is unavailable",
    "address directory is more than", "Saved. Please check your address", "Check address",
    "Bitte prüfen Sie Ihre Adresse weiter unten", "Adresse prüfen", "<noscript",
)


def segment(html, start, end):
    """The part of the page between two markers (the end marker is searched after the start)."""
    begin = html.index(start)
    return html[begin:html.index(end, begin)]


SCRIPT = (Path(settings.BASE_DIR) / "leg_platform" / "static" / "js" / "address-directory.js").read_text(
    encoding="utf-8"
)
CODE = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.S)
PO_FILE = (Path(settings.BASE_DIR) / "locale" / "de_CH" / "LC_MESSAGES" / "django.po").read_text(encoding="utf-8")


class ScriptRuleTests(SimpleTestCase):
    """Rules of the script that a static check can hold (the behaviour itself is DOM-level)."""

    def test_the_check_never_runs_on_change_or_blur_and_the_list_keeps_the_focus_in_the_field(self):
        self.assertNotRegex(CODE, r"""addEventListener\(\s*["'](?:change|blur|focusout)["']""")
        self.assertRegex(CODE, r"""panel\.addEventListener\(\s*"mousedown",\s*\(event\)\s*=>\s*event\.preventDefault\(\)""")

    def test_options_are_built_with_text_content_and_the_request_stays_on_the_own_origin(self):
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write"):
            self.assertNotIn(forbidden, CODE)
        self.assertIn('mode: "same-origin"', CODE)
        self.assertIn("endpoint.origin !== location.origin", CODE)
        self.assertNotIn('addEventListener("submit"', CODE)

    def test_the_placeholders_are_replaced_in_one_pass(self):
        # A single regular expression pass with a function replacement: a typed text that contains
        # a placeholder token is never searched again.
        self.assertRegex(CODE, r"\.replace\(/%\\\(\(\\w\+\)\\\)s/g,\s*\(token,\s*name\)\s*=>")
        self.assertNotIn(".split(`%(", CODE)

    def test_the_only_notice_is_the_not_found_warning_of_an_available_directory(self):
        # D-066: no info notice, no text for an unavailable, old or limited directory, a failed
        # request or an invalid answer. Only one call shows a notice, and only for this case.
        self.assertEqual(CODE.count("showNotice("), 1)  # the one call
        self.assertIn('showNotice("warning", data.warning)', CODE)
        self.assertNotIn('"info"', CODE)
        self.assertNotIn("alert-info", CODE)
        self.assertNotIn("unavailable", CODE)
        self.assertRegex(CODE, r'data\.status === "available" && data\.match === false')
        self.assertRegex(CODE, r"if \(!answer \|\| !answer\.ok\) return;")

    def test_a_form_without_a_country_field_names_a_fixed_country_and_the_lookup_stays_swiss_only(self):
        self.assertIn("widget.dataset.countryValue", CODE)
        self.assertRegex(CODE, r'const isSwiss = \(\) => country\(\) === "CH";')
        # The country is always sent, so the server decides (it answers only for "CH").
        self.assertIn('name === "country" ? country() : field.value', CODE)
        # Without a country field and without a fixed value the widget does nothing.
        self.assertRegex(CODE, r"if \(!fields\.country && !fixedCountry\) return;")


class CatalogueTests(SimpleTestCase):
    def test_the_catalogue_has_no_text_for_the_source_of_the_directory_or_for_an_unavailable_one(self):
        # The attribution belongs to the privacy notice and the imprint, not to the pages (D-066).
        self.assertNotIn("swisstopo", PO_FILE)
        for msgid in (
            "Suggestions from the official address directory",
            "Address suggestions need JavaScript.",
            "The address directory is unavailable. You can continue",
            "The address directory is more than 92 days old. You can continue",
            "The address could not be checked. You can continue",
            "Address suggestions are temporarily limited.",
            "Your address could not be checked and is saved as you entered it.",
            "Saved. Please check your address below.",
            'msgid "Check address"',
            "This address was not found in the official directory.",
        ):
            self.assertNotIn(msgid, PO_FILE)


@tag("mariadb")
class EditFormPlacementTests(TestCase):
    databases = ADDRESS_DATABASES

    def setUp(self):
        self.user = create_active_user()
        self.client.force_login(self.user)
        self.html = self.client.get(f"{PROFILE_URL}?edit=personal").content.decode()

    def test_the_suggestions_sit_inside_the_street_field_under_its_input(self):
        field = segment(self.html, ' id="id_street"', ' id="id_house_number"')

        self.assertIn('<div class="address-assist" data-address-directory', field)
        # The panel, then the announcer; the field wrapper is not closed before the widget.
        self.assertLess(field.index("data-address-suggest"), field.index("data-address-announce"))
        self.assertEqual(self.html.count("data-address-directory"), 1)

    def test_the_notice_follows_the_postal_code_and_town_and_is_empty(self):
        after_town = self.html[self.html.index(' id="id_town"'):]
        notice = re.search(
            r'<div class="address-notice" id="address-notice" data-address-notice role="status"></div>',
            after_town,
        )

        self.assertIsNotNone(notice)
        # Directly after the locality pair, before the further details.
        self.assertNotIn("<details", after_town[:notice.start()])
        self.assertLess(self.html.index("field-pair-locality"), self.html.index("data-address-notice"))
        self.assertLess(self.html.index("data-address-notice"), self.html.index("<details"))
        # The notice is a polite status, never an alert, and nothing is rendered into it.
        self.assertNotIn('role="alert" data-address-notice', self.html)
        self.assertEqual(self.html.count("data-address-notice"), 1)
        self.assertIn('data-notice-id="address-notice"', self.html)

    def test_the_page_works_without_a_script_and_says_nothing_about_it(self):
        widget = segment(self.html, '<div class="address-assist"', "data-address-announce></p>")

        # The panel is hidden and empty, the list has no option, the more line is hidden.
        self.assertRegex(widget, r'<div class="address-suggest" data-address-suggest hidden>')
        self.assertNotIn("address-option", widget)
        self.assertRegex(widget, r'<ul class="address-suggest-list"[^>]*></ul>')
        self.assertRegex(widget, r'<p class="address-more" data-address-more hidden></p>')
        self.assertNotIn("data-kind", widget)
        # No text for a browser without a script, no hint about the source, no text for an
        # unavailable directory (D-066): the widget has no visible text at all.
        self.assertNotIn("<noscript", widget)
        self.assertNotIn("form-hint", widget)
        self.assertNotIn("data-unavailable", widget)
        # The save button and the fields of the form do not depend on the widget.
        self.assertIn('type="submit"', self.html)
        # No inline script or style and no event handler attribute in the widget (CSP).
        self.assertNotRegex(widget, r"<script|\sstyle=|\son[a-z]+=")

    def test_the_script_is_loaded_in_the_edit_form_and_the_form_names_its_fields_and_country(self):
        self.assertIn("js/address-directory.js", self.html)
        widget = segment(self.html, '<div class="address-assist"', "data-address-announce></p>")
        for name, field_id in (("street", "id_street"), ("house-number", "id_house_number"),
                               ("postal-code", "id_postal_code"), ("locality", "id_town"),
                               ("country", "id_country")):
            self.assertIn(f'data-{name}-id="{field_id}"', widget)
        self.assertNotIn("data-country-value", widget)
        self.assertIn(' id="id_country"', self.html)

    def test_the_texts_for_the_script_are_swiss_german_with_placeholders_and_no_sharp_s(self):
        widget = segment(self.html, '<div class="address-assist"', 'data-street-id')

        self.assertIn('data-title-prefix="Passende Adressen"', widget)
        self.assertIn('data-title-approximate-one="Meinten Sie diese Adresse?"', widget)
        self.assertIn('data-title-approximate-many="Meinten Sie eine dieser Adressen?"', widget)
        self.assertIn('data-note-prefix="Wählen Sie eine Adresse aus, um die Felder auszufüllen."', widget)
        self.assertIn(
            "In %(postal_code)s wurde keine Strasse gefunden, die mit «%(street)s» beginnt.", widget
        )
        self.assertIn('data-more="Es passen weitere Adressen. Geben Sie mehr ein, um die Liste einzugrenzen."', widget)
        self.assertIn('data-filled="Adresse eingetragen: %(address)s."', widget)
        self.assertIn(
            'data-announce-prefix-one="%(count)s Adressvorschlag verfügbar. Mit der Pfeiltaste nach unten wählen Sie einen aus."',
            widget,
        )
        self.assertIn(
            'data-announce-prefix-many="%(count)s Adressvorschläge verfügbar. Mit der Pfeiltaste nach unten wählen Sie einen aus."',
            widget,
        )
        self.assertIn(
            'data-announce-approximate-one="Keine genaue Übereinstimmung. %(count)s ähnliche Adresse verfügbar. '
            'Mit der Pfeiltaste nach unten wählen Sie sie aus."',
            widget,
        )
        self.assertIn(
            'data-announce-approximate-many="Keine genaue Übereinstimmung. %(count)s ähnliche Adressen verfügbar. '
            'Mit der Pfeiltaste nach unten wählen Sie eine aus."',
            widget,
        )
        self.assertNotIn("\u00df", widget)

    def test_the_old_attribution_paragraph_and_status_line_are_gone(self):
        widget = segment(self.html, '<div class="address-assist"', "data-address-announce></p>")

        self.assertNotIn("data-address-status", widget)
        self.assertNotIn("Amtliches Adressverzeichnis:", widget)

    def test_the_edit_form_shows_no_source_and_no_text_for_a_directory_that_cannot_answer(self):
        for language in ("de-ch", "en"):
            with self.subTest(language=language), override_settings(LANGUAGE_CODE=language):
                html = self.client.get(f"{PROFILE_URL}?edit=personal").content.decode()
                for text in FORBIDDEN_TEXTS:
                    self.assertNotIn(text, html)


@tag("mariadb")
class ReadOnlyViewTests(TestCase):
    """D-066: nothing is shown and no lookup is made while the address cannot be edited."""

    databases = ADDRESS_DATABASES

    def setUp(self):
        self.user = create_active_user()
        self.client.force_login(self.user)

    def test_the_overview_has_no_widget_no_script_no_notice_and_makes_no_lookup(self):
        with patch("leg_platform.addresses.services.directory_status") as status, \
                patch("leg_platform.addresses.services.lookup") as lookup, \
                patch("leg_platform.addresses.views.lookup") as view_lookup, \
                patch("leg_platform.addresses.views.allow_request") as view_limit:
            html = self.client.get(PROFILE_URL).content.decode()

        for marker in ("data-address-directory", "address-directory.js", "data-address-notice", "address-assist",
                       "data-endpoint", "addresses/lookup", "address-suggest"):
            self.assertNotIn(marker, html)
        self.assertIn(self.user.street, html)
        status.assert_not_called()
        lookup.assert_not_called()
        # The view imports `lookup` by name: its own name must not be reached either.
        view_lookup.assert_not_called()
        view_limit.assert_not_called()

    def test_the_other_cards_in_their_edit_state_have_no_widget_and_no_script(self):
        for card in ("email", "password", "two_factor", "delete"):
            with self.subTest(card=card):
                html = self.client.get(f"{PROFILE_URL}?edit={card}").content.decode()
                self.assertNotIn("data-address-directory", html)
                self.assertNotIn("address-directory.js", html)

    def test_the_overview_shows_no_source_and_no_directory_text_in_both_languages(self):
        for language in ("de-ch", "en"):
            with self.subTest(language=language), override_settings(LANGUAGE_CODE=language):
                html = self.client.get(PROFILE_URL).content.decode()
                for text in FORBIDDEN_TEXTS:
                    self.assertNotIn(text, html)

    def test_saving_shows_only_the_plain_message_and_makes_no_lookup(self):
        with patch("leg_platform.addresses.services.directory_status") as status, \
                patch("leg_platform.addresses.services.lookup") as lookup, \
                patch("leg_platform.addresses.views.lookup") as view_lookup, \
                patch("leg_platform.addresses.views.allow_request") as view_limit:
            response = self.client.post(PROFILE_EDIT_URL, PROFILE, follow=True)
        html = response.content.decode()

        head = segment(html, 'id="card-personal-title"', "<dl")
        self.assertEqual(head.count("data-profile-message"), 1)
        self.assertIn("Gespeichert.", head)
        self.assertNotIn("Gespeichert. ", head)
        row = segment(html, '<div class="profile-row"><dt>Adresse</dt>', "</dl>")
        self.assertNotIn("alert", row)
        self.assertNotIn("<a ", row)
        self.assertEqual(row.count("<dd"), 1)
        for text in FORBIDDEN_TEXTS:
            self.assertNotIn(text, html)
        status.assert_not_called()
        lookup.assert_not_called()
        view_lookup.assert_not_called()
        view_limit.assert_not_called()

    def test_the_removed_post_save_notice_has_no_code_left(self):
        for name in ("check_address", "ADDRESS_WARNING_TAG"):
            self.assertFalse(hasattr(profile_views, name), name)
        self.assertNotIn("profile-row-alert", self.client.get(PROFILE_URL).content.decode())
        css = (Path(settings.BASE_DIR) / "leg_platform" / "static" / "css" / "frame.css").read_text(encoding="utf-8")
        self.assertNotIn("profile-row-alert", css)


@tag("mariadb")
class RealDirectoryTests(TransactionTestCase):
    """With a real directory (no patch); the rows must be committed for the reader."""

    databases = ADDRESS_DATABASES

    def setUp(self):
        self.user = create_active_user()
        self.client.force_login(self.user)
        self.generation = uuid.uuid4()
        Directory.objects.using("address_import").create(
            pk=1, generation=self.generation, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=1,
        )

    def test_an_address_that_is_not_in_the_directory_is_saved_as_entered_without_any_notice(self):
        Address.objects.using("address_import").create(
            generation=self.generation, street="Another Street", house_number="1",
            postal_code="3000", locality="Bern",
        )

        html = self.client.post(PROFILE_EDIT_URL, PROFILE, follow=True).content.decode()

        self.assertIn("Gespeichert.", html)
        self.assertNotIn(NOT_FOUND_DE, html)
        self.assertNotIn("nicht gefunden", html)
        self.assertNotIn("profile-row-alert", html)
        self.user.refresh_from_db()
        self.assertEqual(self.user.street, "Examplestrasse")
        # The edit form carries the empty places only; the notice comes from the script, which asks
        # the endpoint after the person clicked "Edit".
        edit = self.client.get(f"{PROFILE_URL}?edit=personal").content.decode()
        self.assertNotIn(NOT_FOUND_DE, edit)
        self.assertIn('data-address-notice role="status"></div>', edit)
