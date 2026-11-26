"""The transformer circuit of a location follows the station of its EGID unless chosen by hand (DECISIONS section 13,
"Circuit of a location").

Synthetic directory in the postal code 3063. What is proven: the station sets the circuit; a building the directory
answers for without a station removes a derived circuit (one History entry); an address directory that cannot answer
changes nothing (no save, no History); a hand choice and an archived location are left alone; the scheduled command
``fill_transformer_circuits`` and its place in ``run_scheduled_tasks``; removing a hand choice while the directory
cannot answer still removes it."""

import uuid
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import DatabaseError
from django.test import TestCase, tag
from django.utils import timezone

from leg_platform.addresses.models import Address, Directory, TransformerStation
from leg_platform.community import history, locations, transformer_circuits
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Location
from leg_platform.community.tests.helpers import make_location

GENERATION = uuid.uuid4()
WITH_STATION = 101
WITHOUT_STATION = 102


def listed(number, egid):
    return Address(
        generation=GENERATION, street="Stromweg", house_number=number, postal_code="3063", locality="Ittigen",
        egid=egid, entrance_id=0,
    )


@tag("mariadb")
class CircuitDerivationTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        rows = [listed("1", WITH_STATION), listed("2", WITHOUT_STATION)]
        Address.objects.bulk_create(rows)
        Directory.objects.create(
            pk=1, generation=GENERATION, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=len(rows), has_egid=True,
        )
        TransformerStation.objects.create(
            egid=WITH_STATION, postal_code="3063", name="Ittigen_TRA1", match=TransformerStation.Match.EXACT,
            scanned_at=timezone.now(),
        )

    def location(self, egid, circuit=None, **extra):
        location = make_location(house_number=str(egid), egid=egid, **extra)
        if circuit is not None:
            Location.objects.filter(pk=location.pk).update(transformer_circuit=transformer_circuits.create(circuit))
        return Location.objects.select_related("transformer_circuit").get(pk=location.pk)

    def circuit_of(self, location):
        circuit = Location.objects.get(pk=location.pk).transformer_circuit
        return circuit.grid_number if circuit else None

    def entries(self, location):
        return list(history.entries_for(location).filter(event=HistoryEvent.TRANSFORMER_CIRCUIT_CHANGED))

    def unavailable(self):
        Directory.objects.filter(pk=1).update(has_egid=False)

    def test_the_station_sets_the_circuit(self):
        location = self.location(WITH_STATION)

        self.assertTrue(transformer_circuits.derive(location))

        self.assertEqual(self.circuit_of(location), "TRA1")
        (entry,) = self.entries(location)
        self.assertEqual((entry.detail["old"], entry.detail["new"]), ("", "TRA1"))

    def test_no_station_in_an_answering_directory_removes_a_derived_circuit(self):
        location = self.location(WITHOUT_STATION, circuit="TRA9")

        self.assertTrue(transformer_circuits.derive(location))

        self.assertIsNone(self.circuit_of(location))
        (entry,) = self.entries(location)
        self.assertEqual((entry.detail["old"], entry.detail["new"]), ("TRA9", ""))

    def test_a_directory_that_cannot_answer_changes_nothing(self):
        location = self.location(WITHOUT_STATION, circuit="TRA9")
        self.unavailable()

        self.assertFalse(transformer_circuits.derive(location))

        self.assertEqual(self.circuit_of(location), "TRA9")
        self.assertEqual(self.entries(location), [])

    def test_an_unreadable_directory_changes_nothing(self):
        location = self.location(WITHOUT_STATION, circuit="TRA9")

        with patch("leg_platform.addresses.transformers.station_of", side_effect=DatabaseError("locked")):
            self.assertIsNone(transformer_circuits.station_number(WITHOUT_STATION))
            self.assertFalse(transformer_circuits.derive(location))

        self.assertEqual(self.circuit_of(location), "TRA9")
        self.assertEqual(self.entries(location), [])

    def test_the_command_derives_once_and_leaves_hand_choices_and_archived_locations_alone(self):
        derived = self.location(WITH_STATION)
        manual = self.location(WITHOUT_STATION, circuit="TRA7", circuit_manual=True)
        archived = make_location(house_number="9", is_archived=True)  # no EGID: a derived circuit would go
        Location.objects.filter(pk=archived.pk).update(transformer_circuit=transformer_circuits.create("TRA8"))
        out = StringIO()

        call_command("fill_transformer_circuits", stdout=out)
        call_command("fill_transformer_circuits", stdout=out)

        self.assertIn("Changed the transformer circuit of 1 location(s).", out.getvalue())
        self.assertIn("Changed the transformer circuit of 0 location(s).", out.getvalue())
        self.assertEqual(self.circuit_of(derived), "TRA1")
        self.assertEqual(self.circuit_of(manual), "TRA7")
        self.assertEqual(self.circuit_of(archived), "TRA8")
        self.assertEqual(self.entries(manual), [])
        self.assertEqual(self.entries(archived), [])

    def test_the_command_changes_nothing_while_the_directory_cannot_answer(self):
        location = self.location(WITHOUT_STATION, circuit="TRA9")
        self.unavailable()
        out = StringIO()

        call_command("fill_transformer_circuits", stdout=out)

        self.assertIn("Changed the transformer circuit of 0 location(s).", out.getvalue())
        self.assertEqual(self.circuit_of(location), "TRA9")
        self.assertEqual(self.entries(location), [])

    def test_the_command_runs_right_after_the_egids_are_filled(self):
        from leg_platform.accounts.management.commands.run_scheduled_tasks import TASKS

        position = TASKS.index("fill_location_egids")
        self.assertEqual(TASKS[position + 1], "fill_transformer_circuits")

    def test_removing_a_hand_choice_while_the_directory_cannot_answer_still_removes_it(self):
        location = self.location(WITH_STATION, circuit="TRA7", circuit_manual=True)
        self.unavailable()

        locations.set_circuit(location, None)

        fresh = Location.objects.get(pk=location.pk)
        self.assertFalse(fresh.circuit_manual)
        self.assertIsNone(fresh.transformer_circuit)
        self.assertEqual(len(self.entries(location)), 1)
