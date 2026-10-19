"""The design assets of the usability batch (D-054): alert colours, check marks, QR tile and the
rules of the Content Security Policy for style sheets and scripts. No database is needed."""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from leg_platform.branding import contrast

STATIC = Path(settings.BASE_DIR) / "leg_platform" / "static"
CSS = (STATIC / "css" / "portal.css").read_text(encoding="utf-8")
HEX_TOKEN = re.compile(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6});")


def tokens():
    """Return (light, dark) as dicts of the colour tokens of portal.css (D-056).

    The light block is the default; the dark block replaces the tokens it names.
    """
    light = re.search(r":root \{\s*color-scheme: light;(.*?)\n\}", CSS, re.S).group(1)
    dark = re.search(r':root\[data-theme="dark"\] \{\s*color-scheme: dark;(.*?)\n\}', CSS, re.S).group(1)
    light_tokens = {name: value.upper() for name, value in HEX_TOKEN.findall(light)}
    dark_tokens = {**light_tokens, **{name: value.upper() for name, value in HEX_TOKEN.findall(dark)}}
    return light_tokens, dark_tokens


def mix(foreground, background, share):
    """The sRGB mix that CSS color-mix(in srgb, ...) gives (share of the foreground)."""
    parts = (
        round(int(foreground[i : i + 2], 16) * share + int(background[i : i + 2], 16) * (1 - share))
        for i in (1, 3, 5)
    )
    return "#%02X%02X%02X" % tuple(parts)


class TokenTests(SimpleTestCase):
    """The colour tokens of LEG-Design-Vorgaben/login/VORGABEN.md, section 6."""

    def test_the_tokens_are_the_ones_of_the_design_package(self):
        light, dark = tokens()
        expected = {
            "bg": ("#F6F7F8", "#111315"),
            "panel": ("#FFFFFF", "#191C1F"),
            "fg": ("#1B1D20", "#E6E9EC"),
            "muted": ("#5A6068", "#9AA2AB"),
            "line": ("#DEE2E7", "#2A2F35"),
            "accent": ("#005BA2", "#6AA9E0"),
            "on-accent": ("#FFFFFF", "#0B1520"),
            "yellow": ("#DDDD22", "#DDDD22"),
            "bad": ("#B42318", "#F0948A"),
            "ok": ("#2F8F5B", "#5DBB86"),
        }
        for name, (light_value, dark_value) in expected.items():
            with self.subTest(token=name):
                self.assertEqual(light[name], light_value)
                self.assertEqual(dark[name], dark_value)

    def test_the_dark_mode_follows_only_the_button_never_the_device_setting(self):
        # R13 of the design language (login/VORGABEN.md, 5.2): no automatic mode.
        self.assertNotIn("@media (prefers-color-scheme", CSS)
        self.assertNotIn("light-dark(", CSS)
        self.assertNotIn("color-scheme: light dark", CSS)
        for script in ("theme-init.js", "corner-controls.js"):
            text = (STATIC / "js" / script).read_text(encoding="utf-8")
            self.assertNotIn("prefers-color-scheme", text)
            self.assertNotIn("matchMedia", text)

    def test_text_reaches_4_5_to_1_and_graphics_3_to_1(self):
        for index, mode in enumerate(("light", "dark")):
            palette = tokens()[index]
            for surface in ("bg", "panel"):
                for text in ("fg", "muted", "accent", "bad", "warn"):
                    with self.subTest(mode=mode, text=text, surface=surface):
                        self.assertGreaterEqual(contrast(palette[text], palette[surface]), 4.5)
                # Borders of fields and buttons are the muted colour (R14).
                self.assertGreaterEqual(contrast(palette["muted"], palette[surface]), 3)
            self.assertGreaterEqual(contrast(palette["on-accent"], palette["accent"]), 4.5)
            warm = mix(palette["accent"], palette["bg"], 0.09)
            self.assertGreaterEqual(contrast(palette["fg"], warm), 4.5)
            self.assertGreaterEqual(contrast(palette["muted"], warm), 4.5)

    def test_the_yellow_never_carries_text(self):
        # R9: the yellow is only a dot.
        self.assertNotRegex(CSS, r"color:\s*var\(--yellow\)")
        self.assertEqual(len(re.findall(r"var\(--yellow\)", CSS)), 1)


class AlertColourTests(SimpleTestCase):
    """WCAG 2.2: 4.5:1 for text (1.4.3), 3:1 for the icon and the border (1.4.11)."""

    SHARE = {"success": ("ok", 0.12), "warning": ("warn", 0.12), "info": ("accent", 0.12), "danger": ("bad", 0.10)}

    def pairs(self):
        for index, mode in enumerate(("light", "dark")):
            palette = tokens()[index]
            for name, (token, share) in self.SHARE.items():
                accent = palette[token]
                tint = mix(accent, palette["panel"], share)
                # The danger alert keeps the error colour as text colour; the others use the text token.
                text = accent if name == "danger" else palette["fg"]
                yield mode, name, tint, accent, text, palette["panel"]

    def test_the_text_of_every_alert_reaches_4_5_to_1(self):
        for mode, name, tint, _accent, text, _surface in self.pairs():
            with self.subTest(mode=mode, alert=name):
                self.assertGreaterEqual(contrast(text, tint), 4.5)

    def test_the_icon_and_the_border_reach_3_to_1_on_the_tint_and_on_the_surface(self):
        for mode, name, tint, accent, _text, surface in self.pairs():
            with self.subTest(mode=mode, alert=name):
                self.assertGreaterEqual(contrast(accent, tint), 3)
                self.assertGreaterEqual(contrast(accent, surface), 3)

    def test_the_alerts_use_the_status_tokens_of_the_design_language(self):
        for name, token in (("success", "ok"), ("warning", "warn"), ("danger", "bad")):
            with self.subTest(alert=name):
                self.assertIn(f"--portal-alert-{name}-accent: var(--{token});", CSS)
        self.assertIn("--portal-alert-info-accent: var(--accent);", CSS)


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
        # Hints and error texts are 14 px (K6: 14 or 16), not smaller.
        block = re.search(r"\.register-clear \.form-hint,\s*\.register-clear \.invalid-feedback \{([^}]*)\}", CSS)
        self.assertIn("font-size: 0.875rem", block.group(1))


class ScriptTests(SimpleTestCase):
    SCRIPTS = ("password-toggle.js", "recovery-codes.js", "postal-code-input.js")
    # The scripts of the colour mode use the storage of the tab (D-056) and nothing else that
    # leaves the page. The language is chosen on the server (leg_platform/language.py).
    CHROME_SCRIPTS = ("theme-init.js", "corner-controls.js")

    def test_the_scripts_exist_and_use_no_dynamic_code_or_network(self):
        for name in self.SCRIPTS:
            with self.subTest(script=name):
                text = (STATIC / "js" / name).read_text(encoding="utf-8")
                for forbidden in ("eval(", "new Function", "innerHTML", "document.write", "fetch(",
                                  "XMLHttpRequest", "localStorage", "sessionStorage", "console."):
                    self.assertNotIn(forbidden, text)
                self.assertIn('"use strict"', text)

    def test_the_corner_scripts_keep_the_choice_in_the_tab_only(self):
        # K3: no cookie, no localStorage, nothing sent to the server.
        for name in self.CHROME_SCRIPTS:
            with self.subTest(script=name):
                text = (STATIC / "js" / name).read_text(encoding="utf-8")
                for forbidden in ("eval(", "new Function", "innerHTML", "document.write", "fetch(",
                                  "XMLHttpRequest", "sendBeacon", "localStorage", "document.cookie",
                                  "location", "console."):
                    self.assertNotIn(forbidden, text)
        for name in ("theme-init.js", "corner-controls.js"):
            text = (STATIC / "js" / name).read_text(encoding="utf-8")
            self.assertIn("sessionStorage", text)
            self.assertIn('"use strict"', text)
            # The language is not kept in the browser any more (no dictionary, no stored choice).
            self.assertNotIn("leg-lang", text)
            self.assertNotIn("LEG_PAGE_TEXTS", text)

    def test_the_client_side_dictionary_is_gone(self):
        self.assertFalse((STATIC / "js" / "login-texts.js").exists())

    def test_the_scripts_do_not_log_or_send_the_codes(self):
        text = (STATIC / "js" / "recovery-codes.js").read_text(encoding="utf-8")
        for forbidden in ("fetch", "sendBeacon", "XMLHttpRequest", "WebSocket", "console", "location"):
            self.assertNotIn(forbidden, text)
