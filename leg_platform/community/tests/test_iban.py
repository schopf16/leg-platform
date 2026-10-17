"""The IBAN of a person (D-087): validation with the package, the normal form and the display, the encryption, and the data
functions of the community migration 0005 on the real model."""

import importlib

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, tag

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.community import iban
from leg_platform.community.models import PersonRecord

migration = importlib.import_module("leg_platform.community.migrations.0005_encrypted_iban")
GOOD = "CH9300762011623852957"


class IbanTests(SimpleTestCase):
    def test_the_normal_form_is_upper_case_without_spaces_and_hyphens(self):
        self.assertEqual(iban.compact(" ch93 0076-2011 6238  5295 7 "), GOOD)
        self.assertEqual(iban.validate("ch93 0076 2011 6238 5295 7"), GOOD)
        self.assertEqual(iban.validate(""), "")
        self.assertEqual(iban.validate(None), "")

    def test_any_iban_with_a_valid_checksum_is_accepted(self):
        for valid in ("DE89 3704 0044 0532 0130 00", "LI21 0881 0000 2324 013A A", "GB82 WEST 1234 5698 7654 32", GOOD):
            with self.subTest(valid=valid):
                self.assertEqual(iban.validate(valid), iban.compact(valid))

    def test_a_wrong_checksum_length_country_or_character_is_refused(self):
        for bad in (
            "CH9300762011623852958", "CH93007620116238529", "XX0000000000", "CH93 0076 2011 6238 5295 7 1",
            "ÄÄ9300762011623852957", "CH93<0076>2011623852957", "1" * 35, "٣٣" * 11,
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError) as caught:
                iban.validate(bad)
            self.assertEqual(caught.exception.code, "invalid_iban")

    def test_the_display_form_has_groups_of_four(self):
        self.assertEqual(iban.display(GOOD), "CH93 0076 2011 6238 5295 7")
        self.assertEqual(iban.display("ch93 0076"), "CH93 0076")
        self.assertEqual(iban.display(""), "")

    def test_encryption_is_reversible_and_not_deterministic(self):
        first, second = iban.encrypt_iban(GOOD), iban.encrypt_iban("ch93 0076 2011 6238 5295 7")
        self.assertNotEqual(first, second)
        self.assertEqual((iban.decrypt_iban(first), iban.decrypt_iban(second)), (GOOD, GOOD))
        self.assertEqual((iban.encrypt_iban(""), iban.decrypt_iban("")), ("", ""))
        self.assertNotIn("CH93", first)


@tag("mariadb")
class MigrationDataFunctionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.first = PersonRecord.objects.create(person=create_active_user("first.iban@example.org"))
        cls.second = PersonRecord.objects.create(person=create_active_user("second.iban@example.org"))
        cls.empty = PersonRecord.objects.create(person=create_active_user("empty.iban@example.org"))

    def raw(self, record):
        return PersonRecord.objects.filter(pk=record.pk).values_list("iban_encrypted", flat=True).get()

    def test_the_plain_values_are_encrypted_in_place_and_decrypted_again_when_the_migration_is_reversed(self):
        # The state before the migration: the plain text in the column (a written IBAN, one with spaces, none).
        PersonRecord.objects.filter(pk=self.first.pk).update(iban_encrypted=GOOD)
        PersonRecord.objects.filter(pk=self.second.pk).update(iban_encrypted="de89 3704 0044 0532 0130 00")

        migration.encrypt_existing(apps, None)

        self.assertEqual((self.raw(self.first) != GOOD, self.raw(self.first).startswith("gAAAA")), (True, True))
        self.assertEqual(PersonRecord.objects.get(pk=self.first.pk).iban, GOOD)
        self.assertEqual(PersonRecord.objects.get(pk=self.second.pk).iban, "DE89370400440532013000")
        self.assertEqual(self.raw(self.empty), "")
        # The label of the migration is the one of the code (the data would otherwise be unreadable).
        self.assertEqual(migration.PURPOSE, iban.PERSON_IBAN_PURPOSE)

        migration.decrypt_existing(apps, None)

        self.assertEqual((self.raw(self.first), self.raw(self.second), self.raw(self.empty)), (GOOD, "DE89370400440532013000", ""))
