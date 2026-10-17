"""The list framework (D-078): the state of a list from the address, the links that change it, the
pages, the cells of the head, the saved widths and their endpoint (progressive enhancement of the
column widths with a CSRF-protected POST)."""

import json

from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import preferences
from leg_platform.community.models import Location
from leg_platform.community.tests.helpers import make_location, staff_user
from leg_platform.management import lists

COLUMNS = (
    lists.Column("address", "Address"),
    lists.Column("circuit", "Circuit", width=150),
    lists.Column("note", "Note", sortable=False),
)
FILTERS = {"occupancy": (("all", "free"), "all")}


def state(**params):
    return lists.parse(params, path="/management/x/", columns=COLUMNS, default_sort="address", filters=FILTERS)


class ListStateTests(SimpleTestCase):
    def test_the_defaults_give_the_plain_address(self):
        current = state()
        self.assertEqual((current.sort, current.descending, current.per, current.page), ("address", False, "50", 1))
        self.assertEqual(current.url(), "/management/x/")
        self.assertEqual(current.hidden(), [])

    def test_a_value_that_is_not_allowed_falls_back_to_the_default(self):
        current = state(sort="note", per="999", page="-3", occupancy="evil", dir="up", focus="other")
        self.assertEqual((current.sort, current.per, current.page, current.values["occupancy"]), ("address", "50", 1, "all"))
        self.assertFalse(current.descending)
        self.assertEqual(current.focus, "")
        self.assertEqual(state(page="99999999999").page, 1)
        self.assertEqual(state(page="٣").page, 1)  # not an ASCII digit
        for per in ("30", "50", "100", "all"):
            self.assertEqual(state(per=per).per, per)

    def test_the_search_text_is_trimmed_and_limited(self):
        self.assertEqual(state(q="  Bahnhof ").query, "Bahnhof")
        self.assertEqual(len(state(q="x" * 500).query), lists.MAX_QUERY_LENGTH)

    def test_the_links_keep_the_state_and_leave_out_the_defaults(self):
        current = state(q="a b", sort="circuit", dir="desc", per="30", occupancy="free", problems="1", page="3")
        params = current.params()
        self.assertEqual(
            params, {"q": "a b", "occupancy": "free", "sort": "circuit", "dir": "desc", "per": "30", "page": 3, "problems": "1"}
        )
        # Anything but the page goes back to page 1; the page link keeps everything else.
        self.assertNotIn("page", current.params(q="x"))
        self.assertEqual(current.params(page=4)["page"], 4)
        self.assertEqual(state(sort="circuit").url(sort="address"), "/management/x/")
        self.assertEqual(current.hidden(), [("sort", "circuit"), ("dir", "desc"), ("problems", "1")])

    def test_the_head_cells_toggle_ascending_descending_ascending(self):
        cells = {cell["key"]: cell for cell in lists.header_cells(COLUMNS, state(), {"circuit": 222}, "t")}
        self.assertEqual(cells["address"]["url"], "/management/x/?sort=address&dir=desc")
        self.assertEqual((cells["address"]["arrow"], cells["address"]["aria_sort"]), ("↑", "ascending"))
        self.assertEqual((cells["circuit"]["arrow"], cells["circuit"]["aria_sort"]), ("↕", ""))
        self.assertEqual(cells["circuit"]["url"], "/management/x/?sort=circuit")
        self.assertEqual((cells["circuit"]["width"], cells["address"]["width"]), (222, None))
        self.assertEqual((cells["note"]["url"], cells["note"]["sortable"]), ("", False))
        descending = {c["key"]: c for c in lists.header_cells(COLUMNS, state(dir="desc"), {}, "t")}
        self.assertEqual((descending["address"]["arrow"], descending["address"]["url"]), ("↓", "/management/x/"))
        self.assertEqual(cells["circuit"]["width"], 222)
        self.assertEqual(
            {c["key"]: c["width"] for c in lists.header_cells(COLUMNS, state(), {}, "t")}["circuit"], 150
        )


@tag("mariadb")
class PaginateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for number in range(1, 8):
            make_location("Weg", str(number), "3000", "Bern")

    def page(self, per, page=1):
        current = state()
        current.per, current.page = per, page
        return lists.paginate(Location.objects.order_by("pk"), current)

    def test_the_pages_count_and_link(self):
        first = self.page("3")
        self.assertEqual((len(first.items), first.total, first.first, first.last, first.pages), (3, 7, 1, 3, 3))
        self.assertEqual((first.prev_url, first.next_url), ("", "/management/x/?per=3&page=2"))
        last = self.page("3", 3)
        self.assertEqual((len(last.items), last.first, last.last, last.next_url), (1, 7, 7, ""))
        self.assertEqual(last.prev_url, "/management/x/?per=3&page=2")

    def test_a_page_beyond_the_end_shows_the_last_page_and_all_shows_everything(self):
        self.assertEqual(self.page("3", 50).page, 3)
        everything = self.page("all")
        self.assertEqual((len(everything.items), everything.first, everything.last, everything.pages), (7, 1, 7, 1))

    def test_an_empty_list_counts_from_zero(self):
        Location.objects.all().delete()
        empty = self.page("50")
        self.assertEqual((empty.total, empty.first, empty.last, empty.items), (0, 0, 0, []))


@tag("mariadb")
class WidthsEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.person@example.org")
        cls.url = reverse("management:list_widths", args=["locations"])

    def post(self, client, body, url=None):
        return client.post(url or self.url, data=body, content_type="application/json")

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def test_valid_widths_are_saved_for_the_account_of_the_session(self):
        response = self.post(self.login(self.staff), json.dumps({"widths": {"address": 300, "circuit": 120}}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            preferences.get_preference(self.staff, "locations").column_widths, {"address": 300, "circuit": 120}
        )

    def test_wrong_values_are_refused_and_nothing_is_stored(self):
        client = self.login(self.staff)
        for body in (
            json.dumps({"widths": {"address": 10}}),  # below the minimum
            json.dumps({"widths": {"address": "300"}}),
            json.dumps({"widths": {"Bad Key": 300}}),
            json.dumps({"widths": [300]}),
            json.dumps({"other": 1}),
            "not json",
            json.dumps({"widths": {f"c{i}": 100 for i in range(40)}}),
            "x" * 5000,
        ):
            with self.subTest(body=body[:40]):
                self.assertEqual(self.post(client, body).status_code, 400)
        self.assertEqual(preferences.get_preference(self.staff, "locations").column_widths, {})

    def test_an_unknown_list_and_a_get_are_not_served(self):
        client = self.login(self.staff)
        self.assertEqual(self.post(client, "{}", reverse("management:list_widths", args=["nothing"])).status_code, 404)
        self.assertEqual(client.get(self.url).status_code, 405)

    def test_an_account_without_staff_status_is_told_nothing(self):
        plain = self.login(create_active_user("plain.person@example.org"))
        self.assertEqual(self.post(plain, json.dumps({"widths": {"address": 300}})).status_code, 404)

    def test_a_post_without_the_csrf_token_is_refused(self):
        client = self.login(self.staff, enforce_csrf_checks=True)
        response = self.post(client, json.dumps({"widths": {"address": 300}}))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(preferences.get_preference(self.staff, "locations").column_widths, {})
