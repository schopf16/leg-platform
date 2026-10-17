"""The rules of the community domain that need no database (D-077): numbers, status, quota,
discount tier, address normalisation and the checks of the list preferences."""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from leg_platform.community import addressing, legs, numbers, preferences
from leg_platform.community.models import LEGAL_MINIMUM_QUOTA_PERCENT, Direction
from leg_platform.community.settings_service import sender_signature
from leg_platform.community.status import MeteringPointStatus as S
from leg_platform.community.status import derive_status

TODAY = date(2026, 6, 15)


class MeteringPointNumberTests(SimpleTestCase):
    def test_the_number_is_country_identifier_and_the_digits_padded_to_20_positions(self):
        number = numbers.compose_metering_point_number("CH", "1A2B3C4D5E6", "4")

        self.assertEqual(number, "CH1A2B3C4D5E600000000000000000004")
        self.assertEqual(len(number), 33)
        self.assertEqual(numbers.split_metering_point_number(number), ("CH", "1A2B3C4D5E6", "0" * 19 + "4"))
        self.assertEqual(
            numbers.compose_metering_point_number("CH", "1A2B3C4D5E6", " " + "9" * 20 + " ")[13:], "9" * 20
        )

    def test_invalid_parts_are_refused_with_a_message(self):
        for country, identifier, digits in (
            ("ch", "1A2B3C4D5E6", "1"),  # lower case country
            ("C", "1A2B3C4D5E6", "1"),
            ("CH1", "1A2B3C4D5E6", "1"),
            ("CH", "1a2b3c4d5e6", "1"),  # lower case identifier
            ("CH", "1A2B3C4D5E", "1"),  # ten characters
            ("CH", "1A2B3C4D5E6X", "1"),
            ("CH", "1A2B3C4D5-6", "1"),
            ("CH", "1A2B3C4D5E6", ""),
            ("CH", "1A2B3C4D5E6", "A1"),
            ("CH", "1A2B3C4D5E6", "1" * 21),
        ):
            with self.subTest(country=country, identifier=identifier, digits=digits):
                with self.assertRaises(ValidationError):
                    numbers.compose_metering_point_number(country, identifier, digits)

    def test_a_valid_number_has_33_capital_letters_and_digits(self):
        self.assertTrue(numbers.is_valid_metering_point_number("CH1A2B3C4D5E600000000000000000004"))
        for invalid in (
            "", "CH1A2B3C4D5E60000000000000000004", "ch1a2b3c4d5e600000000000000000004",
            "CH1A2B3C4D5E6000000000000000000044", "CH1A2B3C4D5E6 0000000000000000004", None, 33,
        ):
            with self.subTest(value=invalid):
                self.assertFalse(numbers.is_valid_metering_point_number(invalid))
        self.assertIsNone(numbers.split_metering_point_number("short"))
        self.assertEqual(numbers.normalise_metering_point_number("  ch1a2b3c4d5e600000000000000000004 "),
                         "CH1A2B3C4D5E600000000000000000004")

    def test_country_and_identifier_validators(self):
        numbers.validate_country("CH")
        numbers.validate_identifier("1A2B3C4D5E6")
        for bad in ("", "ch", "CHE", "C1"):
            with self.assertRaises(ValidationError):
                numbers.validate_country(bad)
        for bad in ("", "1a2b3c4d5e6", "1A2B3C4D5E", "1A2B3C4D5E67"):
            with self.assertRaises(ValidationError):
                numbers.validate_identifier(bad)


class TransformerCircuitNumberTests(SimpleTestCase):
    def test_the_number_is_stripped_and_capitalised_before_it_is_compared(self):
        self.assertEqual(numbers.normalise_transformer_circuit_number("  tra21359 "), "TRA21359")

    def test_the_format_is_tra_and_digits(self):
        numbers.validate_transformer_circuit_number("TRA21359")
        for bad in ("", "TRA", "TRA12A", "tra1", "XRA1", "TRA" + "1" * 18):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                numbers.validate_transformer_circuit_number(bad)


class StatusTests(SimpleTestCase):
    def status(self, registered=None, confirmed=None, start=None, terminated=None):
        return derive_status(
            registered_with_bkw_on=registered, confirmed_on=confirmed, start_date=start,
            terminated_on=terminated, today=TODAY,
        )

    def test_the_status_follows_the_dates_and_the_day(self):
        self.assertEqual(self.status(), S.REGISTERED)
        self.assertEqual(self.status(registered=date(2026, 6, 1)), S.NOTIFIED)
        self.assertEqual(self.status(date(2026, 6, 1), date(2026, 6, 10)), S.CONFIRMED)
        # Confirmed with a start date that is not reached: still confirmed.
        self.assertEqual(self.status(date(2026, 6, 1), date(2026, 6, 10), date(2026, 6, 16)), S.CONFIRMED)
        # The start day itself is active (the start date is reached), no scheduler needed.
        self.assertEqual(self.status(date(2026, 6, 1), date(2026, 6, 10), TODAY), S.ACTIVE)
        self.assertEqual(self.status(date(2026, 6, 1), date(2026, 6, 10), date(2026, 5, 1)), S.ACTIVE)
        # A start date without a confirmation does not make it active.
        self.assertEqual(self.status(registered=date(2026, 6, 1), start=date(2026, 5, 1)), S.NOTIFIED)

    def test_the_termination_counts_from_its_day(self):
        active = (date(2026, 1, 1), date(2026, 1, 2), date(2026, 2, 1))
        self.assertEqual(self.status(*active, terminated=date(2026, 6, 30)), S.ACTIVE)
        self.assertEqual(self.status(*active, terminated=TODAY), S.TERMINATED)
        self.assertEqual(self.status(*active, terminated=date(2026, 5, 31)), S.TERMINATED)

    def test_the_status_keys_are_english(self):
        self.assertEqual(S.values, ["registered", "notified", "confirmed", "active", "terminated"])


class LegRuleTests(SimpleTestCase):
    def test_the_discount_tier_comes_from_the_circuits_of_the_metering_points(self):
        self.assertEqual(legs.discount_tier([]), legs.DiscountTier.NONE)
        self.assertEqual(legs.discount_tier([1]), legs.DiscountTier.OPTIMISED)
        self.assertEqual(legs.discount_tier([1, 1, 1]), legs.DiscountTier.OPTIMISED)
        self.assertEqual(legs.discount_tier([1, 2]), legs.DiscountTier.BASIC)
        # An unknown circuit cannot be shown to be the same one.
        self.assertEqual(legs.discount_tier([1, None]), legs.DiscountTier.BASIC)
        self.assertEqual(legs.discount_tier([None]), legs.DiscountTier.BASIC)

    def test_the_ratio_is_consumers_to_producers(self):
        self.assertEqual(legs.ratio_text(3, 1), "3 : 1")
        self.assertIsNone(legs.LegSummary(leg_id=1).ratio)
        self.assertEqual(legs.LegSummary(leg_id=1, metering_points=2, consumers=2).ratio, "2 : 0")

    def test_quota_problems(self):
        P = legs.LegProblem
        created = [date(2026, 5, 1)]

        def problems(quota, checked=date(2026, 6, 1), members=created, threshold=10):
            return legs.quota_problems(None if quota is None else Decimal(quota), checked, members, threshold)

        self.assertEqual(problems(None), [])
        self.assertEqual(problems("37.6"), [])
        self.assertEqual(problems("10"), [])  # on the threshold is not below it
        self.assertEqual(problems("9.99"), [P.QUOTA_BELOW_WARNING])
        self.assertEqual(problems("5"), [P.QUOTA_BELOW_WARNING])
        # Below the legal minimum: that problem alone, not also the warning.
        self.assertEqual(problems("4.99"), [P.QUOTA_BELOW_LEGAL])
        self.assertEqual(LEGAL_MINIMUM_QUOTA_PERCENT, 5)
        # The threshold of the settings applies.
        self.assertEqual(problems("12", threshold=15), [P.QUOTA_BELOW_WARNING])
        self.assertEqual(problems("12", threshold=12), [])

    def test_the_quota_is_stale_when_a_metering_point_was_created_after_the_day_it_was_checked(self):
        P = legs.LegProblem
        checked = date(2026, 6, 1)

        def stale(created):
            return P.QUOTA_STALE in legs.quota_problems(Decimal("30"), checked, [created], 10)

        self.assertFalse(stale(date(2026, 5, 31)))
        self.assertFalse(stale(checked))  # the same day: not stale (day exact)
        self.assertTrue(stale(date(2026, 6, 2)))
        # Without a quota there is nothing to be stale.
        self.assertEqual(legs.quota_problems(None, checked, [date(2026, 7, 1)], 10), [])

    def test_the_summary_of_one_leg(self):
        class FakeLeg:
            pk = 7
            production_quota_percent = Decimal("30")
            quota_checked_on = date(2026, 6, 1)
            is_archived = False
        rows = [
            (Direction.CONSUMPTION, 1, date(2026, 5, 1)),
            (Direction.CONSUMPTION, 1, date(2026, 5, 2)),
            (Direction.FEED_IN, 1, date(2026, 5, 3)),
        ]
        summary = legs.build_summary(FakeLeg, rows, 10)

        self.assertEqual((summary.metering_points, summary.consumers, summary.producers), (3, 2, 1))
        self.assertEqual(summary.ratio, "2 : 1")
        self.assertEqual(summary.tier, legs.DiscountTier.OPTIMISED)
        self.assertEqual(summary.problems, ())
        self.assertEqual(summary.circuit_ids, frozenset({1}))

        only_consumers = legs.build_summary(FakeLeg, rows[:2], 10)
        self.assertEqual(only_consumers.problems, (legs.LegProblem.NO_PRODUCER,))
        only_producers = legs.build_summary(FakeLeg, rows[2:], 10)
        self.assertEqual(only_producers.problems, (legs.LegProblem.NO_CONSUMER,))
        # A LEG without metering points has no tier, no ratio and no problem of this kind.
        empty = legs.build_summary(FakeLeg, [], 10)
        self.assertEqual((empty.tier, empty.ratio, empty.problems), (legs.DiscountTier.NONE, None, ()))

    def test_the_problem_texts_name_the_threshold_of_the_settings(self):
        text = legs.problem_text(legs.LegProblem.QUOTA_BELOW_WARNING, 12)
        self.assertEqual(text, "Problem: Produktionsquote unter 12 %")
        self.assertEqual(
            legs.problem_text(legs.LegProblem.QUOTA_BELOW_LEGAL),
            "Problem: Produktionsquote unter 5 % (gesetzlich nötig)",
        )
        self.assertEqual(
            legs.problem_text(legs.LegProblem.QUOTA_STALE),
            "Problem: Messpunkte seit dem Stand hinzugefügt, Wert neu ablesen",
        )


class AddressRuleTests(SimpleTestCase):
    def test_white_space_is_normalised(self):
        self.assertEqual(
            addressing.normalise_address("  Muster   weg ", " 12 a", " 3063", "Ittigen  "),
            ("Muster weg", "12 a", "3063", "Ittigen"),
        )

    def test_the_key_ignores_case_accents_and_white_space(self):
        a = addressing.address_digest("Musterweg", "12", "3063", "Ittigen")
        self.assertEqual(a, addressing.address_digest(" musterweg ", "12", "3063", "ITTIGEN"))
        self.assertEqual(
            addressing.address_digest("Rue de l'Église", "1", "1000", "Lausanne"),
            addressing.address_digest("rue de l'eglise", "1", "1000", "Lausanne"),
        )
        self.assertNotEqual(a, addressing.address_digest("Musterweg", "12a", "3063", "Ittigen"))
        self.assertNotEqual(a, addressing.address_digest("Musterweg", "12", "3000", "Ittigen"))
        self.assertEqual(len(a), 64)

    def test_the_parts_cannot_be_shifted_between_fields(self):
        # "Muster" + "weg 1" must not equal "Musterweg" + "1": the parts are separated.
        self.assertNotEqual(
            addressing.address_key("Muster", "weg 1", "3000", "Bern"),
            addressing.address_key("Musterweg", "1", "3000", "Bern"),
        )

    def test_the_signature_keeps_the_spelling(self):
        self.assertNotEqual(
            addressing.address_signature("Musterweg", "12", "3063", "Ittigen"),
            addressing.address_signature("musterweg", "12", "3063", "Ittigen"),
        )
        self.assertEqual(
            addressing.address_signature(" Musterweg", "12", "3063", "Ittigen "),
            addressing.address_signature("Musterweg", "12", "3063", "Ittigen"),
        )

    def test_the_suggestion_is_shown_as_street_and_number_then_postal_code_and_town(self):
        suggestion = addressing.AddressSuggestion("Musterstrasse", "1", "3063", "Ittigen")
        self.assertEqual(suggestion.display(), "Musterstrasse 1, 3063 Ittigen")
        self.assertEqual(
            addressing.suggestion_banner_text(suggestion),
            "Die Adresse weicht vom Adressverzeichnis ab. Vorschlag: Musterstrasse 1, 3063 Ittigen.",
        )

    def test_the_sender_signature_ignores_the_address_addition(self):
        values = {
            "sender_street": "Musterstrasse", "sender_house_number": "1", "sender_postal_code": "3063",
            "sender_town": "Ittigen", "sender_address_addition": "c/o X",
        }
        self.assertEqual(
            sender_signature(values),
            addressing.address_signature("Musterstrasse", "1", "3063", "Ittigen"),
        )


class PreferenceRuleTests(SimpleTestCase):
    def test_valid_widths_are_whole_pixels_of_at_least_90(self):
        self.assertEqual(preferences.clean_column_widths({"street": 90, "town": 400}), {"street": 90, "town": 400})

    def test_everything_else_is_refused(self):
        for bad in (
            {"street": 89}, {"street": 2001}, {"street": 90.5}, {"street": "100"}, {"street": True},
            {"Street": 100}, {"1street": 100}, {"a b": 100}, {"x" * 31: 100}, ["street"], None,
            {f"c{i}": 100 for i in range(13)},
        ):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                preferences.clean_column_widths(bad)

    def test_only_the_known_lists_exist(self):
        with self.assertRaises(ValueError):
            preferences.get_preference(None, "unknown")
