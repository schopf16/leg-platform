"""The stylesheet of the address suggestions (``static/css/address-suggest.css``), read as text (D-111, A1 to A11, G1).

What a static check can hold: the stylesheet is imported by ``portal.css`` (every portal page) before any other rule, and loaded by
the administration; the list is an overlay (absolute, above the fields, with a shadow, the width of the row), without a maximum
height and without a scrollbar; the foot line can be hidden; the rows are compact; the card is an overlay on a desktop width and in
the page on a phone; the colours come from the portal's variables (the theme follows them), the dark theme has its own shadow.
The behaviour in a browser is proven in ``test_widget_behaviour``."""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

CSS_DIRECTORY = Path(settings.BASE_DIR) / "leg_platform" / "static" / "css"
STYLE = (CSS_DIRECTORY / "address-suggest.css").read_text(encoding="utf-8")
PORTAL = (CSS_DIRECTORY / "portal.css").read_text(encoding="utf-8")
ADMIN = (CSS_DIRECTORY / "admin-portal.css").read_text(encoding="utf-8")
COMMENTLESS = re.sub(r"/\*.*?\*/", "", STYLE, flags=re.S)


def rule(selector):
    """The declarations of the first top-level rule whose selector list is exactly ``selector``."""
    match = re.search(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{(.*?)\}", COMMENTLESS, re.S)
    assert match, selector
    return match.group(1)


def desktop_block():
    start = COMMENTLESS.index("@media (min-width: 761px) {")
    depth, end = 0, start
    for position in range(start, len(COMMENTLESS)):
        depth += COMMENTLESS[position] == "{"
        depth -= COMMENTLESS[position] == "}"
        if depth == 0 and COMMENTLESS[position] == "}":
            end = position
            break
    return COMMENTLESS[start:end + 1]


class StylesheetLoadingTests(SimpleTestCase):
    def test_the_portal_imports_the_stylesheet_before_any_other_rule(self):
        # An @import is valid only before every rule; the comment before it is allowed.
        without_comments = re.sub(r"/\*.*?\*/", "", PORTAL, flags=re.S).lstrip()

        self.assertTrue(without_comments.startswith('@import url("address-suggest.css");'), without_comments[:120])
        self.assertEqual(PORTAL.count("address-suggest.css"), 1)

    def test_the_address_block_is_no_longer_in_the_portal_stylesheet(self):
        for selector in (".address-suggest", ".address-option", ".address-proposal", ".address-notice", ".address-more"):
            self.assertNotIn(selector, re.sub(r"/\*.*?\*/", "", PORTAL, flags=re.S), selector)

    def test_the_administration_has_the_rules_for_the_form_row_and_defines_the_accent(self):
        self.assertIn("--address-accent", ADMIN)
        self.assertIn(".form-row.address-host", ADMIN)

    def test_the_stylesheet_is_a_static_file_without_imports_of_its_own(self):
        self.assertNotIn("@import", COMMENTLESS)
        self.assertNotIn("url(", COMMENTLESS)


class ListRulesTests(SimpleTestCase):
    def test_the_list_is_an_absolute_overlay_over_the_row_above_the_fields_with_a_shadow(self):
        panel = rule(".address-suggest")

        self.assertIn("position: absolute", panel)
        self.assertRegex(panel, r"left:\s*0")
        self.assertRegex(panel, r"right:\s*0")
        self.assertRegex(panel, r"z-index:\s*[1-9][0-9]")
        self.assertRegex(panel, r"margin-top:\s*4px")
        self.assertIn("box-shadow", panel)
        self.assertIn("position: relative", rule(".address-host"))

    def test_the_widget_in_the_flow_has_no_height(self):
        self.assertIn("height: 0", rule(".address-assist"))

    def test_no_list_has_a_scrollbar_or_a_maximum_height(self):
        for selector in (".address-suggest", ".address-suggest-list", ".address-option"):
            block = rule(selector)
            self.assertNotRegex(block, r"max-height", selector)
            self.assertNotRegex(block, r"overflow(?:-y)?:\s*(?:auto|scroll)", selector)
        self.assertNotRegex(COMMENTLESS, r"max-height")
        self.assertNotRegex(COMMENTLESS, r"overflow(?:-y)?:\s*(?:auto|scroll)")

    def test_the_rows_are_compact_and_bold_for_the_street_and_muted_for_the_town(self):
        option = rule(".address-option")

        self.assertRegex(option, r"padding:\s*6px 12px")
        self.assertIn("font-weight: 600", rule(".address-option-main"))
        self.assertIn("var(--muted", rule(".address-option-sub"))
        self.assertNotRegex(rule(".address-suggest-list"), r"gap|padding:\s*[1-9]")

    def test_the_head_is_one_slim_line_of_fourteen_pixels(self):
        head = rule(".address-suggest-head")

        self.assertIn("font-size: 14px", head)
        self.assertIn("display: inline", rule(".address-suggest-title"))
        self.assertIn("display: inline", rule(".address-suggest-note"))

    def test_the_foot_line_can_be_hidden(self):
        self.assertIn("display: none", rule(".address-more[hidden]"))
        self.assertIn("display: none", rule(".address-suggest[hidden]"))

    def test_the_active_option_has_a_ring_not_only_a_colour(self):
        self.assertIn("outline: 2px solid", rule(".address-option.is-active"))

    def test_a_phone_card_reaches_the_edges_of_the_profile_card(self):
        self.assertRegex(COMMENTLESS, r"@media \(max-width: 480px\)\s*\{\s*\.profile-card \.address-suggest\s*\{\s*left:\s*-16px")


class ColourAndThemeTests(SimpleTestCase):
    def test_no_fixed_colour_except_the_black_shadow_of_the_dark_theme(self):
        self.assertNotRegex(COMMENTLESS, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(COMMENTLESS, r"\brgba?\((?!0 0 0 / 55%\))")
        self.assertNotRegex(COMMENTLESS, r"(?<![\w-])(?:white|black|red|green|blue|yellow|orange|gray|grey)(?![\w-])")

    def test_the_colours_are_the_variables_of_the_portal_with_those_of_the_administration_as_fallback(self):
        for selector, variable in (
            (".address-suggest", "--portal-surface"), (".address-option", "--tblr-body-color"),
            (".address-proposal", "--portal-alert-warning-bg"), (".address-proposal-keep", "--address-accent"),
        ):
            self.assertIn(f"var({variable}", rule(selector), selector)
        for declaration in re.findall(r"var\((--[a-z-]+),\s*([^)]+)\)", COMMENTLESS):
            self.assertTrue(declaration[1].strip(), declaration)

    def test_both_themes_have_their_own_shadow(self):
        self.assertIn(':root[data-theme="dark"] .address-suggest', COMMENTLESS)
        self.assertIn(':root:not([data-theme="light"]) .address-suggest', COMMENTLESS)
        self.assertIn('@media (prefers-color-scheme: dark)', COMMENTLESS)


class CardRulesTests(SimpleTestCase):
    def test_the_card_is_an_overlay_above_the_sidebar_breakpoint_and_in_the_page_below_it(self):
        desktop = desktop_block()

        self.assertIn(".address-notice > *", desktop)
        self.assertIn("position: absolute", desktop)
        self.assertRegex(desktop, r"z-index:\s*40")
        self.assertIn("height: 0", desktop)
        # Nothing of this is outside the media query: below 761 px the card stays in the flow.
        outside = COMMENTLESS.replace(desktop, "")
        self.assertNotRegex(outside, r"\.address-notice\s*>\s*\*\s*\{[^}]*position:\s*absolute")
        self.assertNotRegex(rule(".address-proposal"), r"position")

    def test_the_two_ways_out_are_large_enough_to_touch(self):
        self.assertIn("min-height: 44px", rule(".address-proposal-keep"))
        self.assertIn("focus-visible", COMMENTLESS)

    def test_the_options_of_the_card_are_buttons_that_look_like_the_rows(self):
        option = rule(".address-proposal-option")

        self.assertIn("display: block", option)
        self.assertIn("width: 100%", option)
        self.assertIn("cursor: pointer", option)
