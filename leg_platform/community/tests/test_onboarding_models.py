"""The models of the onboarding (D-103): identity of a building, the deadline settings, the contract rows and the cascade
of an account deletion."""

from django.apps import apps
from django.db import IntegrityError, transaction
from django.test import TestCase, tag

from leg_platform.community.models import (
    Application,
    ContractAcceptance,
    ContractVersion,
    Location,
    ManagementSettings,
    MeteringPoint,
)
from leg_platform.community.tests.helpers import make_location, staff_user
from leg_platform.community.tests.onboarding import customer, pdf, publish_contract


@tag("mariadb")
class LocationIdentityTests(TestCase):
    def test_the_egid_is_unique_and_several_buildings_may_have_none(self):
        make_location(egid=90004269)
        make_location(street="Other", egid=None)
        make_location(street="Another", egid=None)

        with self.assertRaises(IntegrityError), transaction.atomic():
            make_location(street="Third", egid=90004269)

    def test_the_egid_is_validated(self):
        location = Location(street="A", house_number="1", postal_code="3000", town="Bern", egid=0)
        with self.assertRaises(Exception):
            location.full_clean()
        location.egid = 1_000_000_000
        with self.assertRaises(Exception):
            location.full_clean()
        location.egid = 999_999_999
        location.full_clean()

    def test_the_names_have_a_length_limit(self):
        from leg_platform.community.models import CustomerBuilding

        self.assertEqual(CustomerBuilding._meta.get_field("name").max_length, 80)
        self.assertEqual(CustomerBuilding._meta.get_field("description").max_length, 200)
        self.assertEqual(MeteringPoint._meta.get_field("name").max_length, 80)
        # The names are per customer: the shared location has none.
        self.assertNotIn("name", [field.name for field in Location._meta.get_fields()])
        self.assertNotIn("description", [field.name for field in Location._meta.get_fields()])

    def test_one_label_per_account_and_building(self):
        from leg_platform.community.models import CustomerBuilding

        location = make_location(egid=90004269)
        first, second = customer("label.first@example.org"), customer("label.second@example.org")
        CustomerBuilding.objects.create(person=first, location=location, name="A")
        CustomerBuilding.objects.create(person=second, location=location, name="B")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomerBuilding.objects.create(person=first, location=location, name="C")


@tag("mariadb")
class SettingsTests(TestCase):
    def test_the_defaults_are_the_examples_of_the_owner(self):
        row = ManagementSettings.load()
        self.assertEqual(
            (row.reminder_after_days, row.rollback_after_days, row.contract_deadline_days,
             row.operator_confirmation_days, row.iban_missing_reminder_days),
            (2, 14, 14, 14, 7),
        )


@tag("mariadb")
class ContractRowsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("rows.staff@example.org")

    def test_only_one_version_can_be_marked_current(self):
        first = publish_contract(self.staff, pdf(b"one"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContractVersion.objects.create(
                version_date=first.version_date, pdf=pdf(b"two"), sha256="b" * 64, size=10, current_marker=True
            )
        # Any number of versions without the marker is fine.
        for index in range(3):
            ContractVersion.objects.create(
                version_date=first.version_date, pdf=pdf(), sha256=f"{index:064d}", size=10
            )

    def test_an_acceptance_is_append_only_and_once_per_person_and_version(self):
        version = publish_contract(self.staff)
        person = customer()
        acceptance = ContractAcceptance.objects.create(
            person=person, version=version, sha256=version.sha256, data_exchange_accepted=True
        )
        acceptance.data_exchange_accepted = False
        with self.assertRaises(ValueError):
            acceptance.save()
        with self.assertRaises(ValueError):
            acceptance.delete()
        with self.assertRaises(ValueError):
            ContractAcceptance.objects.update(sha256="x")
        with self.assertRaises(ValueError):
            ContractAcceptance.objects.all().delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContractAcceptance.objects.create(
                person=person, version=version, sha256=version.sha256, data_exchange_accepted=True
            )

    def test_an_account_with_an_acceptance_is_kept_when_deleted_and_one_with_only_drafts_goes(self):
        from leg_platform.community import contracts

        person = customer("kept.customer@example.org")
        version = publish_contract(self.staff)
        contracts.accept_contract(person, version.pk, agrees_contract=True, agrees_data_exchange=True)
        Application.objects.create(person=person)

        self.assertEqual(person.delete_or_deactivate(), "deactivated")

        self.assertEqual(ContractAcceptance.objects.filter(person=person).count(), 1)
        person.refresh_from_db()
        self.assertTrue(person.retention_obligation)

        drafter = customer("drafter.customer@example.org")
        Application.objects.create(person=drafter)
        self.assertEqual(drafter.delete_or_deactivate(), "deleted")
        self.assertEqual(Application.objects.filter(person=drafter).count(), 0)
        self.assertTrue(ContractVersion.objects.filter(pk=version.pk).exists())

    def test_a_raw_deletion_cascades_to_the_rows_of_the_account(self):
        person = customer("gone.customer@example.org")
        version = publish_contract(self.staff)
        ContractAcceptance.objects.create(
            person=person, version=version, sha256=version.sha256, data_exchange_accepted=True
        )
        Application.objects.create(person=person)

        person.delete()

        self.assertEqual(ContractAcceptance.objects.count(), 0)
        self.assertEqual(Application.objects.count(), 0)
        self.assertTrue(ContractVersion.objects.filter(pk=version.pk).exists())


class NoRightsTests(TestCase):
    def test_the_new_tables_create_no_rights_and_are_not_in_the_administration(self):
        from django.contrib import admin

        names = {
            "Application", "ApplicationBuilding", "ApplicationMeteringPoint", "ApplicationNotice", "CustomerBuilding",
            "ContractVersion", "ContractAcceptance", "Deregistration", "DeregistrationItem",
        }
        for model in apps.get_app_config("community").get_models():
            if model.__name__ in names:
                with self.subTest(model=model.__name__):
                    self.assertEqual(tuple(model._meta.default_permissions), ())
                    self.assertNotIn(model, admin.site._registry)
