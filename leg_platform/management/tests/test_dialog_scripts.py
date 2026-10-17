"""The page scripts and the markup that they work on (D-094): the combobox of the suggestions, the checks while typing,
the cursor in the first field, the transformer circuit field, the order of the menu, and the delays of every suggest
widget. The scripts are not run here (no browser); what is checked is the markup that they need, the contract between
server and script, and rules that a reader of the source can verify (no markup built from text, no dynamic code)."""

import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community.models import ManagementSettings
from leg_platform.community.tests.helpers import make_circuit, make_location, make_metering_point, staff_user

ROOT = Path(settings.BASE_DIR) / "leg_platform"
SCRIPTS = ROOT / "management" / "static" / "management" / "js"
NEW_SCRIPTS = ("field-check.js", "combobox.js", "circuit-field.js", "option-note.js", "dialog-focus.js")


def script(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


class ScriptBehaviourTests(SimpleTestCase):
    """The number rule of ``field-check.js`` run in a headless Chrome or Chromium (skipped where there is none): the same
    cases as ``numbers.normalise_entered_number`` (D-102), so that the script and the server cannot drift apart."""

    CASES = (
        ("23", "0" * 18 + "23", False),
        ("0" + "4" * 20, "4" * 20, False),  # 21 characters, one leading zero
        ("00" + "4" * 20, "4" * 20, False),  # 22, two
        ("00" + "4" * 19, "0" + "4" * 19, False),  # only as many zeros as needed
        ("0" * 25, "0" * 20, False),
        ("ab-12", "0" * 15 + "AB-12", False),  # small letters become capitals
        ("-5", "0" * 18 + "-5", False),
        ("A" * 20, "A" * 20, False),
        ("1" * 21, "1" * 21, True),  # too long: nothing is changed, a message is shown
        ("0" + "1" * 21, "0" + "1" * 21, True),
        ("1" + "0" * 20, "1" + "0" * 20, True),
        ("12!", "12!", True),
        ("ß", "ß", True),
        ("1 2", "1 2", True),
    )

    @staticmethod
    def browser():
        for name in ("chrome", "google-chrome", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                return found
        for path in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ):
            if Path(path).exists():
                return path
        return None

    def test_the_script_normalises_like_the_server(self):
        chrome = self.browser()
        if chrome is None:
            self.skipTest("no Chrome or Chromium found")
        cases = json.dumps([[typed, expected, error] for typed, expected, error in self.CASES])
        page = (
            '<!doctype html><meta charset="utf-8"><body><div class="mgmt-dialog-field">'
            '<input id="n" data-check="digits" data-limit="20" data-prefix="CH1A2B3C4D5E6" '
            'data-message-digits="NOT_ALLOWED" data-message-many="TOO_LONG %(count)s" data-message-empty="EMPTY">'
            '<div id="n_error" hidden></div><div data-number-complete hidden></div><div data-number-feedback></div>'
            f'</div><pre id="out"></pre><script src="{(SCRIPTS / "field-check.js").as_uri()}"></script>'
            "<script>const n=document.getElementById('n');const rows=[];"
            f"for (const [typed, expected, error] of {cases}) {{"
            "n.value=typed;n.dispatchEvent(new Event('input'));n.dispatchEvent(new Event('blur'));"
            "const slot=document.getElementById('n_error');"
            "rows.push({typed, expected, value:n.value, message: slot.hidden ? '' : slot.textContent, error});}"
            "document.getElementById('out').textContent='RESULT'+JSON.stringify(rows)+'END';</script>"
        )
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "check.html"
            file.write_text(page, encoding="utf-8")
            done = subprocess.run(
                [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", f"--user-data-dir={folder}/profile",
                 "--allow-file-access-from-files", "--virtual-time-budget=3000", "--dump-dom", file.as_uri()],
                capture_output=True, timeout=120, check=False,
            )
        output = done.stdout.decode("utf-8", "replace")
        found = re.search(r"RESULT(.*?)END", output, re.S)
        self.assertIsNotNone(found, output[:500])
        for row in json.loads(html.unescape(found.group(1))):
            with self.subTest(typed=row["typed"][:25]):
                self.assertEqual(row["value"], row["expected"])
                if row["error"]:
                    self.assertTrue(row["message"], row)
                else:
                    self.assertEqual(row["message"], "")
        # The messages are the ones of the server (carried in data attributes), with the count of what was typed.
        too_long = [r for r in json.loads(html.unescape(found.group(1))) if r["typed"] == "1" * 21][0]
        self.assertEqual(too_long["message"], "TOO_LONG 21")


class ScriptSourceTests(TestCase):
    def test_no_script_builds_markup_from_text_or_runs_dynamic_code(self):
        for name in NEW_SCRIPTS:
            text = script(name)
            for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(forbidden, text, f"{name}: {forbidden}")
        for name in ("field-check.js", "combobox.js"):
            self.assertIn("textContent", script(name))  # answers are shown as text

    def test_the_suggestions_ask_after_a_short_delay_and_drop_answers_of_older_texts(self):
        text = script("combobox.js")
        self.assertRegex(text, r"const DELAY = 120;")  # D-094: 100 to 150 ms, the server has no budget for it
        self.assertRegex(text, r"const MINIMUM = 2;")
        self.assertRegex(text, r"const MAXIMUM = 10;")
        self.assertIn("new AbortController()", text)
        self.assertIn("controller.abort()", text)
        self.assertIn("signal: controller.signal", text)
        # The answer is used only for the text that is still in the field.
        self.assertIn("input.value.trim() === text", text)

    def test_a_row_that_was_taken_is_not_offered_again_and_a_touch_does_not_bring_the_keyboard_back(self):
        # UX-001: after a tap the list stays closed and the focus is not put back into the field on a touch screen.
        text = script("combobox.js")
        self.assertIn("chosen = last;", text)
        self.assertIn("chosen !== last", text)  # the focus handler does not reopen the list for the chosen text
        self.assertIn('pointerType === "mouse"', text)
        self.assertRegex(text, r"pointerType = event\.pointerType")
        self.assertRegex(text, r'chosen = "";')  # typing again makes the list available again

    def test_the_scripts_stop_asking_after_the_answer_too_many_requests(self):
        # M-3 and M-4: a 429 answer pauses the requests for the time of Retry-After (30 s without it, 120 s at most).
        combobox = script("combobox.js")
        self.assertIn("response.status === 429", combobox)
        self.assertIn("pausedUntil", combobox)
        # L-D: the text is forgotten when the pause starts (twice: the answer, and a search inside the pause), so the
        # same text is searched again afterwards.
        self.assertEqual(combobox.count('last = "";'), 4)  # two here, the short text and the pause in the input handler
        self.assertRegex(combobox, r'pausedUntil = Date\.now\(\)[^\n]*\n\s*last = "";')
        self.assertIn("messageLimited", combobox)
        directory = (ROOT / "static" / "js" / "address-directory.js").read_text(encoding="utf-8")
        self.assertIn("response.status === 429", directory)
        self.assertIn("Date.now() < pausedUntil", directory)
        self.assertIn("Retry-After", directory)

    def test_the_combobox_follows_the_aria_pattern_and_the_keyboard_rules(self):
        text = script("combobox.js")
        for needle in (
            'setAttribute("role", "combobox")', 'setAttribute("aria-expanded"', "aria-activedescendant", 'setAttribute("role", "listbox")',
            'setAttribute("role", "option")', 'aria-live', "ArrowDown", "ArrowUp", '"Enter"', '"Escape"', '"Tab"',
            "scrollIntoView", "pointerdown", "click",
        ):
            self.assertIn(needle, text, needle)
        # Enter sends the form only when no row is active (the handler prevents the default only for a row).
        self.assertRegex(text, r'event.key === "Enter"[\s\S]{0,120}active >= 0[\s\S]{0,80}preventDefault')

    def test_the_cursor_goes_to_the_first_field_only_with_a_fine_pointer_and_not_after_an_error(self):
        text = script("dialog-focus.js")
        self.assertIn("(hover: hover) and (pointer: fine)", text)
        self.assertIn(".error-summary", text)
        self.assertIn("[data-first-field]", text)
        self.assertIn("setSelectionRange", text)  # the cursor at the end of a prefilled value

    def test_the_checks_use_the_messages_of_the_server_and_never_change_what_was_typed_except_on_leaving_the_field(self):
        text = script("field-check.js")
        for needle in ("dataset.messageInvalid", "dataset.messagePlaces", "dataset.messageRange", "dataset.messageDigits",
                       "dataset.messageMany", "dataset.messageEmpty", 'aria-live", "polite"', "aria-invalid", "AbortController"):
            self.assertIn(needle, text, needle)
        self.assertNotIn('role", "alert"', text)  # never an alert per key
        self.assertIn('"0".repeat(limit - result.length)', text)  # zeros are put in front by hand (not with padStart on a hyphen)
        self.assertNotIn("[0-9]+$/.test(value)", text)  # the digits-only rule is gone (D-102)

    def test_every_suggest_widget_has_a_delay_that_the_rate_budget_allows(self):
        # The address lookup: 60 requests per minute and address (addresses/limits.py); 200 ms with the cancellation of
        # the request that is running (the script aborts the stale request) is 5 requests per second at most.
        text = (ROOT / "static" / "js" / "address-directory.js").read_text(encoding="utf-8")
        self.assertIn("const SUGGEST_DELAY = 200;", text)
        self.assertIn("const CHECK_DELAY = 1000;", text)
        self.assertIn("new AbortController()", text)
        limits = (ROOT / "addresses" / "limits.py").read_text(encoding="utf-8")
        self.assertIn("60", limits)

    def test_the_stylesheet_has_the_panel_of_the_combobox_with_the_tokens_of_the_design(self):
        css = (ROOT / "management" / "static" / "management" / "css" / "management.css").read_text(encoding="utf-8")
        panel = re.search(r"\.mgmt-combobox-list \{(.*?)\}", css, re.S).group(1)
        for needle in ("var(--panel)", "var(--line)", "border-radius: 8px", "box-shadow", "max-height: 336px", "overflow-y: auto"):
            self.assertIn(needle, panel, needle)
        self.assertIn(".mgmt-combobox-list[hidden]", css)  # a rule for display must not beat the attribute
        self.assertIn(".mgmt-dialog-row[hidden]", css)


@tag("mariadb")
class MarkupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.scripts@example.org")
        row = ManagementSettings.load()
        row.country_code, row.identifier = "CH", "1A2B3C4D5E6"
        row.save()
        cls.circuit = make_circuit("TRA1", "Eins")
        cls.place = make_location("Musterweg", "12", "3063", "Ittigen", transformer_circuit=cls.circuit)
        cls.point = make_metering_point(1, location=cls.place)

    def page(self, name, *args, **query):
        client = VerifiedClient()
        client.force_login(self.staff)
        response = client.get(reverse(f"management:{name}", args=args), query)
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def test_the_scripts_are_loaded_by_every_dialog(self):
        html = self.page("locations_new")
        for name in NEW_SCRIPTS:
            self.assertIn(f'<script defer src="/static/management/js/{name}"></script>', html)
        self.assertNotIn("lookup.js", html)

    def test_the_first_control_is_marked_in_every_create_form(self):
        for name, args, field in (
            ("locations_new", (), "street"),
            ("metering_points_new", (), "location"),
            ("persons_new", (), "email"),
            ("legs_new", (), "name"),
            ("metering_points_person", (self.point.pk,), "person"),
            ("metering_points_leg", (self.point.pk,), "leg"),
            ("locations_remark", (self.place.pk,), "remark"),
        ):
            with self.subTest(form=name):
                html = self.page(name, *args)
                marked = re.findall(r"<[^>]*data-first-field[^>]*>", html)
                self.assertEqual(len(marked), 1, marked)
                self.assertIn(f'name="{field}"', marked[0])
        # A locked or fixed field is never the first control: the cursor goes to the next one.
        locked = self.page("locations_metering_point_new", self.place.pk)
        self.assertIn('name="number_digits"', re.findall(r"<[^>]*data-first-field[^>]*>", locked)[0])
        changed = self.page("metering_points_edit", self.point.pk)
        self.assertIn('name="location"', re.findall(r"<[^>]*data-first-field[^>]*>", changed)[0])
        # A question puts the cursor on "Cancel", never on the red button.
        question = self.page("locations_delete", make_location("Leerweg", "1").pk)
        marked = re.findall(r"<[^>]*data-first-field[^>]*>", question)
        self.assertEqual(len(marked), 1)
        self.assertIn("Abbrechen", question[question.index(marked[0]):][:200])

    def test_every_control_names_its_message_and_hint_and_the_first_one_the_lead_sentence(self):
        html = self.page("metering_points_new")
        number = re.search(r'<input[^>]*name="number_digits"[^>]*>', html).group(0)
        self.assertIn('aria-describedby="id_number_digits_error id_number_digits_hint"', number)
        self.assertIn('data-check="digits"', number)
        self.assertIn('data-limit="20"', number)
        self.assertNotIn("maxlength", number)  # a limit that cuts silently hides the message
        self.assertNotIn("inputmode", number)  # letters are allowed: the full keyboard
        self.assertIn('autocapitalize="characters"', number)
        self.assertIn('data-prefix="CH1A2B3C4D5E6"', number)
        self.assertIn(f'data-check-url="{reverse("management:metering_points_check_number")}"', number)
        first = re.search(r'<select[^>]*name="location"[^>]*>', html).group(0)
        self.assertIn("dialog-lead", first)
        self.assertIn('id="dialog-lead"', html)
        self.assertIn('<div class="mgmt-field-error" id="id_number_digits_error" hidden></div>', html)
        self.assertIn('id="id_number_digits_hint"', html)

    def test_the_power_fields_carry_the_rules_for_the_script_without_a_native_limit(self):
        html = self.page("metering_points_new")
        for name, unit in (("pv_power_kwp", "kWp"), ("battery_kwh", "kWh"), ("wallbox_kw", "kW")):
            field = re.search(rf'<input[^>]*name="{name}"[^>]*>', html).group(0)
            for needle in ('data-check="decimal"', 'data-places="2"', 'data-max="999999.99"', 'inputmode="decimal"'):
                self.assertIn(needle, field)
            for gone in ("maxlength", 'type="number"', "max="):
                self.assertNotIn(gone, field.replace('data-max="999999.99"', ""))
            self.assertIn(f'mgmt-input-unit-sign" aria-hidden="true">{unit}<', html)

    def test_the_locked_location_is_a_fact_row_and_the_circuit_follows_the_location(self):
        html = self.page("locations_metering_point_new", self.place.pk)
        self.assertIn("mgmt-fact-value", html)
        self.assertNotIn('name="location"', html)
        self.assertIn("Der Messpunkt wird an diesem Standort angelegt.", html)
        edit = self.page("metering_points_edit", self.point.pk)
        self.assertIn("Der Trafokreis ergibt sich aus dem Standort.", edit)
        self.assertNotIn("transformer_circuit", edit)  # not editable there

    def test_after_the_detour_for_a_missing_location_the_cursor_goes_to_the_number(self):
        # UX-010: the location is chosen already; the first field that is still empty gets the cursor.
        html = self.page("metering_points_new", location=self.place.pk)
        self.assertIn(f'<option value="{self.place.pk}" selected>', html)
        marked = re.findall(r"<[^>]*data-first-field[^>]*>", html)
        self.assertEqual(len(marked), 1)
        self.assertIn('name="number_digits"', marked[0])

    def test_the_edit_dialog_has_one_statement_of_the_point_and_all_locked_rows_above_the_divider(self):
        # S-3: no lead that repeats the locked rows; number, direction and LEG stand together above "Optional".
        html = self.page("metering_points_edit", self.point.pk)
        self.assertNotIn('id="dialog-lead"', html)
        self.assertNotRegex(html, r"Messpunkt CH\S+ \(")
        location = re.search(r'<select[^>]*name="location"[^>]*>', html).group(0)
        self.assertNotIn("dialog-lead", location)
        divider = html.index("mgmt-dialog-divider")
        for label in ("Messpunkt-Nummer", "Richtung", "LEG"):
            self.assertLess(html.index(f">{label}<"), divider, label)
        self.assertEqual(html.count("is-fact"), 3)
        for name in ("pv_power_kwp", "battery_kwh", "wallbox_kw", "remark"):
            self.assertGreater(html.index(f'name="{name}"'), divider, name)
        # The create form keeps its lead and its divider before the LEG.
        created = self.page("metering_points_new")
        self.assertIn('id="dialog-lead"', created)
        self.assertLess(created.index("mgmt-dialog-divider"), created.index('name="leg"'))

    def test_a_move_names_the_change_of_the_transformer_circuit_only_where_it_changes(self):
        # UX-005: a live line under the location drop-down; the notes are on the options, the server decides.
        far = make_location("Fernweg", "3", "3000", "Bern", transformer_circuit=make_circuit("TRA2", "Zwei"))
        same = make_location("Gleichweg", "4", "3063", "Ittigen", transformer_circuit=self.circuit)
        html = self.page("metering_points_edit", self.point.pk)
        select = re.search(r'<select name="location".*?</select>', html, re.S).group(0)
        self.assertIn('data-option-note="id_location_note"', select)
        self.assertIn('<p class="mgmt-hint mgmt-option-note" id="id_location_note" role="status" aria-live="polite"></p>', html)
        note = re.search(rf'<option value="{far.pk}"[^>]*data-note="([^"]*)"', select)
        self.assertEqual(
            note.group(1), "Der Trafokreis ändert sich: TRA1 → TRA2. Das kann die Rabattstufe der LEG ändern."
        )
        for unchanged in (same, self.place):
            self.assertNotRegex(select, rf'<option value="{unchanged.pk}"[^>]*data-note=')
        empty = re.search(r'<option value=""[^>]*data-note="([^"]*)"', select)
        self.assertEqual(
            empty.group(1),
            "Ohne Standort hat der Messpunkt keinen Trafokreis mehr (jetzt: TRA1). Das kann die Rabattstufe der LEG ändern.",
        )
        self.assertIn("option-note.js", html)
        # The create form has no such line.
        created = self.page("metering_points_new")
        self.assertNotIn("data-option-note", created)
        self.assertNotIn("mgmt-option-note", created)

    def test_the_assign_dialog_of_a_location_notes_the_move_only_for_points_at_another_location(self):
        target = make_location("Zielweg", "9", "3000", "Bern", transformer_circuit=make_circuit("TRA3", "Drei"))
        loose = make_metering_point(2)  # no location: no circuit changes away
        html = self.page("locations_metering_point_assign", target.pk)
        select = re.search(r'<select name="metering_point".*?</select>', html, re.S).group(0)
        self.assertIn('data-option-note="id_metering_point_note"', select)
        moving = re.search(rf'<option value="{self.point.pk}"[^>]*data-note="([^"]*)"', select)
        self.assertEqual(
            moving.group(1), "Der Trafokreis ändert sich: TRA1 → TRA3. Das kann die Rabattstufe der LEG ändern."
        )
        self.assertNotRegex(select, rf'<option value="{loose.pk}"[^>]*data-note=')  # the lead sentence says it
        self.assertIn('id="id_metering_point_note"', html)

    def test_a_point_without_a_leg_says_so_as_a_fact_and_not_like_an_input(self):
        html = self.page("metering_points_edit", self.point.pk)
        self.assertIn("Keine LEG zugeordnet", html)
        self.assertNotIn("Noch keine LEG gewählt", html)

    def test_the_circuit_list_ends_with_the_option_that_creates_a_new_circuit(self):
        html = self.page("locations_new")
        select = re.search(r'<select name="transformer_circuit".*?</select>', html, re.S).group(0)
        options = re.findall(r'<option value="([^"]*)"[^>]*>(.*?)</option>', select)
        self.assertEqual(options[0], ("", "Kein Trafokreis"))
        self.assertEqual(options[1][1], "TRA1 · Eins")
        self.assertEqual(options[-1], ("__new__", "+ Neuen Trafokreis anlegen …"))
        # Both fields of the new circuit are always in the page (without a script the server decides).
        self.assertIn('name="new_circuit_number"', html)
        self.assertIn('class="mgmt-dialog-row is-circuit-new"', html)
        self.assertIn("TRA gefolgt von Ziffern, z. B. TRA21359.", html)
        self.assertNotIn("<datalist", html)

    def test_the_person_field_is_a_combobox_with_its_texts_in_the_language_of_the_page(self):
        html = self.page("metering_points_person", self.point.pk)
        field = re.search(r'<input[^>]*name="person"[^>]*>', html).group(0)
        for needle in (
            f'data-suggest-url="{reverse("management:metering_points_person_search")}"', 'data-detail-label="Kundennr."',
            'data-message-one="1 Vorschlag verfügbar."', 'data-message-many="%(count)s Vorschläge verfügbar."',
            'data-message-none="Keine Person gefunden. Zugewiesen werden können nur aktive Personen mit Kundennummer."',
            'data-message-limited="Zu viele Suchanfragen. Bitte warten Sie einen Moment."',
        ):
            self.assertIn(needle, field, needle)
        self.assertNotIn("list=", field)


@tag("mariadb")
class MenuOrderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.menu@example.org", superuser=True)  # can use the administration (D-102)

    def test_the_system_administration_is_the_very_last_entry_of_the_menu_on_a_desktop_and_a_phone(self):
        client = VerifiedClient()
        client.force_login(self.staff)
        html = client.get(reverse("management:overview")).content.decode()
        menus = [part for part in re.split(r'(?=<aside|<nav)', html) if "frame-item" in part]
        self.assertGreaterEqual(len(menus), 2)  # the left menu and the menu of the phone
        all_items = re.findall(r'<(?:a|button)[^>]*class="frame-item[^"]*"[^>]*>.*?</(?:a|button)>', html, re.S)
        labels = [re.sub(r"<[^>]+>|\s+", " ", item).strip() for item in all_items]
        # In every copy of the menu "Systemverwaltung" comes after "Mitteilungen" and before "Abmelden".
        for index, label in enumerate(labels):
            if label.startswith("Systemverwaltung"):
                self.assertTrue(labels[index - 1].startswith("Mitteilungen"), labels[index - 1])
                self.assertTrue(labels[index + 1].startswith("Abmelden"), labels[index + 1])
        self.assertEqual(sum(label.startswith("Systemverwaltung") for label in labels), 2)
        for part in menus:
            self.assertLess(part.index("Verwaltung</p>"), part.index("Systemverwaltung"))
            self.assertLess(part.index("Demnächst"), part.index("Systemverwaltung"))
            self.assertIn('<p class="frame-group">System</p>', part)
            self.assertIn("#i-server", part)  # its own icon, not the gear of "Einstellungen"
