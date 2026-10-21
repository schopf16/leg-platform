"""The script of the typing suggestions run in a headless Chrome or Chromium (skipped where there is none), the same pattern as
the number rule of ``field-check.js``: the real markup of ``addresses/widget.html``, the real script, a ``fetch`` that answers
like the endpoint. What is proven: three characters before the street asks; the street, the postal code and the town are
comboboxes with the ARIA attributes (role, expanded, controls, active descendant) over listboxes of options; the keyboard
(arrow down, Enter, Escape, arrow up) works from the input; choosing a street suggestion fills street, house number, postal
code and town; one town for a complete postal code and one postal code for a complete town are filled in and announced
(never over what the person typed); several are offered as a choice, the LEG's area first as the server ranked them; a
postal code that the script filled in does not restrict a new town; the sentence for an empty directory is shown to a staff
account only."""

import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import translation

SCRIPT = Path(settings.BASE_DIR) / "leg_platform" / "static" / "js" / "address-directory.js"

STUB = """
const calls = [];
const ADDRESSES = [
  {street: "Gantrischweg", house_number: "1", postal_code: "3063", locality: "Ittigen"},
  {street: "Gantrischweg", house_number: "2", postal_code: "3063", locality: "Ittigen"},
  {street: "Gantrischweg", house_number: "1", postal_code: "3004", locality: "Bern"},
];
const BERN = [{postal_code: "3000", locality: "Bern"}, {postal_code: "3001", locality: "Bern"}];
const ITTIGEN = [{postal_code: "3063", locality: "Ittigen"}];
const SHARED = [{postal_code: "9990", locality: "Alpha"}, {postal_code: "9990", locality: "Beta"}];
window.fetch = async (url, options) => {
  const values = Object.fromEntries(new URLSearchParams(options.body).entries());
  values.url = String(url);
  values.csrf = options.headers["X-CSRFToken"];
  calls.push(values);
  let data = {status: STATUS, data_date: null, stale: false, warning: "", results: [], match: null, kind: "prefix"};
  if (STATUS === "available") {
    const town = (values.locality || "").toLowerCase();
    if (values.mode === "suggest" && values.street.toLowerCase().startsWith("gan")) data.results = ADDRESSES;
    if (values.mode === "locality") {
      data.kind = "locality";
      if (values.postal_code === "3063") data.results = ITTIGEN;
      else if (values.postal_code === "9990") data.results = SHARED;
      else if (values.postal_code === "") {
        if (town.startsWith("itt")) data.results = ITTIGEN;
        else if (town.startsWith("ber")) data.results = BERN;
      }
    }
    if (values.mode === "check") data.match = true;
  }
  return {ok: true, status: 200, headers: {get: () => null}, json: async () => data};
};
"""

SCENARIO = """
const $ = (id) => document.getElementById(id);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const type = (id, value) => {
  const field = $(id);
  field.focus();
  field.value = value;
  field.dispatchEvent(new Event("input", {bubbles: true}));
};
const press = (id, key) => $(id).dispatchEvent(new KeyboardEvent("keydown", {key, bubbles: true, cancelable: true}));
const announced = () => document.querySelector("[data-address-announce]").textContent;
const combo = (id) => {
  const input = $(id);
  const list = $(input.getAttribute("aria-controls") || "none");
  const options = list ? Array.from(list.querySelectorAll("[role=option]")) : [];
  return {
    role: input.getAttribute("role"), expanded: input.getAttribute("aria-expanded"),
    popup: input.getAttribute("aria-haspopup"), autocomplete: input.getAttribute("aria-autocomplete"),
    listRole: list ? list.getAttribute("role") : null, listLabel: list ? list.getAttribute("aria-labelledby") : null,
    active: input.getAttribute("aria-activedescendant"),
    options: options.map((option) => ({id: option.id, text: option.textContent, selected: option.getAttribute("aria-selected")})),
    visible: list ? !list.closest(".address-suggest").hidden : false,
  };
};
const values = () => ({street: $("id_street").value, house: $("id_house_number").value,
                      postal: $("id_postal_code").value, town: $("id_town").value});
const reset = () => { ["id_street", "id_house_number", "id_postal_code", "id_town"].forEach((id) => { $(id).value = ""; }); };
const out = {};
const run = async () => {
  out.initial = {street: combo("id_street"), postal: combo("id_postal_code"), town: combo("id_town")};

  // Two characters ask nothing; three do, after the short delay.
  type("id_street", "ga");
  await wait(500);
  out.afterTwo = {calls: calls.filter((c) => c.mode === "suggest").length, street: combo("id_street")};
  type("id_street", "gantrischweg");
  await wait(500);
  const asked = calls.filter((c) => c.mode === "suggest");
  out.afterTyping = {street: combo("id_street"), announced: announced(), asked: asked.length, last: asked[asked.length - 1]};

  // The keyboard: down moves to the first option, down again to the second, up back, Enter takes it.
  press("id_street", "ArrowDown");
  out.down1 = combo("id_street");
  press("id_street", "ArrowDown");
  out.down2 = combo("id_street");
  press("id_street", "ArrowUp");
  press("id_street", "ArrowUp");
  out.up = combo("id_street");
  press("id_street", "Escape");
  out.escaped = {street: combo("id_street"), typed: $("id_street").value};
  press("id_street", "ArrowDown");
  await wait(500);
  press("id_street", "ArrowDown");
  press("id_street", "ArrowDown");
  press("id_street", "ArrowDown");
  out.third = combo("id_street").active;
  const enter = new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true});
  $("id_street").dispatchEvent(enter);
  out.enter = {prevented: enter.defaultPrevented, values: values(), street: combo("id_street"), announced: announced()};
  const noRow = new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true});
  $("id_street").dispatchEvent(noRow);
  out.enterWithoutRow = noRow.defaultPrevented;

  // A postal code with one town fills the empty town and says so; the town is then the script's, not the person's.
  reset();
  type("id_postal_code", "3063");
  await wait(500);
  out.oneTown = {values: values(), announced: announced(), postal: combo("id_postal_code")};
  // A postal code with two towns offers both; a click takes one and fills both fields.
  type("id_postal_code", "9990");
  await wait(500);
  out.twoTowns = combo("id_postal_code");
  document.getElementById(out.twoTowns.options[1].id).click();
  out.chosenTown = {values: values(), postal: combo("id_postal_code")};
  // What the person typed is not replaced: a typed town stays when the postal code names one other town.
  reset();
  type("id_town", "Thun");
  type("id_postal_code", "3063");
  await wait(500);
  out.typedTownStays = {values: values(), postal: combo("id_postal_code")};

  // A town with one postal code fills the empty postal code; a town with several offers the list.
  reset();
  type("id_town", "Ittigen");
  await wait(500);
  out.onePostalCode = {values: values(), announced: announced(), town: combo("id_town")};
  // The postal code that the script filled in does not restrict the new town: the request carries none.
  type("id_town", "Bern");
  await wait(500);
  const localityCalls = calls.filter((c) => c.mode === "locality");
  out.severalPostalCodes = {town: combo("id_town"), last: localityCalls[localityCalls.length - 1]};
  document.getElementById(out.severalPostalCodes.town.options[1].id).click();
  out.chosenPostalCode = values();
  // Two characters of a town ask nothing.
  const before = calls.length;
  reset();
  type("id_town", "Be");
  await wait(500);
  out.twoOfTown = {asked: calls.length - before, town: combo("id_town")};

  out.noInlineMarkup = document.querySelectorAll("[onclick], [style]").length;
  out.notice = $("address-notice").textContent;
  out.csrf = calls.length ? calls[0].csrf : null;
  out.endpoint = calls.length ? calls[0].url : null;
};
run().then(() => { document.getElementById("out").textContent = "RESULT" + JSON.stringify(out) + "END"; });
"""

MISSING_SCENARIO = """
const $ = (id) => document.getElementById(id);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {};
const run = async () => {
  const field = $("id_street");
  field.focus();
  field.value = "gantrischweg";
  field.dispatchEvent(new Event("input", {bubbles: true}));
  await wait(600);
  out.notice = $("address-notice").textContent;
  out.noticeClass = $("address-notice").firstElementChild ? $("address-notice").firstElementChild.className : "";
  out.described = $("id_town").getAttribute("aria-describedby");
  out.panelHidden = document.querySelector("[data-address-suggest]").hidden;
  // It stays while the person types.
  field.value = "gantrischweg 1";
  field.dispatchEvent(new Event("input", {bubbles: true}));
  out.stillThere = $("address-notice").textContent !== "";
};
run().then(() => { document.getElementById("out").textContent = "RESULT" + JSON.stringify(out) + "END"; });
"""


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


class ScriptInAChromeTests(SimpleTestCase):
    """The page is a bare form; no styles are needed (the script never depends on them)."""

    def run_page(self, scenario, *, status="available", staff=False):
        chrome = browser()
        if chrome is None:
            self.skipTest("no Chrome or Chromium found")
        request = SimpleNamespace(user=SimpleNamespace(is_staff=staff))
        with translation.override("en"):
            widget = render_to_string("addresses/widget.html", {"address_country_value": "CH", "request": request})
            notice = render_to_string("addresses/widget_notice.html", {})
        page = (
            '<!doctype html><meta charset="utf-8"><body><form method="post">'
            '<input type="hidden" name="csrfmiddlewaretoken" value="token-value">'
            f'<div><label for="id_street">Street</label><input id="id_street" type="text">{widget}</div>'
            '<div><label for="id_house_number">No.</label><input id="id_house_number" type="text"></div>'
            '<div><label for="id_postal_code">Postal code</label><input id="id_postal_code" type="text"></div>'
            '<div><label for="id_town">Town</label><input id="id_town" type="text"></div>'
            f'{notice}<button type="submit">Save</button></form><pre id="out"></pre>'
            f'<script>const STATUS = "{status}";{STUB}</script>'
            f'<script src="{SCRIPT.as_uri()}"></script><script>{scenario}</script>'
        )
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "address.html"
            file.write_text(page, encoding="utf-8")
            done = subprocess.run(
                [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", f"--user-data-dir={folder}/profile",
                 "--allow-file-access-from-files", "--virtual-time-budget=60000", "--dump-dom", file.as_uri()],
                capture_output=True, timeout=180, check=False,
            )
        output = done.stdout.decode("utf-8", "replace")
        found = re.search(r"RESULT(.*?)END", output, re.S)
        self.assertIsNotNone(found, output[:800])
        return json.loads(html.unescape(found.group(1)))

    def test_the_three_inputs_are_comboboxes_and_the_street_and_the_keyboard_work(self):
        out = self.run_page(SCENARIO)

        # Before anything is typed: comboboxes over closed listboxes.
        for name in ("street", "postal", "town"):
            state = out["initial"][name]
            self.assertEqual(
                (state["role"], state["expanded"], state["popup"], state["autocomplete"], state["listRole"]),
                ("combobox", "false", "listbox", "list", "listbox"), name,
            )
            self.assertTrue(state["listLabel"], name)
            self.assertEqual((state["options"], state["visible"]), ([], False), name)
        # Two characters ask nothing; the text of three or more does, once after the delay, as a POST with the token.
        self.assertEqual(out["afterTwo"]["calls"], 0)
        self.assertEqual(out["afterTwo"]["street"]["visible"], False)
        self.assertEqual(out["afterTyping"]["asked"], 1)
        self.assertEqual(out["afterTyping"]["last"]["street"], "gantrischweg")
        self.assertEqual(out["afterTyping"]["last"]["country"], "CH")
        self.assertEqual(out["csrf"], "token-value")
        self.assertTrue(out["endpoint"].endswith("/addresses/lookup/"))
        street = out["afterTyping"]["street"]
        self.assertEqual((street["expanded"], street["visible"], len(street["options"])), ("true", True, 3))
        self.assertEqual(street["options"][0]["text"], "Gantrischweg 1, 3063 Ittigen")  # the order of the server
        self.assertEqual(street["active"], None)
        self.assertIn("3 address suggestions available", out["afterTyping"]["announced"])
        # The keyboard: the focus stays in the input; the active option is named by aria-activedescendant.
        self.assertEqual(out["down1"]["active"], out["down1"]["options"][0]["id"])
        self.assertEqual([o["selected"] for o in out["down1"]["options"]], ["true", "false", "false"])
        self.assertEqual(out["down2"]["active"], out["down2"]["options"][1]["id"])
        self.assertEqual(out["up"]["active"], None)  # up from the first option goes back to the text
        self.assertEqual(out["escaped"]["street"]["expanded"], "false")
        self.assertEqual(out["escaped"]["typed"], "gantrischweg")  # Escape keeps what was typed
        self.assertEqual(out["third"], out["down1"]["options"][2]["id"])
        # Enter on the active option fills ALL four fields, is not sent as the form, and is announced.
        self.assertTrue(out["enter"]["prevented"])
        self.assertEqual(
            out["enter"]["values"],
            {"street": "Gantrischweg", "house": "1", "postal": "3004", "town": "Bern"},
        )
        self.assertEqual(out["enter"]["street"]["expanded"], "false")
        self.assertIn("Address filled in: Gantrischweg 1, 3004 Bern.", out["enter"]["announced"])
        # Without an active option Enter sends the form as always.
        self.assertFalse(out["enterWithoutRow"])

    def test_a_postal_code_gives_its_town_and_a_town_its_postal_code(self):
        out = self.run_page(SCENARIO)

        # One town: filled in an empty field and announced; no list.
        self.assertEqual(out["oneTown"]["values"]["town"], "Ittigen")
        self.assertIn("Town filled in: Ittigen.", out["oneTown"]["announced"])
        self.assertEqual(out["oneTown"]["postal"]["visible"], False)
        # Several towns: a choice under the postal code; a click fills the postal code and the town.
        self.assertEqual([o["text"] for o in out["twoTowns"]["options"]], ["9990 Alpha", "9990 Beta"])
        self.assertEqual(out["twoTowns"]["expanded"], "true")
        self.assertEqual(out["chosenTown"]["values"], {"street": "", "house": "", "postal": "9990", "town": "Beta"})
        self.assertEqual(out["chosenTown"]["postal"]["expanded"], "false")
        # What the person typed stays; the other town is offered, not forced.
        self.assertEqual(out["typedTownStays"]["values"]["town"], "Thun")
        self.assertEqual([o["text"] for o in out["typedTownStays"]["postal"]["options"]], ["3063 Ittigen"])
        # One postal code for a town: filled in an empty field and announced.
        self.assertEqual(out["onePostalCode"]["values"]["postal"], "3063")
        self.assertIn("Postal code filled in: 3063.", out["onePostalCode"]["announced"])
        # A town with several: the list under the town, and the postal code that the script filled in is not sent.
        self.assertEqual([o["text"] for o in out["severalPostalCodes"]["town"]["options"]], ["3000 Bern", "3001 Bern"])
        self.assertEqual(out["severalPostalCodes"]["last"]["postal_code"], "")
        self.assertEqual(out["chosenPostalCode"]["postal"], "3001")
        self.assertEqual(out["chosenPostalCode"]["town"], "Bern")
        # Two characters of a town ask nothing.
        self.assertEqual(out["twoOfTown"], {"asked": 0, "town": out["twoOfTown"]["town"]})
        self.assertEqual(out["twoOfTown"]["town"]["visible"], False)
        self.assertEqual(out["noInlineMarkup"], 0)
        self.assertEqual(out["notice"], "")

    def test_the_sentence_about_an_empty_directory_is_shown_to_staff_only(self):
        staff = self.run_page(MISSING_SCENARIO, status="missing", staff=True)
        other = self.run_page(MISSING_SCENARIO, status="missing", staff=False)

        self.assertIn("address directory is not available or has not been imported yet", staff["notice"])
        self.assertEqual(staff["noticeClass"], "alert alert-secondary")
        self.assertEqual(staff["described"], "address-notice")
        self.assertEqual(staff["panelHidden"], True)
        self.assertEqual(staff["stillThere"], True)
        # Everybody else is told nothing (D-066).
        self.assertEqual((other["notice"], other["described"], other["panelHidden"]), ("", None, True))
