"""The page Buildings with several cards: the order, the folding cards (``<details class="fold">``), the card that stays open
(edit mode, a failed save, just saved) and the search tools from ``overview.SEARCH_MIN_CARDS`` cards on."""

import re
from datetime import date, timedelta

from django.test import TestCase, tag

from leg_platform.community.models import CustomerBuilding, Direction
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point
from leg_platform.customer import overview
from leg_platform.customer.tests.helpers import BUILDINGS, customer, login
from leg_platform.customer.tests.test_card_edit import post_data

LONG_AGO = date.today() - timedelta(days=400)
FUTURE = date.today() + timedelta(days=30)


def add_building(person, last, street, name="", participating=True):
    """A building of the account with one draw point; ``participating=False`` = start date still to come."""
    location = make_location(street, str(last), "3063", "Ittigen", egid=90000000 + last)
    when = LONG_AGO if participating else FUTURE
    point = make_metering_point(
        last, Direction.CONSUMPTION, location,
        registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO, start_date=when,
    )
    assign_person(point, person, valid_from=LONG_AGO)
    if name:
        CustomerBuilding.objects.create(person=person, location=location, name=name)
    return location, point


def folds(html):
    """``[(is_open, text of the card)]`` of every fold in page order."""
    chunks = html.split('<details class="fold"')[1:]
    return [(chunk.startswith(" open"), chunk) for chunk in chunks]


def headings(html):
    return re.findall(r'<h2 id="building-\d+(?:-title)?">([^<]*)</h2>', html)


@tag("mariadb")
class BuildingsFoldTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("fold.mine@example.org")
        self.client = login(self.mine)

    def page(self, url=BUILDINGS):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_one_card_is_shown_open_and_does_not_fold(self):
        add_building(self.mine, 1, "Alleeweg")
        html = self.page()
        self.assertIn('class="building-card"', html)
        self.assertNotIn("<details", html)
        self.assertNotIn("is-fold", html)
        self.assertNotIn("data-building-tools", html)

    def test_two_or_more_cards_fold_and_are_closed(self):
        add_building(self.mine, 1, "Alleeweg")
        add_building(self.mine, 2, "Bahnhofstrasse")
        html = self.page()
        self.assertEqual(html.count("building-card is-fold"), 2)
        found = folds(html)
        self.assertEqual(len(found), 2)
        self.assertFalse(any(is_open for is_open, _ in found))
        self.assertIn('class="fold-summary"', html)
        self.assertIn("Alleeweg 1", found[0][1])

    def test_not_yet_participating_first_participating_last_each_by_name_else_address(self):
        add_building(self.mine, 1, "Zeta", participating=True)
        add_building(self.mine, 2, "Beta", name="Anbau", participating=True)
        add_building(self.mine, 3, "Yps", participating=False)
        add_building(self.mine, 4, "Alpha", name="Zelt", participating=False)
        add_building(self.mine, 5, "Aaa", participating=True)
        order = headings(self.page())
        self.assertEqual(order, ["Yps 3", "Zelt", "Aaa 5", "Anbau", "Zeta 1"])

    def test_the_card_in_edit_mode_is_open_the_others_are_closed(self):
        first, _ = add_building(self.mine, 1, "Alleeweg")
        add_building(self.mine, 2, "Bahnhofstrasse")
        found = folds(self.page(f"{BUILDINGS}?edit=l{first.pk}"))
        self.assertEqual([is_open for is_open, _ in found], [True, False])

    def test_a_failed_save_keeps_the_card_open_with_its_errors(self):
        first, _ = add_building(self.mine, 1, "Alleeweg")
        add_building(self.mine, 2, "Bahnhofstrasse")
        url = f"{BUILDINGS}?edit=l{first.pk}"
        feed_in = make_metering_point(
            3, Direction.FEED_IN, first, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO, start_date=LONG_AGO,
        )
        assign_person(feed_in, self.mine, valid_from=LONG_AGO)
        state = self.client.get(url).context["overview"].cards[0].editing
        form = next(form for form in state.forms if "pv_power_kwp" in form.fields)
        response = self.client.post(url, post_data(state, **{form.add_prefix("pv_power_kwp"): "-4"}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("error-summary", html)
        self.assertEqual([is_open for is_open, _ in folds(html)], [True, False])

    def test_the_card_just_saved_is_open_once(self):
        first, _ = add_building(self.mine, 1, "Alleeweg")
        add_building(self.mine, 2, "Bahnhofstrasse")
        url = f"{BUILDINGS}?edit=l{first.pk}"
        state = self.client.get(url).context["overview"].cards[0].editing
        response = self.client.post(url, post_data(state, **{"b-name": "Mein Haus"}))
        self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
        after = folds(self.page())
        # "Mein Haus" sorts after "Bahnhofstrasse 2".
        self.assertEqual([is_open for is_open, _ in after], [False, True])
        self.assertIn("Mein Haus", after[1][1])
        self.assertEqual([is_open for is_open, _ in folds(self.page())], [False, False])

    def test_the_search_tools_come_with_the_threshold_and_not_before(self):
        for n in range(1, overview.SEARCH_MIN_CARDS):
            add_building(self.mine, n, f"Weg{n}")
        below = self.page()
        self.assertEqual(len(folds(below)), overview.SEARCH_MIN_CARDS - 1)
        for part in ("data-building-tools", "data-building-search", "data-search=", "buildings-filter.js"):
            self.assertNotIn(part, below)
        add_building(self.mine, overview.SEARCH_MIN_CARDS, "Weg8")
        html = self.page()
        self.assertEqual(overview.SEARCH_MIN_CARDS, 8)
        self.assertIn("data-building-tools hidden", html)
        self.assertIn("data-building-search", html)
        self.assertIn("data-expand-all", html)
        self.assertIn("data-collapse-all", html)
        self.assertIn("data-building-empty", html)
        self.assertIn("Alle aufklappen", html)
        self.assertIn("Alle zuklappen", html)
        self.assertIn("Keine Gebäude passen zu Ihrer Suche.", html)
        self.assertIn("customer/js/buildings-filter.js", html)
        self.assertEqual(html.count(' data-search="'), 8)
        # No inline script (CSP 'self').
        self.assertNotRegex(html, r"(?i)<script(?![^>]*\ssrc=\")")

    def test_the_search_text_holds_only_what_the_card_shows_of_the_own_account(self):
        other = customer("fold.other@example.org")
        for n in range(1, 9):
            location, point = add_building(self.mine, n, f"Weg{n}", name=f"Haus{n}")
        # A foreign point at the last building: its number is never in the page.
        foreign = make_metering_point(
            99, Direction.CONSUMPTION, location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO, start_date=LONG_AGO,
        )
        assign_person(foreign, other, valid_from=LONG_AGO)
        html = self.page()
        self.assertIn(f'data-search="Haus8 Weg8 8, 3063 Ittigen 90000008 {point.number}"', html)
        self.assertNotIn(foreign.number, html)
