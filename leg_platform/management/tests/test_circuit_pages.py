"""The pages of the transformer circuits (D-142): the menu entry, the list (figures without a query per circuit, search,
sort, print), the detail page, "Create transformer circuit", "Rename" (the number of an operator circuit is locked) and
the suggestions of the circuit field of the location forms. The access of every route is walked by ``test_access`` and
``test_acceptance_matrix``; here only what the pages add."""

from datetime import date
from unittest import mock

from django.db import connection
from django.test import TestCase, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import history, transformer_circuits
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Direction, TransformerCircuit
from leg_platform.community.tests.helpers import (
    assign_leg,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)


def stations(found):
    """The stations of the address directory (``transformer_circuits.directory_numbers``) as the test needs them."""
    return mock.patch.object(transformer_circuits, "directory_numbers", return_value=found)


@tag("mariadb")
class CircuitPageTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.circuits@example.org")
        cls.plain = create_active_user("plain.circuits@example.org")
        cls.north = make_circuit("TRA21359", "Ittigen Nord")
        cls.hand = make_circuit("TRA9", "Von Hand")
        cls.leg = make_leg("LEG Quartier")
        cls.first = make_location("Musterweg", "12", "3063", "Ittigen", transformer_circuit=cls.north)
        cls.second = make_location("Bachweg", "3", "3063", "Ittigen", transformer_circuit=cls.north)
        make_location("Altweg", "1", "3063", "Ittigen", transformer_circuit=cls.north, is_archived=True)
        cls.consumer = make_metering_point(1, location=cls.first)
        make_metering_point(2, location=cls.first)
        cls.producer = make_metering_point(3, location=cls.second, direction=Direction.FEED_IN)
        make_metering_point(4, location=cls.second, is_archived=True)
        assign_leg(cls.consumer, cls.leg, date(2026, 1, 1))
        assign_leg(cls.producer, cls.leg, date(2026, 1, 1))

    def setUp(self):
        self.client = VerifiedClient()
        self.client.force_login(self.staff)

    def test_the_menu_has_the_entry_after_the_legs(self):
        page = self.client.get(reverse("management:circuits")).content.decode()
        legs, circuits = page.index(reverse("management:legs")), page.index(reverse("management:circuits"))
        self.assertLess(legs, circuits)
        self.assertIn("#i-circuit", page)
        self.assertIn("Trafokreise", page)

    def test_the_list_shows_the_figures_the_source_and_the_legs(self):
        with stations({"TRA21359"}):
            page = self.client.get(reverse("management:circuits")).content.decode()
        for text in ("TRA21359", "Ittigen Nord", "TRA9", "Von Hand", "von Hand", "1 von 2", "1 von 1", "LEG Quartier"):
            self.assertIn(text, page)
        self.assertIn(reverse("management:legs_detail", args=[self.leg.pk]), page)
        self.assertIn(reverse("management:circuits_detail", args=[self.north.pk]), page)
        self.assertIn(reverse("management:circuits_new"), page)
        self.assertIn(reverse("management:circuits_print"), page)
        # The directory cannot answer: the source is unknown, never guessed.
        with stations(None):
            self.assertContains(self.client.get(reverse("management:circuits")), "unbekannt")

    def test_the_list_asks_no_query_per_circuit(self):
        url = reverse("management:circuits")
        with stations(set()), CaptureQueriesContext(connection) as few:
            self.client.get(url)
        for number in range(10, 30):
            make_location("Weg", str(number), "3063", "Ittigen", transformer_circuit=make_circuit(f"TRA{number}"))
        with stations(set()), CaptureQueriesContext(connection) as many:
            self.client.get(url)
        self.assertEqual(len(few.captured_queries), len(many.captured_queries))

    def test_search_and_sort(self):
        url = reverse("management:circuits")
        with stations(set()):
            found = self.client.get(url, {"q": "quartier"}).content.decode()
            self.assertIn("TRA21359", found)
            self.assertNotIn(">TRA9<", found)
            ordered = self.client.get(url, {"sort": "number"}).content.decode()
            self.assertLess(ordered.index(">TRA9<"), ordered.index(">TRA21359<"))  # by the number, not as text
            hostile = self.client.get(url, {"q": "%" * 300, "sort": "x; DROP", "per": "-1"})
            nothing = self.client.get(url, {"q": "nirgendwo"})
        self.assertEqual(hostile.status_code, 200)
        # A search without a hit says so; "none yet" is only for an empty list.
        self.assertContains(nothing, "Keine Trafokreise gefunden.")
        self.assertNotContains(nothing, "Noch keine Trafokreise.")

    def test_the_detail_page(self):
        transformer_circuits.update(self.north, "TRA21359", "Ittigen Nord 2", self.staff)
        with stations({"TRA21359"}):
            page = self.client.get(reverse("management:circuits_detail", args=[self.north.pk])).content.decode()
        for text in ("Ittigen Nord 2", "Musterweg 12", "Bachweg 3", "LEG Quartier", "Stammdaten geändert", "Umbenennen"):
            self.assertIn(text, page)
        self.assertNotIn("Altweg", page)  # archived
        self.assertIn(reverse("management:locations_detail", args=[self.first.pk]), page)
        self.assertIn(reverse("management:circuits_edit", args=[self.north.pk]), page)
        self.assertEqual(self.client.get(reverse("management:circuits_detail", args=[999999])).status_code, 404)
        self.assertEqual(self.login_plain().get(reverse("management:circuits_detail", args=[self.north.pk])).status_code, 404)

    def test_every_circuit_number_links_to_its_page(self):
        # Follow-up of the walk-through (D-142): the lists and pages that show a circuit number open its page.
        target = reverse("management:circuits_detail", args=[self.north.pk])
        with stations({"TRA21359"}):
            pages = {
                "locations": self.client.get(reverse("management:locations")),
                "location": self.client.get(reverse("management:locations_detail", args=[self.first.pk])),
                "point": self.client.get(reverse("management:metering_points_detail", args=[self.consumer.pk])),
                "leg": self.client.get(reverse("management:legs_detail", args=[self.leg.pk])),
            }
        for name, page in pages.items():
            with self.subTest(page=name):
                self.assertContains(page, f'href="{target}')
        self.assertContains(pages["leg"], f'<a class="mgmt-tag" href="{target}">TRA21359</a>')
        self.assertContains(pages["location"], f'<p class="mgmt-subtitle"><a class="tnum" href="{target}?next=')

    def login_plain(self):
        client = VerifiedClient()
        client.force_login(self.plain)
        return client

    def test_a_circuit_is_created_in_the_menu_with_its_history(self):
        url = reverse("management:circuits_new")
        self.assertContains(self.client.get(url), "Trafokreis anlegen")
        for bad in ("555", "TRA", "TRA21359"):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post(url, {"grid_number": bad, "name": ""}).status_code, 200)
        response = self.client.post(url, {"grid_number": " tra777 ", "name": "Neu", "next": reverse("management:circuits")})
        created = TransformerCircuit.objects.get(grid_number="TRA777")
        self.assertRedirects(
            response, reverse("management:circuits_detail", args=[created.pk]) + "?new=1&next=%2Fmanagement%2Ftransformer-circuits%2F",
            fetch_redirect_response=False,
        )
        entry = history.entries_for(created).get()
        self.assertEqual((entry.event, entry.actor), (HistoryEvent.CREATED, self.staff))

    def test_an_operator_circuit_is_renamed_but_keeps_its_number(self):
        url = reverse("management:circuits_edit", args=[self.north.pk])
        for found in ({"TRA21359"}, None):  # the operator's station, or the directory cannot answer
            with self.subTest(found=found), stations(found):
                page = self.client.get(url).content.decode()
                self.assertNotIn('name="grid_number"', page)
                self.assertIn("Die Änderung betrifft 3 Standorte.", page)
                self.client.post(url, {"grid_number": "TRA1", "name": f"Neu {found is None}"})
            self.north.refresh_from_db()
            self.assertEqual(self.north.grid_number, "TRA21359")
        self.assertEqual(self.north.name, "Neu True")

    def test_a_circuit_entered_by_hand_changes_number_and_name(self):
        url = reverse("management:circuits_edit", args=[self.hand.pk])
        with stations({"TRA21359"}):
            self.assertContains(self.client.get(url), 'name="grid_number"')
            taken = self.client.post(url, {"grid_number": "TRA21359", "name": ""})
            self.assertContains(taken, "Diesen Trafokreis gibt es schon.")
            self.client.post(url, {"grid_number": "tra10", "name": "Zehn"})
        self.hand.refresh_from_db()
        self.assertEqual((self.hand.grid_number, self.hand.name), ("TRA10", "Zehn"))
        entry = history.entries_for(self.hand).get()
        self.assertEqual(entry.detail, {"fields": ["grid_number", "name"], "old": "TRA9", "new": "TRA10"})

    def test_a_circuit_keeps_its_number_while_its_source_is_unknown(self):
        # The directory cannot answer: a circuit entered by hand might be the operator's, so its number stays locked.
        url = reverse("management:circuits_edit", args=[self.hand.pk])
        with stations(None):
            self.assertNotContains(self.client.get(url), 'name="grid_number"')
            self.client.post(url, {"grid_number": "TRA10", "name": "Zehn"})
        self.hand.refresh_from_db()
        self.assertEqual((self.hand.grid_number, self.hand.name), ("TRA9", "Zehn"))

    def test_the_suggestions_start_with_the_automatic_choice_and_list_every_circuit(self):
        url = reverse("management:circuits_search")
        with stations({"TRA21359", "TRA500"}):
            everything = self.client.get(url, {"q": "", "location": str(self.first.pk)}).json()["results"]
            self.assertEqual(
                everything,
                [
                    "Automatisch (gemäss Netzbetreiber): TRA21359", "TRA9 · Von Hand · von Hand",
                    "TRA500 · Netzbetreiber", "TRA21359 · Ittigen Nord · Netzbetreiber",
                ],
            )
            self.assertEqual(self.client.get(url, {"q": "tra5"}).json()["results"], ["TRA500 · Netzbetreiber"])
            self.assertEqual(self.client.get(url, {"q": "auto"}).json()["results"], ["Automatisch (gemäss Netzbetreiber)"])
            for location in ("abc", "-1", "9" * 40, "999999"):
                with self.subTest(location=location):
                    first = self.client.get(url, {"q": "", "location": location}).json()["results"][0]
                    self.assertEqual(first, "Automatisch (gemäss Netzbetreiber)")
        for number in range(100, 120):
            make_circuit(f"TRA{number}")
        with stations(set()):
            self.assertEqual(len(self.client.get(url, {"q": ""}).json()["results"]), 10)

    def test_the_list_and_the_details_are_printed(self):
        url = reverse("management:circuits_print")
        with stations({"TRA21359"}):
            self.assertEqual(self.client.get(url).status_code, 200)
            listed = self.client.post(url, {"scope": "list"})
            details = self.client.post(url, {"scope": "all"})
        for response in (listed, details):
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertTrue(response.content.startswith(b"%PDF"))

