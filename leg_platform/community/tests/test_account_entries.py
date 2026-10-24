"""What counts as an entry of an account for the recovery by email (D-106): buildings, metering points, an IBAN."""

from django.test import TestCase, tag

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.community import account_entries, own_iban
from leg_platform.community.models import Application, ApplicationBuilding, CustomerBuilding
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point
from leg_platform.community.tests.onboarding import GOOD_IBAN, start_with_building


_number = iter(range(1, 10_000))


def label(user):
    return CustomerBuilding.objects.create(person=user, location=make_location(house_number=str(next(_number))))


def assignment(user):
    number = next(_number)
    return assign_person(make_metering_point(number, location=make_location(house_number=str(number))), user)


@tag("mariadb")
class AccountEntriesTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.user = create_active_user("customer.person@example.org")
        self.other = create_active_user("other.person@example.org")

    def test_a_new_account_has_no_entries(self):
        self.assertFalse(account_entries.exist(self.user))

    def test_an_application_without_a_building_is_not_an_entry(self):
        Application.objects.create(person=self.user)

        self.assertFalse(account_entries.exist(self.user))

    def test_every_kind_of_entry_counts_for_its_own_account_only(self):
        entries = {
            "a building of an application": lambda user: start_with_building(user),
            "a name for a building": label,
            "an IBAN": lambda user: own_iban.set_own_iban(user, GOOD_IBAN),
            "a metering point of the LEG": assignment,
        }
        for name, add in entries.items():
            with self.subTest(entry=name):
                user = create_active_user(f"entry.{next(_number)}@example.org")
                self.assertFalse(account_entries.exist(user))

                add(user)

                self.assertTrue(account_entries.exist(user))
                self.assertFalse(account_entries.exist(self.user))

    def test_a_deleted_entry_is_none_any_more(self):
        application, _building = start_with_building(self.user)
        self.assertTrue(account_entries.exist(self.user))

        ApplicationBuilding.objects.filter(application=application).delete()

        self.assertFalse(account_entries.exist(self.user))

    def test_a_removed_iban_is_none_any_more(self):
        own_iban.set_own_iban(self.user, GOOD_IBAN)
        self.assertTrue(account_entries.exist(self.user))

        own_iban.remove_own_iban(self.user)

        self.assertFalse(account_entries.exist(self.user))
