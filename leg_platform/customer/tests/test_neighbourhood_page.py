"""The page Nachbarschaft (D-120): the circuit of the profile building with public counts only, the map data without any
other person, the invitation as a mailto link, the menu entry and the Content Security Policy for the swisstopo tiles.
The address directory is filled with synthetic rows; no network."""

import json
import re
import uuid
from datetime import date, timedelta

from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.addresses.models import Address, Directory, ServicePostalCode, TransformerStation
from leg_platform.community.models import Direction, PersonAssignment
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point
from leg_platform.customer.tests.helpers import customer, login

NEIGHBOURHOOD = reverse("customer:neighbourhood")
OWN_EGID = 90001234
OTHER_EGID = 90001235
POSTAL_CODE = "3063"
LONG_AGO = date.today() - timedelta(days=400)


def publish_directory(rows):
    """Publish a generation with the rows ``{egid: (easting, northing)}`` in postal code 3063."""
    generation = uuid.uuid4()
    Directory.objects.update_or_create(
        pk=1, defaults={
            "generation": generation, "data_date": timezone.localdate(), "imported_at": timezone.now(),
            "checksum": "0" * 64, "row_count": len(rows), "has_egid": True,
        },
    )
    for egid, (easting, northing) in rows.items():
        Address.objects.create(
            generation=generation, street="Musterstrasse", house_number=str(egid % 100), locality="Ittigen",
            postal_code=POSTAL_CODE, egid=egid, easting=easting, northing=northing,
        )


@tag("mariadb")
class NeighbourhoodPageTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.neighbourhood@example.org")
        cls.other = customer("other.neighbourhood@example.org")
        cls.location = make_location("Musterstrasse", "34", POSTAL_CODE, "Ittigen", egid=OWN_EGID)
        point = make_metering_point(
            1, Direction.CONSUMPTION, cls.location, registered_with_operator_on=LONG_AGO,
            confirmed_on=LONG_AGO, start_date=LONG_AGO,
        )
        assign_person(point, cls.mine, valid_from=LONG_AGO)
        cls.mine.street, cls.mine.house_number = "Musterstrasse", "34"
        cls.mine.swiss_postal_code, cls.mine.town = 3063, "Ittigen"
        cls.mine.save()
        # The other account has a building of its own: its name must never reach this page.
        other_location = make_location("Anderweg", "2", POSTAL_CODE, "Ittigen", egid=OTHER_EGID)
        other_point = make_metering_point(
            2, Direction.CONSUMPTION, other_location, registered_with_operator_on=LONG_AGO,
            confirmed_on=LONG_AGO, start_date=LONG_AGO,
        )
        assign_person(other_point, cls.other, valid_from=LONG_AGO)
        cls.other.street, cls.other.house_number = "Anderweg", "2"
        cls.other.swiss_postal_code, cls.other.town = 3063, "Ittigen"
        cls.other.save()

    def setUp(self):
        self.client = login(self.mine)

    def page(self):
        response = self.client.get(NEIGHBOURHOOD)
        self.assertEqual(response.status_code, 200)
        return response

    def test_without_a_profile_address_the_page_shows_the_circuits_of_the_postal_code(self):
        self.mine.street = ""
        self.mine.save()
        publish_directory({OWN_EGID: (2_603_000, 1_199_000), 90001236: (2_603_000, 1_199_100)})
        TransformerStation.objects.create(
            egid=OWN_EGID, postal_code=POSTAL_CODE, name="Ittigen Nord", match=TransformerStation.Match.EXACT,
            scanned_at=timezone.now(),
        )
        self.assertIn("Ittigen Nord", self.page().content.decode())

    def test_without_a_postal_code_the_page_takes_the_service_area(self):
        self.mine.street = ""
        self.mine.swiss_postal_code = None
        self.mine.save()
        publish_directory({OWN_EGID: (2_603_000, 1_199_000)})
        TransformerStation.objects.create(
            egid=OWN_EGID, postal_code=POSTAL_CODE, name="Ittigen Nord", match=TransformerStation.Match.EXACT,
            scanned_at=timezone.now(),
        )
        ServicePostalCode.objects.create(postal_code=POSTAL_CODE)
        self.assertIn("Ittigen Nord", self.page().content.decode())

    def test_without_any_postal_code_or_service_area_the_page_still_renders(self):
        self.mine.street = ""
        self.mine.swiss_postal_code = None
        self.mine.save()
        publish_directory({})
        self.assertEqual(self.page().status_code, 200)

    def test_a_profile_address_in_the_directory_without_a_position_is_not_a_missing_building(self):
        # The address is in the directory (the profile building is found without a metering point), but it has no
        # position: the page shows the building's circuit section, not the hint about the missing building.
        publish_directory({OWN_EGID: (None, None)})
        content = self.page().content.decode()
        self.assertNotIn("Kein Gebäude an Ihrer Profiladresse", content)

    def test_the_profile_building_needs_no_metering_point_of_the_account(self):
        # The circuit comes from the building (transformer station, positions), not from the account's metering points.
        publish_directory({OWN_EGID: (2_603_000, 1_199_000), 90001236: (2_603_000, 1_199_100)})
        TransformerStation.objects.create(
            egid=OWN_EGID, postal_code=POSTAL_CODE, name="Ittigen Nord", match=TransformerStation.Match.EXACT,
            scanned_at=timezone.now(),
        )
        PersonAssignment.objects.filter(person=self.mine).delete()
        content = self.page().content.decode()
        self.assertNotIn("Kein Gebäude an Ihrer Profiladresse", content)
        self.assertIn("Ittigen Nord", content)

    def test_the_circuit_of_the_profile_building_shows_public_counts_only(self):
        publish_directory({
            OWN_EGID: (2_603_000, 1_199_000),
            OTHER_EGID: (2_603_100, 1_199_000),
            90001236: (2_603_000, 1_199_100),
        })
        TransformerStation.objects.create(
            egid=OWN_EGID, postal_code=POSTAL_CODE, name="Ittigen Nord", match=TransformerStation.Match.EXACT,
            scanned_at=timezone.now(),
        )
        TransformerStation.objects.create(
            egid=OTHER_EGID, postal_code=POSTAL_CODE, name="Ittigen Nord", match=TransformerStation.Match.EXACT,
            scanned_at=timezone.now(),
        )
        html = self.page().content.decode()
        self.assertIn("Ihr Trafokreis: Ittigen Nord", html)
        self.assertIn("3 Gebäude", html)
        self.assertNotIn("Musterstrasse", html)
        self.assertNotIn("Anderweg", html)

    def test_the_map_data_holds_outlines_and_points_but_no_person(self):
        publish_directory({OWN_EGID: (2_603_000, 1_199_000), OTHER_EGID: (2_603_100, 1_199_000)})
        for egid in (OWN_EGID, OTHER_EGID):
            TransformerStation.objects.create(
                egid=egid, postal_code=POSTAL_CODE, name="Ittigen Nord", match=TransformerStation.Match.EXACT,
                scanned_at=timezone.now(),
            )
        html = self.page().content.decode()
        match = re.search(r'<script id="nb-map-data" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(match)
        data = json.loads(match.group(1))
        self.assertEqual(data["ownCircuit"], "Ittigen Nord")
        self.assertEqual(set(data), {"own", "ownCircuit", "circuits"})
        circuit = data["circuits"][0]
        self.assertEqual(set(circuit), {"name", "buildings", "pv", "colour", "outline", "pvPoints"})
        self.assertNotIn(self.other.email, html)

    def test_the_invitation_is_a_mailto_link_without_an_address(self):
        publish_directory({OWN_EGID: (2_603_000, 1_199_000)})
        html = self.page().content.decode()
        match = re.search(r'href="(mailto:[^"]*)"', html)
        self.assertIsNotNone(match)
        self.assertTrue(match.group(1).startswith("mailto:?subject="))
        self.assertNotIn("@", match.group(1))

    def test_the_content_security_policy_allows_the_swisstopo_tiles_and_nothing_else_external(self):
        publish_directory({OWN_EGID: (2_603_000, 1_199_000)})
        policy = self.page()["Content-Security-Policy"]
        self.assertIn("img-src 'self' https://wmts.geo.admin.ch", policy)
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("unsafe-inline", policy)

    def test_the_menu_entry_is_a_link_to_the_page(self):
        publish_directory({})
        self.assertIn(f'href="{NEIGHBOURHOOD}"', self.page().content.decode())

    def test_the_other_pages_of_the_participation_do_not_allow_the_tiles(self):
        publish_directory({})
        policy = self.client.get(reverse("customer:buildings"))["Content-Security-Policy"]
        self.assertIn("img-src 'self';", policy)
        self.assertNotIn("wmts.geo.admin.ch", policy)
