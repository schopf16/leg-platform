"""The address suggestions of every mask (D-111, owner specification of 2026-10-28): the house number at the end of the street,
the ``more`` flag, and the fixes of the check ("Did you mean this address?").

What is proven, from the server side: a street field that ends in a house number is split only when the street as typed finds
nothing (streets with digits stay whole); ``more`` is true exactly when the directory holds more matches than the answer; the
check names a reason (postal code, house number, town, street) and offers at most three complete addresses, nearest place
first (by the distance of the centres of the places, else by the number of the postal code); nothing is offered for an empty
house number, a stale directory, a passed deadline, a correct address or an anonymous client that would need the approximate
street search; the bounds ``HOUSE_CAP`` and ``SIMILAR_CAP`` hold; the lookup endpoint keeps its contract (POST only, CSRF, 4 KiB,
no-store, the keys of the answer, ``not_checked`` outside Switzerland) and an anonymous check costs two requests of the budget.
The addresses are invented; the postal codes and towns are real ones of the canton of Bern."""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlencode

from django.db import connections
from django.test import Client, SimpleTestCase, TestCase, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.models import RateLimitEvent
from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.addresses import services
from leg_platform.addresses.models import Address, AddressStreet, Directory, fill_place_centres
from leg_platform.addresses.services import (
    HOUSE_CAP,
    SIMILAR_CAP,
    fixes,
    lookup,
    prerank,
    ranked_rows,
    similar_streets,
    split_house_number,
)

URL = reverse("addresses:lookup")
FORM = "application/x-www-form-urlencoded"
HOME = ("3063", "Ittigen")
ANSWER_KEYS = {"status", "data_date", "stale", "warning", "results", "match", "kind", "more", "proposals", "reason"}

# LV95 positions of the places (invented, but at the right distances: Bern is 4 km from Ittigen, Zollikofen 11 km).
CENTRES = {
    ("3063", "Ittigen"): (2602500, 1203500),
    ("3000", "Bern"): (2600500, 1199800),
    ("3052", "Zollikofen"): (2611000, 1210000),
    ("8001", "Zürich"): (2683000, 1247000),
    ("6900", "Lugano"): (2717000, 1096000),
    ("1204", "Genève"): (2500000, 1117000),
}

# (street, house numbers, postal code, town)
STREETS = (
    ("Ittigenstrasse", ("5", "11", "15", "51"), "3063", "Ittigen"),
    ("Zahlenweg", ("10", "11", "12", "13", "14", "15"), "3063", "Ittigen"),
    # Streets whose name ends in a number-like word, next to the street that the word would be cut from.
    ("Zone 7", ("1", "2"), "3063", "Ittigen"),
    ("Zone", ("7", "9"), "3063", "Ittigen"),
    ("Strada 53", ("1", "2"), "6900", "Lugano"),
    ("Rue du 1er-Aout", ("3", "5"), "1204", "Genève"),
    # The same number in three places: the nearest place comes first.
    ("Teststrasse", ("7",), "3000", "Bern"),
    ("Teststrasse", ("7",), "3052", "Zollikofen"),
    ("Teststrasse", ("7",), "8001", "Zürich"),
    ("Hauptstrasse", ("1", "2"), "3000", "Bern"),
)


def rows_of(streets, *, coordinates=True):
    rows = []
    for street, numbers, code, town in streets:
        centre = CENTRES[(code, town)] if coordinates else (None, None)
        for number in numbers:
            rows.append((street, number, code, town, *centre))
    return rows


@tag("mariadb")
class SuggestionBase(TestCase):
    databases = {"default", "addresses"}
    COORDINATES = True

    def setUp(self):
        self.generation = uuid.uuid4()
        self.directory = Directory.objects.create(
            pk=1, generation=self.generation, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=1, has_egid=True,
        )
        self.add(rows_of(STREETS, coordinates=self.COORDINATES))

    def add(self, rows):
        Address.objects.bulk_create([
            Address(
                generation=self.generation, street=street, house_number=number, postal_code=code, locality=town,
                easting=None if east is None else Decimal(east), northing=None if north is None else Decimal(north),
            )
            for street, number, code, town, east, north in rows
        ])
        fill_place_centres(self.generation)

    def check(self, street, house_number, postal_code, town, **options):
        return lookup(
            {"street": street, "house_number": house_number, "postal_code": postal_code, "locality": town},
            mode="check", home=HOME, **options,
        )

    def suggest(self, street, house_number="", postal_code="", town="", **options):
        return lookup(
            {"street": street, "house_number": house_number, "postal_code": postal_code, "locality": town},
            mode="suggest", home=HOME, minimum=3, **options,
        )

    @staticmethod
    def labels(rows):
        return [f"{row['street']} {row['house_number']}, {row['postal_code']} {row['locality']}" for row in rows]


class SplitHouseNumberTests(SimpleTestCase):
    """B1: the unit. The caller (``lookup``) uses the result only after the street as typed found nothing."""

    def values(self, street, house_number=""):
        return {"street": street, "house_number": house_number, "postal_code": "", "locality": "", "country": "CH"}

    def test_a_house_number_at_the_end_is_split_off_an_empty_house_number_field(self):
        for typed, street, house_number in (
            ("Ittigenstrasse 5", "Ittigenstrasse", "5"),
            ("Bahnhofstrasse 12a", "Bahnhofstrasse", "12a"),
            ("Route de Genève 12-14", "Route de Genève", "12-14"),
            ("Im Winkel 3/4", "Im Winkel", "3/4"),
            ("Weg  7", "Weg", "7"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(
                    split_house_number(self.values(typed)),
                    {**self.values(typed), "street": street, "house_number": house_number},
                )

    def test_a_filled_house_number_field_is_never_overridden(self):
        self.assertIsNone(split_house_number(self.values("Ittigenstrasse 5", "7")))

    def test_nothing_is_split_where_no_house_number_follows(self):
        for typed in (
            "Ittigenstrasse", "Ittigenstrasse 5 a", "5", " 5", "Zone Sieben", "Strasse 5x y", "", "   ",
            "Hauptstrasse a5", "Hauptstrasse -5",
        ):
            with self.subTest(typed=typed):
                self.assertIsNone(split_house_number(self.values(typed)))

    def test_the_street_part_must_keep_a_searchable_text(self):
        # Only combining marks fold to nothing: no street is left.
        self.assertIsNone(split_house_number(self.values("́ 5")))

    def test_the_input_is_not_changed(self):
        values = self.values("Ittigenstrasse 5")
        split_house_number(values)
        self.assertEqual(values, self.values("Ittigenstrasse 5"))


class SplitInTheSearchTests(SuggestionBase):
    def test_street_with_the_number_gives_the_same_result_as_street_and_number_apart(self):
        together = self.suggest("Ittigenstrasse 5")
        apart = self.suggest("Ittigenstrasse", "5")

        self.assertEqual(self.labels(together["results"]), ["Ittigenstrasse 5, 3063 Ittigen", "Ittigenstrasse 51, 3063 Ittigen"])
        self.assertEqual(together["results"], apart["results"])
        self.assertEqual((together["kind"], together["more"]), (apart["kind"], apart["more"]))

    def test_the_split_applies_with_a_postal_code_and_a_town_too(self):
        answer = self.suggest("ittigenstrasse 1", "", "3063", "Ittigen")

        self.assertEqual(
            self.labels(answer["results"]), ["Ittigenstrasse 11, 3063 Ittigen", "Ittigenstrasse 15, 3063 Ittigen"],
        )

    def test_a_street_whose_name_ends_in_a_number_is_found_as_typed_and_not_cut(self):
        # "Zone 7" is a street; cut, it would be the house 7 of the street "Zone".
        answer = self.suggest("Zone 7")
        self.assertEqual(self.labels(answer["results"]), ["Zone 7 1, 3063 Ittigen", "Zone 7 2, 3063 Ittigen"])
        # Without the digit it is the other street, and with its house number the split finds house 7 of "Zone".
        self.assertEqual(self.labels(self.suggest("Zone", "7")["results"]), ["Zone 7, 3063 Ittigen"])
        self.assertEqual(self.labels(self.suggest("Strada 53")["results"]), ["Strada 53 1, 6900 Lugano", "Strada 53 2, 6900 Lugano"])

    def test_a_street_with_digits_inside_stays_whole_and_a_number_after_it_is_still_split(self):
        self.assertEqual(
            self.labels(self.suggest("Rue du 1er-Aout")["results"]),
            ["Rue du 1er-Aout 3, 1204 Genève", "Rue du 1er-Aout 5, 1204 Genève"],
        )
        self.assertEqual(self.labels(self.suggest("Rue du 1er-Aout 5")["results"]), ["Rue du 1er-Aout 5, 1204 Genève"])

    def test_a_street_that_does_not_exist_with_a_number_finds_nothing_and_changes_nothing(self):
        answer = self.suggest("Nirgendwoweg 5")
        self.assertEqual((answer["results"], answer["more"]), ([], False))

    def test_a_filled_house_number_field_is_not_split(self):
        # "Ittigenstrasse 5" as the street and house number 51: the street as typed does not exist, and the number is not cut.
        self.assertEqual(self.suggest("Ittigenstrasse 5", "51")["results"], [])


class MoreTests(SuggestionBase):
    """B2: the server reads one row beyond the limit, so that the foot line of the list is exact (A5)."""

    def rows(self, count):
        return [("Mehrweg", str(number), "3063", "Ittigen", *CENTRES[("3063", "Ittigen")]) for number in range(1, count + 1)]

    def test_exactly_the_limit_has_no_more_and_one_beyond_has(self):
        self.add(self.rows(services.RESULT_LIMIT))
        exact = self.suggest("Mehrweg")
        self.assertEqual((len(exact["results"]), exact["more"]), (services.RESULT_LIMIT, False))
        self.add([("Mehrweg", "99", "3063", "Ittigen", *CENTRES[("3063", "Ittigen")])])
        beyond = self.suggest("Mehrweg")
        self.assertEqual((len(beyond["results"]), beyond["more"]), (services.RESULT_LIMIT, True))

    def test_the_answer_is_trimmed_to_the_limit_and_less_than_the_limit_has_no_more(self):
        self.add(self.rows(3))
        answer = self.suggest("Mehrweg")
        self.assertEqual((len(answer["results"]), answer["more"]), (3, False))

    def test_the_other_modes_carry_the_key(self):
        self.assertIs(self.check("Ittigenstrasse", "5", "3063", "Ittigen")["more"], False)
        self.assertIs(lookup({"postal_code": "3063"}, mode="locality")["more"], False)

    def test_towns_have_more_when_a_town_prefix_matches_more_places_than_the_limit(self):
        self.add([("Ortsweg", "1", f"{4000 + number}", f"Ortsdorf{number:02d}", 2600000, 1200000) for number in range(12)])
        rows = lookup({"locality": "Ortsdorf"}, mode="locality")
        self.assertEqual((len(rows["results"]), rows["more"]), (services.RESULT_LIMIT, True))

    def test_the_approximate_search_has_more_when_more_streets_fit(self):
        # A typing mistake of a street whose addresses exceed the limit.
        self.add([("Mehrstrasse", str(n), "3063", "Ittigen", *CENTRES[("3063", "Ittigen")]) for n in range(1, 13)])
        answer = self.suggest("Mehrstrase", "", "3063", approximate=lambda: True)
        self.assertEqual((answer["kind"], len(answer["results"]), answer["more"]), ("approximate", services.RESULT_LIMIT, True))


class RankedRowsTests(SuggestionBase):
    def query(self):
        return Address.objects.filter(generation=self.generation, street_key="hauptstrasse")

    def test_the_rows_of_the_area_come_first_then_the_others(self):
        self.add([("Hauptstrasse", "3", "3063", "Ittigen", *CENTRES[("3063", "Ittigen")])])
        rows, more = ranked_rows(
            self.query(), HOME, order=("street_key", "house_number_key"), columns=("street", "house_number", "locality"),
        )
        self.assertEqual([row["locality"] for row in rows], ["Ittigen", "Bern", "Bern"])
        self.assertFalse(more)

    def test_more_is_true_only_when_a_row_beyond_the_limit_exists(self):
        columns = ("street", "house_number")
        order = ("street_key", "house_number_key")
        rows, more = ranked_rows(self.query(), HOME, order=order, columns=columns, limit=2)
        self.assertEqual((len(rows), more), (2, False))
        rows, more = ranked_rows(self.query(), HOME, order=order, columns=columns, limit=1)
        self.assertEqual((len(rows), more), (1, True))
        rows, more = ranked_rows(self.query(), None, order=order, columns=columns, limit=1)
        self.assertEqual((len(rows), more), (1, True))

    def test_the_area_rows_alone_can_fill_the_limit(self):
        self.add([("Hauptstrasse", str(n), "3063", "Ittigen", *CENTRES[("3063", "Ittigen")]) for n in range(10, 14)])
        rows, more = ranked_rows(
            self.query(), HOME, order=("street_key", "house_number_key"), columns=("house_number", "locality"), limit=3,
        )
        self.assertEqual([row["locality"] for row in rows], ["Ittigen"] * 3)
        self.assertTrue(more)


class FixReasonTests(SuggestionBase):
    """E1, F1 to F9: one reason at a time, up to three complete addresses."""

    def proposals(self, *values, **options):
        answer = self.check(*values, **options)
        self.assertEqual(answer["match"], False)
        self.assertTrue(answer["warning"])
        return answer["reason"], self.labels(answer["proposals"])

    def test_a_postal_code_that_does_not_exist_offers_the_address_with_the_right_one(self):
        self.assertEqual(
            self.proposals("ittigenstrasse", "51", "3061", "Ittigen"),
            ("postal_code", ["Ittigenstrasse 51, 3063 Ittigen"]),
        )

    def test_a_postal_code_of_another_place_offers_the_places_that_have_the_address_nearest_first(self):
        reason, labels = self.proposals("Teststrasse", "7", "3063", "Ittigen")

        self.assertEqual(reason, "postal_code")
        # Bern is 4 km away, Zollikofen 11 km (the postal code 3052 is nearer by NUMBER than 3000), Zuerich far away.
        self.assertEqual(
            labels,
            ["Teststrasse 7, 3000 Bern", "Teststrasse 7, 3052 Zollikofen", "Teststrasse 7, 8001 Zürich"],
        )

    def test_a_house_number_that_does_not_exist_offers_the_closest_existing_numbers(self):
        reason, labels = self.proposals("Ittigenstrasse", "151", "3063", "Ittigen")

        self.assertEqual(reason, "house_number")
        self.assertLessEqual(len(labels), 3)
        self.assertIn("Ittigenstrasse 15, 3063 Ittigen", labels)
        self.assertIn("Ittigenstrasse 51, 3063 Ittigen", labels)
        for label in labels:
            self.assertTrue(label.startswith("Ittigenstrasse "))
            self.assertTrue(label.endswith(", 3063 Ittigen"))

    def test_a_letter_after_a_number_that_does_not_exist_offers_the_number(self):
        self.assertEqual(
            self.proposals("Ittigenstrasse", "51a", "3063", "Ittigen"),
            ("house_number", ["Ittigenstrasse 51, 3063 Ittigen"]),
        )

    def test_a_town_that_does_not_belong_to_the_postal_code_offers_the_town_that_does(self):
        self.assertEqual(
            self.proposals("Ittigenstrasse", "51", "3063", "Bern"),
            ("town", ["Ittigenstrasse 51, 3063 Ittigen"]),
        )

    def test_a_typing_mistake_in_the_street_offers_the_street(self):
        for postal_code, town in (("3063", "Ittigen"), ("", "Ittigen"), ("3063", "")):
            with self.subTest(postal_code=postal_code, town=town):
                self.assertEqual(
                    self.proposals("Ittigenstrase", "51", postal_code, town, approximate=True),
                    ("street", ["Ittigenstrasse 51, 3063 Ittigen"]),
                )

    def test_a_street_known_only_elsewhere_with_a_number_that_exists_nowhere_names_the_street(self):
        reason, labels = self.proposals("Ittigenstrasse", "999", "8001", "Zürich")

        self.assertEqual(reason, "street")
        self.assertTrue(labels)
        self.assertTrue(all(label.startswith("Ittigenstrasse ") for label in labels))

    def test_a_correct_address_has_no_proposals(self):
        answer = self.check("Ittigenstrasse", "51", "3063", "Ittigen")

        self.assertEqual((answer["match"], answer["proposals"], answer["reason"], answer["warning"]), (True, [], "", ""))

    def test_an_address_with_only_a_different_case_or_accent_is_correct_and_has_no_proposals(self):
        answer = self.check("ITTIGENSTRASSE", "51", "3063", "ITTIGEN")

        self.assertEqual((answer["match"], answer["proposals"]), (True, []))

    def test_one_reason_at_a_time_the_address_that_fixes_the_most_is_offered(self):
        # Postal code AND town wrong: one card with the complete address, not a stack of cards.
        answer = self.check("Ittigenstrasse", "51", "3061", "Bern")

        self.assertEqual(self.labels(answer["proposals"]), ["Ittigenstrasse 51, 3063 Ittigen"])

    def test_every_proposal_is_a_complete_address_with_the_four_fields(self):
        for proposal in self.check("Teststrasse", "7", "3063", "Ittigen")["proposals"]:
            self.assertEqual(set(proposal), {"street", "house_number", "postal_code", "locality"})
            self.assertTrue(all(proposal.values()))

    def test_the_proposals_are_public_directory_rows_only(self):
        answer = self.check("Ittigenstrasse", "151", "3063", "Ittigen")

        for proposal in answer["proposals"]:
            self.assertTrue(
                Address.objects.filter(
                    generation=self.generation, street=proposal["street"], house_number=proposal["house_number"],
                    postal_code=proposal["postal_code"], locality=proposal["locality"],
                ).exists()
            )


class FixLimitsTests(SuggestionBase):
    def test_at_most_three_proposals(self):
        # Six numbers start with the typed "1": the card offers three.
        answer = self.check("Zahlenweg", "1", "3063", "Ittigen")

        self.assertEqual(answer["reason"], "house_number")
        self.assertEqual(len(answer["proposals"]), 3)
        self.assertEqual(len(answer["proposals"]), services.PROPOSAL_LIMIT)

    def test_an_empty_house_number_gives_no_proposals_but_the_notice_stays(self):
        answer = self.check("Ittigenstrasse", "", "3061", "Ittigen")

        self.assertEqual((answer["proposals"], answer["reason"]), ([], ""))
        self.assertEqual(answer["match"], False)
        self.assertTrue(answer["warning"])

    def test_too_few_fields_to_judge_give_no_proposals(self):
        for values in (
            ("", "51", "3061", "Ittigen"),  # no street
            ("Ittigenstrasse", "51", "", ""),  # no place at all
            ("Ittigenstrasse", "51", "306", "It"),  # a partial postal code and a town of two characters
        ):
            with self.subTest(values=values):
                self.assertEqual(self.check(*values)["proposals"], [])

    def test_a_stale_directory_gives_no_proposal_and_no_warning(self):
        Directory.objects.filter(pk=1).update(data_date=timezone.localdate() - timedelta(days=93))
        answer = self.check("ittigenstrasse", "51", "3061", "Ittigen")

        self.assertEqual((answer["proposals"], answer["reason"], answer["warning"]), ([], "", ""))

    def test_the_boundary_of_the_stale_directory_still_gives_proposals(self):
        Directory.objects.filter(pk=1).update(data_date=timezone.localdate() - timedelta(days=92))

        self.assertTrue(self.check("ittigenstrasse", "51", "3061", "Ittigen")["proposals"])

    def test_a_missing_directory_gives_nothing(self):
        Directory.objects.all().delete()
        answer = self.check("ittigenstrasse", "51", "3061", "Ittigen")

        self.assertEqual((answer["status"], answer["proposals"], answer["warning"]), ("missing", [], ""))

    def test_a_passed_deadline_returns_no_card(self):
        with patch.object(services, "FIX_TIME_BUDGET", -1):
            answer = self.check("ittigenstrasse", "51", "3061", "Ittigen")
            direct = fixes(
                self.generation, Address.objects.filter(generation=self.generation),
                {"street": "ittigenstrasse", "house_number": "51", "postal_code": "3061", "locality": "Ittigen"},
                HOME, True,
            )

        self.assertEqual((answer["proposals"], answer["reason"]), ([], ""))
        self.assertEqual(direct, ("", []))
        # The notice itself stays: only the fix is skipped.
        self.assertTrue(answer["warning"])

    def test_fix_false_asks_for_no_proposal_and_makes_no_query_for_it(self):
        with CaptureQueriesContext(connections["addresses"]) as with_fix:
            self.check("ittigenstrasse", "51", "3061", "Ittigen")
        with CaptureQueriesContext(connections["addresses"]) as without:
            answer = self.check("ittigenstrasse", "51", "3061", "Ittigen", fix=False)

        self.assertEqual((answer["proposals"], answer["reason"]), ([], ""))
        self.assertTrue(answer["warning"])
        self.assertLess(len(without), len(with_fix))
        self.assertFalse([q for q in without.captured_queries if "addresses_addressstreet" in q["sql"]])

    def test_the_check_of_a_correct_address_reads_no_lookup_table(self):
        with CaptureQueriesContext(connections["addresses"]) as queries:
            self.check("Ittigenstrasse", "51", "3063", "Ittigen")
        self.assertFalse([q for q in queries.captured_queries if "addresses_addressstreet" in q["sql"]])

    def test_an_unknown_street_needs_the_approximate_budget_and_asks_it_once(self):
        calls = []

        def budget():
            calls.append(1)
            return True

        answer = self.check("Ittigenstrase", "51", "3063", "Ittigen", approximate=budget)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.labels(answer["proposals"]), ["Ittigenstrasse 51, 3063 Ittigen"])

    def test_a_known_street_never_asks_the_approximate_budget(self):
        calls = []
        self.check("ittigenstrasse", "51", "3061", "Ittigen", approximate=lambda: calls.append(1) or True)
        self.assertEqual(calls, [])

    def test_a_refused_budget_gives_no_proposal_for_a_typing_mistake_and_the_notice_stays(self):
        answer = self.check("Ittigenstrase", "51", "3063", "Ittigen", approximate=False)

        self.assertEqual((answer["proposals"], answer["reason"]), ([], ""))
        self.assertTrue(answer["warning"])

    def test_a_street_in_too_many_places_gives_no_card_instead_of_a_scan(self):
        with patch.object(services, "PLACES_CAP", 2):
            answer = self.check("Teststrasse", "7", "3063", "Ittigen")

        self.assertEqual(answer["proposals"], [])
        self.assertTrue(answer["warning"])


class FixRankingTests(SuggestionBase):
    def test_places_are_ranked_by_the_distance_of_their_centres(self):
        proposals = self.check("Teststrasse", "7", "3063", "Ittigen")["proposals"]

        self.assertEqual([row["locality"] for row in proposals], ["Bern", "Zollikofen", "Zürich"])

    def test_the_home_area_does_not_beat_the_distance(self):
        # Bern as the area of the LEG: Bern was first anyway; with the home in Zuerich it must not jump ahead of Bern.
        answer = lookup(
            {"street": "Teststrasse", "house_number": "7", "postal_code": "3063", "locality": "Ittigen"},
            mode="check", home=("8001", "Zürich"),
        )

        self.assertEqual([row["locality"] for row in answer["proposals"]], ["Bern", "Zollikofen", "Zürich"])


class FixRankingWithoutCoordinatesTests(SuggestionBase):
    COORDINATES = False

    def test_without_coordinates_the_places_are_ranked_by_the_number_of_the_postal_code(self):
        proposals = self.check("Teststrasse", "7", "3063", "Ittigen")["proposals"]

        # 3052 is 11 away from 3063, 3000 is 63 away.
        self.assertEqual([row["locality"] for row in proposals], ["Zollikofen", "Bern", "Zürich"])

    def test_the_fixes_still_work_for_a_wrong_postal_code(self):
        self.assertEqual(
            self.labels(self.check("ittigenstrasse", "51", "3061", "Ittigen")["proposals"]),
            ["Ittigenstrasse 51, 3063 Ittigen"],
        )


class BoundsTests(SuggestionBase):
    def test_prerank_keeps_at_most_the_cap_of_house_numbers_and_the_nearest_first(self):
        houses = [str(number) for number in range(1, 501)]

        kept = prerank("250", houses)
        self.assertEqual(len(kept), HOUSE_CAP)
        self.assertEqual(kept[0], "250")
        self.assertIn("249", kept)
        self.assertNotIn("1", kept)
        self.assertEqual(len(prerank("250", houses, cap=5)), 5)

    def test_a_street_with_thousands_of_numbers_costs_the_capped_comparisons_only(self):
        houses = {str(number) for number in range(1, 2001)}
        with patch.object(services, "closest", wraps=services.closest) as closest:
            services.nearest_number("777", houses)

        self.assertLessEqual(len(closest.call_args.args[1]), HOUSE_CAP)

    def test_similar_streets_are_cut_at_the_cap(self):
        self.add([(f"Testallee{number:03d}", "1", "3063", "Ittigen", *CENTRES[("3063", "Ittigen")]) for number in range(20)])
        places = AddressStreet.objects.filter(generation=self.generation, postal_code="3063")
        import time

        with patch.object(services, "SIMILAR_CAP", 5):
            streets, kind, bounded_out = similar_streets(places, "Testallee", time.monotonic() + 5)
        self.assertEqual((len(streets), kind, bounded_out), (5, "prefix", False))
        streets, _kind, _out = similar_streets(places, "Testallee", time.monotonic() + 5)
        self.assertEqual(len(streets), 20)
        self.assertLessEqual(len(streets), SIMILAR_CAP)

    def test_similar_streets_fail_closed_after_the_deadline_or_above_the_street_limit(self):
        import time

        places = AddressStreet.objects.filter(generation=self.generation)
        self.assertEqual(similar_streets(places, "Ittigenstrase", time.monotonic() - 1), ([], "approximate", True))
        with patch.object(services, "APPROXIMATE_STREET_LIMIT", 2):
            self.assertEqual(similar_streets(places, "Ittigenstrase", time.monotonic() + 5), ([], "approximate", True))

    def test_text_shorter_than_three_characters_finds_no_similar_street(self):
        import time

        places = AddressStreet.objects.filter(generation=self.generation)
        self.assertEqual(similar_streets(places, "It", time.monotonic() + 5), ([], "approximate", False))


@tag("mariadb")
class LookupEndpointTests(SuggestionBase):
    def post(self, values, client=None, **extra):
        return (client or Client()).post(URL, urlencode(values), content_type=FORM, **extra)

    def test_post_only_with_csrf_a_small_body_and_no_store(self):
        protected = Client(enforce_csrf_checks=True)
        self.assertEqual(protected.get(URL).status_code, 405)
        self.assertEqual(protected.post(URL, urlencode({"mode": "check", "street": "x"}), content_type=FORM).status_code, 403)
        # 4 KiB: the middleware refuses a bigger body before any parsing, the view refuses it again (defence in depth).
        too_long = self.post({"mode": "suggest", "street": "Ittigen", "pad": "x" * 5000})
        self.assertEqual(too_long.status_code, 413)
        padded = self.post({"mode": "suggest", "street": "ittigen", "country": "CH", "pad": "x" * 3000})
        self.assertEqual(padded.status_code, 200)
        from django.test import RequestFactory

        from leg_platform.addresses.views import address_lookup

        request = RequestFactory().post(URL, urlencode({"street": "ittigen", "pad": "x" * 5000}), content_type=FORM)
        request.user = create_active_user("body.limit@example.org")
        request._dont_enforce_csrf_checks = True
        with patch("leg_platform.addresses.views.allow_request", return_value=True):
            response = address_lookup(request)
        self.assertEqual(response.status_code, 400)
        self.assertIn("no-store", response["Cache-Control"])
        answer = self.post({"mode": "suggest", "street": "ittigen", "country": "CH"})
        self.assertEqual(answer.status_code, 200)
        self.assertIn("no-store", answer["Cache-Control"])
        self.assertIn("private", answer["Cache-Control"])

    def test_the_answer_has_the_old_keys_and_the_new_ones(self):
        for mode, values in (
            ("suggest", {"street": "ittigen"}),
            ("check", {"street": "ittigenstrasse", "house_number": "51", "postal_code": "3061", "locality": "Ittigen"}),
            ("locality", {"postal_code": "3063"}),
        ):
            with self.subTest(mode=mode):
                answer = self.post({"mode": mode, "country": "CH", **values}).json()
                self.assertEqual(set(answer), ANSWER_KEYS)

    def test_the_check_answer_carries_the_proposals_and_the_reason(self):
        client = Client()
        client.force_login(create_active_user("fix.person@example.org"))
        answer = self.post(
            {"mode": "check", "country": "CH", "street": "ittigenstrasse", "house_number": "51", "postal_code": "3061",
             "locality": "Ittigen"},
            client,
        ).json()

        self.assertEqual((answer["status"], answer["match"], answer["reason"]), ("available", False, "postal_code"))
        self.assertEqual(
            answer["proposals"], [{"street": "Ittigenstrasse", "house_number": "51", "postal_code": "3063", "locality": "Ittigen"}]
        )
        self.assertTrue(answer["warning"])

    def test_a_country_other_than_switzerland_is_not_checked_and_has_all_keys(self):
        for mode in ("suggest", "check", "locality"):
            with self.subTest(mode=mode):
                answer = self.post({"mode": mode, "country": "DE", "street": "Ittigenstrasse", "postal_code": "3063"}).json()
                self.assertEqual(answer["status"], "not_checked")
                self.assertEqual(set(answer), ANSWER_KEYS)
                self.assertEqual((answer["results"], answer["proposals"], answer["reason"], answer["more"]), ([], [], "", False))

    def test_the_answer_carries_nothing_of_the_account_or_the_instance(self):
        client = Client()
        client.force_login(create_active_user("fix.private@example.org"))
        raw = self.post({"mode": "check", "country": "CH", "street": "ittigenstrasse", "house_number": "51",
                         "postal_code": "3061", "locality": "Ittigen"}, client).content.decode()

        self.assertNotIn("fix.private", raw)

    def test_a_repeated_parameter_and_a_bad_mode_are_refused(self):
        self.assertEqual(
            Client().post(URL, "mode=check&street=a&street=b", content_type=FORM).status_code, 400,
        )
        self.assertEqual(self.post({"mode": "nearby", "street": "x"}).status_code, 400)

    def test_an_anonymous_client_gets_no_card_for_a_typing_mistake_a_signed_in_account_does(self):
        values = {"mode": "check", "country": "CH", "street": "Ittigenstrase", "house_number": "51", "postal_code": "3063",
                  "locality": "Ittigen"}
        anonymous = self.post(values).json()
        client = Client()
        client.force_login(create_active_user("fix.typing@example.org"))
        signed_in = self.post(values, client).json()

        self.assertEqual((anonymous["proposals"], anonymous["reason"]), ([], ""))
        self.assertTrue(anonymous["warning"])
        self.assertEqual(signed_in["reason"], "street")
        self.assertEqual(len(signed_in["proposals"]), 1)

    def test_an_anonymous_client_still_gets_the_fix_for_a_street_the_directory_knows(self):
        answer = self.post({"mode": "check", "country": "CH", "street": "ittigenstrasse", "house_number": "51",
                            "postal_code": "3061", "locality": "Ittigen"}).json()

        self.assertEqual(answer["reason"], "postal_code")

    def test_a_check_of_an_anonymous_client_costs_two_requests_of_the_budget(self):
        calls = []

        def counting(request, **kwargs):
            calls.append(request.POST.get("mode"))
            return True

        with patch("leg_platform.addresses.views.allow_request", side_effect=counting):
            self.post({"mode": "suggest", "street": "ittigen", "country": "CH"})
            self.assertEqual(calls, ["suggest"])
            calls.clear()
            self.post({"mode": "locality", "postal_code": "3063", "country": "CH"})
            self.assertEqual(calls, ["locality"])
            calls.clear()
            self.post({"mode": "check", "street": "ittigenstrasse", "house_number": "51", "country": "CH"})
            self.assertEqual(calls, ["check", "check"])
            calls.clear()
            client = Client()
            client.force_login(create_active_user("fix.cost@example.org"))
            self.post({"mode": "check", "street": "ittigenstrasse", "house_number": "51", "country": "CH"}, client)
            self.assertEqual(calls, ["check"])

    def test_the_real_budget_counts_twice_for_an_anonymous_check(self):
        self.post({"mode": "suggest", "street": "ittigen", "country": "CH"})
        one = RateLimitEvent.objects.count()
        RateLimitEvent.objects.all().delete()
        self.post({"mode": "check", "street": "ittigenstrasse", "house_number": "51", "country": "CH"})

        self.assertGreater(one, 0)
        self.assertEqual(RateLimitEvent.objects.count(), 2 * one)

    def test_a_refused_second_unit_answers_429_with_a_pause(self):
        results = iter([True, False])
        with patch("leg_platform.addresses.views.allow_request", side_effect=lambda request, **kw: next(results)):
            response = self.post({"mode": "check", "street": "ittigenstrasse", "house_number": "51", "country": "CH"})

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_card_is_never_logged(self):
        with self.assertNoLogs("leg_platform.addresses", level="DEBUG"):
            self.post({"mode": "check", "country": "CH", "street": "ittigenstrasse", "house_number": "51", "postal_code": "3061",
                       "locality": "Ittigen"})
