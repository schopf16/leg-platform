"""Static checks of the onboarding pages of the management area (D-103): templates, symbols and the stylesheet.

The scripts of the pages are external files and the stylesheet uses only the colour tokens (docs/DESIGN.md, sections 3 and
9; docs/SECURITY.md, section 6: no inline script or style). The new pages need no script of their own: everything works
as links, forms and a native progress element.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE = Path(settings.BASE_DIR) / "leg_platform"
MANAGEMENT = BASE / "management"
TEMPLATES = MANAGEMENT / "templates" / "management"
CSS = MANAGEMENT / "static" / "management" / "css" / "management.css"
NEW_TEMPLATES = sorted(
    [
        *TEMPLATES.glob("processes/*.html"), *TEMPLATES.glob("contracts/*.html"),
        TEMPLATES / "_open.html", TEMPLATES / "_deadlines.html", TEMPLATES / "_run_time.html",
        TEMPLATES / "overview.html",
    ]
)
# The page marker classes of the body (they carry no rule of their own).
MARKERS = {
    "mgmt-processes", "mgmt-process", "mgmt-contracts", "mgmt-deadlines",
    # Hooks that older pages use the same way (a script or the page scope; no rule of their own).
    "mgmt-detail-header", "mgmt-list", "mgmt-overview",
}
FIRST_NEW_RULE = "/* ---- Processes, contracts, deadlines"


class OnboardingTemplateTests(SimpleTestCase):
    def read(self, path):
        return path.read_text(encoding="utf-8")

    def test_the_templates_exist(self):
        names = {path.name for path in NEW_TEMPLATES}
        for name in ("list.html", "application.html", "deregistration.html", "notices.html", "_open.html",
                     "_deadlines.html", "overview.html", "_actions.html", "_person_card.html", "_history.html",
                     "_notices.html"):
            self.assertIn(name, names)

    def test_no_inline_script_style_or_event_handler(self):
        for path in NEW_TEMPLATES:
            text = self.read(path)
            with self.subTest(template=path.name):
                self.assertNotRegex(text, r"<script(?![^>]*\bsrc=)")
                self.assertNotRegex(text, r"\sstyle\s*=")
                self.assertNotRegex(text, r"\son[a-z]+\s*=")
                self.assertNotIn("javascript:", text.lower())
                self.assertNotRegex(text, r"\|\s*safe\b|mark_safe|\{%\s*autoescape\s+off")

    def test_every_text_is_translatable(self):
        for path in NEW_TEMPLATES:
            text = self.read(path)
            with self.subTest(template=path.name):
                if "{% translate" in text or "{% blocktranslate" in text:
                    self.assertIn("{% load i18n", text)
                # No visible English or German word outside the tags: every element text is a tag or a variable.
                body = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", text, flags=re.S)
                body = re.sub(r"\{%\s*block inner_class\s*%\}.*?\{%\s*endblock\s*%\}", "", body, flags=re.S)
                body = re.sub(r"\{%\s*blocktranslate\b.*?%\}.*?\{%\s*endblocktranslate\s*%\}", "", body, flags=re.S)
                body = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", "", body, flags=re.S)
                for found in re.findall(r">([^<>]*[A-Za-z]{3,}[^<>]*)<", body):
                    self.assertEqual(found.strip(), "", f"untranslated text {found!r}")

    def test_every_class_of_the_management_stylesheet_that_the_templates_use_is_defined(self):
        css = self.read(CSS)
        defined = set(re.findall(r"\.(mgmt-[a-z0-9-]+)", css))
        used = set()
        for path in NEW_TEMPLATES:
            for value in re.findall(r'class="([^"]*)"', self.read(path)):
                used |= set(value.split())
        missing = sorted(name for name in used if name.startswith("mgmt-") and name not in defined | MARKERS)
        self.assertEqual(missing, [])

    def test_every_symbol_that_a_template_uses_is_in_a_sprite(self):
        sprites = self.read(TEMPLATES / "_icons.html") + self.read(BASE / "templates" / "_frame_icons.html")
        ids = set(re.findall(r'<symbol id="([^"]+)"', sprites))
        for path in NEW_TEMPLATES:
            for name in re.findall(r'<use href="#([a-z0-9-]+)"', self.read(path)):
                with self.subTest(template=path.name, symbol=name):
                    self.assertIn(name, ids)
        for name in ("m-download", "m-set-current", "m-trash", "i-processes"):
            self.assertIn(name, ids)

    def test_the_icon_buttons_have_a_name_and_a_text_label_for_hover_and_focus(self):
        text = self.read(TEMPLATES / "contracts" / "_actions.html")
        tags = re.findall(r"<(?:a|button)\b[^>]*class=\"mgmt-action[^>]*>", text)
        self.assertEqual(len(tags), 3)
        for tag in tags:
            self.assertIn("aria-label=", tag)
        # The symbol and the short label are decoration for the screen reader (the name says it all, with the file).
        self.assertEqual(text.count('aria-hidden="true"'), 6)
        self.assertEqual(text.count('class="mgmt-action-text"'), 3)
        self.assertGreaterEqual(len(re.findall(r'class="mgmt-action is-', text)), 3)
        # Label in name (WCAG 2.5.3): every visible label is the start of the accessible name.
        for visible, name in (
            ("Download", "Download {{ name }}"), ("Set as current", "Set as current: {{ name }}"),
            ("Delete", "Delete: {{ name }}"),
        ):
            self.assertIn(f'{{% translate "{visible}" %}}', text)
            self.assertIn(f"%}}{name}{{% endblocktranslate", text)

    def test_the_progress_is_a_native_element_with_a_name(self):
        for name in ("list.html", "application.html", "deregistration.html"):
            text = self.read(next(path for path in NEW_TEMPLATES if path.name == name))
            for element in re.findall(r"<progress\b[^>]*>", text):
                self.assertIn("aria-label=", element)
                self.assertIn("value=", element)
                self.assertIn("max=", element)

    def test_every_table_has_a_card_view_for_the_phone(self):
        """The table is hidden on a phone (LISTEN-STANDARD, section 7): each page that draws one has the same
        content as cards, or the phone would show an empty card."""
        for path in NEW_TEMPLATES:
            text = self.read(path)
            if "mgmt-table-view" in text:
                with self.subTest(template=path.name):
                    self.assertGreaterEqual(text.count("mgmt-cards-view"), text.count("mgmt-table-view"))

    def test_the_new_rules_use_the_colour_tokens_only(self):
        css = self.read(CSS)
        new = css[css.index(FIRST_NEW_RULE):]
        self.assertNotRegex(new, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(new, r"\b(?:rgb|rgba|hsl)\(")
        self.assertNotRegex(new, r"!important")
        self.assertIn("var(--accent)", new)
        # The touch targets: 44 px for the action buttons (WCAG 2.2, 2.5.8), on a desktop (W5-09) and on a phone, where
        # the button grows to hold the text label.
        self.assertRegex(new, r"\.mgmt-action \{[^}]*width: 44px;[^}]*height: 44px;")
        self.assertRegex(new, r"@media \(max-width: 760px\) \{[^@]*\.mgmt-action \{[^}]*min-width: 44px;")
        self.assertNotRegex(new, r"\.mgmt-action \{[^}]*width: 40px;")

    def test_the_new_pages_load_no_new_script(self):
        for path in NEW_TEMPLATES:
            for source in re.findall(r"<script[^>]*src=\"([^\"]+)\"", self.read(path)):
                with self.subTest(template=path.name, script=source):
                    self.assertTrue(source.startswith("{% static 'management/js/"), source)
                    name = re.search(r"js/([a-z-]+\.js)", source).group(1)
                    self.assertTrue((MANAGEMENT / "static" / "management" / "js" / name).exists(), name)
