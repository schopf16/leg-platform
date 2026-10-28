"""The map of the neighbourhood (D-123): the approximate coordinate formulas of swisstopo against the document's example,
the outline of a circuit, and the circuits of a postal code from the address directory. Synthetic data only; no network."""

import uuid
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from leg_platform.addresses import neighbourhood
from leg_platform.addresses.geo import lv95_to_wgs84
from leg_platform.addresses.models import Address, Directory, Plant, PlantRegister, TransformerStation

OWN_EGID = 1000001
POSTAL_CODE = "3063"


class GeoTests(SimpleTestCase):
    def test_the_example_of_the_swisstopo_document_is_reproduced(self):
        # Document, section 2, example: E = 2 700 000 m, N = 1 100 000 m give 8° 43' 49.80" and 46° 02' 38.86".
        longitude, latitude = lv95_to_wgs84(2_700_000, 1_100_000)
        self.assertAlmostEqual(longitude, 8 + 43 / 60 + 49.80 / 3600, places=5)
        self.assertAlmostEqual(latitude, 46 + 2 / 60 + 38.86 / 3600, places=5)


class HullTests(SimpleTestCase):
    def test_the_hull_of_a_square_with_an_inner_point_is_its_four_corners(self):
        hull = neighbourhood._hull([(0, 0), (4, 0), (4, 4), (0, 4), (2, 2)])
        self.assertEqual(sorted(hull), [(0, 0), (0, 4), (4, 0), (4, 4)])

    def test_one_or_two_points_stay_as_they_are(self):
        self.assertEqual(neighbourhood._hull([(1, 1)]), [(1, 1)])
        self.assertEqual(sorted(neighbourhood._hull([(2, 1), (1, 1), (2, 1)])), [(1, 1), (2, 1)])


def publish(buildings):
    """Publish a generation with one address row per building: ``{egid: (postal_code, easting, northing)}``."""
    generation = uuid.uuid4()
    Directory.objects.update_or_create(
        pk=1, defaults={
            "generation": generation, "data_date": timezone.localdate(), "imported_at": timezone.now(),
            "checksum": "0" * 64, "row_count": len(buildings), "has_egid": True,
        },
    )
    for egid, (postal_code, easting, northing) in buildings.items():
        Address.objects.create(
            generation=generation, street="Example Street", house_number=str(egid % 100), locality="Town",
            postal_code=postal_code, egid=egid, easting=easting, northing=northing,
        )
    return generation


def station(egid, name, postal_code=POSTAL_CODE):
    TransformerStation.objects.create(
        egid=egid, postal_code=postal_code, name=name, match=TransformerStation.Match.EXACT, scanned_at=timezone.now(),
    )


def plant(generation, egid, power="5.00"):
    Plant.objects.create(
        generation=generation, xtf_id=f"plant-{egid}-{power}", egid=egid, subcategory="subcat_2",
        total_power_kw=Decimal(power), x=None, y=None,
    )


class NeighbourhoodTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        # Circuit "Nord": the own building, two named buildings and one building without a station name next to them.
        # Circuit "Sued": two named buildings further east. A building without coordinates is not on the map.
        self.generation = publish({
            OWN_EGID: (POSTAL_CODE, 2_603_000, 1_199_000),
            1000002: (POSTAL_CODE, 2_603_100, 1_199_000),
            1000003: (POSTAL_CODE, 2_603_000, 1_199_100),
            1000004: (POSTAL_CODE, 2_603_020, 1_199_020),
            1000005: (POSTAL_CODE, 2_603_900, 1_199_000),
            1000006: (POSTAL_CODE, 2_603_950, 1_199_050),
        })
        Address.objects.create(
            generation=self.generation, street="No Coordinates", house_number="9", locality="Town",
            postal_code=POSTAL_CODE, egid=1000007,
        )
        station(OWN_EGID, "Nord")
        station(1000002, "Nord")
        station(1000003, "Nord")
        station(1000005, "Sued")
        station(1000006, "Sued")
        PlantRegister.objects.update_or_create(pk=1, defaults={"generation": self.generation, "row_count": 2})
        plant(self.generation, OWN_EGID)
        plant(self.generation, 1000005)

    def test_the_circuits_of_the_postal_code_count_their_buildings_and_plants(self):
        result = neighbourhood.for_building(OWN_EGID)
        self.assertEqual(result.postal_code, POSTAL_CODE)
        self.assertEqual(result.own_circuit, "Nord")
        by_name = {circuit.name: circuit for circuit in result.circuits}
        self.assertEqual(set(by_name), {"Nord", "Sued"})
        self.assertEqual(by_name["Nord"].building_count, 4)
        self.assertEqual(by_name["Nord"].pv_count, 1)
        self.assertEqual(by_name["Sued"].building_count, 2)
        self.assertEqual(by_name["Sued"].pv_count, 1)

    def test_a_building_without_a_station_name_is_in_the_circuit_of_its_nearest_building(self):
        result = neighbourhood.for_building(1000004)
        self.assertEqual(result.own_circuit, "Nord")

    def test_the_outline_of_a_circuit_has_at_least_three_corners_and_the_own_point_is_on_the_map(self):
        result = neighbourhood.for_building(OWN_EGID)
        nord = next(circuit for circuit in result.circuits if circuit.name == "Nord")
        self.assertGreaterEqual(len(nord.outline), 3)
        self.assertIsNotNone(result.own_point)
        longitude, latitude = result.own_point
        self.assertTrue(5 < longitude < 11 and 45 < latitude < 48)

    def test_a_building_outside_the_directory_has_no_neighbourhood(self):
        self.assertIsNone(neighbourhood.for_building(9999999))

    def test_a_building_without_coordinates_has_no_map_point(self):
        result = neighbourhood.for_building(1000007)
        self.assertIsNotNone(result)
        self.assertIsNone(result.own_point)
        self.assertIsNone(result.own_circuit)

    def test_a_postal_code_without_station_names_has_no_circuit(self):
        TransformerStation.objects.all().delete()
        result = neighbourhood.for_building(OWN_EGID)
        self.assertEqual(result.circuits, ())
        self.assertIsNone(result.own_circuit)
