"""The address script in a headless Chrome or Edge, with the real stylesheet (skipped where there is no Chromium browser): the list
(one line per address, seven rows, the foot line only when more matches exist, an overlay that moves nothing), when it closes,
the card "Did you mean this address?" and its two ways out, the fallback link, the retry, the minimum of a check and the
scrolling into view (D-111, owner specification of 2026-10-28, A1 to A10, B4, E2, F7, G1).

The page is the real ``addresses/widget.html`` and ``widget_notice.html`` in a row layout like the portal's, the real
``address-suggest.css`` and ``address-directory.js``; the ``fetch`` answers like the endpoint, a scenario changes its answers.
The existing harness (``test_widget_script``) proves the combobox basics and the fills; this one the rest."""

import html
import json
import re
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import translation

from leg_platform.addresses.tests.test_widget_script import browser

STATIC = Path(settings.BASE_DIR) / "leg_platform" / "static"
SCRIPT = STATIC / "js" / "address-directory.js"
STYLE = STATIC / "css" / "address-suggest.css"

HEAD_STYLE = """
body { font: 16px/1.4 sans-serif; margin: 0; padding: 8px 16px; }
form { display: grid; gap: 16px; }
.field-pair { display: grid; grid-template-columns: 1fr 8rem; gap: 8px; }
.field-pair-locality { grid-template-columns: 8rem 1fr; }
.mb-3 { display: grid; gap: 8px; }
input { width: 100%; box-sizing: border-box; font: inherit; padding: 6px; }
"""

STUB = """
const calls = [];
window.OVERRIDE = null;
window.DELAY = null;
const ADDRESSES = [
  {street: "Gantrischweg", house_number: "1", postal_code: "3063", locality: "Ittigen"},
  {street: "Gantrischweg", house_number: "2", postal_code: "3063", locality: "Ittigen"},
];
window.fetch = async (url, options) => {
  const values = Object.fromEntries(new URLSearchParams(options.body).entries());
  calls.push(values);
  let data = {status: STATUS, data_date: null, stale: false, warning: "", results: [], match: null, kind: "prefix",
              more: false, proposals: [], reason: ""};
  if (STATUS === "available") {
    if (values.mode === "suggest" && values.street.toLowerCase().startsWith("gan")) data.results = ADDRESSES;
    if (values.mode === "locality") data.kind = "locality";
    if (values.mode === "check") data.match = true;
  }
  if (window.OVERRIDE) data = window.OVERRIDE(values, data) || data;
  if (window.DELAY) await new Promise((resolve) => setTimeout(resolve, window.DELAY(values)));
  return {ok: true, status: 200, headers: {get: () => null}, json: async () => data};
};
"""

HELPERS = """
const $ = (id) => document.getElementById(id);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const type = (id, value) => {
  const field = $(id);
  field.focus();
  field.value = value;
  field.dispatchEvent(new Event("input", {bubbles: true}));
};
const fill = (street, house, postal, town) => {
  type("id_street", street);
  type("id_house_number", house);
  type("id_postal_code", postal);
  type("id_town", town);
};
const reset = () => { ["id_street", "id_house_number", "id_postal_code", "id_town"].forEach((id) => { $(id).value = ""; }); };
const press = (id, key, extra = {}) => {
  const event = new KeyboardEvent("keydown", {key, bubbles: true, cancelable: true, ...extra});
  $(id).dispatchEvent(event);
  return event;
};
const pressDocument = (key) => document.dispatchEvent(new KeyboardEvent("keydown", {key, bubbles: true, cancelable: true}));
const announced = () => document.querySelector("[data-address-announce]").textContent;
const values = () => ({street: $("id_street").value, house: $("id_house_number").value,
                      postal: $("id_postal_code").value, town: $("id_town").value});
const notice = $("address-notice");
const checks = () => calls.filter((c) => c.mode === "check").length;
const listOf = (id) => $($(id).getAttribute("aria-controls"));
const panelOf = (id) => listOf(id).closest(".address-suggest");
const shown = (id) => !panelOf(id).hidden;
const rect = (element) => { const r = element.getBoundingClientRect(); return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height}; };
const optionsOf = (id) => Array.from(listOf(id).querySelectorAll("[role=option]"));
const proposal = (reason, list) => (values) => values.mode === "check"
  ? {status: "available", data_date: null, stale: false, warning: "This address was not found.", results: [], match: false,
     kind: "prefix", more: false, proposals: list, reason}
  : null;
const FIX = {street: "Ittigenstrasse", house_number: "51", postal_code: "3063", locality: "Ittigen"};
const SECOND = {street: "Ittigenstrasse", house_number: "15", postal_code: "3063", locality: "Ittigen"};
const out = {};
const finish = () => { document.getElementById("out").textContent = "RESULT" + JSON.stringify(out) + "END"; };
"""

LIST_SCENARIO = HELPERS + """
const many = (count) => Array.from({length: count}, (_, number) => (
  {street: "Gantrischweg", house_number: String(number + 1), postal_code: "3063", locality: "Ittigen"}));
const run = async () => {
  out.hostClass = $("id_street").closest(".field-pair").className;
  out.cases = [];
  const before = rect($("id_house_number")).top;
  const submitBefore = rect(document.querySelector("button[type=submit]")).top;
  for (const [name, count, more] of [
    ["nine, more", 9, true], ["seven", 7, false], ["three", 3, false], ["eight, no flag", 8, undefined], ["three, more", 3, true],
  ]) {
    window.OVERRIDE = (values, data) => values.mode === "suggest" ? {...data, results: many(count), more} : null;
    press("id_street", "Escape");
    reset();
    type("id_street", "gantrischweg " + name.length);
    await wait(600);
    const panel = panelOf("id_street");
    const list = listOf("id_street");
    const first = optionsOf("id_street")[0];
    out.cases.push({
      name, visible: shown("id_street"), options: optionsOf("id_street").length,
      foot: panel.querySelector(".address-more").hidden ? null : panel.querySelector(".address-more").textContent,
      title: panel.querySelector(".address-suggest-title").textContent,
      note: panel.querySelector(".address-suggest-note").textContent,
      texts: optionsOf("id_street").slice(0, 2).map((option) => option.textContent),
      parts: first ? Array.from(first.children).map((child) => [child.className, child.textContent]) : [],
      announced: announced(), expanded: $("id_street").getAttribute("aria-expanded"),
      houseTop: rect($("id_house_number")).top, submitTop: rect(document.querySelector("button[type=submit]")).top,
    });
  }
  // The layout of the biggest list.
  window.OVERRIDE = (values, data) => values.mode === "suggest" ? {...data, results: many(9), more: true} : null;
  press("id_street", "Escape");
  type("id_street", "gantrischweg layout");
  await wait(600);
  const panel = panelOf("id_street");
  const list = listOf("id_street");
  const host = $("id_street").closest(".field-pair");
  const style = getComputedStyle(panel);
  const listStyle = getComputedStyle(list);
  out.layout = {
    position: style.position, zIndex: style.zIndex, boxShadow: style.boxShadow, hostPosition: getComputedStyle(host).position,
    panel: rect(panel), host: rect(host), street: rect($("id_street")),
    overflowY: listStyle.overflowY, maxHeight: listStyle.maxHeight, scrolls: list.scrollHeight > list.clientHeight,
    optionHeights: optionsOf("id_street").map((option) => rect(option).height),
    houseTop: rect($("id_house_number")).top, houseBefore: before, submitBefore, submitTop: rect(document.querySelector("button[type=submit]")).top,
    pageHeight: document.documentElement.scrollHeight,
  };
  press("id_street", "Escape");
  out.layout.pageHeightClosed = document.documentElement.scrollHeight;
  out.layout.houseTopClosed = rect($("id_house_number")).top;
  out.postal = {host: $("id_postal_code").closest(".field-pair").className, role: $("id_postal_code").getAttribute("role")};
  finish();
};
run();
"""

CLOSE_SCENARIO = HELPERS + """
const many = (count) => Array.from({length: count}, (_, number) => (
  {street: "Gantrischweg", house_number: String(number + 1), postal_code: "3063", locality: "Ittigen"}));
const run = async () => {
  window.OVERRIDE = (values, data) => values.mode === "suggest" ? {...data, results: many(3)} : null;
  const open = async (text) => { reset(); type("id_street", text); await wait(600); return shown("id_street"); };

  out.opened = await open("gantrisch one");
  // The focus moves to the house number: the street list stays (the person narrows it there).
  $("id_house_number").focus();
  out.inHouseNumber = shown("id_street");
  // The focus moves to the postal code: the list closes.
  $("id_postal_code").focus();
  out.inPostalCode = {shown: shown("id_street"), expanded: $("id_street").getAttribute("aria-expanded"), announced: announced()};

  // A press outside closes; a press inside the panel does not.
  await open("gantrisch two");
  optionsOf("id_street")[0].dispatchEvent(new PointerEvent("pointerdown", {bubbles: true}));
  out.pressInside = shown("id_street");
  document.body.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true}));
  out.pressOutside = shown("id_street");

  // Tab closes the list and leaves the focus where the browser puts it (the keydown handler does not move it).
  await open("gantrisch three");
  const tab = press("id_street", "Tab");
  out.tab = {shown: shown("id_street"), prevented: tab.defaultPrevented};
  // Shift+Tab: the keydown is ignored (a modifier), the focus leaves for the button before the form: the list closes.
  await open("gantrisch four");
  const shiftTab = press("id_street", "Tab", {shiftKey: true});
  out.shiftTabKeydown = shown("id_street");
  $("before").focus();
  out.shiftTab = {shown: shown("id_street"), prevented: shiftTab.defaultPrevented, active: document.activeElement.id};

  // A stale list never opens over the next field: the answer arrives after the person moved to the postal code.
  window.DELAY = (values) => (values.mode === "suggest" ? 700 : 0);
  reset();
  type("id_street", "gantrisch five");
  await wait(300);   // the request is on its way
  $("id_postal_code").focus();
  await wait(1200);  // the answer has arrived
  out.stale = {street: shown("id_street"), postal: shown("id_postal_code"), expanded: $("id_street").getAttribute("aria-expanded"),
               active: document.activeElement.id};
  // And the same for the timer that has not even sent its request.
  window.DELAY = null;
  const asked = calls.filter((c) => c.mode === "suggest").length;
  reset();
  type("id_street", "gantrisch six");
  $("id_postal_code").focus();
  await wait(800);
  out.timer = {asked: calls.filter((c) => c.mode === "suggest").length - asked, street: shown("id_street")};

  // Escape keeps what was typed and closes; the focus stays in the field.
  await open("gantrisch seven");
  const escape = press("id_street", "Escape");
  out.escape = {shown: shown("id_street"), typed: $("id_street").value, prevented: escape.defaultPrevented, active: document.activeElement.id};
  // ArrowDown opens again from the text without typing.
  press("id_street", "ArrowDown");
  await wait(600);
  out.reopened = shown("id_street");
  finish();
};
run();
"""

CARD_SCENARIO = HELPERS + """
const run = async () => {
  const state = () => ({
    kind: notice.dataset.kind || null, empty: notice.children.length === 0,
    title: (notice.querySelector(".address-proposal-title") || {}).textContent || null,
    note: (notice.querySelector(".address-proposal-note") || {}).textContent || null,
    hint: (notice.querySelector(".address-proposal-hint") || {}).textContent || null,
    options: Array.from(notice.querySelectorAll(".address-proposal-option")).map((option) => option.textContent),
    keep: (notice.querySelector(".address-proposal-keep") || {}).textContent || null,
    describedBy: $("id_street").getAttribute("aria-describedby"),
  });
  const wrong = () => fill("ittigenstrasse", "51", "3061", "Ittigen");

  // One proposal: the card, with every text of it.
  window.OVERRIDE = (values, data) => values.mode === "check" && values.postal_code === "3061"
    ? proposal("postal_code", [FIX])(values) : null;
  wrong();
  await wait(1500);
  out.one = state();
  out.oneChecks = checks();
  out.position = getComputedStyle(notice.firstElementChild).position;
  out.submitWithCard = rect(document.querySelector("button[type=submit]")).top;
  out.reasonSentence = out.one.note;

  // Accept: all four fields are set, the card is gone, the live region says so, the focus is back in a field.
  document.querySelector(".address-proposal-option").click();
  out.accepted = {values: values(), empty: notice.children.length === 0, announced: announced(), active: document.activeElement.id,
                  describedBy: $("id_street").getAttribute("aria-describedby")};
  await wait(1500);
  out.afterAccept = {empty: notice.children.length === 0, checks: checks()};
  out.submitWithoutCard = rect(document.querySelector("button[type=submit]")).top;

  // "No, my address is correct": the input stays, the card does not return for these exact values, but for other values.
  wrong();
  await wait(1500);
  out.again = state().kind;
  document.querySelector(".address-proposal-keep").click();
  out.kept = {values: values(), empty: notice.children.length === 0, announced: announced(), active: document.activeElement.id};
  const keptChecks = checks();
  type("id_town", "Ittigen");   // the same values typed again
  await wait(1500);
  out.notReturning = {empty: notice.children.length === 0, asked: checks() > keptChecks};
  type("id_house_number", "52");
  await wait(1500);
  out.otherValues = state().kind;
  type("id_house_number", "51");
  await wait(1500);
  out.dismissedStillKnown = state().kind;

  // Escape closes the card WITHOUT recording "No, my address is correct": it comes back for the same values.
  type("id_house_number", "53");
  await wait(1500);
  out.beforeEscape = state().kind;
  pressDocument("Escape");
  out.escaped = {empty: notice.children.length === 0, active: document.activeElement.id};
  type("id_town", "Ittigen");
  await wait(1500);
  out.afterEscape = state().kind;

  // Several proposals: the plural title and every option.
  window.OVERRIDE = (values, data) => values.mode === "check" ? proposal("house_number", [FIX, SECOND])(values) : null;
  fill("ittigenstrasse", "151", "3063", "Ittigen");
  await wait(1500);
  const many = state();
  out.many = {title: many.title, options: many.options, kind: many.kind};

  // At most three options, whatever the server sends.
  window.OVERRIDE = (values, data) => values.mode === "check"
    ? proposal("house_number", [FIX, SECOND, FIX, SECOND, FIX])(values) : null;
  fill("ittigenstrasse", "152", "3063", "Ittigen");
  await wait(1500);
  out.cap = state().options.length;

  // Escape on the street with a list open closes the list first and leaves the card; the second Escape closes the card.
  window.OVERRIDE = (values, data) => values.mode === "suggest"
    ? {...data, results: [{street: "Ittigenstrasse", house_number: "5", postal_code: "3063", locality: "Ittigen"}]}
    : proposal("postal_code", [FIX])(values);
  fill("ittigenstrasse", "51", "3062", "Ittigen");
  await wait(1500);
  const cardBefore = state().kind;
  $("id_street").focus();
  press("id_street", "ArrowDown");
  await wait(600);
  const listOpen = shown("id_street");
  press("id_street", "Escape");
  out.escapeOrder = {cardBefore, listOpen, afterFirst: {list: shown("id_street"), card: state().kind}};
  press("id_street", "Escape");
  out.escapeOrder.afterSecond = {list: shown("id_street"), card: state().kind};
  finish();
};
run();
"""

FALLBACK_SCENARIO = HELPERS + """
const run = async () => {
  const nothing = (values) => values.mode === "check"
    ? {status: "available", data_date: null, stale: false, warning: "This address was not found.", results: [], match: false,
       kind: "prefix", more: false, proposals: [], reason: ""}
    : null;
  window.OVERRIDE = nothing;
  fill("nirgendwoweg", "5", "3063", "Ittigen");
  await wait(1500);
  const box = notice.querySelector(".address-warning");
  out.warning = {
    kind: notice.dataset.kind || null, alert: box ? box.querySelector(".alert").textContent : null,
    link: box ? box.querySelector(".address-proposal-keep").textContent : null, card: notice.querySelector(".address-proposal") !== null,
    describedBy: $("id_town").getAttribute("aria-describedby"),
  };
  box.querySelector(".address-proposal-keep").click();
  out.used = {empty: notice.children.length === 0, announced: announced(), values: values(), describedBy: $("id_town").getAttribute("aria-describedby")};
  const keptChecks = checks();
  type("id_town", "Ittigen");
  await wait(1500);
  out.notReturning = {empty: notice.children.length === 0, asked: checks() > keptChecks};
  type("id_house_number", "6");
  await wait(1500);
  out.otherValues = notice.querySelector(".address-warning") !== null;

  // A partial address (no complete postal code): no warning, only the card when there is a fix.
  type("id_postal_code", "30");
  await wait(1500);
  out.partial = {empty: notice.children.length === 0};
  finish();
};
run();
"""

RETRY_SCENARIO = HELPERS + """
const run = async () => {
  // Busy for a moment: one silent retry.
  let busy = 1;
  window.OVERRIDE = (values, data) => {
    if (values.mode !== "check") return null;
    if (busy > 0) { busy -= 1; return {...data, status: "unavailable"}; }
    return null;
  };
  fill("ittigenstrasse", "51", "3063", "Ittigen");
  await wait(1200);
  out.afterFirst = {checks: checks(), empty: notice.children.length === 0};
  await wait(1500);
  out.afterRetry = {checks: checks(), empty: notice.children.length === 0};

  // Still unavailable: asked exactly twice, nothing is shown to a person who is not staff.
  window.OVERRIDE = (values, data) => values.mode === "check" ? {...data, status: "unavailable"} : null;
  const before = checks();
  fill("ittigenstrasse", "53", "3063", "Ittigen");
  await wait(5000);
  out.always = {checks: checks() - before, empty: notice.children.length === 0};
  finish();
};
run();
"""

MINIMUM_SCENARIO = HELPERS + """
const run = async () => {
  out.cases = [];
  const cases = [
    ["street of two characters", "ab", "5", "3063", "Ittigen"],
    ["no house number", "abc", "", "3063", "Ittigen"],
    ["the minimum", "abc", "5", "3063", "Ittigen"],
    ["partial postal code", "abc", "5", "30", "Ittigen"],
    ["town without postal code", "abc", "5", "", "Ittigen"],
    ["town of two characters", "abc", "5", "", "It"],
    ["postal code without town", "abc", "5", "3063", ""],
    ["nothing but a street", "abc", "", "", ""],
    ["house number of spaces", "abc", "   ", "3063", "Ittigen"],
  ];
  for (const [name, street, house, postal, town] of cases) {
    reset();
    const before = checks();
    fill(street, house, postal, town);
    await wait(1500);
    out.cases.push([name, checks() - before]);
  }
  finish();
};
run();
"""

COUNTRY_SCENARIO = HELPERS + """
const run = async () => {
  const country = $("id_country");
  country.value = "DE";
  fill("ittigenstrasse", "51", "3063", "Ittigen");
  await wait(1500);
  out.foreign = {calls: calls.length, notice: notice.children.length, panel: shown("id_street")};
  country.value = "CH";
  fill("ittigenstrasse", "51", "3063", "Ittigen");
  await wait(1500);
  out.swiss = {checks: checks(), sent: calls.length > 0 ? calls[calls.length - 1].country : null};
  finish();
};
run();
"""

REVEAL_SCENARIO = HELPERS + """
const run = async () => {
  window.OVERRIDE = (values, data) => values.mode === "suggest"
    ? {...data, results: Array.from({length: 7}, (_, n) => ({street: "Gantrischweg", house_number: String(n + 1), postal_code: "3063", locality: "Ittigen"}))}
    : proposal("postal_code", [FIX])(values);
  // The street field at the bottom of the window: the list opens below the visible part and is scrolled into view once.
  const street = $("id_street");
  street.focus();
  window.scrollTo(0, window.scrollY + street.getBoundingClientRect().bottom - window.innerHeight + 4);
  out.scrollBefore = window.scrollY;
  out.streetVisible = rect(street).bottom <= window.innerHeight;
  type("id_street", "gantrisch reveal");
  await wait(600);
  const panel = panelOf("id_street");
  out.list = {shown: shown("id_street"), bottom: rect(panel).bottom, top: rect(panel).top, height: window.innerHeight,
              scrolled: window.scrollY > out.scrollBefore};
  press("id_street", "Escape");

  // The card in the notice, far below the fields.
  window.scrollTo(0, 0);
  fill("ittigenstrasse", "51", "3061", "Ittigen");
  await wait(1500);
  const card = notice.firstElementChild;
  out.card = {kind: notice.dataset.kind || null, bottom: card ? rect(card).bottom : null, top: card ? rect(card).top : null,
              height: window.innerHeight, scrolled: window.scrollY > 0};
  finish();
};
run();
"""


PRESS_SCENARIO = HELPERS + """
const run = async () => {
  window.OVERRIDE = (values, data) => values.mode === "check" ? proposal("postal_code", [FIX])(values) : null;
  fill("ittigenstrasse", "51", "3061", "Ittigen");
  await wait(1500);
  const button = document.querySelector("button[type=submit]");
  out.position = getComputedStyle(notice.firstElementChild).position;
  out.before = rect(button).top;
  // The first half of a click on the Save button.
  button.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true}));
  out.after = rect(button).top;
  out.closed = notice.children.length === 0;
  if (!out.closed) { pressDocument("Escape"); out.escapeClosed = notice.children.length === 0; }
  finish();
};
run();
"""


SERVER_CARD_SCENARIO = HELPERS + """
const run = async () => {
  const button = document.querySelector("button[type=submit]");
  out.present = notice.querySelector(".address-proposal") !== null;
  out.position = getComputedStyle(notice.firstElementChild).position;
  out.before = rect(button).top;
  button.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true}));
  out.after = rect(button).top;
  out.stillThere = notice.querySelector(".address-proposal") !== null;
  finish();
};
run();
"""


class ScriptBehaviourTests(SimpleTestCase):
    def run_page(self, scenario, *, status="available", staff=False, country_field=False, width=1000, height=800, spacer=0,
                 top_space=0, body_width=None, server_card=False):
        chrome = browser()
        if chrome is None:
            self.skipTest("no Chrome, Chromium or Edge found")
        request = SimpleNamespace(user=SimpleNamespace(is_staff=staff))
        with translation.override("en"):
            widget = render_to_string(
                "addresses/widget.html",
                {"request": request} if country_field else {"address_country_value": "CH", "request": request},
            )
            notice = render_to_string(
                "addresses/widget_notice.html",
                {"card_template": "customer/_building_suggestions.html",
                 "suggestions": [{"index": 0, "label": "Ittigenstrasse 51, 3063 Ittigen", "egid": 1}]}
                if server_card else {},
            )
        country = (
            '<div class="mb-3"><label for="id_country">Country</label><select id="id_country">'
            '<option value="CH" selected>Switzerland</option><option value="DE">Germany</option></select></div>'
            if country_field else ""
        )
        # A browser window is never narrower than about 500 px: a phone width is a narrow body.
        narrow = f"body {{ width: {body_width}px; box-sizing: border-box; }}" if body_width else ""
        page = (
            f'<!doctype html><meta charset="utf-8"><style>{HEAD_STYLE}{narrow}</style>'
            f'<link rel="stylesheet" href="{STYLE.as_uri()}"><body>'
            f'<div style="height:{top_space}px"></div>'
            '<form method="post" class="step-card"><button type="button" id="before">Before</button>'
            '<input type="hidden" name="csrfmiddlewaretoken" value="token-value">'
            f'<div class="field-pair field-pair-street"><div class="mb-3"><label for="id_street">Street</label>'
            f'<input id="id_street" type="text">{widget}</div>'
            '<div class="mb-3"><label for="id_house_number">No.</label><input id="id_house_number" type="text"></div></div>'
            '<div class="field-pair field-pair-locality">'
            '<div class="mb-3"><label for="id_postal_code">Postal code</label><input id="id_postal_code" type="text"></div>'
            '<div class="mb-3"><label for="id_town">Town</label><input id="id_town" type="text"></div></div>'
            f'{country}<div style="height:{spacer}px"></div>{notice}<button type="submit">Save</button></form><pre id="out"></pre>'
            f'<script>const STATUS = "{status}";{STUB}</script>'
            f'<script src="{SCRIPT.as_uri()}"></script><script>{scenario}</script>'
        )
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "address.html"
            file.write_text(page, encoding="utf-8")
            done = subprocess.run(
                [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", f"--user-data-dir={folder}/profile",
                 f"--window-size={width},{height}", "--allow-file-access-from-files", "--virtual-time-budget=120000",
                 "--dump-dom", file.as_uri()],
                capture_output=True, timeout=240, check=False,
            )
        output = done.stdout.decode("utf-8", "replace")
        found = re.search(r"RESULT(.*?)END", output, re.S)
        self.assertIsNotNone(found, output[:1200])
        return json.loads(html.unescape(found.group(1)))

    # --- the list (A1 to A8) ---

    def test_a_list_has_at_most_seven_rows_of_one_line_and_the_foot_only_when_more_matches_exist(self):
        out = self.run_page(LIST_SCENARIO)
        by_name = {case["name"]: case for case in out["cases"]}

        # Seven rows at most, and the foot "More matches: keep typing." only when the server says more (B2) or when more rows
        # came than are shown.
        expected = {
            "nine, more": (7, "More matches: keep typing."),
            "seven": (7, None),
            "three": (3, None),
            "eight, no flag": (7, "More matches: keep typing."),
            "three, more": (3, "More matches: keep typing."),
        }
        for name, (rows, foot) in expected.items():
            with self.subTest(name):
                case = by_name[name]
                self.assertTrue(case["visible"])
                self.assertEqual((case["options"], case["foot"]), (rows, foot))
                self.assertEqual(case["expanded"], "true")
                # The foot sentence is part of what is announced, and only then.
                self.assertEqual("More matches" in case["announced"], foot is not None)
        # One slim head: title and note (A3); the sentence of the owner is "Matching addresses: choose one." in English.
        first = by_name["nine, more"]
        self.assertEqual((first["title"], first["note"]), ("Matching addresses:", "choose one."))
        # One line per address: street and house number in bold, then ", PLZ Town" in the muted colour (A1).
        self.assertEqual(first["texts"], ["Gantrischweg 1, 3063 Ittigen", "Gantrischweg 2, 3063 Ittigen"])
        self.assertEqual(
            first["parts"], [["address-option-main", "Gantrischweg 1"], ["address-option-sub", ", 3063 Ittigen"]],
        )
        # The row of the address is the host of the overlays.
        self.assertIn("address-host", out["hostClass"])
        self.assertIn("address-host", out["postal"]["host"])

    def test_the_list_is_an_overlay_that_moves_nothing_and_spans_the_row_without_a_scrollbar(self):
        layout = self.run_page(LIST_SCENARIO)["layout"]

        # A7: out of the flow, above the fields below, with a shadow.
        self.assertEqual(layout["position"], "absolute")
        self.assertEqual(layout["hostPosition"], "relative")
        self.assertGreaterEqual(int(layout["zIndex"]), 10)
        self.assertNotEqual(layout["boxShadow"], "none")
        self.assertAlmostEqual(layout["panel"]["top"] - layout["street"]["bottom"], 4, delta=1)
        # Opening and closing it shifts nothing: not the next field, not the button, not the height of the page.
        self.assertEqual(layout["houseTop"], layout["houseBefore"])
        self.assertEqual(layout["submitTop"], layout["submitBefore"])
        self.assertEqual(layout["houseTopClosed"], layout["houseBefore"])
        self.assertEqual(layout["pageHeight"], layout["pageHeightClosed"])
        # A8: the full width of the row of the address (street and house number column).
        self.assertAlmostEqual(layout["panel"]["left"], layout["host"]["left"], delta=1)
        self.assertAlmostEqual(layout["panel"]["width"], layout["host"]["width"], delta=1)
        self.assertGreater(layout["panel"]["width"], layout["street"]["width"])
        # A4: no scrollbar, no maximum height. A2: compact rows (one line of 16 px with 6 px above and below).
        self.assertNotIn(layout["overflowY"], ("auto", "scroll"))
        self.assertEqual(layout["maxHeight"], "none")
        self.assertFalse(layout["scrolls"])
        self.assertEqual(len(layout["optionHeights"]), 7)
        self.assertTrue(all(height < 36 for height in layout["optionHeights"]), layout["optionHeights"])

    def test_one_address_is_one_line_at_320_pixels(self):
        for body_width in (320, 360, 390):
            with self.subTest(width=body_width):
                layout = self.run_page(LIST_SCENARIO, body_width=body_width)["layout"]

                self.assertTrue(all(height < 36 for height in layout["optionHeights"]), layout["optionHeights"])
                self.assertLessEqual(layout["panel"]["right"], body_width - 16 + 1)  # inside the 16 px padding of the page
                self.assertAlmostEqual(layout["panel"]["width"], layout["host"]["width"], delta=1)
                self.assertFalse(layout["scrolls"])
                self.assertEqual(layout["houseTop"], layout["houseBefore"])

    # --- when it closes (A9, D1) ---

    def test_the_list_closes_with_the_focus_and_a_press_outside_and_a_stale_one_never_opens(self):
        out = self.run_page(CLOSE_SCENARIO)

        self.assertTrue(out["opened"])
        # The street list stays while the focus is in the house number, and goes with the focus to any other field.
        self.assertTrue(out["inHouseNumber"])
        self.assertEqual(out["inPostalCode"]["shown"], False)
        self.assertEqual(out["inPostalCode"]["expanded"], "false")
        self.assertEqual(out["inPostalCode"]["announced"], "")  # a list that closes takes its announcement with it
        # A press outside closes it; a press on the panel does not.
        self.assertTrue(out["pressInside"])
        self.assertFalse(out["pressOutside"])
        # Tab closes it and does not move the focus itself.
        self.assertEqual(out["tab"], {"shown": False, "prevented": False})
        # Shift+Tab: the keydown is not handled (a modifier), the focus leaving closes the list.
        self.assertTrue(out["shiftTabKeydown"])
        self.assertEqual(out["shiftTab"], {"shown": False, "prevented": False, "active": "before"})
        # The answer that arrives after the person moved on, or a timer that has not fired, opens nothing.
        self.assertEqual(out["stale"], {"street": False, "postal": False, "expanded": "false", "active": "id_postal_code"})
        self.assertEqual(out["timer"], {"asked": 0, "street": False})
        # Escape closes the list, keeps the text and the focus; the arrow key opens it again.
        self.assertEqual(out["escape"], {"shown": False, "typed": "gantrisch seven", "prevented": True, "active": "id_street"})
        self.assertTrue(out["reopened"])

    # --- the card (B4, E1, E2, F8, G1) ---

    def test_the_card_names_the_reason_offers_the_address_and_has_two_ways_out(self):
        out = self.run_page(CARD_SCENARIO)

        one = out["one"]
        self.assertEqual(one["kind"], "proposal")
        self.assertEqual(one["title"], "Did you mean this address?")
        self.assertEqual(one["note"], "The postal code 3061 does not fit ittigenstrasse 51.")
        self.assertEqual(one["hint"], "Click an address to use it")
        self.assertEqual(one["options"], ["Ittigenstrasse 513063 Ittigen"])
        self.assertEqual(one["keep"], "No, my address is correct")
        self.assertIn("address-notice", one["describedBy"])
        # G1: on a desktop width the card is an overlay: nothing moves when it appears or goes away.
        self.assertEqual(out["position"], "absolute")
        self.assertEqual(out["submitWithCard"], out["submitWithoutCard"])
        # Accept: all four fields are set, the card is gone, the live region says what was set, the focus is in a field.
        self.assertEqual(
            out["accepted"]["values"], {"street": "Ittigenstrasse", "house": "51", "postal": "3063", "town": "Ittigen"},
        )
        self.assertTrue(out["accepted"]["empty"])
        self.assertEqual(out["accepted"]["announced"], "Address filled in: Ittigenstrasse 51, 3063 Ittigen.")
        self.assertIn(out["accepted"]["active"], ("id_street", "id_house_number", "id_postal_code", "id_town"))
        self.assertNotIn("address-notice", out["accepted"]["describedBy"] or "")
        self.assertTrue(out["afterAccept"]["empty"])

    def test_no_my_address_is_correct_keeps_the_input_and_the_card_does_not_return_for_these_values(self):
        out = self.run_page(CARD_SCENARIO)

        self.assertEqual(out["again"], "proposal")
        self.assertEqual(
            out["kept"]["values"], {"street": "ittigenstrasse", "house": "51", "postal": "3061", "town": "Ittigen"},
        )
        self.assertTrue(out["kept"]["empty"])
        self.assertEqual(out["kept"]["announced"], "Address kept as entered.")
        self.assertIn(out["kept"]["active"], ("id_street", "id_house_number", "id_postal_code", "id_town"))
        # The check ran again for the same values (the server was asked) and the card stayed away.
        self.assertEqual(out["notReturning"], {"empty": True, "asked": True})
        # Other values, a card again; back to the dismissed values, no card.
        self.assertEqual(out["otherValues"], "proposal")
        self.assertIsNone(out["dismissedStillKnown"])

    def test_escape_closes_the_card_without_recording_and_a_list_is_closed_before_the_card(self):
        out = self.run_page(CARD_SCENARIO)

        self.assertEqual(out["beforeEscape"], "proposal")
        self.assertEqual(out["escaped"]["empty"], True)
        # Not recorded: the same values show the card again.
        self.assertEqual(out["afterEscape"], "proposal")
        order = out["escapeOrder"]
        self.assertEqual((order["cardBefore"], order["listOpen"]), ("proposal", True))
        self.assertEqual(order["afterFirst"], {"list": False, "card": "proposal"})
        self.assertEqual(order["afterSecond"], {"list": False, "card": None})

    def test_several_proposals_get_the_plural_title_and_at_most_three_options(self):
        out = self.run_page(CARD_SCENARIO)

        self.assertEqual(out["many"]["title"], "Did you mean one of these addresses?")
        self.assertEqual(out["many"]["options"], ["Ittigenstrasse 513063 Ittigen", "Ittigenstrasse 153063 Ittigen"])
        self.assertEqual(out["cap"], 3)

    def test_without_a_fix_the_notice_has_a_link_to_use_the_address_as_entered(self):
        out = self.run_page(FALLBACK_SCENARIO)

        warning = out["warning"]
        self.assertEqual(warning["alert"], "This address was not found.")
        self.assertEqual(warning["link"], "My address is correct, use it as entered")
        self.assertFalse(warning["card"])
        self.assertIn("address-notice", warning["describedBy"])
        used = out["used"]
        self.assertTrue(used["empty"])
        self.assertEqual(used["announced"], "Address kept as entered.")
        self.assertEqual(used["values"], {"street": "nirgendwoweg", "house": "5", "postal": "3063", "town": "Ittigen"})
        self.assertNotIn("address-notice", used["describedBy"] or "")
        self.assertEqual(out["notReturning"], {"empty": True, "asked": True})
        self.assertTrue(out["otherValues"])
        # No complete postal code, no warning: a part of an address is not judged (F7).
        self.assertTrue(out["partial"]["empty"])

    def test_a_press_on_the_save_button_does_not_move_it_with_the_card_open_on_a_desktop_width(self):
        out = self.run_page(PRESS_SCENARIO, width=1000)

        self.assertEqual(out["position"], "absolute")
        self.assertEqual(out["after"], out["before"])

    def test_a_press_on_the_save_button_does_not_move_it_with_the_card_in_the_page_on_a_phone_width(self):
        # Below 761 px the card is in the page flow: an outside press must not close it (the button would move between the
        # press and the release and the click would be lost). G1: the form can be sent at any time.
        for body_width in (360, 390, None):
            with self.subTest(width=body_width):
                out = self.run_page(PRESS_SCENARIO, width=500, body_width=body_width)

                self.assertEqual(out["position"], "static")
                self.assertEqual(out["after"], out["before"])
                self.assertFalse(out["closed"])

    def test_an_outside_press_closes_the_overlay_card_on_a_desktop_width_and_shifts_nothing(self):
        out = self.run_page(PRESS_SCENARIO, width=1000)

        self.assertEqual(out["position"], "absolute")
        self.assertTrue(out["closed"])
        self.assertEqual(out["after"], out["before"])

    def test_the_card_in_the_page_still_closes_with_escape_on_a_phone_width(self):
        out = self.run_page(PRESS_SCENARIO, width=500)

        self.assertTrue(out["escapeClosed"])

    def test_a_card_drawn_by_the_server_survives_a_press_on_a_phone_width_and_does_not_shift_the_button(self):
        out = self.run_page(SERVER_CARD_SCENARIO, width=500, server_card=True)

        self.assertEqual(out["position"], "static")
        self.assertTrue(out["present"])
        self.assertEqual(out["after"], out["before"])
        self.assertTrue(out["stillThere"])
        # And on a desktop width the same card is an overlay that closes with the press, nothing moves.
        desktop = self.run_page(SERVER_CARD_SCENARIO, width=1000, server_card=True)
        self.assertEqual(desktop["position"], "absolute")
        self.assertEqual(desktop["after"], desktop["before"])

    # --- the check (F7) ---

    def test_a_busy_check_is_asked_once_more_silently_and_no_more(self):
        out = self.run_page(RETRY_SCENARIO)

        self.assertEqual(out["afterFirst"], {"checks": 1, "empty": True})
        self.assertEqual(out["afterRetry"], {"checks": 2, "empty": True})
        self.assertEqual(out["always"], {"checks": 2, "empty": True})

    def test_the_check_runs_only_for_a_street_a_house_number_and_a_complete_postal_code_or_a_town(self):
        out = self.run_page(MINIMUM_SCENARIO)

        self.assertEqual(
            dict(out["cases"]),
            {
                "street of two characters": 0,
                "no house number": 0,
                "the minimum": 1,
                "partial postal code": 0,
                "town without postal code": 1,
                "town of two characters": 0,
                "postal code without town": 1,
                "nothing but a street": 0,
                "house number of spaces": 0,
            },
        )

    def test_a_foreign_country_asks_nothing_and_shows_nothing(self):
        out = self.run_page(COUNTRY_SCENARIO, country_field=True)

        self.assertEqual(out["foreign"], {"calls": 0, "notice": 0, "panel": False})
        self.assertGreater(out["swiss"]["checks"], 0)
        self.assertEqual(out["swiss"]["sent"], "CH")

    # --- scrolling into view ---

    def test_a_list_or_card_below_the_visible_part_is_scrolled_into_view_once(self):
        out = self.run_page(REVEAL_SCENARIO, height=500, top_space=900, spacer=1600)

        self.assertTrue(out["streetVisible"])
        self.assertTrue(out["list"]["shown"])
        self.assertTrue(out["list"]["scrolled"], out)
        self.assertLessEqual(out["list"]["bottom"], out["list"]["height"] + 1)
        self.assertEqual(out["card"]["kind"], "proposal")
        self.assertTrue(out["card"]["scrolled"], out)
        self.assertLessEqual(out["card"]["bottom"], out["card"]["height"] + 1)
        self.assertGreaterEqual(out["card"]["top"], -1)
