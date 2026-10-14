"""Tests of the local throw-away address directory (docs/STARTUP.md); no database is used."""

import importlib
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

import leg_platform.settings as normal_settings
from leg_platform.addresses.management.commands import seed_local_address_directory as command

MODULE = "leg_platform.local_directory_settings"
FILE_VARIABLE = "LEG_LOCAL_ADDRESS_DIRECTORY_FILE"
COMMAND = "seed_local_address_directory"


class LocalDirectorySettingsTests(SimpleTestCase):
    def import_settings_module(self, remove=(), **environment):
        # A failed import leaves nothing in sys.modules; a successful one is removed again.
        sys.modules.pop(MODULE, None)
        self.addCleanup(sys.modules.pop, MODULE, None)
        with patch.dict(os.environ, environment):
            for name in remove:
                os.environ.pop(name, None)
            return importlib.import_module(MODULE)

    def test_refuses_to_be_imported_outside_local_development(self):
        for app_env in ("stage", "production", "Local", "unknown", ""):
            with self.subTest(app_env=app_env), self.assertRaises(ImproperlyConfigured):
                self.import_settings_module(APP_ENV=app_env)

    def test_refuses_to_be_imported_without_an_environment_name(self):
        with self.assertRaises(ImproperlyConfigured):
            self.import_settings_module(remove=("APP_ENV",))

    def test_refuses_a_file_inside_the_repository(self):
        inside = Path(settings.BASE_DIR) / "directory.sqlite3"
        for path in (inside, Path(settings.BASE_DIR)):
            with self.subTest(path=path.name), self.assertRaises(ImproperlyConfigured):
                self.import_settings_module(APP_ENV="local", **{FILE_VARIABLE: str(path)})

    def test_refuses_a_relative_path(self):
        with self.assertRaises(ImproperlyConfigured):
            self.import_settings_module(APP_ENV="local", **{FILE_VARIABLE: "directory.sqlite3"})

    def test_default_file_is_outside_the_repository(self):
        module = self.import_settings_module(remove=(FILE_VARIABLE,), APP_ENV="local")

        name = Path(module.DATABASES["addresses"]["NAME"])
        self.assertEqual(name, module.LOCAL_DIRECTORY_DEFAULT_FILE.resolve())
        base = Path(settings.BASE_DIR).resolve()
        self.assertNotIn(base, name.parents)

    def test_adds_two_sqlite_aliases_for_one_file_and_changes_nothing_else(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "directory.sqlite3"
            module = self.import_settings_module(APP_ENV="local", **{FILE_VARIABLE: str(path)})

        for alias in ("addresses", "address_import"):
            with self.subTest(alias=alias):
                self.assertEqual(module.DATABASES[alias]["ENGINE"], "django.db.backends.sqlite3")
                self.assertEqual(Path(module.DATABASES[alias]["NAME"]), path.resolve())
        # The application database is the very same configuration, the normal settings are not
        # modified.
        self.assertIs(module.DATABASES["default"], normal_settings.DATABASES["default"])
        self.assertIsNot(module.DATABASES, normal_settings.DATABASES)
        self.assertNotIn("address_import", normal_settings.DATABASES)
        self.assertEqual(module.APP_ENV, "local")
        self.assertEqual(module.SESSION_COOKIE_SECURE, normal_settings.SESSION_COOKIE_SECURE)
        self.assertEqual(module.SECURE_CSP, normal_settings.SECURE_CSP)


@override_settings(APP_ENV="local")
class SeedLocalAddressDirectoryTests(SimpleTestCase):
    def test_refuses_outside_local_development(self):
        # SimpleTestCase forbids database queries: reaching the database would fail the test.
        for app_env in ("stage", "production"):
            with self.subTest(app_env=app_env), override_settings(APP_ENV=app_env):
                with self.assertRaisesMessage(CommandError, "APP_ENV=local"):
                    call_command(COMMAND)

    def test_refuses_an_alias_that_is_not_sqlite(self):
        for alias in ("addresses", "address_import"):
            with self.subTest(alias=alias):
                with patch.dict(
                    settings.DATABASES[alias], {"ENGINE": "django.db.backends.mysql"}
                ), self.assertRaisesMessage(CommandError, f"'{alias}' is not a SQLite"):
                    call_command(COMMAND)

    def test_refuses_a_missing_alias(self):
        for alias in ("addresses", "address_import"):
            with self.subTest(alias=alias), patch.dict(settings.DATABASES):
                del settings.DATABASES[alias]
                with self.assertRaisesMessage(CommandError, f"'{alias}' is not a SQLite"):
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

    def test_replace_directory_writes_one_generation_through_the_given_alias(self):
        address_model, directory_model, transaction = MagicMock(), MagicMock(), MagicMock()
        with patch.object(command, "Address", address_model), patch.object(
            command, "Directory", directory_model
        ), patch.object(command, "transaction", transaction):
            count = command.replace_directory(using="address_import")

        self.assertEqual(count, len(command.SYNTHETIC_ROWS))
        transaction.atomic.assert_called_once_with(using="address_import")
        address_model.objects.using.assert_called_with("address_import")
        created = address_model.objects.using.return_value.bulk_create.call_args.args[0]
        self.assertEqual(len(created), len(command.SYNTHETIC_ROWS))
        generations = {call.kwargs["generation"] for call in address_model.call_args_list}
        self.assertEqual(len(generations), 1)
        update = directory_model.objects.using.return_value.update_or_create
        self.assertEqual(update.call_args.kwargs["pk"], 1)
        defaults = update.call_args.kwargs["defaults"]
        self.assertEqual(defaults["checksum"], command.SYNTHETIC_CHECKSUM)
        self.assertEqual(defaults["row_count"], len(command.SYNTHETIC_ROWS))
        self.assertEqual(defaults["generation"], generations.pop())
