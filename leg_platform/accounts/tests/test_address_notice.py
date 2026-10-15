"""The address suggestions and the address notice on the profile (D-054, D-065).

The server renders the places for the suggestions and the notice without any script, and the
message that follows a save is a one-time notice in the Address row, not in the header of the
card. The behaviour of the script (mouse-down, keys, timers) is DOM-level and is not tested here.
"""

import re
import uuid
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.tests.helpers import PASSWORD, PROFILE, create_active_user
from leg_platform.addresses.models import Address, Directory

PROFILE_URL = reverse("accounts:profile")
PROFILE_EDIT_URL = reverse("accounts:profile_edit")
ADDRESS_DATABASES = {"default", "addresses", "address_import"}
NOT_FOUND_DE = "Diese Adresse wurde im amtlichen Verzeichnis nicht gefunden."
NOT_FOUND_REPLY = {
    "status": "available", "match": False, "results": [], "kind": "prefix",
    "warning": "Not found warning of the directory.",
}


def segment(html, start, end):
    """The part of the page between two markers (the end marker is searched after the start)."""
    begin = html.index(start)
    return html[begin:html.index(end, begin)]


SCRIPT = (Path(settings.BASE_DIR) / "leg_platform" / "static" / "js" / "address-directory.js").read_text(
    encoding="utf-8"
)


class ScriptRuleTests(SimpleTestCase):
    """Rules of the script that a static check can hold (the behaviour itself is DOM-level)."""

    def test_the_check_never_runs_on_change_or_blur_and_the_list_keeps_the_focus_in_the_field(self):
        code = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.S)

        self.assertNotRegex(code, r"""addEventListener\(\s*["'](?:change|blur|focusout)["']""")
        self.assertRegex(code, r"""panel\.addEventListener\(\s*"mousedown",\s*\(event\)\s*=>\s*event\.preventDefault\(\)""")

    def test_options_are_built_with_text_content_and_the_request_stays_on_the_own_origin(self):
        code = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.S)

        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write"):
            self.assertNotIn(forbidden, code)
        self.assertIn('mode: "same-origin"', code)
        self.assertIn("endpoint.origin !== location.origin", code)
        self.assertNotIn('addEventListener("submit"', code)

    def test_the_placeholders_are_replaced_in_one_pass(self):
        code = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.S)

        # A single regular expression pass with a function replacement: a typed text that contains
        # a placeholder token is never searched again.
        self.assertRegex(code, r"\.replace\(/%\\\(\(\\w\+\)\\\)s/g,\s*\(token,\s*name\)\s*=>")
        self.assertNotIn(".split(`%(", code)


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
        # The panel, then the source hint, then the announcer; the field wrapper is not closed
        # before the widget.
        self.assertLess(field.index("data-address-suggest"), field.index("form-hint"))
        self.assertLess(field.index("form-hint"), field.index("data-address-announce"))
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

    def test_the_page_works_without_a_script_and_renders_no_result(self):
        widget = segment(self.html, '<div class="address-assist"', "</noscript>")

        # The panel is hidden and empty, the list has no option, the more line is hidden.
        self.assertRegex(widget, r'<div class="address-suggest" data-address-suggest hidden>')
        self.assertNotIn("address-option", widget)
        self.assertRegex(widget, r'<ul class="address-suggest-list"[^>]*></ul>')
        self.assertRegex(widget, r'<p class="address-more" data-address-more hidden></p>')
        self.assertNotIn("data-kind", widget)
        # The Swiss German texts for the script and for the people without it.
        self.assertIn("Vorschläge aus dem amtlichen Adressverzeichnis (swisstopo).", widget)
        self.assertIn("Adressvorschläge benötigen JavaScript.", widget)
        # The save button and the fields of the form do not depend on the widget.
        self.assertIn('type="submit"', self.html)
        # No inline script or style and no event handler attribute in the widget (CSP).
        self.assertNotRegex(widget, r"<script|\sstyle=|\son[a-z]+=")
        self.assertIn("js/address-directory.js", self.html)

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
        widget = segment(self.html, '<div class="address-assist"', "</noscript>")

        self.assertNotIn("data-address-status", widget)
        self.assertNotIn("Amtliches Adressverzeichnis:", widget)


@tag("mariadb")
class SavedAddressNoticeTests(TestCase):
    databases = ADDRESS_DATABASES

    def setUp(self):
        self.user = create_active_user()
        self.client.force_login(self.user)

    def save(self, reply=None, **changes):
        data = {**PROFILE, **changes}
        if reply is None:
            return self.client.post(PROFILE_EDIT_URL, data, follow=True)
        with patch("leg_platform.accounts.profile_views.check_address", return_value=reply):
            return self.client.post(PROFILE_EDIT_URL, data, follow=True)

    def test_a_not_found_address_gets_its_notice_in_the_address_row_not_in_the_card_header(self):
        html = self.save(NOT_FOUND_REPLY).content.decode()

        row = segment(html, '<div class="profile-row"><dt>Adresse</dt>', "</dl>")
        self.assertIn('<dd class="profile-row-alert"><div class="alert alert-warning" role="status">', row)
        self.assertIn("Not found warning of the directory.", row)
        self.assertIn(f'href="{PROFILE_URL}?edit=personal#card-personal">Adresse prüfen</a>', row)
        # The header of the card has one message, the one that points to the row below it.
        head = segment(html, 'id="card-personal-title"', "<dl")
        self.assertEqual(head.count("data-profile-message"), 1)
        self.assertIn("Gespeichert. Bitte prüfen Sie Ihre Adresse weiter unten.", head)
        self.assertNotIn("Not found warning of the directory.", head)
        self.assertNotIn("alert-warning", head)
        # The notice is not shown as a general message at the top of the page either.
        self.assertEqual(html.count("Not found warning of the directory."), 1)

    def test_the_notice_is_shown_once(self):
        self.save(NOT_FOUND_REPLY)

        html = self.client.get(PROFILE_URL).content.decode()

        self.assertNotIn("profile-row-alert", html)
        self.assertNotIn("Not found warning of the directory.", html)
        self.assertNotIn("Bitte prüfen Sie Ihre Adresse weiter unten", html)

    def test_the_notice_text_is_escaped(self):
        reply = {**NOT_FOUND_REPLY, "warning": '<script>alert(1)</script> & "quoted" <b>bold</b>'}

        html = self.save(reply).content.decode()

        row = segment(html, '<div class="profile-row"><dt>Adresse</dt>', "</dl>")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<b>bold</b>", row)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quoted&quot; &lt;b&gt;bold&lt;/b&gt;", row)

    def test_the_notice_is_not_shown_to_another_session_or_user(self):
        # Save without following the redirect, so that the message is still waiting.
        with patch("leg_platform.accounts.profile_views.check_address", return_value=NOT_FOUND_REPLY):
            self.client.post(PROFILE_EDIT_URL, PROFILE)
        same_user_other_session = Client()
        same_user_other_session.force_login(self.user)
        other_user = Client()
        other_user.force_login(create_active_user("other.person@example.org"))
        for name, client in (("same user, other session", same_user_other_session), ("other user", other_user)):
            with self.subTest(name):
                html = client.get(PROFILE_URL).content.decode()
                self.assertNotIn("Not found warning of the directory.", html)
                self.assertNotIn("profile-row-alert", html)
                self.assertNotIn("Bitte prüfen Sie Ihre Adresse weiter unten", html)
        # Control: the session that saved still gets it, once.
        html = self.client.get(PROFILE_URL).content.decode()
        self.assertEqual(html.count("Not found warning of the directory."), 1)

    def test_a_found_or_unchecked_address_gets_only_the_plain_saved_message(self):
        replies = {
            "found": {"status": "available", "match": True, "warning": "", "results": []},
            "stale": {"status": "available", "match": True, "warning": "Stale.", "results": []},
            "unavailable": {"status": "unavailable", "match": None, "warning": "Unavailable.", "results": []},
            "missing": {"status": "missing", "match": None, "warning": "Missing.", "results": []},
            "not checked": {"status": "not_checked", "match": None, "warning": "", "results": []},
            # A not found answer of a directory that is not available is not a data problem.
            "not found, unavailable": {"status": "unavailable", "match": False, "warning": "Unavailable.", "results": []},
        }
        for name, reply in replies.items():
            with self.subTest(name):
                html = self.save(reply).content.decode()
                head = segment(html, 'id="card-personal-title"', "<dl")
                self.assertIn("Gespeichert.", head)
                self.assertNotIn("Bitte prüfen Sie Ihre Adresse", html)
                self.assertNotIn("profile-row-alert", html)
                self.assertNotIn(reply["warning"] or "\0", html)

    def test_other_cards_keep_their_messages_in_their_header(self):
        response = self.client.post(
            reverse("accounts:password_change"),
            {"current_password": PASSWORD,
             "new_password1": "silver lantern rests under quiet rain",
             "new_password2": "silver lantern rests under quiet rain"},
            REMOTE_ADDR="10.6.9.1",
            follow=True,
        )
        html = response.content.decode()

        head = segment(html, 'id="card-password-title"', "</section>")
        self.assertIn("data-profile-message", head)
        self.assertNotIn("profile-row-alert", html)


@tag("mariadb")
class RealDirectoryNoticeTests(TransactionTestCase):
    """The notice with the real check (no patch); the directory rows must be committed for the reader."""

    databases = ADDRESS_DATABASES

    def test_the_notice_comes_from_the_directory_when_the_address_is_not_in_it(self):
        user = create_active_user()
        self.client.force_login(user)
        generation = uuid.uuid4()
        Directory.objects.using("address_import").create(
            pk=1, generation=generation, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=1,
        )
        Address.objects.using("address_import").create(
            generation=generation, street="Another Street", house_number="1",
            postal_code="3000", locality="Bern",
        )

        html = self.client.post(PROFILE_EDIT_URL, PROFILE, follow=True).content.decode()

        row = segment(html, 'class="profile-rows"', "</dl>")
        self.assertIn(NOT_FOUND_DE, row)
        self.assertIn("Sie können sie trotzdem so speichern, wie Sie sie eingegeben haben.", row)
        # The address is saved as entered: the notice never blocks saving.
        user.refresh_from_db()
        self.assertEqual(user.street, "Examplestrasse")

    def test_a_directory_that_has_the_address_gives_no_notice(self):
        user = create_active_user()
        self.client.force_login(user)
        generation = uuid.uuid4()
        Directory.objects.using("address_import").create(
            pk=1, generation=generation, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=1,
        )
        Address.objects.using("address_import").create(
            generation=generation, street="Examplestrasse", house_number="12a",
            postal_code="3000", locality="Bern",
        )

        html = self.client.post(PROFILE_EDIT_URL, PROFILE, follow=True).content.decode()

        self.assertNotIn("profile-row-alert", html)
        self.assertIn("Gespeichert.", html)
