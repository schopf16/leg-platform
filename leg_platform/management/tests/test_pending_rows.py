"""Buildings and metering points of submitted applications on the lists of locations and metering points (owner
feedback 2026-11-01: the administrator corrects an address at the address, not only on the application)."""

from unittest.mock import patch
from urllib.parse import urlencode

from django.test import tag
from django.urls import reverse

from leg_platform.community import location_list, preferences
from leg_platform.community.models import Application, ApplicationBuilding
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, number_markup
from leg_platform.community.tests.onboarding import TODAY, full_number
from leg_platform.management import location_views, pending
from leg_platform.management.tests.test_process_corrections import CorrectionBase


@tag("mariadb")
class PendingRowsTests(CorrectionBase):
    LOCATIONS = reverse("management:locations")
    POINTS = reverse("management:metering_points")

    def test_the_locations_list_shows_the_new_building_with_its_dialogs(self):
        page = self.client.get(self.LOCATIONS)

        self.assertContains(page, "In Anmeldung")
        self.assertContains(page, "Ittigenstrasse 51")
        self.assertContains(page, f'href="{self.building_url()}?next=')
        self.assertContains(page, f'href="{self.point_url()}?next=')
        self.assertContains(page, number_markup(full_number(1)))
        self.assertContains(page, "Anna Muster")

    def test_the_metering_points_list_shows_the_points_with_their_dialog(self):
        page = self.client.get(self.POINTS)

        self.assertContains(page, "In Anmeldung")
        self.assertContains(page, f'href="{self.point_url()}?next=')
        self.assertContains(page, f'href="{self.point_url(point=self.feed_in)}?next=')

    def test_the_search_of_the_list_applies(self):
        self.assertNotContains(self.client.get(self.LOCATIONS, {"q": "Nirgendwo"}), "Ittigenstrasse 51")
        self.assertContains(self.client.get(self.LOCATIONS, {"q": "Ittigenstrasse"}), "Ittigenstrasse 51")
        self.assertNotContains(self.client.get(self.POINTS, {"q": "Nirgendwo"}), f'href="{self.point_url()}')

    def test_the_search_finds_by_number_address_and_applicant(self):
        for text in (full_number(1), "Ittigenstrasse 51", "Muster"):
            with self.subTest(text=text):
                self.assertContains(self.client.get(self.LOCATIONS, {"q": text}), f'href="{self.building_url()}?next=')
                self.assertContains(self.client.get(self.POINTS, {"q": text}), f'href="{self.point_url()}?next=')
        self.assertNotContains(
            self.client.get(self.POINTS, {"q": full_number(1)}), f'href="{self.point_url(point=self.feed_in)}?next='
        )

    def test_the_block_is_only_on_the_first_page_without_filters(self):
        for house_number in range(1, 32):
            make_location(house_number=str(house_number))
        self.assertContains(self.client.get(self.LOCATIONS, {"per": "30"}), "In Anmeldung")
        self.assertNotContains(self.client.get(self.LOCATIONS, {"per": "30", "page": "2"}), "In Anmeldung")
        self.assertNotContains(self.client.get(self.LOCATIONS, {"occupancy": "archived"}), "In Anmeldung")
        self.assertNotContains(self.client.get(self.POINTS, {"problems": "1"}), "In Anmeldung")

    def test_rows_over_the_cap_are_counted_with_a_link_to_the_applications_in_the_processes(self):
        self.submitted(self.bert, egid=90004270, number=5, address={"house_number": "52"})
        processes = f'href="{reverse("management:processes")}?{urlencode({"kind": "application"})}"'
        self.assertNotContains(self.client.get(self.LOCATIONS), processes)
        with patch.object(pending, "MAX_ROWS", 1):
            locations = self.client.get(self.LOCATIONS)
            points = self.client.get(self.POINTS)
        self.assertContains(locations, f"{processes}>1 weiteres Gebäude in Anmeldung: siehe Vorgänge</a>")
        self.assertContains(points, f"{processes}>2 weitere Messpunkte in Anmeldung: siehe Vorgänge</a>")

    def test_a_draft_and_a_completed_application_do_not_appear(self):
        for state in (Application.State.DRAFT, Application.State.PAUSED, Application.State.COMPLETED):
            Application.objects.filter(pk=self.application.pk).update(state=state)
            with self.subTest(state=state):
                self.assertNotContains(self.client.get(self.LOCATIONS), "In Anmeldung")
                self.assertNotContains(self.client.get(self.POINTS), "In Anmeldung")

    def test_a_customer_gets_no_list(self):
        client = self.login(self.anna)
        self.assertEqual(client.get(self.LOCATIONS).status_code, 404)


@tag("mariadb")
class BuildingAtAStoredLocationTests(CorrectionBase):
    """A new building of an application whose EGID or address names a stored location is shown under that location,
    not as a row of its own (owner walk-through 2026-11-27, D-140)."""

    LOCATIONS = reverse("management:locations")
    POINTS = reverse("management:metering_points")

    def stored(self, **extra):
        return make_location(street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen", **extra)

    def expanded(self, location, **params):
        preferences.save_expanded_locations(self.staff, {location.pk})
        return self.client.get(self.LOCATIONS, params)

    def test_the_building_is_folded_into_the_location_with_the_same_address(self):
        location = self.stored()

        page = self.expanded(location)

        self.assertContains(page, 'class="mgmt-addr-name">Ittigenstrasse 51</span>', count=2)  # table and card
        self.assertNotContains(page, f'href="{self.building_url()}?next=')
        self.assertContains(page, f'href="{self.point_url()}?next=')
        self.assertContains(page, f'href="{self.point_url(point=self.feed_in)}?next=')
        self.assertContains(page, "Anna Muster")
        self.assertContains(page, "1–1 von 1 Standorten</span>")
        self.assertContains(page, "<td>1 Person</td>")

    def test_the_building_is_folded_into_the_location_with_its_egid(self):
        location = make_location(street="Andere Strasse", house_number="3", egid=self.building.egid)

        page = self.expanded(location)

        self.assertNotContains(page, f'href="{self.building_url()}?next=')
        self.assertContains(page, f'href="{self.point_url()}?next=')

    def assert_a_row_of_its_own(self):
        page = self.client.get(self.LOCATIONS)
        self.assertContains(page, f'href="{self.building_url()}?next=')
        self.assertContains(page, "+ 1 Gebäude in Anmeldung")
        self.assertEqual(location_list.folded_buildings(), {})

    def test_an_archived_location_keeps_the_row_of_its_own(self):
        self.stored(is_archived=True)
        self.assert_a_row_of_its_own()

    def test_a_contradicting_egid_keeps_the_row_of_its_own(self):
        self.stored(egid=90009999)
        self.assert_a_row_of_its_own()

    def test_a_draft_or_paused_application_is_never_folded(self):
        # L-3: the customer still edits a draft; only submitted applications (CORRECTABLE_STATES) are shown.
        by_address = self.stored()
        by_egid = make_location(street="Andere Strasse", house_number="3", egid=self.building.egid)
        for state in (Application.State.DRAFT, Application.State.PAUSED):
            Application.objects.filter(pk=self.application.pk).update(state=state)
            with self.subTest(state=state):
                self.assertEqual(location_list.folded_buildings(), {})
                rows = {row.pk: row for row in location_views.build_list({}).queryset}
                for location in (by_address, by_egid):
                    row = rows[location.pk]
                    self.assertEqual(
                        (row.mp_count, row.pending_count, row.point_count, row.occupant_count), (0, 0, 0, 0)
                    )
                preferences.save_expanded_locations(self.staff, {by_address.pk, by_egid.pk})
                page = self.client.get(self.LOCATIONS)
                self.assertNotContains(page, "Anna Muster")
                self.assertNotContains(page, "Muster")
                self.assertNotContains(page, "In Anmeldung")
                for text in ("Muster", full_number(1)):
                    found = self.client.get(self.LOCATIONS, {"q": text})
                    self.assertNotContains(found, 'class="mgmt-addr-name">')
                    self.assertNotContains(found, "Muster</")

    def test_a_person_counts_once_also_when_applicant_and_occupant(self):
        self.submitted(self.bert, egid=90004270, number=5)
        location = self.stored()
        point = make_metering_point(30, location=location)
        assign_person(point, self.anna, valid_from=TODAY)

        page = self.client.get(self.LOCATIONS)

        self.assertContains(page, "<td>2 Personen</td>")
        self.assertEqual(location_list.folded_buildings().keys(), set(ApplicationBuilding.objects.values_list("pk", flat=True)))

    def test_the_points_stay_under_the_location_on_every_page_and_filter(self):
        for house_number in range(1, 32):
            make_location(street="Aaa", house_number=str(house_number))
        location = self.stored()
        preferences.save_expanded_locations(self.staff, {location.pk})

        second = self.client.get(self.LOCATIONS, {"per": "30", "page": "2"})
        occupied = self.client.get(self.LOCATIONS, {"occupancy": "occupied"})
        empty = self.client.get(self.LOCATIONS, {"occupancy": "empty"})

        for page in (second, occupied):
            self.assertContains(page, f'href="{self.point_url()}?next=')
        self.assertNotContains(empty, 'class="mgmt-addr-name">Ittigenstrasse 51</span>')

    def test_the_search_finds_the_location_by_the_applicant_and_the_number(self):
        self.stored()
        for text in ("Muster", full_number(1)):
            with self.subTest(text=text):
                page = self.client.get(self.LOCATIONS, {"q": text})
                self.assertContains(page, 'class="mgmt-addr-name">Ittigenstrasse 51</span>', count=2)  # table and card
                self.assertContains(page, "1–1 von 1 Standorten</span>")

    def test_the_metering_points_list_links_the_point_to_the_location(self):
        location = self.stored()

        page = self.client.get(self.POINTS)

        self.assertContains(page, reverse("management:locations_detail", args=[location.pk]))

    def test_the_detail_page_counts_like_the_list(self):
        # Main session (D-140): the subtitle of the detail page and of its print use the counting of the list.
        location = self.stored()

        page = self.client.get(reverse("management:locations_detail", args=[location.pk]))

        self.assertContains(page, "– · 2 Messpunkte (2 in Anmeldung) · 1 Person</p>")
        self.assertEqual(
            location_views.counted_phrases([location.pk], TODAY), {location.pk: "2 Messpunkte (2 in Anmeldung) · 1 Person"}
        )

    def test_the_printed_list_counts_the_applicant(self):
        self.stored()
        found = location_views.build_list({})
        self.assertEqual([location.occupant_count for location in found.queryset], [1])

    def test_the_points_in_registration_are_counted_in_the_column_and_in_print(self):
        self.stored()

        page = self.client.get(self.LOCATIONS)
        row = location_views.build_list({}).queryset.get()

        self.assertContains(page, "2 Messpunkte (2 in Anmeldung)")  # the card
        # The table: the points in registration on a second, muted line, so the column is not cut (tester finding 4).
        self.assertContains(page, '<td>2 Messpunkte<span class="mgmt-cell-sub">2 in Anmeldung</span></td>')
        self.assertNotContains(page, "keine Messpunkte")
        self.assertEqual((row.mp_count, row.pending_count, row.point_count), (0, 2, 2))
        self.assertEqual(location_views.list_count_phrase(row), "2 Messpunkte (2 in Anmeldung)")
