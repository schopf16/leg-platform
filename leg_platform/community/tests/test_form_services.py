"""The services behind the forms of the management area (D-093, D-095): the typed digits of a metering point number,
changing the location and the plant data of a metering point, creating a location together with a new transformer
circuit, and assigning a metering point to a LEG from the page of the LEG. Every refusal leaves the data as it was."""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, tag

from leg_platform.community import assignments, history, leg_services, locations, metering_points, numbers
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Location, MeteringPoint, TransformerCircuit
from leg_platform.community.tests.helpers import (
    assign_leg,
    day,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)

TODAY = day()


class TypedDigitsTests(SimpleTestCase):
    """What a person types of the number: only the last digits, filled up with zeros on the left (MC-CH 2025, 4.2.3)."""

    def test_short_entries_are_filled_up_to_twenty_digits(self):
        for typed, expected in (("23", "0" * 18 + "23"), ("0", "0" * 20), ("1" * 20, "1" * 20), (" 7 ", "0" * 19 + "7")):
            with self.subTest(typed=typed):
                self.assertEqual(numbers.validate_entered_digits(typed), expected)
        self.assertEqual(
            numbers.compose_metering_point_number("CH", "1A2B3C4D5E6", "23"), "CH1A2B3C4D5E6" + "0" * 18 + "23"
        )

    def test_every_wrong_entry_has_its_own_code_and_message(self):
        for typed, code in (
            ("", "empty"), ("   ", "empty"), (None, "empty"),
            ("12ab", "not_digits"), ("ab", "not_digits"), ("1 2", "not_digits"), ("1.5", "not_digits"),
            ("-1", "not_digits"), ("+1", "not_digits"), ("٣", "not_digits"), ("²", "not_digits"), ("1\n2", "not_digits"),
            ("1" * 21, "too_many_digits"), ("9" * 40, "too_many_digits"),
        ):
            with self.subTest(typed=typed):
                with self.assertRaises(ValidationError) as caught:
                    numbers.validate_entered_digits(typed)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ValidationError) as caught:
            numbers.validate_entered_digits("1" * 23)
        self.assertEqual(
            caught.exception.messages, ["Es sind höchstens 20 Ziffern erlaubt (eingegeben: 23)."]
        )  # the count of what was typed


@tag("mariadb")
class ChangingAMeteringPointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = staff_user("actor.change@example.org")
        cls.place = make_location("Musterweg", "12", "3063", "Ittigen", transformer_circuit=make_circuit("TRA1", "Eins"))
        cls.other = make_location("Zielweg", "3", "3063", "Ittigen")

    def setUp(self):
        self.point = make_metering_point(1)

    def test_the_location_is_set_changed_and_removed_with_a_history_entry_of_ids(self):
        metering_points.update(self.point, location=self.place, actor=self.actor)
        metering_points.update(self.point, location=self.other, actor=self.actor)
        metering_points.update(self.point, location=None, actor=self.actor)
        self.point.refresh_from_db()
        self.assertIsNone(self.point.location)
        details = [e.detail for e in history.entries_for(self.point).filter(event=HistoryEvent.METERING_POINT_LOCATION_CHANGED)]
        self.assertEqual(
            details,
            [
                {"old_location_id": None, "new_location_id": self.place.pk},
                {"old_location_id": self.place.pk, "new_location_id": self.other.pk},
                {"old_location_id": self.other.pk, "new_location_id": None},
            ],
        )

    def test_the_plant_data_is_saved_and_the_history_names_the_fields_but_never_the_values(self):
        metering_points.update(
            self.point, location=None, actor=self.actor,
            details={"pv_power_kwp": Decimal("9.5"), "battery_kwh": None, "wallbox_kw": None, "remark": "Dach"},
        )
        self.point.refresh_from_db()
        self.assertEqual((self.point.pv_power_kwp, self.point.remark), (Decimal("9.50"), "Dach"))
        entry = history.entries_for(self.point).get(event=HistoryEvent.METERING_POINT_DATA_CHANGED)
        self.assertEqual(entry.detail, {"fields": ["pv_power_kwp", "remark"]})
        # Nothing changed: nothing is written.
        count = history.entries_for(self.point).count()
        metering_points.update(
            self.point, location=None, actor=self.actor,
            details={"pv_power_kwp": Decimal("9.5"), "battery_kwh": None, "wallbox_kw": None, "remark": "Dach"},
        )
        self.assertEqual(history.entries_for(self.point).count(), count)

    def test_refusals_leave_the_metering_point_as_it_was(self):
        archived = make_location("Altweg", "9", is_archived=True)
        with self.assertRaises(locations.Archived):
            metering_points.update(self.point, location=archived, actor=self.actor)
        with self.assertRaises(metering_points.LocationUnavailable):
            metering_points.update(self.point, location=Location(pk=999999), actor=self.actor)
        with self.assertRaises(ValidationError):
            metering_points.update(self.point, location=self.place, details={"pv_power_kwp": Decimal("-1")}, actor=self.actor)
        self.point.refresh_from_db()
        self.assertIsNone(self.point.location)
        self.assertFalse(history.entries_for(self.point).exists())
        # An archived metering point is not changed.
        self.point.is_archived = True
        self.point.save()
        with self.assertRaises(metering_points.StatusError) as caught:
            metering_points.update(self.point, location=self.place, actor=self.actor)
        self.assertEqual(caught.exception.code, "archived")

    def test_a_point_may_stay_at_an_archived_location_while_its_data_changes(self):
        archived = make_location("Altweg", "9", is_archived=True)
        self.point.location = archived
        self.point.save()
        metering_points.update(self.point, location=archived, details={"remark": "x"}, actor=self.actor)
        self.point.refresh_from_db()
        self.assertEqual((self.point.location, self.point.remark), (archived, "x"))


@tag("mariadb")
class CreatingALocationWithACircuitTests(TestCase):
    databases = {"default", "addresses"}  # creating a location asks the address directory once
    ADDRESS = {"street": "Bachweg", "house_number": "7", "postal_code": "3000", "town": "Bern"}

    def test_the_circuit_and_the_location_are_created_in_one_transaction(self):
        location = locations.create_location(**self.ADDRESS, new_circuit=("tra777", "Neu"))
        self.assertEqual((location.transformer_circuit.grid_number, location.transformer_circuit.name), ("TRA777", "Neu"))
        self.assertEqual(TransformerCircuit.objects.count(), 1)

    def test_a_refused_location_creates_no_circuit_and_a_refused_circuit_creates_no_location(self):
        locations.create_location(**self.ADDRESS)
        with self.assertRaises(ValidationError):
            locations.create_location(**self.ADDRESS, new_circuit=("TRA888", ""))  # the address exists
        self.assertFalse(TransformerCircuit.objects.exists())
        for number in ("555", "TRA", "tra-1", "", "TRA" + "9" * 40):
            with self.subTest(number=number), self.assertRaises(ValidationError):
                locations.create_location(**{**self.ADDRESS, "street": "Anderer Weg"}, new_circuit=(number, ""))
        self.assertFalse(Location.objects.filter(street="Anderer Weg").exists())
        self.assertFalse(TransformerCircuit.objects.exists())
        # A circuit that exists is not created a second time, and the location is not created either.
        make_circuit("TRA999")
        with self.assertRaises(ValidationError) as caught:
            locations.create_location(**{**self.ADDRESS, "street": "Dritter Weg"}, new_circuit=("tra999", ""))
        self.assertIn("grid_number", caught.exception.error_dict)
        self.assertFalse(Location.objects.filter(street="Dritter Weg").exists())
        self.assertEqual(TransformerCircuit.objects.count(), 1)

    def test_changing_the_circuit_of_a_location_with_a_new_one_is_one_step(self):
        actor = staff_user("actor.circuit@example.org")
        location = make_location("Neuweg", "1")
        changed = locations.set_circuit(location, None, actor, new_circuit=("TRA555", "Fuenf"))
        self.assertEqual(changed.transformer_circuit.grid_number, "TRA555")
        entry = history.entries_for(location).get(event=HistoryEvent.TRANSFORMER_CIRCUIT_CHANGED)
        self.assertEqual(entry.detail, {"old": "", "new": "TRA555"})
        with self.assertRaises(ValidationError):
            locations.set_circuit(location, None, actor, new_circuit=("TRA555", ""))  # exists: refused, nothing changes
        location.refresh_from_db()
        self.assertEqual(location.transformer_circuit.grid_number, "TRA555")
        location.is_archived = True
        location.save()
        with self.assertRaises(locations.Archived):
            locations.set_circuit(location, None, actor, new_circuit=("TRA556", ""))
        self.assertFalse(TransformerCircuit.objects.filter(grid_number="TRA556").exists())  # rolled back


@tag("mariadb")
class AssigningAMeteringPointToALegTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = staff_user("actor.leg@example.org")
        cls.leg = make_leg("LEG Eins")
        cls.other = make_leg("LEG Zwei")

    def setUp(self):
        self.point = make_metering_point(1)

    def test_a_point_without_a_leg_joins_the_leg_from_the_day(self):
        assignment = leg_services.assign_metering_point(self.leg, self.point, TODAY, self.actor, today=TODAY)
        self.assertEqual((assignment.leg, assignment.valid_from, assignment.valid_to), (self.leg, TODAY, None))
        entry = history.entries_for(self.point).get(event=HistoryEvent.LEG_ASSIGNED)
        self.assertEqual(entry.detail, {"leg_id": self.leg.pk, "valid_from": TODAY.isoformat()})

    def test_a_point_that_has_a_leg_or_is_archived_or_a_leg_that_is_archived_is_refused(self):
        assign_leg(self.point, self.other, day(-30))
        with self.assertRaises(assignments.AssignmentError) as caught:
            leg_services.assign_metering_point(self.leg, self.point, TODAY, self.actor, today=TODAY)
        self.assertEqual(caught.exception.code, "already_has_leg")
        self.assertEqual(self.point.leg_assignments.count(), 1)
        # A LEG assignment that ended in the past does not count: the point has none now.
        ended = make_metering_point(2)
        assign_leg(ended, self.other, day(-30), valid_to=day(-10))
        leg_services.assign_metering_point(self.leg, ended, TODAY, self.actor, today=TODAY)
        self.assertEqual(ended.leg_assignments.count(), 2)
        archived_point = make_metering_point(3, is_archived=True)
        with self.assertRaises(assignments.AssignmentError) as caught:
            leg_services.assign_metering_point(self.leg, archived_point, TODAY, self.actor, today=TODAY)
        self.assertEqual(caught.exception.code, "point_archived")
        self.other.is_archived = True
        self.other.save()
        with self.assertRaises(assignments.AssignmentError) as caught:
            leg_services.assign_metering_point(self.other, make_metering_point(4), TODAY, self.actor, today=TODAY)
        self.assertEqual(caught.exception.code, "leg_archived")

    def test_the_day_must_be_after_the_start_of_an_earlier_assignment(self):
        point = make_metering_point(5)
        assign_leg(point, self.other, date(2026, 6, 1), valid_to=date(2026, 6, 10))
        with self.assertRaises(assignments.AssignmentError) as caught:
            leg_services.assign_metering_point(self.leg, point, date(2026, 5, 1), self.actor, today=date(2026, 6, 15))
        self.assertEqual(caught.exception.code, "starts_not_after_previous")
        self.assertEqual(point.leg_assignments.count(), 1)
        self.assertEqual(MeteringPoint.objects.get(pk=point.pk).leg_assignments.get().leg, self.other)
