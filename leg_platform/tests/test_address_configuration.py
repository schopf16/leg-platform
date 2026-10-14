from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from leg_platform.address_configuration import address_database, importer_database, validate_process_credentials


class AddressConfigurationTests(SimpleTestCase):
    def test_writer_credentials_are_refused_in_the_web_process(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_process_credentials({"ADDRESS_IMPORT_DB_USER": "directory-writer"})
        validate_process_credentials({
            "ADDRESS_IMPORT_DB_USER": "directory-writer",
            "DJANGO_SETTINGS_MODULE": "leg_platform.import_settings",
        })
        validate_process_credentials({})

    def setUp(self):
        self.env = {
            "DB_NAME": "application", "DB_USER": "application-reader",
            "ADDRESS_DB_NAME": "directory", "ADDRESS_DB_USER": "directory-reader",
            "ADDRESS_DB_PASSWORD": "synthetic-test-value",
        }

    def test_web_reader_configuration_contains_no_importer_credentials(self):
        self.env.update(ADDRESS_IMPORT_DB_USER="directory-writer", ADDRESS_IMPORT_DB_PASSWORD="writer-value")
        reader = address_database(self.env, ("STRICT_TRANS_TABLES",))
        self.assertEqual(reader["USER"], "directory-reader")
        self.assertNotIn("writer-value", repr(reader))
        self.assertEqual(reader["OPTIONS"]["isolation_level"], "read committed")
        self.assertIsNone(address_database({}, ()))

    def test_partial_config_ports_and_shared_accounts_are_rejected(self):
        for overrides in (
            {"ADDRESS_DB_USER": ""}, {"ADDRESS_DB_PORT": "65536"},
            {"ADDRESS_DB_PORT": "²"}, {"ADDRESS_DB_PORT": "0"},
            {"ADDRESS_DB_NAME": "application"}, {"ADDRESS_DB_USER": "application-reader"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ImproperlyConfigured):
                address_database({**self.env, **overrides}, ())

    def test_writer_is_explicit_and_uses_same_directory_with_different_account(self):
        reader = address_database(self.env, ())
        self.env.update(ADDRESS_IMPORT_DB_USER="directory-writer", ADDRESS_IMPORT_DB_PASSWORD="writer-value")
        writer = importer_database(self.env, reader)
        self.assertEqual(writer["NAME"], reader["NAME"])
        self.assertNotEqual(writer["USER"], reader["USER"])
        self.assertEqual(reader["USER"], "directory-reader")
        for values, configuration in (
            ({}, reader), (self.env, None),
            ({**self.env, "ADDRESS_IMPORT_DB_USER": "directory-reader"}, reader),
            ({**self.env, "ADDRESS_IMPORT_DB_USER": "application-reader"}, reader),
        ):
            with self.subTest(values=values), self.assertRaises(ImproperlyConfigured):
                importer_database(values, configuration)
