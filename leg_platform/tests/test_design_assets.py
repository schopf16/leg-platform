"""The design assets of the usability batch (D-054): alert colours, check marks, QR tile and the
rules of the Content Security Policy for style sheets and scripts. No database is needed."""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from leg_platform.branding import contrast

STATIC = Path(settings.BASE_DIR) / "leg_platform" / "static"
CSS = (STATIC / "css" / "portal.css").read_text(encoding="utf-8")
LIGHT_DARK = r"light-dark\(\s*(#[0-9a-fA-F]{6})\s*,\s*(#[0-9a-fA-F]{6})\s*\)"


def variable(name):
    """Return (light, dark) of a custom property that is defined with light-dark()."""
    match = re.search(rf"{re.escape(name)}:\s*{LIGHT_DARK}", CSS)
    assert match, f"{name} is not defined with two hex colours"
    return match.groups()


def mix(foreground, background, share):
    """The sRGB mix that CSS color-mix(in srgb, ...) gives (share of the foreground)."""
    parts = (
        round(int(foreground[i : i + 2], 16) * share + int(background[i : i + 2], 16) * (1 - share))
        for i in (1, 3, 5)
    )
    return "#%02X%02X%02X" % tuple(parts)


class AlertColourTests(SimpleTestCase):
    """WCAG 2.2: 4.5:1 for text (1.4.3), 3:1 for the icon and the border (1.4.11)."""

    TEXT = ("#1C1917", "#F5F5F4")  # the text token of the register, docs/DESIGN.md, section 3.1
    DEFAULT_ACCENT = ("#2F6B4F", "#74C59A")

    def pairs(self):
        for index, mode in enumerate(("light", "dark")):
            for name in ("success", "warning", "danger"):
                tint = variable(f"--portal-alert-{name}-bg")[index]
                accent = variable(f"--portal-alert-{name}-accent")[index]
                # The danger alert keeps the error colour as text colour; the others use the text token.
                text = accent if name == "danger" else self.TEXT[index]
                yield mode, name, tint, accent, text
            share, base = (0.12, "#FFFFFF") if index == 0 else (0.14, "#1C1917")
            accent = self.DEFAULT_ACCENT[index]
            yield mode, "info", mix(accent, base, share), accent, self.TEXT[index]

    def test_the_text_of_every_alert_reaches_4_5_to_1(self):
        for mode, name, tint, _accent, text in self.pairs():
            with self.subTest(mode=mode, alert=name):
                self.assertGreaterEqual(contrast(text, tint), 4.5)

    def test_the_icon_and_the_border_reach_3_to_1_on_the_tint_and_on_the_surface(self):
        for mode, name, tint, accent, _text in self.pairs():
            surface = "#FFFFFF" if mode == "light" else "#1C1917"
            with self.subTest(mode=mode, alert=name):
                self.assertGreaterEqual(contrast(accent, tint), 3)
                self.assertGreaterEqual(contrast(accent, surface), 3)

    def test_success_and_warning_use_the_energy_colours_of_the_design_language(self):
        # docs/DESIGN.md, section 3.4: "used within the LEG" and "production".
        self.assertEqual(
            tuple(c.lower() for c in variable("--portal-alert-success-accent")), ("#2f8f5b", "#5dbb86")
        )
        self.assertEqual(
            tuple(c.lower() for c in variable("--portal-alert-warning-accent")), ("#b7791f", "#e8b34a")
        )


class StyleSheetTests(SimpleTestCase):
    def test_the_own_stylesheet_loads_no_data_image_and_only_existing_local_files(self):
        self.assertNotRegex(CSS, r"url\(\s*[\"']?data:")
        for reference in re.findall(r'url\("([^"]+)"\)', CSS):
            if reference.startswith("../"):
                with self.subTest(reference=reference):
                    self.assertTrue((STATIC / "css" / reference).resolve().is_file())

    def test_the_icons_of_the_alerts_and_the_check_marks_exist_and_are_plain_svg(self):
        for reference in (
            "vendor/tabler-icons/circle-check.svg",
            "vendor/tabler-icons/alert-triangle.svg",
            "vendor/tabler-icons/info-circle.svg",
            "vendor/tabler-icons/alert-circle.svg",
            "img/check.svg",
            "img/radio-dot.svg",
        ):
            with self.subTest(reference=reference):
                text = (STATIC / reference).read_text(encoding="utf-8")
                self.assertIn("<svg", text)
                self.assertIn(reference.rsplit("/", 1)[-1], CSS)
                for forbidden in ("<script", "javascript:", "onload", "href=", "<image", "<foreignObject"):
                    self.assertNotIn(forbidden, text)

    def test_the_check_marks_follow_the_text_colour_of_the_accent(self):
        self.assertIn("--tblr-form-check-icon-color: var(--tblr-primary-fg)", CSS)
        self.assertIn('.form-check-input[type="checkbox"]::before', CSS)
        self.assertIn('.form-check-input[type="radio"]::before', CSS)

    def test_the_qr_code_has_a_white_tile_in_every_colour_mode(self):
        tile = re.search(r"\.qr-tile \{(.*?)\}", CSS, re.S).group(1)
        self.assertIn("background: #ffffff", tile)
        self.assertNotIn("light-dark", tile)
        self.assertRegex(tile, r"padding: 1\.5rem")

    def test_members_get_16_pixel_text_and_a_larger_title(self):
        for selector, size in (
            (".register-clear .form-label", "1rem"),
            (".register-clear .form-control", "1rem"),
            (".register-clear .card h1", "1.75rem"),
        ):
            with self.subTest(selector=selector):
                block = re.search(re.escape(selector) + r"[^{]*\{([^}]*)\}", CSS).group(1)
                self.assertIn(f"font-size: {size}", block)
        # Hints and error texts may be 15 px, not smaller.
        block = re.search(r"\.register-clear \.form-hint,\s*\.register-clear \.invalid-feedback \{([^}]*)\}", CSS)
        self.assertIn("font-size: 0.9375rem", block.group(1))


class ScriptTests(SimpleTestCase):
    SCRIPTS = ("password-toggle.js", "recovery-codes.js", "postal-code-input.js")

    def test_the_scripts_exist_and_use_no_dynamic_code_or_network(self):
        for name in self.SCRIPTS:
            with self.subTest(script=name):
                text = (STATIC / "js" / name).read_text(encoding="utf-8")
                for forbidden in ("eval(", "new Function", "innerHTML", "document.write", "fetch(",
                                  "XMLHttpRequest", "localStorage", "sessionStorage", "console."):
                    self.assertNotIn(forbidden, text)
                self.assertIn('"use strict"', text)

    def test_the_scripts_do_not_log_or_send_the_codes(self):
        text = (STATIC / "js" / "recovery-codes.js").read_text(encoding="utf-8")
        for forbidden in ("fetch", "sendBeacon", "XMLHttpRequest", "WebSocket", "console", "location"):
            self.assertNotIn(forbidden, text)
