"""The data function of the community migration 0016: a circuit stored before the derivation was chosen by hand and
stays manual, a location without a circuit stays derived (owner decision 2026-11-26)."""

import importlib

from django.apps import apps
from django.test import TestCase, tag

from leg_platform.community.models import Location
from leg_platform.community.tests.helpers import make_location
from leg_platform.community.transformer_circuits import create

migration = importlib.import_module("leg_platform.community.migrations.0016_location_circuit_manual")


@tag("mariadb")
class MarkExistingCircuitsManualTests(TestCase):
    def test_a_location_with_a_circuit_becomes_manual_and_one_without_stays_derived(self):
        with_circuit = make_location(house_number="1")
        without_circuit = make_location(house_number="2")
        Location.objects.filter(pk=with_circuit.pk).update(transformer_circuit=create("TRA21359"), circuit_manual=False)
        Location.objects.filter(pk=without_circuit.pk).update(transformer_circuit=None, circuit_manual=False)

        migration.mark_existing_circuits_manual(apps, None)

        with_circuit.refresh_from_db()
        without_circuit.refresh_from_db()
        self.assertTrue(with_circuit.circuit_manual)
        self.assertEqual(with_circuit.transformer_circuit.grid_number, "TRA21359")
        self.assertFalse(without_circuit.circuit_manual)
        self.assertIsNone(without_circuit.transformer_circuit)
