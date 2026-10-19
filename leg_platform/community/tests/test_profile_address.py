"""The building at the account's profile address (D-105, PROFIL.md section 1): the EGID is shown and the address is locked
only where the address of the profile is a building of the account itself."""

from datetime import date

from django.test import TestCase, tag

from leg_platform.community import buildings
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point
from leg_platform.community.tests.onboarding import customer

EGID = 61_001


@tag("mariadb")
class ProfileAddressTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer(
            "profile.address@example.org", street="Musterstrasse", house_number="1", swiss_postal_code=3063, town="Ittigen"
        )
        cls.other = customer("profile.address.other@example.org")

    def building(self, person=None, **extra):
        location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=EGID, **extra)
        assign_person(make_metering_point(1, location=location), person or self.person)
        return location

    def test_an_own_confirmed_building_gives_the_egid_and_the_lock(self):
        self.building()

        info = buildings.profile_address_building(self.person)

        self.assertEqual((info.egid, info.locked), (EGID, True))

    def test_an_own_building_that_is_not_confirmed_is_not_locked(self):
        self.building(needs_confirmation=True)

        info = buildings.profile_address_building(self.person)

        self.assertEqual((info.egid, info.locked), (EGID, False))

    def test_the_comparison_ignores_case_and_spacing_of_the_address(self):
        self.building()
        self.person.street = "  musterstrasse "
        self.assertEqual(buildings.profile_address_building(self.person).egid, EGID)

    def test_nothing_for_a_building_of_another_account_an_archived_one_or_another_address(self):
        owner = customer(
            "profile.address.owner@example.org", street="Musterstrasse", house_number="1", swiss_postal_code=3063,
            town="Ittigen",
        )
        location = self.building(person=owner)
        # The same address, but the building is another account's: nothing.
        self.assertIsNone(buildings.profile_address_building(self.person))
        self.assertEqual(buildings.profile_address_building(owner).egid, EGID)
        location.is_archived = True
        location.save()
        self.assertIsNone(buildings.profile_address_building(owner))
        stranger = customer(
            "profile.address.third@example.org", street="Nebenweg", house_number="4", swiss_postal_code=3063, town="Ittigen"
        )
        self.assertIsNone(buildings.profile_address_building(stranger))

    def test_a_former_holder_owns_nothing(self):
        location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=EGID)
        assign_person(make_metering_point(1, location=location), self.person, date(2020, 1, 1), date(2020, 12, 31))

        self.assertIsNone(buildings.profile_address_building(self.person))

    def test_an_incomplete_address_finds_nothing(self):
        self.building()
        for field, value in (("street", ""), ("town", ""), ("swiss_postal_code", None)):
            with self.subTest(field=field):
                person = customer(f"incomplete.{field}@example.org", street="Musterstrasse", house_number="1",
                                  swiss_postal_code=3063, town="Ittigen")
                setattr(person, field, value)
                self.assertIsNone(buildings.profile_address_building(person))
