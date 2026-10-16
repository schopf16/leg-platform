"""The directory suggestion for an address (D-077) against a real, small directory file."""

import uuid
from datetime import timedelta

from django.test import TestCase, tag
from django.utils import timezone

from leg_platform.addresses.models import Address, Directory
from leg_platform.community import addressing

STREETS = (
    ("Musterweg", "12", "3063", "Ittigen"),
    ("Musterweg", "12a", "3063", "Ittigen"),
    ("Bahnhofstrasse", "5", "3063", "Ittigen"),
    ("Bahnhofstrasse Nord", "5", "3063", "Ittigen"),
    ("Dorfstrasse", "7", "3000", "Bern"),
    ("Dorfstrasse", "7", "3001", "Bern Dorf"),
)


@tag("mariadb")
class AddressCheckTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=cls.generation, data_date=timezone.localdate(),
            imported_at=timezone.now(), checksum="0" * 64, row_count=len(STREETS),
        )
        for street, number, postal_code, locality in STREETS:
            Address.objects.create(
                generation=cls.generation, street=street, house_number=number,
                postal_code=postal_code, locality=locality,
            )

    def suggestion(self, *address):
        found = addressing.suggest_correction(*address)
        return None if found is None else found.as_tuple()

    def test_an_address_as_the_directory_has_it_has_no_suggestion(self):
        self.assertIsNone(self.suggestion("Musterweg", "12", "3063", "Ittigen"))
        self.assertIsNone(self.suggestion(" Musterweg ", "12", "3063", " Ittigen "))

    def test_a_different_spelling_or_town_in_the_same_postal_code_is_suggested(self):
        self.assertEqual(self.suggestion("musterweg", "12", "3063", "Ittigen"),
                         ("Musterweg", "12", "3063", "Ittigen"))
        self.assertEqual(self.suggestion("Musterweg", "12", "3063", "Bern"),
                         ("Musterweg", "12", "3063", "Ittigen"))
        # A typing error (approximate match, D-065).
        self.assertEqual(self.suggestion("Musterwg", "12", "3063", "Ittigen"),
                         ("Musterweg", "12", "3063", "Ittigen"))
        # Only the start of the street.
        self.assertEqual(self.suggestion("Muster", "12a", "3063", "Ittigen"),
                         ("Musterweg", "12a", "3063", "Ittigen"))

    def test_no_suggestion_when_it_is_not_clear_what_was_meant(self):
        # Two streets start with it: the typed one is a complete street and is found as typed.
        self.assertIsNone(self.suggestion("Bahnhofstrasse", "5", "3063", "Ittigen"))
        # Two streets start with it and neither is the typed one: ambiguous.
        self.assertIsNone(self.suggestion("Bahnhof", "5", "3063", "Ittigen"))
        # The house number is not in the directory.
        self.assertIsNone(self.suggestion("Musterweg", "13", "3063", "Ittigen"))
        # Another postal code is never guessed.
        self.assertIsNone(self.suggestion("Musterweg", "12", "3000", "Ittigen"))
        # An address in its own postal code is found as typed, also if the street exists elsewhere.
        self.assertIsNone(self.suggestion("Dorfstrasse", "7", "3001", "Bern Dorf"))

    def test_incomplete_or_invalid_input_has_no_suggestion(self):
        for address in (
            ("", "12", "3063", "Ittigen"), ("Musterweg", "12", "", "Ittigen"),
            ("Musterweg", "12", "30", "Ittigen"), ("Musterweg", "12", "3063", ""),
            ("Musterweg", "12", "30a3", "Ittigen"), ("Musterweg" * 40, "12", "3063", "Ittigen"),
        ):
            with self.subTest(address=address):
                self.assertIsNone(self.suggestion(*address))

    def test_a_directory_that_is_old_gives_no_suggestion(self):
        Directory.objects.filter(pk=1).update(data_date=timezone.localdate() - timedelta(days=93))
        self.assertIsNone(self.suggestion("musterweg", "12", "3063", "Ittigen"))

    def test_without_a_directory_nothing_is_said(self):
        Directory.objects.all().delete()
        self.assertIsNone(self.suggestion("musterweg", "12", "3063", "Ittigen"))
        self.assertEqual(addressing.not_found_notice("Musterweg", "12", "3063", "Ittigen"), "")

    def test_the_notice_is_given_only_for_an_address_the_directory_does_not_know(self):
        self.assertEqual(addressing.not_found_notice("Musterweg", "12", "3063", "Ittigen"), "")
        notice = addressing.not_found_notice("Nowhere", "1", "3063", "Ittigen")
        self.assertIn("nicht gefunden", notice)
        self.assertEqual(addressing.not_found_notice("", "1", "3063", "Ittigen"), "")

    def test_a_suggestion_replaces_the_notice(self):
        check = addressing.check_address("musterweg", "12", "3063", "Ittigen")
        self.assertIsNotNone(check.suggestion)
        self.assertEqual(check.not_found_notice, "")
        unknown = addressing.check_address("Nowhere", "1", "3063", "Ittigen")
        self.assertIsNone(unknown.suggestion)
        self.assertNotEqual(unknown.not_found_notice, "")
