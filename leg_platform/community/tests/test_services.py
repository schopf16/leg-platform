"""The services of the community domain (D-077): assignments, problems, LEG figures, the decisions
about an address suggestion, the settings with their history, the preferences and the history helper.

Services are tested directly, without a browser client. Shared data is created once per class."""

from datetime import date
from unittest.mock import patch

from django.test import TestCase, tag

from leg_platform.accounts.models import AccountIntegrityError, SecurityEvent, User
from leg_platform.community import assignments, history, legs, locations, preferences, problems, settings_service
from leg_platform.community.addressing import AddressSuggestion
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Direction,
    HistoryEntry,
    ListPreference,
    Location,
    ManagementSettings,
    PersonAssignment,
    PersonRecord,
    ShareChange,
)
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    day,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)

TODAY = day()


@tag("mariadb")
class AssignmentRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = staff_user("actor.person@example.org")
        cls.anna = staff_user("anna.person@example.org")
        cls.bruno = staff_user("bruno.person@example.org")
        cls.legs = [make_leg("LEG One"), make_leg("LEG Two")]

    def setUp(self):
        self.mp = make_metering_point(1)

    def assignments_of(self, kind):
        model = assignments.model_of(kind)
        return list(model.objects.filter(metering_point=self.mp).values_list("valid_from", "valid_to"))

    def test_the_first_assignment_is_open_and_goes_to_the_history_of_the_metering_point(self):
        assignment = assignments.assign("person", self.mp, self.anna, day(0), self.actor)

        self.assertIsNone(assignment.valid_to)
        entry = history.entries_for(self.mp).get()
        self.assertEqual(entry.event, HistoryEvent.PERSON_ASSIGNED)
        self.assertEqual(entry.actor, self.actor)
        self.assertEqual(entry.detail, {"person_id": self.anna.pk, "valid_from": day(0).isoformat()})

    def test_assigning_a_person_whose_assignment_is_in_force_sets_the_retention_obligation(self):
        # The account is kept, not deleted, when the person closes it (D-016); an assignment to a
        # protected row would otherwise make the deletion fail.
        self.assertFalse(self.anna.retention_obligation)
        assignments.assign("person", self.mp, self.anna, day(0), self.actor, today=TODAY)
        self.anna.refresh_from_db()
        self.assertTrue(self.anna.retention_obligation)
        # M-2: the security log names the account and the actor, nothing else.
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.RETENTION_OBLIGATION_SET)
        self.assertEqual((event.account_id, event.actor_id), (self.anna.pk, self.actor.pk))
        # Assigning again, or another assignment of the same person, logs nothing more.
        other = make_metering_point(2)
        assignments.assign("person", other, self.anna, day(-1), self.actor, today=TODAY)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.RETENTION_OBLIGATION_SET).count(), 1)

    def test_a_future_assignment_leaves_the_account_untouched_also_when_it_is_deleted(self):
        # M-1: only an assignment in force makes the account part of the accounting.
        planned = assignments.assign("person", self.mp, self.anna, day(10), self.actor, today=TODAY)
        self.anna.refresh_from_db()
        self.assertFalse(self.anna.retention_obligation)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.RETENTION_OBLIGATION_SET).exists())
        assignments.delete("person", planned, self.actor, today=TODAY)
        self.anna.refresh_from_db()
        self.assertFalse(self.anna.retention_obligation)
        # The day itself counts as in force.
        assignments.assign("person", self.mp, self.bruno, TODAY, self.actor, today=TODAY)
        self.bruno.refresh_from_db()
        self.assertTrue(self.bruno.retention_obligation)

    def test_an_account_with_any_assignment_is_deactivated_not_deleted(self):
        # M-1: also one without the flag (a future assignment); the protected reference must not crash.
        assignments.assign("person", self.mp, self.anna, day(10), self.actor, today=TODAY)
        self.anna.refresh_from_db()
        self.assertFalse(self.anna.retention_obligation)
        self.assertEqual(self.anna.delete_or_deactivate(), "deactivated")
        self.anna.refresh_from_db()
        self.assertEqual(self.anna.status, self.anna.Status.DEACTIVATED)
        with self.assertRaises(AccountIntegrityError):
            self.anna.delete()
        self.assertTrue(PersonAssignment.objects.filter(person=self.anna).exists())
        # An account without any assignment is still deleted.
        self.assertEqual(self.bruno.delete_or_deactivate(), "deleted")

    def test_the_person_is_read_again_under_a_lock_and_must_be_active(self):
        # L-1: a stale object of an account that was blocked or deactivated meanwhile is refused.
        stale = User.objects.get(pk=self.anna.pk)
        self.anna.set_status(User.Status.DEACTIVATED)
        self.assertEqual(stale.status, User.Status.ACTIVE)
        with self.assertRaises(assignments.AssignmentError) as caught:
            assignments.assign("person", self.mp, stale, day(0), self.actor, today=TODAY)
        self.assertEqual(caught.exception.code, "person_not_active")
        self.assertFalse(PersonAssignment.objects.exists())
        # An account that does not exist any more.
        stale_bruno = User.objects.get(pk=self.bruno.pk)
        self.assertEqual(self.bruno.delete_or_deactivate(), "deleted")
        with self.assertRaises(assignments.AssignmentError) as caught:
            assignments.assign("person", self.mp, stale_bruno, day(0), self.actor, today=TODAY)
        self.assertEqual(caught.exception.code, "not_a_person")

    def test_a_flying_change_ends_the_previous_assignment_the_day_before(self):
        assignments.assign("person", self.mp, self.anna, day(-30))
        assignments.assign("person", self.mp, self.bruno, day(10), self.actor)

        self.assertEqual(self.assignments_of("person"), [(day(-30), day(9)), (day(10), None)])
        # Both are "not ended": the one in force and the planned one.
        self.assertEqual(PersonAssignment.objects.not_ended(TODAY).count(), 2)
        self.assertEqual(assignments.in_force("person", self.mp, TODAY).person, self.anna)
        self.assertEqual(assignments.in_force("person", self.mp, day(10)).person, self.bruno)

    def test_a_change_after_an_earlier_end_leaves_the_previous_assignment_alone(self):
        assignments.assign("person", self.mp, self.anna, day(-30))
        assignments.end("person", PersonAssignment.objects.get(), day(-10))
        assignments.assign("person", self.mp, self.bruno, day(0))

        self.assertEqual(self.assignments_of("person"), [(day(-30), day(-10)), (day(0), None)])

    def test_a_new_assignment_must_start_after_the_last_one_started(self):
        assignments.assign("person", self.mp, self.anna, day(0))
        for start in (day(0), day(-1)):
            with self.subTest(start=start), self.assertRaises(assignments.AssignmentError) as caught:
                assignments.assign("person", self.mp, self.bruno, start)
            self.assertEqual(caught.exception.code, "starts_not_after_previous")
        self.assertEqual(self.assignments_of("person"), [(day(0), None)])

    def test_only_an_account_with_a_customer_number_is_a_person(self):
        from leg_platform.accounts.models import User

        unverified = User.objects.create_user("unverified.person@example.org")
        with self.assertRaises(assignments.AssignmentError) as caught:
            assignments.assign("person", self.mp, unverified, day(0))
        self.assertEqual(caught.exception.code, "not_a_person")

    def test_ending_an_assignment(self):
        assignment = assignments.assign("person", self.mp, self.anna, day(-5))

        with self.assertRaises(assignments.AssignmentError) as caught:
            assignments.end("person", assignment, day(-6))
        self.assertEqual(caught.exception.code, "end_before_start")
        assignments.end("person", assignment, day(3), self.actor)
        self.assertEqual(self.assignments_of("person"), [(day(-5), day(3))])
        with self.assertRaises(assignments.AssignmentError) as caught:
            assignments.end("person", assignment, day(4))
        self.assertEqual(caught.exception.code, "already_ended")
        self.assertEqual(history.entries_for(self.mp).last().event, HistoryEvent.PERSON_ASSIGNMENT_ENDED)

    def test_only_an_assignment_that_never_took_effect_can_be_deleted(self):
        in_force = assignments.assign("person", self.mp, self.anna, day(-5))
        planned = assignments.assign("person", self.mp, self.bruno, day(10))

        self.assertFalse(assignments.is_deletable(in_force, TODAY))
        self.assertTrue(assignments.is_deletable(planned, TODAY))
        # It took effect on its first day.
        self.assertFalse(assignments.is_deletable(planned, day(10)))
        with self.assertRaises(assignments.AssignmentError) as caught:
            assignments.delete("person", in_force, today=TODAY)
        self.assertEqual(caught.exception.code, "not_deletable")

        # Deleting the planned one undoes the end of the previous assignment.
        assignments.delete("person", planned, self.actor, today=TODAY)
        self.assertEqual(self.assignments_of("person"), [(day(-5), None)])
        self.assertEqual(history.entries_for(self.mp).last().event, HistoryEvent.PERSON_ASSIGNMENT_DELETED)

    def test_deleting_restores_a_later_end_of_the_previous_assignment(self):
        previous = assignments.assign("person", self.mp, self.anna, day(-5))
        assignments.end("person", previous, day(30))
        planned = assignments.assign("person", self.mp, self.bruno, day(10))
        self.assertEqual(self.assignments_of("person"), [(day(-5), day(9)), (day(10), None)])

        assignments.delete("person", planned, today=TODAY)

        self.assertEqual(self.assignments_of("person"), [(day(-5), day(30))])

    def test_an_ended_or_started_assignment_is_not_deletable(self):
        planned = assignments.assign("person", self.mp, self.anna, day(10))
        assignments.end("person", planned, day(20))
        self.assertFalse(assignments.is_deletable(PersonAssignment.objects.get(), TODAY))

    def test_the_same_rules_hold_for_legs(self):
        assignments.assign("leg", self.mp, self.legs[0], day(-30), self.actor)
        assignments.assign("leg", self.mp, self.legs[1], day(0), self.actor)

        self.assertEqual(self.assignments_of("leg"), [(day(-30), day(-1)), (day(0), None)])
        self.assertEqual(assignments.in_force("leg", self.mp, TODAY).leg, self.legs[1])
        events = list(history.entries_for(self.mp).values_list("event", flat=True))
        self.assertEqual(events, [HistoryEvent.LEG_ASSIGNED, HistoryEvent.LEG_ASSIGNED])
        with self.assertRaises(ValueError):
            assignments.assign("unknown", self.mp, self.legs[0], day(1))


@tag("mariadb")
class ProblemTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = staff_user("person.person@example.org")
        cls.leg = make_leg("LEG A")
        cls.place = make_location()
        cls.ok = make_metering_point(1, location=cls.place)
        assign_person(cls.ok, cls.person)
        assign_leg(cls.ok, cls.leg)
        cls.bare = make_metering_point(2)  # nothing assigned, no location
        cls.no_person = make_metering_point(3, location=cls.place)
        assign_leg(cls.no_person, cls.leg)
        cls.no_leg = make_metering_point(4, location=cls.place)
        assign_person(cls.no_leg, cls.person)
        cls.ended = make_metering_point(5, location=cls.place)
        assign_person(cls.ended, cls.person, date(2026, 1, 1), date(2026, 3, 1))
        assign_leg(cls.ended, cls.leg)
        cls.planned = make_metering_point(6, location=cls.place)
        assign_person(cls.planned, cls.person, date(2026, 9, 1))
        assign_leg(cls.planned, cls.leg, date(2026, 9, 1))
        cls.archived = make_metering_point(7, is_archived=True)

    def test_metering_point_problems(self):
        P = problems.MeteringPointProblem
        expected = {
            self.ok: [],
            self.bare: [P.NO_LOCATION, P.NO_LEG, P.NO_PERSON],
            self.no_person: [P.NO_PERSON],
            self.no_leg: [P.NO_LEG],
            # Ended before today: no person. A planned assignment counts (the person is known).
            self.ended: [P.NO_PERSON],
            self.planned: [],
            # Archived metering points have no problem.
            self.archived: [],
        }
        for mp, wanted in expected.items():
            with self.subTest(number=mp.number):
                self.assertEqual(problems.metering_point_problems(mp, TODAY), wanted)

    def test_the_list_filter_and_the_single_check_agree(self):
        listed = set(problems.metering_points_with_problem(TODAY).values_list("number", flat=True))
        single = {
            mp.number
            for mp in problems.annotate_metering_points(self.ok.__class__.objects.all(), TODAY)
            if problems.metering_point_problems(mp, TODAY)
        }
        self.assertEqual(listed, single)
        self.assertEqual(
            listed, {self.bare.number, self.no_person.number, self.no_leg.number, self.ended.number}
        )

    def test_messages_of_the_banner(self):
        P = problems.MeteringPointProblem
        self.assertEqual(problems.describe(P.NO_LEG), "Dieser Messpunkt ist keiner LEG zugeordnet.")
        self.assertEqual(problems.describe(P.NO_PERSON), "Dieser Messpunkt ist keiner Person zugeordnet.")
        self.assertEqual(problems.describe(P.NO_LOCATION), "Dieser Messpunkt ist keinem Standort zugeordnet.")
        self.assertEqual(
            problems.describe(problems.LocationProblem.ADDRESS_CORRECTION_OPEN),
            "Problem: Adresskorrektur offen",
        )
        with self.assertRaises(ValueError):
            problems.describe("unknown")

    def test_locations_with_an_open_address_correction(self):
        flagged = make_location("Flagged", "1", "3000", "Bern", address_correction_open=True)
        make_location("Archived", "1", "3000", "Bern", address_correction_open=True, is_archived=True)

        self.assertEqual(list(problems.locations_with_problem()), [flagged])
        self.assertEqual(problems.location_problems(flagged), [problems.LocationProblem.ADDRESS_CORRECTION_OPEN])
        self.assertEqual(problems.location_problems(self.place), [])

    def test_the_counts_of_the_overview(self):
        make_location("Flagged", "1", "3000", "Bern", address_correction_open=True)
        # LEG A has consumers only (all metering points of this class are consumption).
        counts = problems.problem_counts(TODAY, threshold_percent=10)
        self.assertEqual(counts, {"locations": 1, "metering_points": 4, "legs": 1})


@tag("mariadb")
class LegFigureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.circuit_a = make_circuit("TRA1")
        cls.circuit_b = make_circuit("TRA2")
        cls.place_a = make_location("A", "1", "3000", "Bern", transformer_circuit=cls.circuit_a)
        cls.place_a2 = make_location("A", "2", "3000", "Bern", transformer_circuit=cls.circuit_a)
        cls.place_b = make_location("B", "1", "3000", "Bern", transformer_circuit=cls.circuit_b)
        cls.place_none = make_location("C", "1", "3000", "Bern")
        cls.leg_one = make_leg("One", production_quota_percent="30", quota_checked_on=date(2999, 1, 1))
        cls.leg_two = make_leg("Two")
        cls.leg_empty = make_leg("Empty")
        # One circuit, a consumer and a producer.
        assign_leg(make_metering_point(1, location=cls.place_a), cls.leg_one)
        assign_leg(make_metering_point(2, Direction.FEED_IN, location=cls.place_a2), cls.leg_one)
        # Two circuits, consumers only.
        assign_leg(make_metering_point(3, location=cls.place_a), cls.leg_two)
        assign_leg(make_metering_point(4, location=cls.place_b), cls.leg_two)
        # An archived metering point and an ended assignment are not members.
        assign_leg(make_metering_point(5, location=cls.place_b, is_archived=True), cls.leg_one)
        assign_leg(make_metering_point(6, location=cls.place_b), cls.leg_one, date(2026, 1, 1), date(2026, 2, 1))

    def test_summaries_of_all_legs_come_from_one_query(self):
        legs_ = [self.leg_one, self.leg_two, self.leg_empty]
        with self.assertNumQueries(1):
            rows = legs.member_rows([leg.pk for leg in legs_], TODAY)
        self.assertEqual({pk: len(r) for pk, r in rows.items()},
                         {self.leg_one.pk: 2, self.leg_two.pk: 2, self.leg_empty.pk: 0})

        found = legs.summaries(legs_, 10, TODAY)

        one, two, empty = (found[leg.pk] for leg in legs_)
        self.assertEqual((one.ratio, one.tier, one.problems), ("1 : 1", legs.DiscountTier.OPTIMISED, ()))
        self.assertEqual(one.circuit_ids, frozenset({self.circuit_a.pk}))
        self.assertEqual((two.ratio, two.tier), ("2 : 0", legs.DiscountTier.BASIC))
        self.assertEqual(two.problems, (legs.LegProblem.NO_PRODUCER,))
        self.assertEqual((empty.ratio, empty.tier, empty.problems), (None, legs.DiscountTier.NONE, ()))

    def test_a_metering_point_without_a_circuit_gives_the_basic_discount(self):
        leg = make_leg("Three")
        assign_leg(make_metering_point(10, location=self.place_a), leg)
        assign_leg(make_metering_point(11, location=self.place_none), leg)
        assign_leg(make_metering_point(12), leg)  # no location at all

        self.assertEqual(legs.summaries([leg], 10, TODAY)[leg.pk].tier, legs.DiscountTier.BASIC)

    def test_a_metering_point_created_after_the_check_day_makes_the_quota_stale(self):
        # The metering points of the tests are created now; this LEG was checked long ago, the
        # other one (leg_one) in the far future.
        old = make_leg("Old", production_quota_percent="30", quota_checked_on=date(2020, 1, 1))
        assign_leg(make_metering_point(20, location=self.place_a), old)

        self.assertIn(legs.LegProblem.QUOTA_STALE, legs.summaries([old], 10)[old.pk].problems)
        self.assertNotIn(
            legs.LegProblem.QUOTA_STALE, legs.summaries([self.leg_one], 10)[self.leg_one.pk].problems
        )

    def test_the_threshold_comes_from_the_settings_by_default(self):
        row = ManagementSettings.load()
        row.warning_threshold_percent = 40
        row.save()
        found = legs.summaries([self.leg_one], on=TODAY)[self.leg_one.pk]
        self.assertIn(legs.LegProblem.QUOTA_BELOW_WARNING, found.problems)

    def test_legs_with_problems_skip_archived_legs(self):
        listed = [leg.name for leg, _summary in problems.legs_with_problem(TODAY, 10)]
        self.assertIn("Two", listed)
        self.leg_two.is_archived = True
        self.leg_two.save()
        listed = [leg.name for leg, _summary in problems.legs_with_problem(TODAY, 10)]
        self.assertNotIn("Two", listed)


@tag("mariadb")
class LocationDecisionTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.actor = staff_user("actor.person@example.org")

    def setUp(self):
        self.location = make_location("musterweg", "12", "3063", "Ittigen")
        self.suggestion = AddressSuggestion("Musterweg", "12", "3063", "Ittigen Dorf")

    def test_the_directory_check_is_stored_on_the_location(self):
        with patch("leg_platform.community.addressing.suggest_correction", return_value=self.suggestion):
            locations.refresh_suggestion(self.location)
        self.location.refresh_from_db()
        self.assertTrue(self.location.address_correction_open)
        self.assertEqual(self.location.suggestion, self.suggestion)

        with patch("leg_platform.community.addressing.suggest_correction", return_value=None):
            locations.refresh_suggestion(self.location)
        self.location.refresh_from_db()
        self.assertFalse(self.location.address_correction_open)
        self.assertIsNone(self.location.suggestion)

    def test_accepting_replaces_the_address_and_writes_the_history(self):
        self.location.apply_suggestion(self.suggestion)
        self.location.save()

        locations.accept_suggestion(self.location, self.actor)

        self.location.refresh_from_db()
        self.assertEqual((self.location.street, self.location.town), ("Musterweg", "Ittigen Dorf"))
        self.assertFalse(self.location.address_correction_open)
        self.assertEqual(Location.find_by_address("Musterweg", "12", "3063", "Ittigen Dorf"), self.location)
        entry = history.entries_for(self.location).get()
        self.assertEqual((entry.event, entry.actor), (HistoryEvent.ADDRESS_CORRECTION_ACCEPTED, self.actor))
        with self.assertRaises(locations.SuggestionError) as caught:
            locations.accept_suggestion(self.location, self.actor)
        self.assertEqual(caught.exception.code, "no_open_suggestion")

    def test_the_suggestion_that_is_the_address_of_another_location_is_refused(self):
        make_location("Musterweg", "12", "3063", "Ittigen Dorf")
        self.location.apply_suggestion(self.suggestion)
        self.location.save()

        with self.assertRaises(locations.SuggestionError) as caught:
            locations.accept_suggestion(self.location, self.actor)

        self.assertEqual(caught.exception.code, "address_in_use")
        self.location.refresh_from_db()
        self.assertEqual((self.location.street, self.location.town), ("musterweg", "Ittigen"))

    def test_not_accepting_keeps_the_address_and_the_warning_stays_away(self):
        self.location.apply_suggestion(self.suggestion)
        self.location.save()

        locations.dismiss_suggestion(self.location, self.actor)

        self.location.refresh_from_db()
        self.assertEqual(self.location.street, "musterweg")
        self.assertFalse(self.location.address_correction_open)
        self.assertEqual(history.entries_for(self.location).get().event, HistoryEvent.ADDRESS_CORRECTION_DISMISSED)
        # The check runs again for the same address: still dismissed.
        with patch("leg_platform.community.addressing.suggest_correction", return_value=self.suggestion):
            locations.refresh_suggestion(self.location)
        self.location.refresh_from_db()
        self.assertFalse(self.location.address_correction_open)
        # After a change of the address the warning returns.
        self.location.town = "Ittigen b"
        self.location.save()
        with patch("leg_platform.community.addressing.suggest_correction", return_value=self.suggestion):
            locations.refresh_suggestion(self.location)
        self.location.refresh_from_db()
        self.assertTrue(self.location.address_correction_open)


@tag("mariadb")
class SettingsServiceTests(TestCase):
    VALUES = {
        "country_code": "CH", "identifier": "1A2B3C4D5E6", "warning_threshold_percent": 10,
        "sender_street": "Musterstrasse", "sender_house_number": "1", "sender_address_addition": "",
        "sender_postal_code": "3063", "sender_town": "Ittigen",
    }

    @classmethod
    def setUpTestData(cls):
        cls.actor = staff_user("actor.person@example.org")

    def events(self):
        return list(HistoryEntry.objects.filter(subject_type="settings").values_list("event", flat=True))

    def test_the_first_save_records_the_address_and_the_identifier_but_not_the_unchanged_country(self):
        events = settings_service.update(self.VALUES, self.actor)

        self.assertEqual(events, [HistoryEvent.SENDER_ADDRESS_CHANGED, HistoryEvent.IDENTIFIER_CHANGED])
        entry = HistoryEntry.objects.get(event=HistoryEvent.IDENTIFIER_CHANGED)
        self.assertEqual((entry.actor, entry.detail), (self.actor, {"old": "", "new": "1A2B3C4D5E6"}))
        self.assertIsNotNone(entry.created_at)
        row = ManagementSettings.load()
        self.assertEqual((row.sender_town, row.identifier), ("Ittigen", "1A2B3C4D5E6"))

    def test_only_what_changed_is_recorded(self):
        settings_service.update(self.VALUES, self.actor)
        before = len(self.events())

        self.assertEqual(settings_service.update(self.VALUES, self.actor), [])
        # The threshold and the address addition are not recorded.
        settings_service.update({**self.VALUES, "warning_threshold_percent": 15}, self.actor)
        self.assertEqual(len(self.events()), before)
        events = settings_service.update(
            {**self.VALUES, "warning_threshold_percent": 15, "country_code": "LI", "sender_town": "Bern"},
            self.actor,
        )
        self.assertEqual(events, [HistoryEvent.SENDER_ADDRESS_CHANGED, HistoryEvent.COUNTRY_CHANGED])
        self.assertEqual(ManagementSettings.load().warning_threshold_percent, 15)

    def test_the_service_checks_the_values_itself(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            settings_service.update({**self.VALUES, "identifier": "short"}, self.actor)
        with self.assertRaises(ValueError):
            settings_service.update({"country_code": "CH"}, self.actor)
        self.assertEqual(self.events(), [])

    def test_dismissing_the_sender_suggestion_is_remembered(self):
        settings_service.dismiss_sender_suggestion("some address")
        self.assertEqual(ManagementSettings.load().sender_suggestion_dismissed_for, "some address")


@tag("mariadb")
class HistoryPreferenceAndPersonRecordTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = staff_user("person.person@example.org")
        cls.other = staff_user("other.person@example.org")
        cls.place = make_location()

    def test_entries_belong_to_their_subject_and_creation_is_no_history(self):
        history.record(self.place, HistoryEvent.CREATED, self.person)
        self.assertFalse(history.has_history(self.place))
        history.record(self.place, HistoryEvent.ADDRESS_CORRECTION_DISMISSED, self.person)
        self.assertTrue(history.has_history(self.place))
        self.assertEqual(history.entries_for(self.place).count(), 2)
        self.assertEqual(history.entries_for((history.PERSON, self.person.pk)).count(), 0)
        history.record(self.person, HistoryEvent.CREATED)
        self.assertEqual(history.subject_of(self.person), ("person", self.person.pk))
        self.assertEqual(history.subject_of(PersonRecord(person=self.person)), ("person", self.person.pk))
        with self.assertRaises(TypeError):
            history.subject_of(object())
        with self.assertRaises(ValueError):
            history.record(("nothing", 1), HistoryEvent.CREATED)
        with self.assertRaises(ValueError):
            history.record(self.place, "not an event")

    def test_preferences_are_per_user_and_list(self):
        preferences.save_column_widths(self.person, "locations", {"street": 300})
        preferences.save_column_widths(self.person, "locations", {"street": 320, "town": 150})
        preferences.save_expanded_locations(self.person, [5, 3, 5])
        preferences.save_column_widths(self.other, "locations", {"street": 100})

        mine = preferences.get_preference(self.person, "locations")
        self.assertEqual(mine.column_widths, {"street": 320, "town": 150})
        self.assertEqual(mine.expanded_locations, [3, 5])
        self.assertEqual(preferences.get_preference(self.other, "locations").column_widths, {"street": 100})
        self.assertEqual(ListPreference.objects.count(), 2)
        # A list without a row gives the defaults and saves nothing.
        empty = preferences.get_preference(self.person, "legs")
        self.assertEqual((empty.column_widths, empty.expanded_locations, empty.pk), ({}, [], None))
        self.assertEqual(ListPreference.objects.count(), 2)
        for bad in ({"street": 10}, ):
            with self.assertRaises(ValueError):
                preferences.save_column_widths(self.person, "locations", bad)
        with self.assertRaises(ValueError):
            preferences.save_column_widths(self.person, "nope", {"street": 100})
        with self.assertRaises(ValueError):
            preferences.save_expanded_locations(self.person, [0])

    def test_the_side_record_belongs_to_the_account_and_leaves_it_untouched(self):
        record = PersonRecord.objects.create(person=self.person, staff_note="note", operator_customer_number="OPR 3")
        ShareChange.objects.create(record=record, quantity=3, valid_from=TODAY)
        self.assertEqual(self.person.person_record, record)
        self.assertEqual([entry.quantity for entry in record.share_changes.all()], [3])
        self.assertEqual(record.current_address(), ("Examplestrasse", "12a", "3000", "Bern"))
        self.assertEqual(PersonRecord.objects.filter(person=self.other).count(), 0)
        # The side record is removed with the account; the account is not changed by it.
        self.assertFalse(hasattr(type(self.person), "staff_note"))
