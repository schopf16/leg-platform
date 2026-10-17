"""The domain of the pages of the locations (D-078): the services (create, change the address, merge,
circuit, confirm, archive, delete), the transformer circuits, creating a metering point and the
queries of the list (search, filters, sort, details). Service level, no client round trips."""

from datetime import date
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase, tag
from django.utils import timezone

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.community import (
    history,
    location_list,
    locations,
    metering_points,
    transformer_circuits,
)
from leg_platform.community.addressing import AddressSuggestion
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Direction, HistoryEntry, Location, MeteringPoint, PersonRecord
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    number,
)

NO_SUGGESTION = mock.patch("leg_platform.community.addressing.suggest_correction", return_value=None)


def events(subject):
    return list(history.entries_for(subject).values_list("event", flat=True))


@tag("mariadb")
class LocationServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = create_active_user("actor.person@example.org")

    def setUp(self):
        patcher = NO_SUGGESTION
        self.suggest = patcher.start()
        self.addCleanup(patcher.stop)

    def test_creating_a_location_normalises_records_the_history_and_checks_the_directory_once(self):
        circuit = make_circuit("TRA7")
        location = locations.create_location(
            street="  Neuweg ", house_number=" 4 ", postal_code="3000", town="Bern",
            address_addition="Hinterhaus", transformer_circuit=circuit, remark="  Schluessel beim Hauswart ",
            actor=self.actor,
        )
        self.assertEqual((location.street, location.house_number, location.town), ("Neuweg", "4", "Bern"))
        self.assertEqual(location.remark, "Schluessel beim Hauswart")
        self.assertEqual(location.address_addition, "Hinterhaus")
        self.assertEqual(events(location), [HistoryEvent.CREATED])
        self.assertEqual(self.suggest.call_count, 1)
        self.assertTrue(locations.can_delete(location))

    def test_an_address_that_exists_or_is_wrong_is_refused(self):
        make_location("Neuweg", "4", "3000", "Bern")
        with self.assertRaises(ValidationError):
            # Case and spaces do not make another address; the addition does not either.
            locations.create_location(
                street="neuweg", house_number="4", postal_code="3000", town="BERN", address_addition="Anbau"
            )
        with self.assertRaises(ValidationError):
            locations.create_location(street="Neuweg", house_number="4", postal_code="30", town="Bern")

    def test_changing_the_address_is_logged_and_asks_the_directory_again(self):
        location = make_location("Neuweg", "4", "3000", "Bern")
        saved = locations.update_address(
            location, street="Neuweg", house_number="6", postal_code="3000", town="Bern", actor=self.actor
        )
        self.assertEqual(saved.house_number, "6")
        self.assertEqual(events(saved), [HistoryEvent.ADDRESS_CHANGED])
        self.assertEqual(self.suggest.call_count, 1)
        # Only the addition changes: no entry (it is not part of the address).
        locations.update_address(
            saved, street="Neuweg", house_number="6", postal_code="3000", town="Bern",
            address_addition="Anbau", actor=self.actor,
        )
        self.assertEqual(events(saved), [HistoryEvent.ADDRESS_CHANGED])
        saved.refresh_from_db()
        self.assertEqual(saved.address_addition, "Anbau")

    def test_the_new_address_of_another_location_needs_the_merge_and_moves_the_metering_points(self):
        source = make_location("Neuweg", "4", "3000", "Bern")
        target = make_location("Neuweg", "6", "3000", "Bern")
        point = make_metering_point(1, location=source)
        before = MeteringPoint.objects.count()
        with self.assertRaises(locations.MergeNeeded) as raised:
            locations.update_address(source, street="Neuweg", house_number="6", postal_code="3000", town="Bern")
        self.assertEqual((raised.exception.target, raised.exception.metering_points), (target, 1))
        # Nothing happened without the confirmation.
        self.assertTrue(Location.objects.filter(pk=source.pk).exists())
        merged = locations.update_address(
            source, street="neuweg", house_number="6", postal_code="3000", town="Bern", actor=self.actor, merge=True,
            merge_target_id=target.pk,
        )
        self.assertEqual(merged, target)
        point.refresh_from_db()
        self.assertEqual(point.location, target)
        self.assertEqual(MeteringPoint.objects.count(), before)
        self.assertFalse(Location.objects.filter(pk=source.pk).exists())
        entry = history.entries_for(target).get(event=HistoryEvent.LOCATIONS_MERGED)
        self.assertEqual(entry.actor, self.actor)
        self.assertEqual(entry.detail["metering_points"], 1)
        # Every metering point that moved has its own entry (D-084, M-3).
        moved = history.entries_for(point).get(event=HistoryEvent.METERING_POINT_LOCATION_CHANGED)
        self.assertEqual(moved.actor, self.actor)
        self.assertEqual(moved.detail, {"old_location_id": source.pk, "new_location_id": target.pk})

    def test_a_merge_needs_the_confirmed_target_and_never_goes_to_an_archived_location(self):
        source = make_location("Neuweg", "14", "3000", "Bern")
        target = make_location("Neuweg", "16", "3000", "Bern")
        make_metering_point(2, location=source)
        new = {"street": "Neuweg", "house_number": "16", "postal_code": "3000", "town": "Bern"}
        # The confirmation names the target that was shown; another id (or none) asks again.
        for target_id in (None, source.pk, 999999):
            with self.assertRaises(locations.MergeNeeded):
                locations.update_address(source, **new, merge=True, merge_target_id=target_id)
        self.assertTrue(Location.objects.filter(pk=source.pk).exists())
        # An archived location is no target, with or without the confirmation.
        target.is_archived = True
        target.save()
        for confirmed in (False, True):
            with self.assertRaises(locations.MergeRefused):
                locations.update_address(source, **new, merge=confirmed, merge_target_id=target.pk)
        self.assertTrue(Location.objects.filter(pk=source.pk).exists())

    def test_the_circuit_is_set_logged_and_removed(self):
        location = make_location()
        circuit = make_circuit("TRA21359", "Ittigen Nord")
        locations.set_circuit(location, circuit, self.actor)
        locations.set_circuit(location, circuit, self.actor)  # no change, no entry
        locations.set_circuit(location, None, self.actor)
        entries = list(history.entries_for(location).order_by("pk"))
        self.assertEqual([e.event for e in entries], [HistoryEvent.TRANSFORMER_CIRCUIT_CHANGED] * 2)
        self.assertEqual((entries[0].detail["old"], entries[0].detail["new"]), ("", "TRA21359"))
        self.assertEqual((entries[1].detail["old"], entries[1].detail["new"]), ("TRA21359", ""))

    def test_confirming_clears_the_mark_once(self):
        location = make_location(needs_confirmation=True)
        locations.confirm(location, self.actor)
        locations.confirm(location, self.actor)
        location.refresh_from_db()
        self.assertFalse(location.needs_confirmation)
        self.assertEqual(events(location), [HistoryEvent.LOCATION_CONFIRMED])

    def test_archiving_and_restoring_are_logged_and_stop_a_deletion(self):
        location = make_location()
        self.assertTrue(locations.can_delete(location))
        locations.set_archived(location, True, self.actor)
        locations.set_archived(location, True, self.actor)
        self.assertFalse(locations.can_delete(location))
        locations.set_archived(location, False, self.actor)
        self.assertEqual(events(location), [HistoryEvent.ARCHIVED, HistoryEvent.UNARCHIVED])

    def test_only_a_location_without_history_and_without_metering_points_is_deleted(self):
        created = locations.create_location(
            street="Neuweg", house_number="4", postal_code="3000", town="Bern", actor=self.actor
        )
        self.assertTrue(locations.can_delete(created))
        with_point = make_location("Neuweg", "5", "3000", "Bern")
        make_metering_point(1, location=with_point)
        self.assertFalse(locations.can_delete(with_point))
        with self.assertRaises(locations.NotDeletable):
            locations.delete(with_point)
        locations.delete(created)
        self.assertFalse(Location.objects.filter(pk=created.pk).exists())
        self.assertFalse(HistoryEntry.objects.filter(subject_id=created.pk, subject_type=history.LOCATION).exists())
        changed = make_location("Neuweg", "7", "3000", "Bern")
        locations.set_circuit(changed, make_circuit("TRA9"), self.actor)
        with self.assertRaises(locations.NotDeletable):
            locations.delete(changed)


@tag("mariadb")
class SuggestionServiceTests(TestCase):
    """The directory suggestion of a location that has one (patched: no directory data needed)."""

    @classmethod
    def setUpTestData(cls):
        cls.actor = create_active_user("actor.person@example.org")

    def test_the_directory_is_asked_when_the_address_is_created_and_changed(self):
        suggestion = AddressSuggestion("Musterweg", "12", "3063", "Ittigen")
        with mock.patch("leg_platform.community.addressing.suggest_correction", return_value=suggestion):
            location = locations.create_location(
                street="Musterwg", house_number="12", postal_code="3063", town="Ittigen", actor=self.actor
            )
            self.assertTrue(location.address_correction_open)
            self.assertEqual(location.suggestion, suggestion)
            locations.dismiss_suggestion(location, self.actor)
            location.refresh_from_db()
            self.assertFalse(location.address_correction_open)
            # The same address again: the dismissal holds. Another address: the check returns.
            locations.update_address(
                location, street="Musterwg", house_number="12", postal_code="3063", town="Ittigen",
                address_addition="Anbau",
            )
            location.refresh_from_db()
            self.assertFalse(location.address_correction_open)
            locations.update_address(location, street="Musterw", house_number="12", postal_code="3063", town="Ittigen")
            location.refresh_from_db()
            self.assertTrue(location.address_correction_open)
            locations.accept_suggestion(location, self.actor)
        location.refresh_from_db()
        self.assertEqual(location.current_address(), ("Musterweg", "12", "3063", "Ittigen"))
        self.assertIn(HistoryEvent.ADDRESS_CORRECTION_ACCEPTED, events(location))
        self.assertIn(HistoryEvent.ADDRESS_CORRECTION_DISMISSED, events(location))

    def test_a_suggestion_that_is_the_address_of_another_location_is_not_accepted(self):
        make_location("Musterweg", "12", "3063", "Ittigen")
        suggestion = AddressSuggestion("Musterweg", "12", "3063", "Ittigen")
        with mock.patch("leg_platform.community.addressing.suggest_correction", return_value=suggestion):
            location = locations.create_location(
                street="Musterwg", house_number="12", postal_code="3063", town="Ittigen", actor=self.actor
            )
            with self.assertRaises(locations.SuggestionError) as raised:
                locations.accept_suggestion(location, self.actor)
            self.assertEqual(raised.exception.code, "address_in_use")


@tag("mariadb")
class TransformerCircuitServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.circuit = make_circuit("TRA21359", "Ittigen Nord")
        cls.other = make_circuit("TRA21361")
        cls.first = make_location("Aweg", "1", "3000", "Bern", transformer_circuit=cls.circuit)
        cls.second = make_location("Aweg", "2", "3000", "Bern", transformer_circuit=cls.circuit)
        cls.archived = make_location("Aweg", "3", "3000", "Bern", transformer_circuit=cls.circuit, is_archived=True)
        make_metering_point(1, location=cls.first)
        make_metering_point(2, Direction.FEED_IN, location=cls.first)
        make_metering_point(3, location=cls.second)
        make_metering_point(4, location=cls.second, is_archived=True)
        make_metering_point(5, location=cls.archived)

    def test_labels_and_the_number_of_a_choice(self):
        self.assertEqual(transformer_circuits.label(self.circuit), "TRA21359 · Ittigen Nord")
        self.assertEqual(transformer_circuits.label(self.other), "TRA21361")
        self.assertEqual(transformer_circuits.label(None), "")
        for text in ("TRA21359 · Ittigen Nord", " tra21359 ", "tra21359·x"):
            self.assertEqual(transformer_circuits.number_of_choice(text), "TRA21359")
        self.assertEqual(transformer_circuits.number_of_choice(""), "")

    def test_find_normalises_and_create_refuses_a_number_that_exists(self):
        self.assertEqual(transformer_circuits.find(" tra21359"), self.circuit)
        self.assertIsNone(transformer_circuits.find("TRA1"))
        self.assertIsNone(transformer_circuits.find(""))
        with self.assertRaises(ValidationError):
            transformer_circuits.create("tra21359", "Doppelt")
        with self.assertRaises(ValidationError):
            transformer_circuits.create("21359")
        created = transformer_circuits.create(" tra555 ", "Neu")
        self.assertEqual((created.bkw_number, created.name), ("TRA555", "Neu"))

    def test_a_change_reaches_every_location_and_may_not_take_another_number(self):
        self.assertEqual(transformer_circuits.affected_locations(self.circuit), 3)
        with self.assertRaises(ValidationError):
            transformer_circuits.update(self.circuit, "tra21361", "x")
        changed = transformer_circuits.update(self.circuit, "tra21360", "Renamed")
        self.assertEqual((changed.bkw_number, changed.name), ("TRA21360", "Renamed"))
        self.assertEqual(Location.objects.get(pk=self.first.pk).transformer_circuit.bkw_number, "TRA21360")

    def test_the_figures_of_the_card_do_not_count_what_is_archived(self):
        self.assertEqual(
            transformer_circuits.stats(self.circuit), {"locations": 2, "consumption": 2, "feed_in": 1}
        )
        self.assertEqual(
            transformer_circuits.stats(self.other), {"locations": 0, "consumption": 0, "feed_in": 0}
        )
        self.assertEqual([c.bkw_number for c in transformer_circuits.choices()], ["TRA21359", "TRA21361"])


@tag("mariadb")
class MeteringPointCreationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = create_active_user("actor.person@example.org")
        cls.location = make_location()
        cls.leg = make_leg()

    def test_the_number_and_the_leg_are_optional_and_the_history_has_the_entry(self):
        point = metering_points.create_metering_point(
            direction=Direction.FEED_IN, location=self.location, actor=self.actor
        )
        self.assertIsNone(point.number)
        self.assertEqual(point.location, self.location)
        self.assertEqual(events(point), [HistoryEvent.CREATED])
        self.assertFalse(point.leg_assignments.exists())
        second = metering_points.create_metering_point(direction=Direction.CONSUMPTION, location=self.location, number="")
        self.assertIsNone(second.number)  # several without a number are possible

    def test_the_leg_is_assigned_from_today(self):
        point = metering_points.create_metering_point(
            direction=Direction.CONSUMPTION, location=self.location, number=number(7), leg=self.leg, actor=self.actor
        )
        assignment = point.leg_assignments.get()
        self.assertEqual((assignment.leg, assignment.valid_from), (self.leg, timezone.localdate()))
        self.assertIn(HistoryEvent.LEG_ASSIGNED, events(point))

    def test_a_number_that_exists_is_refused_with_the_existing_one(self):
        existing = make_metering_point(9, location=self.location)
        with self.assertRaises(metering_points.DuplicateNumber) as raised:
            metering_points.create_metering_point(
                direction=Direction.CONSUMPTION, location=self.location, number=existing.number
            )
        self.assertEqual(raised.exception.existing, existing)
        self.assertEqual(metering_points.find_by_number(existing.number), existing)
        self.assertIsNone(metering_points.find_by_number(None))

    def test_a_wrong_number_is_refused(self):
        with self.assertRaises(ValidationError):
            metering_points.create_metering_point(
                direction=Direction.CONSUMPTION, location=self.location, number="CH123"
            )


@tag("mariadb")
class LocationListQueryTests(TestCase):
    TODAY = date(2026, 6, 15)

    @classmethod
    def setUpTestData(cls):
        cls.circuit = make_circuit("TRA1", "Nord")
        cls.leg = make_leg("LEG Quartier")
        cls.ada = create_active_user("ada.person@example.org", first_name="Ada", last_name="Müller")
        cls.ben = create_active_user("ben.person@example.org", first_name="Ben", last_name="Meier")
        cls.l10 = make_location("Bahnhofstrasse", "10", "3000", "Bern", transformer_circuit=cls.circuit)
        cls.l9 = make_location("Bahnhofstrasse", "9", "3000", "Bern", transformer_circuit=cls.circuit)
        cls.l9a = make_location("Bahnhofstrasse", "9a", "3000", "Bern", address_addition="Hinterhaus", remark="secretword")
        cls.empty = make_location("Aarestrasse", "1", "3001", "Zürich")
        cls.archived = make_location("Zeile", "1", "3000", "Bern", is_archived=True)
        cls.point10 = make_metering_point(10, location=cls.l10)
        cls.point10b = make_metering_point(11, Direction.FEED_IN, location=cls.l10)
        cls.point9 = make_metering_point(12, location=cls.l9)
        cls.archived_point = make_metering_point(13, location=cls.l9, is_archived=True)
        assign_leg(cls.point10, cls.leg, valid_from=date(2026, 1, 1))
        assign_person(cls.point10, cls.ada, valid_from=date(2026, 1, 1))
        assign_person(cls.point10b, cls.ada, valid_from=date(2026, 1, 1))
        # An assignment that ended does not occupy; one that starts later does.
        assign_person(cls.point9, cls.ben, valid_from=date(2025, 1, 1), valid_to=date(2025, 12, 31))

    def names(self, queryset):
        return [f"{location.street} {location.house_number}" for location in queryset]

    def base(self):
        return location_list.base_queryset(self.TODAY)

    def test_the_counts_do_not_include_archived_metering_points_and_count_a_person_once(self):
        by_id = {location.pk: location for location in self.base()}
        self.assertEqual((by_id[self.l10.pk].mp_count, by_id[self.l10.pk].person_count), (2, 1))
        self.assertEqual((by_id[self.l9.pk].mp_count, by_id[self.l9.pk].person_count), (1, 0))
        self.assertEqual((by_id[self.empty.pk].mp_count, by_id[self.empty.pk].person_count), (0, 0))

    def test_the_occupancy_filter(self):
        def run(value):
            return self.names(location_list.apply_sort(location_list.apply_occupancy(self.base(), value), "address"))

        self.assertEqual(run("all"), ["Aarestrasse 1", "Bahnhofstrasse 9", "Bahnhofstrasse 9a", "Bahnhofstrasse 10"])
        self.assertEqual(run("occupied"), ["Bahnhofstrasse 10"])
        self.assertEqual(run("free"), ["Bahnhofstrasse 9"])
        self.assertEqual(run("empty"), ["Aarestrasse 1", "Bahnhofstrasse 9a"])
        self.assertEqual(run("archived"), ["Zeile 1"])

    def test_the_circuit_filter(self):
        found = location_list.apply_circuit(self.base(), "TRA1")
        self.assertEqual(sorted(self.names(found)), ["Bahnhofstrasse 10", "Bahnhofstrasse 9"])
        self.assertEqual(location_list.apply_circuit(self.base(), "").count(), 5)

    def test_the_search_finds_address_circuit_numbers_and_legs_but_never_the_remark(self):
        def run(text, **kwargs):
            return sorted(self.names(location_list.apply_search(self.base(), text, today=self.TODAY, **kwargs)))

        self.assertEqual(run("bahnhof 9"), ["Bahnhofstrasse 9", "Bahnhofstrasse 9a"])
        self.assertEqual(run("ZURICH"), ["Aarestrasse 1"])  # umlaut-insensitive
        self.assertEqual(run("hinterhaus"), ["Bahnhofstrasse 9a"])
        self.assertEqual(run("nord"), ["Bahnhofstrasse 10", "Bahnhofstrasse 9"])
        self.assertEqual(run("tra1"), ["Bahnhofstrasse 10", "Bahnhofstrasse 9"])
        self.assertEqual(run(self.point10.number.lower()), ["Bahnhofstrasse 10"])
        self.assertEqual(run("quartier"), ["Bahnhofstrasse 10"])
        self.assertEqual(run("secretword"), [])
        self.assertEqual(len(run("")), 5)

    def test_persons_are_found_only_with_the_right_to_see_them(self):
        def run(text, see):
            return self.names(location_list.apply_search(self.base(), text, see_persons=see, today=self.TODAY))

        self.assertEqual(run("muller", True), ["Bahnhofstrasse 10"])
        self.assertEqual(run("muller", False), [])
        # An assignment that has ended no longer places the person at the location.
        self.assertEqual(run("meier", True), [])

    def test_sorting_by_house_number_as_a_number_and_by_the_other_columns(self):
        def run(key, descending=False):
            return self.names(location_list.apply_sort(location_list.apply_occupancy(self.base(), "all"), key, descending))

        self.assertEqual(
            run("address"), ["Aarestrasse 1", "Bahnhofstrasse 9", "Bahnhofstrasse 9a", "Bahnhofstrasse 10"]
        )
        self.assertEqual(
            run("address", True), ["Bahnhofstrasse 10", "Bahnhofstrasse 9a", "Bahnhofstrasse 9", "Aarestrasse 1"]
        )
        self.assertEqual(run("metering_points")[-1], "Bahnhofstrasse 10")
        # Empty first, then free, then occupied.
        self.assertEqual(run("occupancy")[-1], "Bahnhofstrasse 10")
        self.assertEqual(run("occupancy")[-2], "Bahnhofstrasse 9")
        # Ascending: the locations with a circuit first (by number), those without last.
        self.assertEqual(run("circuit")[-1], "Bahnhofstrasse 9a")

    def test_the_details_give_the_rows_and_the_persons_and_hide_names_without_the_right(self):
        rows, persons = location_list.details([self.l10, self.l9, self.empty], self.TODAY, see_persons=True)
        self.assertEqual(persons, {self.l10.pk: 1, self.l9.pk: 0, self.empty.pk: 0})
        first, second = rows[self.l10.pk]
        self.assertEqual((first.leg_name, first.person_name), ("LEG Quartier", "Ada Müller"))
        self.assertEqual((second.leg_name, second.person_id), ("", self.ada.pk))
        # The archived metering point is not shown; the ended assignment is not shown.
        (only,) = rows[self.l9.pk]
        self.assertEqual((only.leg_id, only.person_id), (None, None))
        self.assertEqual(rows[self.empty.pk], [])
        hidden, _persons = location_list.details([self.l10], self.TODAY, see_persons=False)
        self.assertEqual((hidden[self.l10.pk][0].person_name, hidden[self.l10.pk][0].person_id), ("", self.ada.pk))

    def test_a_planned_assignment_is_shown_when_none_is_in_force(self):
        point = make_metering_point(20, location=self.l9a)
        assign_person(point, self.ben, valid_from=date(2026, 9, 1))
        rows, persons = location_list.details([self.l9a], self.TODAY, see_persons=True)
        self.assertEqual(rows[self.l9a.pk][0].person_name, "Ben Meier")
        self.assertEqual(persons[self.l9a.pk], 1)

    def test_the_problem_filter_shows_the_open_address_corrections(self):
        Location.objects.filter(pk=self.l9.pk).update(address_correction_open=True)
        self.assertEqual(self.names(location_list.apply_problems(self.base())), ["Bahnhofstrasse 9"])

    def test_what_the_staff_keep_about_a_person_is_never_searched(self):
        # L-6: the IBAN, the note and the BKW customer number of a person record are no search field.
        PersonRecord.objects.create(
            person=self.ada, iban="CH9300762011623852957", staff_note="zahlt immer spaet", bkw_customer_number="BKW-4711"
        )
        for text in ("CH9300762011623852957", "spaet", "BKW-4711", "4711"):
            found = location_list.apply_search(self.base(), text, see_persons=True, today=self.TODAY)
            self.assertEqual(self.names(found), [], text)

    def test_the_history_refuses_data_that_must_not_be_in_it(self):
        # L-6: nothing of a person record, no credentials, also nested.
        for detail in (
            {"iban": "CH93"}, {"old": {"staff_note": "x"}}, {"list": [{"bkw_customer_number": "1"}]}, {"Email": "a@b"},
        ):
            with self.subTest(detail=detail), self.assertRaises(ValueError):
                history.record(self.l9, HistoryEvent.ARCHIVED, None, detail)
        self.assertFalse(HistoryEntry.objects.filter(subject_id=self.l9.pk).exists())
        history.record(self.l9, HistoryEvent.ARCHIVED, None, {"old": "TRA1", "new": ""})
