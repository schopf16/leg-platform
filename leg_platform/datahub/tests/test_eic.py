"""The EIC format and check character (D-139; ENTSO-E reference manual 5.5, section 5, and check-digit generator 1.5)."""

import itertools

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from leg_platform.datahub.eic import ALPHABET, check_character, is_valid, validate_eic

# The two codes of the Datahub entries of the owner and the examples of the reference manual.
VALID = ("12X-00000020RL-R", "12X-0000000858-F", "10X168Y4E6H0041Z", "10X---ENTSOE---L", "10X1001A1001A450")


class EicTests(SimpleTestCase):
    def test_known_codes_are_valid(self):
        for code in VALID:
            with self.subTest(code=code):
                self.assertTrue(is_valid(code))
                validate_eic(code)

    def test_a_wrong_check_character_is_refused(self):
        for code in VALID:
            for wrong in (c for c in ALPHABET[:36] if c != code[15]):
                with self.subTest(code=code, wrong=wrong):
                    self.assertFalse(is_valid(code[:15] + wrong))

    def test_a_changed_character_is_detected(self):
        code = "12X-00000020RL-R"
        for position in range(15):
            for char in ALPHABET:
                if char == code[position]:
                    continue
                changed = code[:position] + char + code[position + 1:]
                with self.subTest(changed=changed):
                    self.assertFalse(is_valid(changed))

    def test_malformed_codes_are_refused(self):
        for code in (
            "", "12X-00000020RL-", "12X-00000020RL-RR", "12x-00000020rl-r", "12X 00000020RL-R",
            "-2X-00000020RL-R", "1-X-00000020RL-R", "12X-00000020RL-Ä", "12X-00000020RL-\n", None, 12,
        ):
            with self.subTest(code=code):
                self.assertFalse(is_valid(code))
        with self.assertRaises(ValidationError):
            validate_eic("12X-00000020RL-A")

    def test_a_check_value_of_hyphen_gives_no_code(self):
        # Some prefixes would need "-" as check character, which the scheme does not allow.
        prefix = next(
            "10X" + "".join(rest)
            for rest in itertools.product("0123456789", repeat=12)
            if check_character("10X" + "".join(rest)) is None
        )
        self.assertIsNone(check_character(prefix))
        self.assertFalse(is_valid(prefix + "-"))
