"""Tests of the synthetic address directory of a development machine (docs/STARTUP.md)."""

import io
import re
import uuid
from unittest.mock import MagicMock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.utils import timezone

from leg_platform.addresses.management.commands import seed_local_address_directory as command
from leg_platform.addresses.models import Address, Directory
from leg_platform.addresses.services import lookup

COMMAND = "seed_local_address_directory"


@override_settings(APP_ENV="local")
class SeedLocalAddressDirectoryUnitTests(SimpleTestCase):
    def test_refuses_outside_local_development(self):
        # SimpleTestCase forbids database queries: reaching the database would fail the test.
        for app_env in ("stage", "production"):
            with self.subTest(app_env=app_env), override_settings(APP_ENV=app_env):
                with self.assertRaisesMessage(CommandError, "APP_ENV=local"):
                    call_command(COMMAND)

    def test_an_empty_directory_may_be_filled(self):
        command.ensure_replaceable(None, has_addresses=False)

    def test_a_synthetic_directory_may_be_replaced(self):
        directory = MagicMock(checksum=command.SYNTHETIC_CHECKSUM)

        command.ensure_replaceable(directory, has_addresses=True)

    def test_refuses_to_replace_imported_data(self):
        # The checksum of a real import is a SHA-256 value (importer.py).
        for checksum in ("0" * 64, "d85f85ac36115e492f77e9c7fb2d567e1aa0d5eaff57e2efeffba302dc6f164f", ""):
            with self.subTest(checksum=checksum[:8]):
                directory = MagicMock(checksum=checksum)
                with self.assertRaisesMessage(CommandError, "may contain imported data"):
                    command.ensure_replaceable(directory, has_addresses=True)

    def test_refuses_addresses_without_metadata(self):
        with self.assertRaisesMessage(CommandError, "without metadata"):
            command.ensure_replaceable(None, has_addresses=True)

    def test_the_marker_is_not_a_checksum_and_the_rows_are_valid_and_synthetic(self):
        self.assertIsNone(re.fullmatch(r"[0-9a-f]{64}", command.SYNTHETIC_CHECKSUM))
        self.assertLessEqual(len(command.SYNTHETIC_CHECKSUM), 64)
        self.assertTrue(command.SYNTHETIC_ROWS)
        for street, house_number, postal_code, locality in command.SYNTHETIC_ROWS:
            with self.subTest(street=street, house_number=house_number):
                self.assertIsNotNone(re.fullmatch(r"[1-9][0-9]{3}", postal_code))
                self.assertGreaterEqual(int(postal_code), 9990)
                self.assertTrue(street and locality)
                self.assertLessEqual(len(street), 255)
                self.assertLessEqual(len(house_number), 32)
                self.assertLessEqual(len(locality), 255)
                self.assertRegex(
                    street, r"^(Example|Sample|Test|Throwaway) ", "synthetic street name"
                )


@tag("mariadb")
@override_settings(APP_ENV="local")
class SeedLocalAddressDirectoryDatabaseTests(TestCase):
    """The command writes into the directory file (the alias addresses, D-075), only with APP_ENV=local."""

    databases = {"default", "addresses"}

    def run_command(self):
        output = io.StringIO()
        call_command(COMMAND, stdout=output)
        return output.getvalue()

    def test_it_loads_one_synthetic_generation_that_the_lookup_finds(self):
        self.assertIn("Loaded", self.run_command())
        directory = Directory.objects.get(pk=1)
        self.assertEqual(directory.checksum, command.SYNTHETIC_CHECKSUM)
        self.assertEqual(directory.row_count, len(command.SYNTHETIC_ROWS))
        self.assertEqual(Address.objects.count(), len(command.SYNTHETIC_ROWS))
        self.assertEqual(set(Address.objects.values_list("generation", flat=True)), {directory.generation})
        result = lookup({"street": "Example Street", "postal_code": "9999"})
        self.assertEqual(result["status"], "available")
        self.assertEqual(len(result["results"]), 3)

    def test_a_second_run_replaces_the_synthetic_directory(self):
        self.run_command()
        first = Directory.objects.get(pk=1).generation
        self.run_command()
        self.assertNotEqual(Directory.objects.get(pk=1).generation, first)
        self.assertEqual(Address.objects.count(), len(command.SYNTHETIC_ROWS))

    def test_it_never_replaces_a_directory_of_a_real_import(self):
        generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=generation, data_date=timezone.localdate(),
            imported_at=timezone.now(), checksum="0" * 64, row_count=1,
        )
        Address.objects.create(
            generation=generation, street="Imported Street", house_number="1",
            postal_code="9999", locality="Example Town",
        )
        with self.assertRaisesMessage(CommandError, "may contain imported data"):
            self.run_command()
        self.assertEqual(list(Address.objects.values_list("street", flat=True)), ["Imported Street"])
        self.assertEqual(Directory.objects.get(pk=1).generation, generation)

    def test_it_refuses_and_writes_nothing_outside_local_development(self):
        for app_env in ("stage", "production"):
            with self.subTest(app_env=app_env), override_settings(APP_ENV=app_env):
                with self.assertRaisesMessage(CommandError, "APP_ENV=local"):
                    self.run_command()
        self.assertFalse(Address.objects.exists())
        self.assertFalse(Directory.objects.exists())

    def test_replace_directory_publishes_one_generation(self):
        count = command.replace_directory()
        self.assertEqual(count, len(command.SYNTHETIC_ROWS))
        self.assertEqual(Address.objects.values("generation").distinct().count(), 1)
