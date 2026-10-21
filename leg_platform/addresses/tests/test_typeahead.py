"""The typing suggestions of every address form (D-111): the ranking of the LEG's own area, the towns of a postal code and
the postal codes of a town, the limits of the endpoint and the budget of an account.

What is proven: a street search lists the addresses of the LEG's area (the postal code and the town of the sender address
in the settings) first and then all others, at most ten; a complete postal code gives its town or towns, a town of three
characters gives its postal codes, both with the LEG's area first; nothing is answered below three characters, for a foreign
country or for a bad value; the queries read through the indexes; the answer is public register data, never cached; the
endpoint refuses a GET, a missing CSRF token and a body of another type; a signed-in account has its own budget (the same
for every address it comes from) and an anonymous client the budget of its address."""

import uuid
from urllib.parse import urlencode

from django.db import connections
from django.test import Client, TestCase, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.addresses.models import Address, Directory
from leg_platform.addresses.services import TYPEAHEAD_MINIMUM, lookup
from leg_platform.addresses.tests.test_directory import sql_of
from leg_platform.community.addressing import home_area
from leg_platform.community.models import ManagementSettings

URL = reverse("addresses:lookup")
FORM = "application/x-www-form-urlencoded"
HOME = ("3063", "Ittigen")

# (street, house number, postal code, town); invented rows, but real postal codes and towns of the canton of Bern.
ROWS = (
    ("Gantrischstrasse", "3", "3000", "Bern"),
    ("Gantrischweg", "1", "3004", "Bern"),
    ("Gantrischweg", "1", "3063", "Ittigen"),
    ("Gantrischweg", "2", "3063", "Ittigen"),
    ("Gantrischweg", "5", "8001", "Z\u00fcrich"),
    ("Hauptstrasse", "1", "3000", "Bern"),
    ("Hauptstrasse", "1", "3001", "Bern"),
    ("Hauptstrasse", "2", "3063", "Ittigen"),
    ("Dorfstrasse", "3", "8001", "Z\u00fcrich"),
    # One postal code with two towns (as in the real register).
    ("Weg", "1", "9990", "Alpha"),
    ("Weg", "2", "9990", "Beta"),
    # Twelve addresses before the home area in the alphabet, one in it: the limit must not hide the home area.
    *(("Testweg", str(number), "5000", "Aarau") for number in range(1, 13)),
    ("Testweg", "99", "3063", "Ittigen"),
)


def post(client, values, **extra):
    return client.post(URL, urlencode(values), content_type=FORM, **extra)


@tag("mariadb")
class TypeaheadBase(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=self.generation, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=len(ROWS),
        )
        Address.objects.bulk_create(
            [
                Address(generation=self.generation, street=street, house_number=number, postal_code=code, locality=town)
                for street, number, code, town in ROWS
            ]
        )

    def places(self, values, mode="suggest", home=None, **extra):
        return [
            (row["postal_code"], row["locality"]) if mode == "locality" else
            (row["street"], row["house_number"], row["postal_code"])
            for row in lookup(values, mode=mode, home=home, **extra)["results"]
        ]


class StreetRankingTests(TypeaheadBase):
    def test_the_home_area_comes_first_then_all_other_places(self):
        values = {"street": "gantrischweg"}
        without = self.places(values)
        ranked = self.places(values, home=HOME)

        # Without an area: the order of the register (street, house number, postal code).
        self.assertEqual(without[:2], [("Gantrischweg", "1", "3004"), ("Gantrischweg", "1", "3063")])
        # With the area of Ittigen: its addresses first, the rest in the same order after them.
        self.assertEqual(
            ranked,
            [("Gantrischweg", "1", "3063"), ("Gantrischweg", "2", "3063"),
             ("Gantrischweg", "1", "3004"), ("Gantrischweg", "5", "8001")],
        )
        self.assertEqual(sorted(ranked), sorted(without))

    def test_the_area_is_the_postal_code_or_the_town_of_the_leg(self):
        values = {"street": "hauptstrasse"}
        self.assertEqual(self.places(values, home=("", "Ittigen"))[0], ("Hauptstrasse", "2", "3063"))
        self.assertEqual(self.places(values, home=("3063", ""))[0], ("Hauptstrasse", "2", "3063"))
        # The town is compared without case and accents; a postal code that is not four digits names no area.
        self.assertEqual(self.places({"street": "gantrischweg"}, home=("", "ZURICH"))[0], ("Gantrischweg", "5", "8001"))
        self.assertEqual(self.places(values, home=("30", "")), self.places(values))

    def test_at_most_ten_addresses_and_the_limit_does_not_hide_the_home_area(self):
        plain = self.places({"street": "testweg"})
        ranked = self.places({"street": "testweg"}, home=HOME)

        self.assertEqual((len(plain), len(ranked)), (10, 10))
        self.assertNotIn(("Testweg", "99", "3063"), plain)
        self.assertEqual(ranked[0], ("Testweg", "99", "3063"))

    def test_the_narrowing_by_house_number_postal_code_and_town_still_applies(self):
        self.assertEqual(
            self.places({"street": "gantrischweg", "postal_code": "8001"}, home=HOME), [("Gantrischweg", "5", "8001")]
        )
        self.assertEqual(
            self.places({"street": "gantrischweg", "house_number": "2"}, home=HOME), [("Gantrischweg", "2", "3063")]
        )
        self.assertEqual(
            self.places({"street": "gantrischweg", "locality": "ber"}, home=HOME), [("Gantrischweg", "1", "3004")]
        )

    def test_the_typing_suggestions_start_at_three_characters_and_the_advice_of_the_community_keeps_two(self):
        self.assertEqual(TYPEAHEAD_MINIMUM, 3)
        self.assertEqual(self.places({"street": "ga"}, minimum=TYPEAHEAD_MINIMUM), [])
        self.assertTrue(self.places({"street": "gan"}, minimum=TYPEAHEAD_MINIMUM))
        self.assertTrue(self.places({"street": "ga"}))  # the server-side advice (``suggest_correction``) asks with two

    def test_a_missing_directory_is_said_to_the_caller_and_gives_no_results(self):
        Directory.objects.all().delete()
        answer = lookup({"street": "gantrischweg"}, home=HOME)

        self.assertEqual((answer["status"], answer["results"]), ("missing", []))


class LocalityTests(TypeaheadBase):
    def test_a_postal_code_gives_its_town(self):
        self.assertEqual(self.places({"postal_code": "3063"}, mode="locality"), [("3063", "Ittigen")])

    def test_a_postal_code_with_several_towns_gives_all_of_them(self):
        self.assertEqual(
            self.places({"postal_code": "9990"}, mode="locality"), [("9990", "Alpha"), ("9990", "Beta")]
        )
        # The LEG's own town first when the postal code is shared.
        self.assertEqual(self.places({"postal_code": "9990"}, mode="locality", home=("", "Beta"))[0], ("9990", "Beta"))

    def test_a_town_gives_its_postal_code_and_a_town_with_several_gives_the_list(self):
        self.assertEqual(self.places({"locality": "Ittigen"}, mode="locality"), [("3063", "Ittigen")])
        self.assertEqual(self.places({"locality": "ber"}, mode="locality"), [("3000", "Bern"), ("3001", "Bern"), ("3004", "Bern")])
        # The postal code of the LEG first.
        self.assertEqual(
            self.places({"locality": "bern"}, mode="locality", home=("3004", ""))[0], ("3004", "Bern")
        )
        # Accents and case are ignored.
        self.assertEqual(self.places({"locality": "zurich"}, mode="locality"), [("8001", "Z\u00fcrich")])

    def test_a_postal_code_prefix_narrows_the_towns_and_a_complete_one_the_typed_town(self):
        self.assertEqual(self.places({"locality": "bern", "postal_code": "300"}, mode="locality"),
                         [("3000", "Bern"), ("3001", "Bern"), ("3004", "Bern")])
        self.assertEqual(self.places({"locality": "bern", "postal_code": "3001"}, mode="locality"), [("3001", "Bern")])
        self.assertEqual(self.places({"locality": "zurich", "postal_code": "3001"}, mode="locality"), [])

    def test_nothing_is_answered_for_less_than_three_characters_a_partial_postal_code_or_a_foreign_country(self):
        for values in ({"locality": "be"}, {"postal_code": "306"}, {"locality": "", "postal_code": ""}):
            with self.subTest(values=values):
                answer = lookup(values, mode="locality")
                self.assertEqual((answer["status"], answer["results"]), ("not_checked", []))
        self.assertEqual(lookup({"postal_code": "3063", "country": "DE"}, mode="locality")["status"], "not_checked")

    def test_a_bad_value_is_refused(self):
        for values in ({"postal_code": "30a3"}, {"locality": "x" * 256}, {"postal_code": "30630"}, {"locality": "a\nb"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                lookup(values, mode="locality")
        with self.assertRaises(ValueError):
            lookup({"postal_code": "3063"}, mode="nearby")

    def test_the_answer_of_the_locality_mode_has_the_kind_and_only_two_fields(self):
        answer = lookup({"postal_code": "3063"}, mode="locality")

        self.assertEqual(answer["kind"], "locality")
        self.assertEqual(answer["results"], [{"postal_code": "3063", "locality": "Ittigen"}])


class QueryPlanTests(TypeaheadBase):
    PLAN = r"SEARCH addresses_address USING (COVERING )?INDEX addr_generation_"

    def test_the_ranked_street_search_and_the_town_searches_read_through_the_indexes(self):
        scenarios = {
            "street and area": ({"street": "gantrisch"}, "suggest"),
            "street, postal code and area": ({"street": "gantrisch", "postal_code": "30"}, "suggest"),
            "postal code": ({"postal_code": "3063"}, "locality"),
            "town": ({"locality": "bern"}, "locality"),
            "town and postal code": ({"locality": "bern", "postal_code": "300"}, "locality"),
        }
        for name, (values, mode) in scenarios.items():
            with self.subTest(name), CaptureQueriesContext(connections["addresses"]) as queries:
                lookup(values, mode=mode, home=HOME)
            selects = [sql for sql in sql_of(queries) if 'FROM "addresses_address"' in sql]
            self.assertTrue(selects, name)
            for sql in selects:
                with connections["addresses"].cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + sql)
                    plan = " | ".join(row[3] for row in cursor.fetchall())
                with self.subTest(name, sql=sql[:80]):
                    self.assertRegex(plan, self.PLAN)
                    self.assertNotIn("SCAN addresses_address", plan)

    def test_the_search_for_a_town_uses_the_index_of_the_towns(self):
        with CaptureQueriesContext(connections["addresses"]) as queries:
            lookup({"locality": "bern"}, mode="locality")
        plans = []
        for sql in sql_of(queries):
            if 'FROM "addresses_address"' in sql:
                with connections["addresses"].cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + sql)
                    plans.append(" | ".join(row[3] for row in cursor.fetchall()))
        self.assertTrue(plans)
        for plan in plans:
            self.assertIn("addr_generation_locality", plan)


class EndpointTests(TypeaheadBase):
    def set_home(self, postal_code="3063", town="Ittigen"):
        ManagementSettings.objects.update_or_create(
            id=1, defaults={"sender_postal_code": postal_code, "sender_town": town}
        )

    def test_the_leg_area_comes_from_the_sender_address_of_the_settings_and_nothing_is_created_while_reading(self):
        self.assertEqual(home_area(), ("", ""))
        self.assertFalse(ManagementSettings.objects.exists())
        self.set_home("3063", "  Ittigen ")
        self.assertEqual(home_area(), ("3063", "Ittigen"))

    def test_the_endpoint_ranks_the_area_of_the_leg_first(self):
        client = Client()
        values = {"mode": "suggest", "street": "gantrischweg", "country": "CH"}
        self.assertEqual(post(client, values).json()["results"][0]["locality"], "Bern")
        self.set_home()
        answer = post(client, values).json()

        self.assertEqual([row["locality"] for row in answer["results"]], ["Ittigen", "Ittigen", "Bern", "Z\u00fcrich"])
        self.assertEqual(set(answer["results"][0]), {"street", "house_number", "postal_code", "locality"})

    def test_the_endpoint_answers_the_locality_mode(self):
        self.set_home()
        answer = post(Client(), {"mode": "locality", "postal_code": "3063", "country": "CH"}).json()

        self.assertEqual((answer["status"], answer["kind"], answer["results"]),
                         ("available", "locality", [{"postal_code": "3063", "locality": "Ittigen"}]))

    def test_the_answer_is_never_cached_and_carries_no_personal_data(self):
        self.set_home()
        for mode, values in (("suggest", {"street": "gantrischweg"}), ("locality", {"locality": "bern"})):
            with self.subTest(mode=mode):
                response = post(Client(), {"mode": mode, "country": "CH", **values})
                self.assertEqual(response.status_code, 200)
                self.assertIn("no-store", response["Cache-Control"])
                self.assertIn("private", response["Cache-Control"])
                # Register data and the status only: nothing about the account or the instance.
                self.assertEqual(
                    set(response.json()), {"status", "data_date", "stale", "warning", "results", "match", "kind"}
                )

    def test_the_endpoint_is_post_only_checks_the_token_and_refuses_what_is_not_a_form(self):
        protected = Client(enforce_csrf_checks=True)
        self.assertEqual(protected.get(URL).status_code, 405)
        self.assertEqual(protected.post(URL, urlencode({"mode": "locality", "postal_code": "3063"}), content_type=FORM).status_code, 403)
        self.assertEqual(Client().post(URL, {"mode": "locality", "postal_code": "3063"}).status_code, 400)  # multipart

    def test_a_bad_mode_a_long_text_and_repeated_parameters_are_refused(self):
        client = Client()
        for values in (
            {"mode": "nearby", "street": "gantrischweg"},
            {"mode": "locality", "locality": "x" * 300},
            {"mode": "locality", "postal_code": "3063x"},
        ):
            with self.subTest(values=str(values)[:40]):
                self.assertEqual(post(client, values).status_code, 400)
        # A long body is refused before the view (the limit of the body, 413).
        self.assertEqual(post(client, {"mode": "suggest", "street": "x" * 5000}).status_code, 413)
        repeated = client.post(URL, "mode=locality&postal_code=3063&postal_code=3000", content_type=FORM)
        self.assertEqual(repeated.status_code, 400)

    def test_an_anonymous_client_is_served_because_the_registration_uses_the_endpoint_before_an_account_exists(self):
        # D-066: the details step of the registration is used by a person who is not signed in. The answer is public
        # register data; the budget of the address and of the instance, the CSRF token and POST apply.
        response = post(Client(), {"mode": "locality", "postal_code": "3063", "country": "CH"})

        self.assertEqual(response.status_code, 200)


@tag("mariadb")
class BudgetTests(TestCase):
    """The budget of the typing suggestions: per signed-in account, per address for an anonymous client."""

    def values(self):
        return {"mode": "locality", "postal_code": "", "locality": "", "country": "CH"}

    def count_until_refused(self, client, **extra):
        for number in range(1, 400):
            if post(client, self.values(), **extra).status_code == 429:
                return number - 1
        return None

    def test_an_account_has_its_own_budget_that_does_not_depend_on_its_address(self):
        first, second = create_active_user("first.typing@example.org"), create_active_user("second.typing@example.org")
        one, two = Client(), Client()
        one.force_login(first)
        two.force_login(second)

        self.assertEqual(self.count_until_refused(one, REMOTE_ADDR="10.20.30.40"), 120)
        refused = post(one, self.values(), REMOTE_ADDR="10.20.30.41")
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused["Retry-After"], "60")
        self.assertIn("no-store", refused["Cache-Control"])
        # Another account at the same address is not held back by it.
        self.assertEqual(post(two, self.values(), REMOTE_ADDR="10.20.30.40").status_code, 200)

    def test_an_anonymous_client_has_the_budget_of_its_address(self):
        self.assertEqual(self.count_until_refused(Client(), REMOTE_ADDR="10.20.30.50"), 60)
        self.assertEqual(post(Client(), self.values(), REMOTE_ADDR="10.20.30.51").status_code, 200)
