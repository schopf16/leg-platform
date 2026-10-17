"""The community models (D-077): constraints, normalisation, status filter, the singleton and the
absence of rights (D-086). Shared data is created once per class."""

from datetime import date

from django.apps import apps
from django.contrib import admin
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
    Location,
    ManagementSettings,
    MeteringPoint,
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
        circuit = TransformerCircuit.objects.create(grid_number=" tra99 ", name="  free   name ")
        self.assertEqual((circuit.grid_number, circuit.name), ("TRA99", "free name"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            TransformerCircuit.objects.create(grid_number="TRA21359")
        # The name is not unique.
        TransformerCircuit.objects.create(grid_number="TRA100", name="Ittigen Nord")
        with self.assertRaises(ValidationError):
            TransformerCircuit(grid_number="XYZ1").full_clean()

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
            {"registered_with_operator_on": date(2026, 6, 1)},
            {"registered_with_operator_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2)},
            {"registered_with_operator_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2), "start_date": date(2026, 6, 16)},
            {"registered_with_operator_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2), "start_date": TODAY},
            {"registered_with_operator_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2), "start_date": date(2026, 6, 3),
             "terminated_on": TODAY},
            {"registered_with_operator_on": date(2026, 6, 1), "confirmed_on": date(2026, 6, 2), "start_date": date(2026, 6, 3),
             "terminated_on": date(2026, 6, 30)},
        ]
        created = [
            make_metering_point(100 + index, **values) for index, values in enumerate(cases)
        ]
        for mp in created:
            expected = mp.status(TODAY)
            with self.subTest(number=mp.number, status=expected):
                self.assertEqual(expected, derive_status(
                    registered_with_operator_on=mp.registered_with_operator_on, confirmed_on=mp.confirmed_on,
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
        for bad in (-1, 1000):  # no upper bound of 100 %, but at most 999.99 (D-088)
            with self.assertRaises(ValidationError):
                Leg(name="Q", production_quota_percent=bad, quota_checked_on=TODAY).full_clean()
        for good in (100, 204.1, 999.99):
            Leg(name="R", production_quota_percent=good, quota_checked_on=TODAY).full_clean()

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
class SingletonAndHistoryTests(TestCase):
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
            (MeteringPoint, "number"), (TransformerCircuit, "grid_number"), (Location, "address_digest"),
        ):
            with self.subTest(model=model.__name__):
                self.assertEqual(model._meta.get_field(name).db_collation, EXACT_COLLATION)
                self.assertTrue(model._meta.get_field(name).unique)

    # The plain tables of the Django administration have the "view" right (D-091); the grid operator and the
    # list preferences have none. Nothing has an add, change or delete right and no custom right.
    VIEWABLE = {
        "HistoryEntry", "Leg", "LegAssignment", "Location", "ManagementSettings", "MeteringPoint",
        "PersonAssignment", "PersonRecord", "ShareChange", "TransformerCircuit",
    }

    def test_the_community_models_have_at_most_the_view_right_for_the_administration_tables(self):
        # D-086: every staff account may use the management area; no model creates a right it needs.
        for model in apps.get_app_config("community").get_models():
            with self.subTest(model=model.__name__):
                expected = ("view",) if model.__name__ in self.VIEWABLE else ()
                self.assertEqual(tuple(model._meta.default_permissions), expected)
                self.assertEqual(list(model._meta.permissions), [])

    def test_no_group_holds_a_right_of_the_community_after_the_migrations(self):
        from django.contrib.auth.models import Group

        self.assertEqual(Group.objects.count(), 0)

    def test_the_community_tables_and_the_grid_operator_are_in_the_django_administration(self):
        # The pages of the management area are the interface (D-077); the administration shows plain read-only
        # tables of the data and, for system administrators, the grid operator of the instance (D-086, D-091).
        registered = {model._meta.label for model in admin.site._registry if model._meta.app_label == "community"}
        self.assertEqual(registered, {f"community.{name}" for name in self.VIEWABLE} | {"community.GridOperator"})
        # The preferences of a staff member's lists are no data to look at.
        self.assertNotIn("community.ListPreference", registered)
