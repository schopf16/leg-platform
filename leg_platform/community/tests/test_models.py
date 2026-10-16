"""The community models (D-077): constraints, normalisation, status filter, the singleton and the
permissions. Shared data is created once per class."""

from datetime import date

from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, tag

from leg_platform.accounts.models import EXACT_COLLATION
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Direction,
    HistoryEntry,
    Leg,
    LegAssignment,
    ListPreference,
    Location,
    ManagementSettings,
    MeteringPoint,
    PersonAssignment,
    PersonRecord,
    TransformerCircuit,
)
from leg_platform.community.status import MeteringPointStatus, derive_status
from leg_platform.community.tests.helpers import (
    day,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    number,
)

TODAY = day()


@tag("mariadb")
class LocationAndCircuitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.location = make_location("Musterweg", "12", "3063", "Ittigen")
        cls.circuit = make_circuit("TRA21359", "Ittigen Nord")

    def test_the_address_is_stored_normalised_and_unique_ignoring_case_and_accents(self):
        location = make_location("  Muster   strasse ", " 1 ", "3000", "Bern")
        self.assertEqual((location.street, location.house_number), ("Muster strasse", "1"))

        for street, number_, town in (("muster strasse", "1", "BERN"), ("Muster  strasse", "1", "Bern")):
            with self.subTest(street=street), self.assertRaises(IntegrityError), transaction.atomic():
                make_location(street, number_, "3000", town)
        # Different house number or postal code: a different location.
        make_location("Muster strasse", "1a", "3000", "Bern")
        make_location("Muster strasse", "1", "3001", "Bern")

    def test_validation_reports_a_duplicate_and_a_bad_postal_code(self):
        duplicate = Location(street="MUSTERWEG", house_number="12", postal_code="3063", town="Ittigen")
        with self.assertRaises(ValidationError) as caught:
            duplicate.full_clean()
        codes = [e.code for errors in caught.exception.error_dict.values() for e in errors]
        self.assertIn("duplicate_address", codes)
        for bad in ("123", "0999", "12345", "abcd"):
            with self.subTest(postal_code=bad), self.assertRaises(ValidationError):
                Location(street="A", house_number="1", postal_code=bad, town="X").full_clean(
                    exclude=["address_digest"]
                )
        # An unchanged location is not its own duplicate.
        self.location.full_clean()

    def test_find_by_address_uses_the_normalised_comparison(self):
        self.assertEqual(Location.find_by_address("musterweg", " 12", "3063", "ittigen"), self.location)
        self.assertIsNone(Location.find_by_address("Musterweg", "13", "3063", "Ittigen"))

    def test_saving_only_the_street_keeps_the_key_in_step(self):
        location = make_location("Alpha", "1", "3000", "Bern")
        location.street = "Beta"
        location.save(update_fields=["street"])
        location.refresh_from_db()
        self.assertEqual(Location.find_by_address("Beta", "1", "3000", "Bern"), location)

    def test_the_circuit_number_is_unique_after_capitals_and_trimming(self):
        circuit = TransformerCircuit.objects.create(bkw_number=" tra99 ", name="  free   name ")
        self.assertEqual((circuit.bkw_number, circuit.name), ("TRA99", "free name"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            TransformerCircuit.objects.create(bkw_number="TRA21359")
        # The name is not unique.
        TransformerCircuit.objects.create(bkw_number="TRA100", name="Ittigen Nord")
        with self.assertRaises(ValidationError):
            TransformerCircuit(bkw_number="XYZ1").full_clean()

    def test_the_suggestion_is_remembered_until_the_address_changes(self):
        from leg_platform.community.addressing import AddressSuggestion

        location = self.location
        location.apply_suggestion(AddressSuggestion("Musterweg", "12", "3063", "Ittigen Dorf"))
        self.assertTrue(location.address_correction_open)

        location.dismiss_suggestion()
        self.assertFalse(location.address_correction_open)
        # The same suggestion again for the same address stays dismissed.
        location.apply_suggestion(AddressSuggestion("Musterweg", "12", "3063", "Ittigen Dorf"))
        self.assertFalse(location.address_correction_open)
        # A change of the address (here only the spelling) brings it back.
        location.street = "musterweg"
        location.apply_suggestion(AddressSuggestion("Musterweg", "12", "3063", "Ittigen Dorf"))
        self.assertTrue(location.address_correction_open)
        # No suggestion: nothing open.
        location.apply_suggestion(None)
        self.assertFalse(location.address_correction_open)
        self.assertIsNone(location.suggestion)


@tag("mariadb")
class MeteringPointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.location = make_location()
        cls.mp = make_metering_point(1, location=cls.location)

    def test_the_number_is_stored_in_capitals_and_unique_and_valid(self):
        mp = MeteringPoint.objects.create(number=" " + number(7).lower() + " ", direction=Direction.FEED_IN)
        self.assertEqual(mp.number, number(7))
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_metering_point(1)
        with self.assertRaises(ValidationError):
            MeteringPoint(number="SHORT", direction=Direction.FEED_IN).full_clean()
        with self.assertRaises(ValidationError):
            MeteringPoint(number=number(8), direction="both").full_clean()

    def test_the_status_of_the_object_and_the_database_filter_agree(self):
        cases = [
            {},
            {"registered_with_bkw_on": date(2026, 6, 1)},
            {"registered_with_bkw_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2)},
            {"registered_with_bkw_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2), "start_date": date(2026, 6, 16)},
            {"registered_with_bkw_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2), "start_date": TODAY},
            {"registered_with_bkw_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2), "start_date": date(2026, 6, 3),
             "terminated_on": TODAY},
            {"registered_with_bkw_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2), "start_date": date(2026, 6, 3),
             "terminated_on": date(2026, 6, 30)},
        ]
        created = [
            make_metering_point(100 + index, **values) for index, values in enumerate(cases)
        ]
        for mp in created:
            expected = mp.status(TODAY)
            with self.subTest(number=mp.number, status=expected):
                self.assertEqual(expected, derive_status(
                    registered_with_bkw_on=mp.registered_with_bkw_on, confirmed_on=mp.confirmed_on,
                    start_date=mp.start_date, terminated_on=mp.terminated_on, today=TODAY))
                for status in MeteringPointStatus:
                    matches = MeteringPoint.objects.with_status(status, TODAY).filter(pk=mp.pk).exists()
                    self.assertEqual(matches, status == expected, status)
        self.assertEqual(
            [mp.status(TODAY) for mp in created],
            ["registered", "notified", "confirmed", "confirmed", "active", "terminated", "active"],
        )
        self.assertEqual(created[0].get_status_display(TODAY), "registriert")

    def test_the_decimals_must_not_be_negative(self):
        with self.assertRaises(ValidationError):
            MeteringPoint(number=number(9), direction=Direction.CONSUMPTION, pv_power_kwp=-1).full_clean()


@tag("mariadb")
class LegAndAssignmentConstraintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leg = make_leg("LEG Bahnhof")
        cls.mp = make_metering_point(1)

    def test_the_name_is_unique_ignoring_case(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_leg("leg bahnhof")

    def test_a_quota_needs_its_date(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Leg.objects.create(name="X", production_quota_percent=30)
        with self.assertRaises(ValidationError):
            Leg(name="Y", production_quota_percent=30).full_clean()
        Leg.objects.create(name="Z", production_quota_percent=30, quota_checked_on=TODAY)
        Leg.objects.create(name="W")  # no quota, no date
        for bad in (-1, 101):
            with self.assertRaises(ValidationError):
                Leg(name="Q", production_quota_percent=bad, quota_checked_on=TODAY).full_clean()

    def test_an_assignment_cannot_end_before_it_starts_or_start_twice(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            LegAssignment.objects.create(
                metering_point=self.mp, leg=self.leg, valid_from=TODAY, valid_to=day(-1)
            )
        LegAssignment.objects.create(metering_point=self.mp, leg=self.leg, valid_from=TODAY)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LegAssignment.objects.create(metering_point=self.mp, leg=self.leg, valid_from=TODAY)

    def test_the_assignment_queryset_knows_what_is_in_force(self):
        LegAssignment.objects.create(metering_point=self.mp, leg=self.leg, valid_from=day(-30), valid_to=day(-1))
        current = LegAssignment.objects.create(metering_point=self.mp, leg=self.leg, valid_from=day(0), valid_to=day(10))
        future = LegAssignment.objects.create(metering_point=self.mp, leg=self.leg, valid_from=day(11))

        self.assertEqual(list(LegAssignment.objects.in_force(TODAY)), [current])
        self.assertEqual(list(LegAssignment.objects.in_force(day(10))), [current])
        self.assertEqual(list(LegAssignment.objects.in_force(day(11))), [future])
        self.assertEqual(list(LegAssignment.objects.not_ended(TODAY)), [current, future])


@tag("mariadb")
class SingletonHistoryAndPermissionTests(TestCase):
    def test_the_settings_row_exists_once_with_defaults(self):
        row = ManagementSettings.load()
        self.assertEqual(row.pk, 1)
        self.assertEqual((row.country_code, row.identifier, row.warning_threshold_percent), ("CH", "", 10))
        self.assertEqual(ManagementSettings.load().pk, 1)
        self.assertEqual(ManagementSettings.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ManagementSettings.objects.create(id=2)
        row.identifier = "1A2B3C4D5E6"
        self.assertEqual(row.metering_point_prefix, "CH1A2B3C4D5E6")

    def test_the_history_is_append_only(self):
        entry = HistoryEntry.objects.create(subject_type="leg", subject_id=1, event=HistoryEvent.CREATED)
        entry.event = "other"
        with self.assertRaises(ValueError):
            entry.save()

    def test_identifier_columns_use_the_binary_collation(self):
        for model, name in (
            (MeteringPoint, "number"), (TransformerCircuit, "bkw_number"), (Location, "address_digest"),
        ):
            with self.subTest(model=model.__name__):
                self.assertEqual(model._meta.get_field(name).db_collation, EXACT_COLLATION)
                self.assertTrue(model._meta.get_field(name).unique)

    def test_the_rights_of_the_models_have_english_codenames(self):
        def codenames(model):
            return set(
                Permission.objects.filter(
                    content_type__app_label="community", content_type__model=model._meta.model_name
                ).values_list("codename", flat=True)
            )

        self.assertEqual(codenames(Location), {"view_location", "add_location", "change_location", "delete_location"})
        self.assertEqual(
            codenames(MeteringPoint),
            {"view_meteringpoint", "add_meteringpoint", "change_meteringpoint", "delete_meteringpoint"},
        )
        self.assertEqual(codenames(Leg), {"view_leg", "add_leg", "change_leg", "delete_leg"})
        self.assertEqual(codenames(TransformerCircuit), {"view_transformercircuit", "add_transformercircuit", "change_transformercircuit"})
        self.assertEqual(codenames(ManagementSettings), {"view_managementsettings", "change_managementsettings"})
        self.assertEqual(
            codenames(PersonRecord), {"view_personrecord", "change_personrecord", "correct_person_address"}
        )
        # Assignments, history and preferences have no rights of their own.
        for model in (PersonAssignment, LegAssignment, HistoryEntry, ListPreference):
            self.assertEqual(codenames(model), set(), model.__name__)

    def test_no_group_holds_a_right_of_the_community_after_the_migrations(self):
        from django.contrib.auth.models import Group

        self.assertEqual(Group.objects.count(), 0)

    def test_the_community_models_are_not_in_the_django_administration(self):
        # The pages of the management area are the interface (D-077); the model registry test of
        # the accounts app (D-047) stays an exact set.
        registered = {model._meta.app_label for model in admin.site._registry}
        self.assertNotIn("community", registered)
