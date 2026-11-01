"""Buildings and metering points of submitted applications on the lists of locations and metering points (owner
feedback 2026-11-01: the administrator corrects an address at the address, not only on the application)."""

from django.test import tag
from django.urls import reverse

from leg_platform.community.models import Application
from leg_platform.management.tests.test_process_corrections import CorrectionBase, shown


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
        self.assertContains(page, shown(1))
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

    def test_a_draft_and_a_completed_application_do_not_appear(self):
        for state in (Application.State.DRAFT, Application.State.PAUSED, Application.State.COMPLETED):
            Application.objects.filter(pk=self.application.pk).update(state=state)
            with self.subTest(state=state):
                self.assertNotContains(self.client.get(self.LOCATIONS), "In Anmeldung")
                self.assertNotContains(self.client.get(self.POINTS), "In Anmeldung")

    def test_a_customer_gets_no_list(self):
        client = self.login(self.anna)
        self.assertEqual(client.get(self.LOCATIONS).status_code, 404)
