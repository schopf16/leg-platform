"""The texts of the customer's pages in Swiss German (AGENTS.md, section 2): every text that the code and the templates of
the customer's pages mark for translation has a Swiss German text of its own, none contains the letter that Swiss German
does not use, the placeholders are the same as in the source, and the words of the owner's documents are used as they are.
The sources are read as text (the templates by their translation tags, the code by its syntax tree), so a text that is added
later without a translation fails here."""

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase
from django.utils import translation

PACKAGE = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((PACKAGE / "templates" / "customer").glob("*.html"))
SOURCES = [
    PACKAGE / name
    for name in (
        "forms.py", "texts.py", "journey.py", "overview.py", "wizard.py", "views.py", "apps.py", "deregistration.py",
        "returns.py", "steps.py", "banners.py", "application_edit.py", "card_edit.py",
    )
] + [PACKAGE.parent / "accounts" / "tasks.py", PACKAGE / "templatetags" / "customer_ui.py"]
MENU = [PACKAGE.parent / "templates" / "_frame_menu.html", PACKAGE.parent / "templates" / "base.html"]
FUNCTIONS = {"_", "_lazy", "gettext", "gettext_lazy"}
# Where the German text really is the same as the source.
SAME = {"Information", "Wallbox in kW", "IBAN", "EGID", "optional", "Optional", "Name", "Start", "Energie", "System", "Details", "LEG", "Wallbox", "kW", "kWh", "kWp"}
TRANSLATE = re.compile(r'\{%\s*translate\s+"((?:[^"\\]|\\.)*)"(?:\s+context\s+"([^"]*)")?\s*%\}')
BLOCK = re.compile(r"\{%\s*blocktranslate(?P<head>[^%]*)%\}(?P<body>.*?)\{%\s*endblocktranslate\s*%\}", re.S)
VARIABLE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def template_texts():
    found = set()
    for path in TEMPLATES + MENU + [PACKAGE.parent / "accounts" / "templates" / "accounts" / "home.html"]:
        source = path.read_text(encoding="utf-8")
        for msgid, context in TRANSLATE.findall(source):
            found.add((context or None, msgid))
        for msgid in re.findall(r'[\s:=(]_\("((?:[^"\\]|\\.)*)"\)', source):
            found.add((None, msgid))
        for match in BLOCK.finditer(source):
            if "plural" in match.group("body"):
                continue
            body = " ".join(match.group("body").split())
            found.add((None, VARIABLE.sub(lambda m: f"%({m.group(1)})s", body)))
    return found


def code_texts():
    found = set()
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
            ):
                found.add((None, node.args[0].value))
    return found


class CustomerTextsTests(SimpleTestCase):
    def translated(self, context, msgid):
        with translation.override("de-ch"):
            if context:
                return translation.pgettext(context, msgid)
            return translation.gettext(msgid)

    def test_there_are_texts_to_check(self):
        self.assertGreater(len(template_texts()), 100)
        self.assertGreater(len(code_texts()), 80)

    def test_every_text_has_a_swiss_german_text_of_its_own(self):
        missing = []
        for context, msgid in sorted(template_texts() | code_texts(), key=lambda item: (item[0] or "", item[1])):
            german = self.translated(context, msgid)
            if german == msgid and msgid not in SAME:
                missing.append(msgid)
        self.assertEqual(missing, [], "Texts without a Swiss German translation")

    def test_no_translation_has_the_letter_swiss_german_does_not_use(self):
        for context, msgid in template_texts() | code_texts():
            with self.subTest(msgid=msgid):
                self.assertNotIn("\u00df", self.translated(context, msgid))

    def test_the_placeholders_are_the_same_in_the_translation(self):
        for context, msgid in template_texts() | code_texts():
            german = self.translated(context, msgid)
            with self.subTest(msgid=msgid):
                self.assertEqual(sorted(re.findall(r"%\(\w+\)s", msgid)), sorted(re.findall(r"%\(\w+\)s", german)))

    def test_the_plural_of_the_open_steps_has_both_forms(self):
        with translation.override("de-ch"):
            self.assertEqual(translation.ngettext("%(remaining)s step still open", "%(remaining)s steps still open", 1) % {"remaining": 1}, "Noch 1 Schritt offen")
            self.assertEqual(translation.ngettext("%(remaining)s step still open", "%(remaining)s steps still open", 4) % {"remaining": 4}, "Noch 4 Schritte offen")

    def test_the_words_of_the_owner_are_used_as_they_are(self):
        owner = {
            "Start application": "Anmeldung starten",
            "Start deregistration": "Abmeldung starten",
            "Add metering point": "Messpunkt hinzufügen",
            "Deregister metering points": "Messpunkte abmelden",
            "What do we still need to know?": "Was müssen wir noch wissen?",
            "Where is your building?": "Wo liegt Ihr Gebäude?",
            "Which metering points belong to your building?": "Welche Messpunkte gehören zu Ihrem Gebäude?",
            "Where should we transfer your compensation?": "Wohin sollen wir Ihre Vergütung überweisen?",
            "Read the contract and agree": "Vertrag lesen und zustimmen",
            "When should your participation start?": "Ab wann soll Ihre Teilnahme beginnen?",
            "Your application was submitted": "Ihre Anmeldung ist abgeschickt",
            "Your way into the LEG": "Ihr Weg in die LEG",
            "Cancel application?": "Anmeldung abbrechen?",
            "Continue application": "Weiter anmelden",
            "Cancel application": "Anmeldung abbrechen",
            "Withdraw application": "Anmeldung zurückziehen",
            "Already on file": "Bereits hinterlegt",
            "No building registered yet": "Noch kein Gebäude angemeldet",
            "Building found": "Gebäude gefunden",
            "Not found clearly": "Nicht eindeutig gefunden",
            "Participating": "Teilnehmend",
            "Deregistration running": "In Abmeldung",
            "Registration running": "In Anmeldung",
            "Participation": "Teilnahme",
            "Questions and answers": "Frage und Antwort",
            "Invoice": "Rechnung",
            "Message": "Mitteilung",
            "Report feed-in later": "Einspeisung nachmelden",
            "Register building": "Gebäude anmelden",
            "Submit application": "Anmeldung abschicken",
            "I have read the current version of %(date)s and agree to it.":
                "Ich habe die aktuelle Fassung vom %(date)s gelesen und stimme ihr zu.",
        }
        for msgid, german in owner.items():
            with self.subTest(msgid=msgid):
                self.assertEqual(self.translated(None, msgid), german)

    def test_the_catalogue_is_not_used_for_the_other_languages_here(self):
        # The French and Italian catalogues are the owner's; an English text is shown where there is no translation.
        with translation.override("en"):
            self.assertEqual(translation.gettext("Start application"), "Start application")
