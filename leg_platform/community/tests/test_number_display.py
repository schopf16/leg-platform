"""How a metering point number is shown and typed (D-109): the grouping (the grid operator's block of 13 characters, then
blocks of four, no-break spaces), white space that is never part of a number, the pasted complete number and the template
filter that every page uses. The structure of the number is that of the validator (``numbers``, MC-CH 2025 4.2.3, D-102):
2 + 11 + 20 characters."""

from django.core.exceptions import ValidationError
from django.template import Context, Template
from django.test import SimpleTestCase

from leg_platform.community import numbers

NBSP = "\u00a0"
# The owner's example (walkthrough 3): the grid operator's part, then the customer's part with the leading zeros.
OWNER_NUMBER = "CH10222012345" + "0" * 18 + "23"
OWNER_SHOWN = f"CH10222012345{NBSP}0000{NBSP}0000{NBSP}0000{NBSP}0000{NBSP}0023"


def render(source, **values):
    return Template("{% load metering_points %}" + source).render(Context(values))


class GroupingTests(SimpleTestCase):
    def test_the_standard_number_is_one_block_of_thirteen_and_five_blocks_of_four(self):
        self.assertEqual(len(OWNER_NUMBER), numbers.METERING_POINT_LENGTH)
        self.assertTrue(numbers.is_valid_metering_point_number(OWNER_NUMBER))
        self.assertEqual(numbers.format_metering_point_number(OWNER_NUMBER), OWNER_SHOWN)
        self.assertEqual(numbers.format_metering_point_number(OWNER_NUMBER).count(NBSP), 5)

    def test_the_separator_is_a_no_break_space_so_that_the_number_never_wraps(self):
        shown = numbers.format_metering_point_number(OWNER_NUMBER)
        self.assertNotIn(" ", shown)
        self.assertEqual(numbers.format_metering_point_number(OWNER_NUMBER, separator=" "), OWNER_SHOWN.replace(NBSP, " "))

    def test_other_lengths_group_the_tail_in_fours_and_a_short_value_is_one_block(self):
        for value, expected in (
            ("CH10222012345" + "1234567", f"CH10222012345{NBSP}1234{NBSP}567"),  # 20 characters
            ("CH10222012345" + "12", f"CH10222012345{NBSP}12"),
            ("CH10222012345" + "0" * 24, f"CH10222012345{NBSP}" + NBSP.join(["0000"] * 6)),  # 37 characters
            ("CH10222012345", "CH10222012345"),
            ("CH1022", "CH1022"),
            ("X", "X"),
        ):
            with self.subTest(value=value):
                self.assertEqual(numbers.format_metering_point_number(value), expected)

    def test_a_missing_number_gives_nothing_and_a_value_that_is_not_valid_is_still_grouped_not_changed(self):
        for value in (None, "", "  ", NBSP):
            with self.subTest(value=value):
                self.assertEqual(numbers.format_metering_point_number(value), "")
        # Not validated: the page shows what is stored (a number from an older rule stays readable).
        self.assertEqual(
            numbers.format_metering_point_number("ab-cd" + "9" * 20), f"ab-cd99999999{NBSP}9999{NBSP}9999{NBSP}9999"
        )

    def test_grouping_is_idempotent_and_ignores_white_space_in_its_input(self):
        shown = numbers.format_metering_point_number(OWNER_NUMBER)
        self.assertEqual(numbers.format_metering_point_number(shown), shown)
        self.assertEqual(numbers.format_metering_point_number(shown.replace(NBSP, " ")), shown)

    def test_what_is_displayed_reads_back_as_the_stored_number(self):
        shown = numbers.format_metering_point_number(OWNER_NUMBER)
        self.assertEqual(numbers.compact_number(shown), OWNER_NUMBER)
        self.assertEqual(numbers.normalise_metering_point_number(shown.lower()), OWNER_NUMBER)
        self.assertTrue(numbers.is_valid_metering_point_number(numbers.normalise_metering_point_number(shown)))


class WhiteSpaceTests(SimpleTestCase):
    def test_every_kind_of_white_space_is_removed(self):
        for text in (" 1 2 ", f"1{NBSP}2", "1\u202f2", "1\u20092", "1\t2", "1\n2", "1\u30002"):
            with self.subTest(text=text):
                self.assertEqual(numbers.compact_number(text), "12")
        self.assertEqual(numbers.compact_number(None), "")

    def test_the_typed_end_ignores_white_space_but_still_refuses_every_other_character(self):
        self.assertEqual(numbers.normalise_entered_number(" 0000 0000 0000 0000 0023 "), "0" * 18 + "23")
        self.assertEqual(numbers.normalise_entered_number(f"1{NBSP}2"), "0" * 18 + "12")
        self.assertEqual(numbers.normalise_entered_number("a b-1"), "0" * 16 + "AB-1")
        for typed, code in (("   ", "empty"), ("1 !", "not_allowed"), ("1\u200b2", "not_allowed")):
            with self.subTest(typed=typed):
                with self.assertRaises(ValidationError) as refused:
                    numbers.normalise_entered_number(typed)
                self.assertEqual(refused.exception.code, code)

    def test_a_pasted_complete_number_loses_the_prefix_of_the_settings_only_for_that_prefix(self):
        prefix = "CH10222012345"
        self.assertEqual(numbers.normalise_entered_number(OWNER_SHOWN, prefix), "0" * 18 + "23")
        self.assertEqual(numbers.normalise_entered_number(OWNER_NUMBER.lower(), prefix), "0" * 18 + "23")
        self.assertEqual(numbers.compose_metering_point_number("CH", "10222012345", OWNER_SHOWN), OWNER_NUMBER)
        # Another prefix, or no prefix known: a number of 33 characters is "too long" as before.
        for other in ("CH99999999999", "", "CH"):
            with self.subTest(prefix=other):
                with self.assertRaises(ValidationError) as refused:
                    numbers.normalise_entered_number(OWNER_SHOWN, other)
                self.assertEqual(refused.exception.code, "too_long")
        # A shorter or longer entry that merely starts like the prefix is not a complete number.
        with self.assertRaises(ValidationError):
            numbers.normalise_entered_number(prefix + "1" * 21, prefix)

    def test_a_number_copied_from_a_page_is_one_piece_for_the_search(self):
        self.assertEqual(numbers.pasted_number(OWNER_SHOWN), OWNER_NUMBER)
        self.assertEqual(numbers.pasted_number(OWNER_SHOWN.replace(NBSP, " ").lower()), OWNER_NUMBER)
        self.assertEqual(numbers.pasted_number("CH10222012345 0000 0023"), "CH1022201234500000023")
        for text in (OWNER_NUMBER, "0023", "Musterweg 12", "CH10222012345 !", "", None, f"{OWNER_SHOWN} {OWNER_SHOWN}"):
            with self.subTest(text=text):
                self.assertIsNone(numbers.pasted_number(text))


class FilterTests(SimpleTestCase):
    def test_the_filter_draws_the_grouped_number_in_one_element_that_does_not_wrap(self):
        self.assertEqual(
            render("{{ number|metering_point }}", number=OWNER_NUMBER),
            f'<span class="mp-number">{OWNER_SHOWN}</span>',
        )

    def test_the_text_filter_gives_the_grouped_number_without_the_element(self):
        self.assertEqual(render("{{ number|metering_point_text }}", number=OWNER_NUMBER), OWNER_SHOWN)

    def test_no_number_gives_nothing_so_the_template_keeps_its_own_placeholder(self):
        for value in (None, "", " "):
            with self.subTest(value=value):
                self.assertEqual(render("{{ number|metering_point }}", number=value), "")
        self.assertEqual(render("{% if n %}{{ n|metering_point }}{% else %}–{% endif %}", n=None), "–")

    def test_the_value_is_escaped_never_trusted(self):
        shown = render("{{ number|metering_point }}", number='<script>alert(1)</script>"&')
        self.assertNotIn("<script>", shown)
        self.assertIn("&lt;script&gt;", shown)
        text = render('<a title="{{ number|metering_point_text }}">', number='"><b>')
        self.assertNotIn("<b>", text)
