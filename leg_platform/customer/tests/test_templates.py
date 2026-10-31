"""The templates, the stylesheet and the script of the customer's pages (docs/DESIGN.md; docs/SECURITY.md, section 6): no
inline script or style, no handler attribute, no dynamic code in the script, only the tokens of the design for colours, only
sizes of the scale, no shadow or gradient (but the focus ring of a field). Read as text, like the other style tests."""

import re
from pathlib import Path

from django.test import SimpleTestCase

PACKAGE = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((PACKAGE / "templates" / "customer").glob("*.html"))
CSS = (PACKAGE / "static" / "customer" / "css" / "customer.css").read_text(encoding="utf-8")
SCRIPT = (PACKAGE / "static" / "customer" / "js" / "customer.js").read_text(encoding="utf-8")
FRAME_CSS = (PACKAGE.parent / "static" / "css" / "frame.css").read_text(encoding="utf-8")
ALLOWED_SIZES = {11, 12, 13, 14, 16, 18, 20, 22, 24, 26, 32, 40}


class TemplateTests(SimpleTestCase):
    def test_there_are_templates(self):
        self.assertGreaterEqual(len(TEMPLATES), 20)

    def test_no_template_has_an_inline_script_style_or_handler(self):
        for path in TEMPLATES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(template=path.name):
                self.assertNotRegex(source, r"(?i)<script(?![^>]*\ssrc=)")
                self.assertNotRegex(source, r"(?i)<style")
                self.assertNotRegex(source, r'(?i)\sstyle\s*=')
                self.assertNotRegex(source, r"(?i)\son[a-z]+\s*=")
                self.assertNotRegex(source, r"(?i)javascript:")
                self.assertNotIn("|safe", source)
                self.assertNotIn("autoescape off", source)

    def test_every_post_form_has_the_csrf_token(self):
        for path in TEMPLATES:
            source = path.read_text(encoding="utf-8")
            forms = len(re.findall(r'<form[^>]*method="post"', source))
            with self.subTest(template=path.name):
                self.assertEqual(source.count("{% csrf_token %}"), forms)

    def test_no_template_has_an_external_address(self):
        for path in TEMPLATES:
            with self.subTest(template=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r'(?i)(src|href|action)="https?://')

    def test_the_forms_are_not_sent_to_another_address(self):
        for path in TEMPLATES:
            for action in re.findall(r'<form[^>]*action="([^"]+)"', path.read_text(encoding="utf-8")):
                with self.subTest(template=path.name, action=action):
                    self.assertTrue(action.startswith("{% url "), action)


class StyleTests(SimpleTestCase):
    def test_colours_come_from_the_tokens_only(self):
        without_comments = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        self.assertNotRegex(without_comments, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(without_comments, r"\brgba?\(")

    def test_sizes_are_the_scale_of_the_design(self):
        sizes = {int(size) for size in re.findall(r"font-size: (\d+)px", CSS)}
        self.assertTrue(sizes <= ALLOWED_SIZES, sizes - ALLOWED_SIZES)
        sizes = {int(size) for size in re.findall(r"font-size: (\d+)px", FRAME_CSS)}
        self.assertTrue(sizes <= ALLOWED_SIZES, sizes - ALLOWED_SIZES)

    def test_no_gradient_and_the_only_shadow_is_the_focus_ring_of_a_field(self):
        self.assertNotIn("gradient(", CSS)
        shadows = re.findall(r"([^{}]+)\{[^}]*box-shadow:\s*([^;}]*)", CSS)
        self.assertTrue(all("focus" in selector or value.strip() == "none" for selector, value in shadows), shadows)

    def test_spacing_stays_on_the_scale(self):
        allowed = {0, 4, 8, 12, 16, 24, 32, 48}
        for prop in ("gap", "padding", "margin"):
            for declaration in re.findall(rf"\b{prop}(?:-[a-z]+)?: ([^;]+);", CSS):
                for value in re.findall(r"(?<![\w.#-])(\d+)px", declaration):
                    with self.subTest(declaration=declaration):
                        self.assertIn(int(value), allowed | {2, 6})

    def test_buttons_and_controls_are_big_enough_on_touch(self):
        self.assertRegex(CSS, r"\.step-actions \.btn \{[^}]*min-height: 48px")
        self.assertRegex(CSS, r"\.icon-btn \{[^}]*width: 44px;[^}]*height: 44px")
        self.assertRegex(CSS, r"\.btn-small \{[^}]*min-height: 44px")
        self.assertRegex(FRAME_CSS, r"\.frame-sub-item \{[^}]*min-height: 44px")

    def test_the_part_that_a_script_hides_is_hidden_only_when_the_script_runs(self):
        # Without the script every part of the form is visible (progressive enhancement).
        self.assertRegex(CSS, r"\.js \.reveal:not\(\.is-open\) \{\s*display: none;")
        self.assertNotRegex(CSS, r"(?m)^\.reveal:not")

    def test_a_phone_gets_one_column_and_full_width_buttons(self):
        phone = CSS[CSS.index("@media (max-width: 760px)") :]
        self.assertRegex(phone, r"\.head-actions \{[^}]*flex-direction: column")
        self.assertRegex(phone, r"\.facts \{[^}]*grid-template-columns: minmax\(0, 1fr\)")
        self.assertIn("width: 100%", phone)


class ScriptTests(SimpleTestCase):
    def test_the_forms_that_carry_data_submit_once_get_the_script_from_the_base(self):
        base = (PACKAGE / "templates" / "customer" / "base.html").read_text(encoding="utf-8")
        self.assertIn("{% static 'js/submit-once.js' %}", base)
        self.assertIn("<script defer src=", base)
        users = [path.name for path in TEMPLATES if "data-submit-once" in path.read_text(encoding="utf-8")]
        self.assertGreaterEqual(len(users), 8)
        for name in users:
            source = (PACKAGE / "templates" / "customer" / name).read_text(encoding="utf-8")
            self.assertIn('{% extends "customer/', source, name)

    def test_the_shared_script_exists_is_found_and_uses_no_network_storage_or_dynamic_code(self):
        from django.contrib.staticfiles import finders

        path = finders.find("js/submit-once.js")
        self.assertIsNotNone(path)
        source = Path(path).read_text(encoding="utf-8")
        for forbidden in ("eval(", "new Function", "innerHTML", "fetch(", "XMLHttpRequest", "localStorage",
                          "sessionStorage", "document.cookie", "document.write"):
            self.assertNotIn(forbidden, source)
        self.assertIn("form[data-submit-once]", source)

    def test_the_script_uses_no_network_storage_or_dynamic_code(self):
        for forbidden in (
            "eval(", "new Function", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "fetch(",
            "XMLHttpRequest", "localStorage", "sessionStorage", "document.cookie", "console.", "setTimeout(\"",
            "location.href", "window.open",
        ):
            self.assertNotIn(forbidden, SCRIPT)
        self.assertIn('"use strict"', SCRIPT)

    def test_the_script_only_helps_it_has_the_four_enhancements_and_nothing_else(self):
        for hook in ("data-reveal-name", ".iban-input"):
            self.assertIn(hook, SCRIPT)
