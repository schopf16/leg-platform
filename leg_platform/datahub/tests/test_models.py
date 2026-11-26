"""The Datahub configuration row (D-139): one row, the write-only encrypted password, the collection switch."""

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from leg_platform.accounts.encryption import DecryptionError, encrypt, is_current
from leg_platform.community import raw_tables
from leg_platform.datahub.models import PASSWORD_PURPOSE, DatahubConfiguration

# Test values only; not secrets of any environment.
KEY_A = "test-key-a-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH"
KEY_B = "test-key-b-" + "HGFEDCBA9876543210zyxwvutsrqponmlkjihgfedcba"
PASSWORD = "Datahub-Test-Password-31"
OWN_EIC = "12X-00000020RL-R"


def complete(**values):
    """A row with everything a login needs (not saved)."""
    configuration = DatahubConfiguration(host="datahub.example.org", port=21, username="leg-test-user", **values)
    configuration.set_password(PASSWORD)
    return configuration


class SingletonTests(TestCase):
    def test_load_creates_and_returns_the_one_row(self):
        self.assertFalse(DatahubConfiguration.objects.exists())
        first = DatahubConfiguration.load()
        second = DatahubConfiguration.load()
        self.assertEqual(first.pk, 1)
        self.assertEqual(second.pk, 1)
        self.assertEqual(DatahubConfiguration.objects.count(), 1)

    def test_a_second_row_is_refused_by_the_database(self):
        DatahubConfiguration.load()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DatahubConfiguration.objects.create(id=2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DatahubConfiguration.objects.create(id=1)
        self.assertEqual(list(DatahubConfiguration.objects.values_list("pk", flat=True)), [1])


class PasswordTests(TestCase):
    def test_the_password_is_stored_encrypted(self):
        configuration = DatahubConfiguration.load()
        configuration.set_password(PASSWORD)
        configuration.save()
        stored = DatahubConfiguration.objects.get(pk=1).encrypted_password
        self.assertTrue(stored)
        self.assertNotEqual(stored, PASSWORD)
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(is_current(stored, PASSWORD_PURPOSE))
        self.assertTrue(DatahubConfiguration.objects.get(pk=1).has_password)

    def test_the_password_round_trip(self):
        configuration = DatahubConfiguration.load()
        configuration.set_password(PASSWORD)
        configuration.save()
        self.assertEqual(DatahubConfiguration.objects.get(pk=1).password(), PASSWORD)

    def test_another_purpose_label_cannot_read_the_password(self):
        configuration = DatahubConfiguration.load()
        configuration.encrypted_password = encrypt(PASSWORD.encode("utf-8"), b"another purpose")
        with self.assertRaises(DecryptionError):
            configuration.password()

    def test_an_empty_password_removes_the_stored_one(self):
        configuration = DatahubConfiguration.load()
        configuration.set_password(PASSWORD)
        configuration.save()
        configuration.set_password("")
        configuration.save()
        stored = DatahubConfiguration.objects.get(pk=1)
        self.assertEqual(stored.encrypted_password, "")
        self.assertFalse(stored.has_password)

    def test_a_password_under_a_fallback_key_is_read_and_encrypted_again(self):
        with override_settings(SECRET_KEY=KEY_A, SECRET_KEY_FALLBACKS=[]):
            configuration = DatahubConfiguration.load()
            configuration.set_password(PASSWORD)
            configuration.save()
            old = configuration.encrypted_password
        with override_settings(SECRET_KEY=KEY_B, SECRET_KEY_FALLBACKS=[KEY_A]):
            self.assertFalse(is_current(old, PASSWORD_PURPOSE))
            row = DatahubConfiguration.objects.get(pk=1)
            self.assertEqual(row.password(), PASSWORD)
            stored = DatahubConfiguration.objects.get(pk=1).encrypted_password
            self.assertNotEqual(stored, old)
            self.assertEqual(row.encrypted_password, stored)
            self.assertTrue(is_current(stored, PASSWORD_PURPOSE))
        # The old key alone cannot read the value any more: it was encrypted with the new key.
        with override_settings(SECRET_KEY=KEY_B, SECRET_KEY_FALLBACKS=[]):
            self.assertEqual(DatahubConfiguration.objects.get(pk=1).password(), PASSWORD)

    def test_a_password_no_key_can_read_raises_a_decryption_error(self):
        with override_settings(SECRET_KEY=KEY_A, SECRET_KEY_FALLBACKS=[]):
            configuration = DatahubConfiguration.load()
            configuration.set_password(PASSWORD)
            configuration.save()
        with override_settings(SECRET_KEY=KEY_B, SECRET_KEY_FALLBACKS=[]):
            with self.assertRaises(DecryptionError):
                DatahubConfiguration.objects.get(pk=1).password()
        for damaged in ("not-a-token", "ä"):
            with self.subTest(damaged=damaged):
                configuration.encrypted_password = damaged
                with self.assertRaises(DecryptionError):
                    configuration.password()

    def test_the_encrypted_password_is_never_editable(self):
        field = DatahubConfiguration._meta.get_field("encrypted_password")
        self.assertFalse(field.editable)
        self.assertIn("encrypted_password", raw_tables.NEVER_EDITABLE)


class CollectionActiveTests(TestCase):
    def test_the_server_configuration_decides(self):
        with override_settings(DATAHUB_COLLECTION_ALLOWED=False):
            self.assertFalse(complete(collection_enabled=True).collection_active)
        with override_settings(DATAHUB_COLLECTION_ALLOWED=True):
            self.assertTrue(complete(collection_enabled=True).collection_active)
            self.assertFalse(complete(collection_enabled=False).collection_active)

    def test_incomplete_data_never_collects(self):
        with override_settings(DATAHUB_COLLECTION_ALLOWED=True):
            for missing in ("host", "port", "username", "encrypted_password"):
                with self.subTest(missing=missing):
                    configuration = complete(collection_enabled=True)
                    setattr(configuration, missing, "" if missing != "port" else None)
                    self.assertFalse(configuration.is_complete)
                    self.assertFalse(configuration.collection_active)

