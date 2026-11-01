"""The EGID of a location follows its address, whoever saves it and whenever (D-135).

Synthetic directory: invented streets in the postal code 3063. What is proven: a corrected address sets the EGID in the
services that save an address; the fixed rules (unique building, the current EGID is kept when it is one of several,
cleared when it belongs to another building, nothing changes for an unknown address, an unavailable directory or an
archived location); the scheduled fill; the transformer station of an address with several buildings."""

import uuid
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, tag
from django.utils import timezone

from leg_platform.addresses import buildings
from leg_platform.addresses.models import Address, Directory, TransformerStation
from leg_platform.community import location_egid, locations
from leg_platform.community.models import HistoryEntry, Location
from leg_platform.community.tests.helpers import make_location

PLACE = ("3063", "Ittigen")
GENERATION = uuid.uuid4()


def listed(street, number, egid, entrance=0):
    return Address(
        generation=GENERATION, street=street, house_number=number, postal_code=PLACE[0], locality=PLACE[1],
        egid=egid, entrance_id=entrance,
    )


def station(egid, name):
    return TransformerStation(
        egid=egid, postal_code=PLACE[0], name=name, match=TransformerStation.Match.EXACT, scanned_at=timezone.now(),
    )


@tag("mariadb")
class LocationEgidTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        rows = [
            listed("Teststrasse", "51", 101),
            listed("Doppelweg", "7", 201), listed("Doppelweg", "7", 202, 1),
            listed("Zweiweg", "1", 401), listed("Zweiweg", "3", 401, 1),
            listed("Stationsweg", "5", 501), listed("Stationsweg", "5", 502, 1),
            listed("Stationsweg", "6", 601), listed("Stationsweg", "6", 602, 1),
        ]
        Address.objects.bulk_create(rows)
        Directory.objects.create(
            pk=1, generation=GENERATION, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=len(rows), has_egid=True,
        )
        TransformerStation.objects.bulk_create([
            station(101, "Ittigen_TRA1"), station(201, "Ittigen_TRA2"), station(202, "Ittigen_TRA2"),
            station(501, "Ittigen_TRA3"), station(502, "Ittigen_TRA3"),
            station(601, "Ittigen_TRA4"), station(602, "Ittigen_TRA5"),
        ])

    def history_of(self, location):
        return list(HistoryEntry.objects.filter(subject_type="location", subject_id=location.pk, event="egid_changed"))

    def test_correcting_the_postal_code_sets_the_egid_and_records_it(self):
        location = make_location("Teststrasse", "51", "3036", "Ittigen")
        self.assertIsNone(location.egid)

        saved = locations.update_address(
            location, street="Teststrasse", house_number="51", postal_code="3063", town="Ittigen",
        )

        self.assertEqual(saved.egid, 101)
        self.assertEqual(Location.objects.get(pk=location.pk).egid, 101)
        (entry,) = self.history_of(location)
        self.assertEqual(entry.detail, {"old_egid": None, "new_egid": 101})

    def test_a_location_created_with_an_address_of_the_directory_gets_its_egid(self):
        location = locations.create_location(
            street="Teststrasse", house_number="51", postal_code="3063", town="Ittigen",
        )

        self.assertEqual(location.egid, 101)

    def test_saving_an_address_again_fills_a_missing_egid(self):
        location = make_location("Teststrasse", "51", "3063", "Ittigen")

        locations.update_address(
            location, street="Teststrasse", house_number="51", postal_code="3063", town="Ittigen",
        )

        self.assertEqual(Location.objects.get(pk=location.pk).egid, 101)

    def test_a_building_belongs_to_one_location_only(self):
        first = make_location("Zweiweg", "1", "3063", "Ittigen", egid=401)
        second = make_location("Zweiweg", "3", "3063", "Ittigen")

        self.assertFalse(location_egid.resolve(second))

        self.assertIsNone(Location.objects.get(pk=second.pk).egid)
        self.assertEqual(Location.objects.get(pk=first.pk).egid, 401)

    def test_with_several_buildings_the_current_one_stays_and_nothing_is_guessed(self):
        chosen = make_location("Doppelweg", "7", "3063", "Ittigen", egid=202)

        self.assertFalse(location_egid.resolve(chosen))

        self.assertEqual(Location.objects.get(pk=chosen.pk).egid, 202)

    def test_with_several_buildings_and_no_egid_nothing_is_guessed(self):
        open_one = make_location("Doppelweg", "7", "3063", "Ittigen")

        self.assertFalse(location_egid.resolve(open_one))

        self.assertIsNone(Location.objects.get(pk=open_one.pk).egid)

    def test_an_egid_of_another_building_is_cleared_when_the_address_is_ambiguous(self):
        location = make_location("Doppelweg", "7", "3063", "Ittigen", egid=999)

        self.assertTrue(location_egid.resolve(location))

        self.assertIsNone(Location.objects.get(pk=location.pk).egid)
        (entry,) = self.history_of(location)
        self.assertEqual(entry.detail, {"old_egid": 999, "new_egid": None})

    def test_an_unknown_address_leaves_the_egid_alone(self):
        location = make_location("Nirgendwo", "1", "3063", "Ittigen", egid=555)

        self.assertFalse(location_egid.resolve(location))

        self.assertEqual(Location.objects.get(pk=location.pk).egid, 555)

    def test_an_unavailable_directory_changes_nothing(self):
        location = make_location("Teststrasse", "51", "3063", "Ittigen")
        Directory.objects.filter(pk=1).update(has_egid=False)

        self.assertFalse(location_egid.resolve(location))

        self.assertIsNone(Location.objects.get(pk=location.pk).egid)

    def test_an_archived_location_is_left_alone(self):
        location = make_location("Teststrasse", "51", "3063", "Ittigen", is_archived=True)

        self.assertFalse(location_egid.resolve(location))

    def test_the_scheduled_command_fills_the_missing_egids_once(self):
        make_location("Teststrasse", "51", "3063", "Ittigen")
        make_location("Nirgendwo", "1", "3063", "Ittigen")
        out = StringIO()

        call_command("fill_location_egids", stdout=out)
        call_command("fill_location_egids", stdout=out)

        self.assertIn("Set the EGID of 1 location(s).", out.getvalue())
        self.assertIn("Set the EGID of 0 location(s).", out.getvalue())
        self.assertEqual(Location.objects.get(street="Teststrasse").egid, 101)

    def test_the_command_is_scheduled(self):
        from leg_platform.accounts.management.commands.run_scheduled_tasks import TASKS

        self.assertIn("fill_location_egids", TASKS)

    def test_the_station_of_an_address_with_several_buildings(self):
        self.assertEqual(buildings.station_name("Teststrasse", "51", "3063"), "Ittigen_TRA1")
        # Two buildings, one station.
        self.assertEqual(buildings.station_name("Doppelweg", "7", "3063"), "Ittigen_TRA2")
        self.assertEqual(buildings.station_name("Stationsweg", "5", "3063"), "Ittigen_TRA3")
        # Two buildings, two stations: no answer is better than a wrong one.
        self.assertEqual(buildings.station_name("Stationsweg", "6", "3063"), "")
        # Unknown address, building without a scanned station, unavailable directory.
        self.assertEqual(buildings.station_name("Nirgendwo", "1", "3063"), "")
        self.assertEqual(buildings.station_name("Zweiweg", "1", "3063"), "")
        Directory.objects.filter(pk=1).update(has_egid=False)
        self.assertEqual(buildings.station_name("Teststrasse", "51", "3063"), "")
