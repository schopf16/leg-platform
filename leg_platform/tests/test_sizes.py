"""The style rules of the acceptance (blocks 5c, A1-A3): the sizes of labels, hints and messages.

The stylesheets are plain files, so the rules are read as text: a rule that is removed or changed
fails here, without a browser.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

STATIC = Path(__file__).resolve().parents[1] / "static" / "css"


def rule(css, selector):
    """The declarations of the first rule whose selector list ends with ``selector``."""
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        selectors = [part.strip() for part in match.group(1).split(",")]
        if selector in selectors:
            return match.group(2)
    raise AssertionError(f"No rule for {selector}.")


class SizesOfTheEntryFormsTests(SimpleTestCase):
    def setUp(self):
        strip = lambda css: re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)  # noqa: E731
        self.portal = strip((STATIC / "portal.css").read_text(encoding="utf-8"))
        self.frame = strip((STATIC / "frame.css").read_text(encoding="utf-8"))

    def test_the_label_of_a_field_is_16_px_and_the_hint_stays_14_px(self):
        labels = [
            match.group(2)
            for match in re.finditer(r"([^{}]+)\{([^}]*)\}", self.portal)
            if [part.strip() for part in match.group(1).split(",")] == [".entry-card .form-label"]
        ]

        self.assertTrue(any("font-size: 16px" in declarations for declarations in labels))
        self.assertIn("font-size: 14px", rule(self.portal, ".entry-card .form-hint"))

    def test_a_message_below_a_field_is_14_px(self):
        self.assertIn("font-size: 0.875rem", rule(self.portal, ".register-clear .invalid-feedback"))
        self.assertIn("font-size: 0.875rem", rule(self.portal, ".register-clear .form-hint"))

    def test_the_button_of_a_tile_fills_the_width_on_a_phone(self):
        phone = self.frame[self.frame.index("@media (max-width: 760px)") :]

        self.assertIn("width: 100%", rule(phone, ".task-actions > form:first-child .btn"))
