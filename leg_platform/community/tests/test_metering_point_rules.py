"""The rules of the metering point pages (D-079) at service level: the steps towards the BKW, archiving and
deleting, the LEG switch with the move mask, the search for a person and the queries of the list. The pages
only call these; their own tests are in ``management/tests/test_metering_point_pages.py``."""

from datetime import date

from django.test import TestCase, tag

from leg_platform.accounts.models import User
from leg_platform.community import (
    assignments,
    history,
    leg_switch,
    location_list,
    metering_point_list,
    metering_points,
    person_search,
    problems,
)
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Direction, HistoryEntry, LegAssignment, MeteringPoint
from leg_platform.community.status import MeteringPointStatus
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    create_active_user,
    day,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)

TODAY = day()


@tag("mariadb")
class StepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = staff_user("actor.person@example.org")

    def setUp(self):
        self.mp = make_metering_point(1)

    def events(self):
        return [entry.event for entry in history.entries_for(self.mp)]

    def test_the_steps_follow_each_other_to_active_and_terminated_and_are_written_to_the_history(self):
        status = lambda on: MeteringPoint.objects.get(pk=self.mp.pk).status(on)  # noqa: E731
        self.assertEqual(status(TODAY), MeteringPointStatus.REGISTERED)
        metering_points.mark_notified(self.mp, day(-3), self.actor, today=TODAY)
        self.assertEqual(status(TODAY), MeteringPointStatus.NOTIFIED)
        metering_points.record_confirmation(self.mp, day(-2), day(5), self.actor, today=TODAY)
        self.assertEqual(status(TODAY), MeteringPointStatus.CONFIRMED)
        # Active by itself when the day is reached: no scheduler, no change of the row.
        self.assertEqual(status(day(5)), MeteringPointStatus.ACTIVE)
        metering_points.terminate(self.mp, date(2026, 6, 30), self.actor, today=day(5))
        self.assertEqual(status(day(5)), MeteringPointStatus.ACTIVE)  # the date is in the future
        self.assertEqual(status(date(2026, 7, 1)), MeteringPointStatus.TERMINATED)
        self.assertEqual(
            self.events(),
            [HistoryEvent.REGISTERED_WITH_BKW, HistoryEvent.CONFIRMATION_RECORDED, HistoryEvent.TERMINATION_RECORDED],
        )
        confirmation = history.entries_for(self.mp).get(event=HistoryEvent.CONFIRMATION_RECORDED)
        self.assertEqual(confirmation.detail, {"confirmed_on": day(-2).isoformat(), "start_date": day(5).isoformat()})
        self.assertEqual(confirmation.actor, self.actor)

    def test_a_confirmation_without_a_start_date_stays_on_standby_until_the_start_date_is_recorded(self):
        metering_points.mark_notified(self.mp, day(-3), today=TODAY)
        metering_points.record_confirmation(self.mp, day(-1), None, today=TODAY)
        self.mp.refresh_from_db()
        self.assertEqual(self.mp.status(TODAY), MeteringPointStatus.CONFIRMED)
        self.assertEqual(metering_points.next_step(self.mp, True, TODAY), metering_points.START)
        metering_points.record_start_date(self.mp, day(-1), today=TODAY)
        self.mp.refresh_from_db()
        self.assertEqual(self.mp.status(TODAY), MeteringPointStatus.ACTIVE)
        self.assertIsNone(metering_points.next_step(self.mp, True, TODAY))
        with self.assertRaises(metering_points.StatusError):  # a second start date is no step
            metering_points.record_start_date(self.mp, day(3), today=TODAY)

    def test_every_step_is_refused_out_of_order_with_a_wrong_date_or_for_an_archived_point(self):
        def refused(code, function, *args):
            with self.assertRaises(metering_points.StatusError) as caught:
                function(self.mp, *args, today=TODAY)
            self.assertEqual(caught.exception.code, code)

        refused("wrong_status", metering_points.record_confirmation, day(0), None)
        refused("wrong_status", metering_points.record_start_date, day(0))
        refused("wrong_status", metering_points.terminate, date(2026, 6, 30))
        refused("date_in_future", metering_points.mark_notified, day(1))
        metering_points.mark_notified(self.mp, day(-3), today=TODAY)
        refused("wrong_status", metering_points.mark_notified, day(-3))
        refused("date_before_previous", metering_points.record_confirmation, day(-4), None)
        refused("date_in_future", metering_points.record_confirmation, day(1), None)
        metering_points.record_confirmation(self.mp, day(-3), day(-3), today=TODAY)
        refused("not_month_end", metering_points.terminate, date(2026, 6, 29))
        refused("date_before_previous", metering_points.terminate, date(2026, 5, 31))
        # Nothing was changed by the refusals.
        self.mp.refresh_from_db()
        self.assertIsNone(self.mp.terminated_on)
        metering_points.set_archived(self.mp, True, self.actor)
        refused("archived", metering_points.terminate, date(2026, 6, 30))

    def test_the_next_step_is_the_button_of_the_list_and_a_missing_leg_stops_the_registration(self):
        step = lambda has_leg: metering_points.next_step(self.mp, has_leg, TODAY)  # noqa: E731
        self.assertEqual(step(True), metering_points.NOTIFY)
        self.assertEqual(step(False), metering_points.LEG_MISSING)
        self.mp.registered_with_bkw_on = day(-1)
        self.assertEqual(step(False), metering_points.CONFIRM)
        self.mp.is_archived = True
        self.assertIsNone(step(True))
        self.assertTrue(metering_points.is_month_end(date(2028, 2, 29)))
        self.assertFalse(metering_points.is_month_end(date(2027, 2, 27)))

    def test_deleting_is_possible_only_without_a_history_and_removes_the_entry_created(self):
        history.record(self.mp, HistoryEvent.CREATED, self.actor)
        self.assertTrue(metering_points.can_delete(self.mp))  # "Created" is no history
        number = self.mp.number
        metering_points.delete(self.mp)
        self.assertFalse(MeteringPoint.objects.filter(number=number).exists())
        self.assertFalse(HistoryEntry.objects.filter(subject_type=history.METERING_POINT, subject_id=self.mp.pk).exists())

        other = make_metering_point(2)
        metering_points.mark_notified(other, day(0), today=TODAY)
        self.assertFalse(metering_points.can_delete(other))
        with self.assertRaises(metering_points.NotDeletable):
            metering_points.delete(other)
        # Archive and restore are written to the history.
        metering_points.set_archived(other, True, self.actor)
        metering_points.set_archived(other, False, self.actor)
        self.assertEqual(
            [e.event for e in history.entries_for(other)][-2:], [HistoryEvent.ARCHIVED, HistoryEvent.UNARCHIVED]
        )

    def test_the_chain_lists_the_assignments_that_are_valid_now_or_later_oldest_first(self):
        person = create_active_user("chain.person@example.org")
        first = assign_person(self.mp, person, day(-20), day(-11))
        assignments.assign("person", self.mp, person, day(-5), today=TODAY)
        assignments.assign("person", self.mp, person, day(5), today=TODAY)
        chain = metering_points.chain("person", self.mp, TODAY)
        self.assertEqual([(a.current, a.pending) for a in chain], [(True, False), (False, True)])
        self.assertNotIn(first, [a.assignment for a in chain])  # ended in the past: history only
        self.assertEqual(chain[0].assignment.valid_to, day(4))


@tag("mariadb")
class LegSwitchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = staff_user("actor.person@example.org")
        cls.anna = create_active_user("anna.person@example.org", first_name="Anna", last_name="Muster")
        cls.ruth = create_active_user("ruth.person@example.org", first_name="Ruth", last_name="Beispiel")
        cls.old, cls.new = make_leg("LEG Old"), make_leg("LEG New")
        cls.place, cls.elsewhere = make_location("Musterweg", "12"), make_location("Lindenweg", "7")
        cls.consumption = make_metering_point(1, Direction.CONSUMPTION, cls.place)
        cls.feed_in = make_metering_point(2, Direction.FEED_IN, cls.place)
        cls.of_ruth = make_metering_point(3, Direction.CONSUMPTION, cls.place)
        cls.far = make_metering_point(4, Direction.CONSUMPTION, cls.elsewhere)
        cls.archived = make_metering_point(5, Direction.CONSUMPTION, cls.place, is_archived=True)
        for point, person in (
            (cls.consumption, cls.anna), (cls.feed_in, cls.anna), (cls.of_ruth, cls.ruth),
            (cls.far, cls.anna), (cls.archived, cls.anna),
        ):
            assign_person(point, person, day(-60))
        for point in (cls.consumption, cls.feed_in, cls.of_ruth, cls.far):
            assign_leg(point, cls.old, day(-60))

    def current_leg(self, point):
        return leg_switch.current_assignment("leg", point, TODAY).leg

    def test_related_are_the_other_points_of_the_same_person_at_the_same_location(self):
        self.assertEqual(leg_switch.related_points(self.consumption, TODAY), [self.feed_in])
        # Not archived, not another location, not another person; nothing without location or person.
        self.assertEqual(leg_switch.related_points(self.far, TODAY), [])
        loose = make_metering_point(6)
        self.assertEqual(leg_switch.related_points(loose, TODAY), [])
        assign_person(loose, self.anna, day(-1))
        self.assertEqual(leg_switch.related_points(loose, TODAY), [])
        # A person assignment that has ended no longer counts.
        assignments.model_of("person").objects.filter(metering_point=self.feed_in).update(valid_to=day(-1))
        self.assertEqual(leg_switch.related_points(self.consumption, TODAY), [])

    def test_only_the_ticked_points_change_along_and_the_old_leg_ends_the_day_before(self):
        made = leg_switch.switch_leg(self.consumption, self.new, day(10), also=[self.feed_in], actor=self.actor, today=TODAY)
        self.assertEqual(len(made), 2)
        self.assertEqual(self.current_leg(self.consumption), self.old)  # valid until the day before
        self.assertEqual(
            list(LegAssignment.objects.filter(metering_point=self.consumption).order_by("valid_from").values_list("valid_to", flat=True)),
            [day(9), None],
        )
        self.assertEqual(LegAssignment.objects.filter(metering_point=self.of_ruth).count(), 1)  # not ticked
        self.assertEqual(
            history.entries_for(self.feed_in).filter(event=HistoryEvent.LEG_ASSIGNED).count(), 1,
        )

    def test_a_refused_assignment_changes_nothing_and_a_point_that_does_not_belong_is_refused(self):
        # The date is not after the start of the current assignment of the second point.
        assign_leg(self.feed_in, self.old, day(8))  # replaces nothing, but starts later than the new date
        with self.assertRaises(assignments.AssignmentError) as caught:
            leg_switch.switch_leg(self.consumption, self.new, day(5), also=[self.feed_in], today=TODAY)
        self.assertEqual(caught.exception.code, "starts_not_after_previous")
        self.assertEqual(LegAssignment.objects.filter(metering_point=self.consumption, leg=self.new).count(), 0)
        for also, code in (([self.of_ruth], "not_related"), ([self.far], "not_related")):
            with self.assertRaises(assignments.AssignmentError) as caught:
                leg_switch.switch_leg(self.consumption, self.new, day(20), also=also, today=TODAY)
            self.assertEqual(caught.exception.code, code)
        self.new.is_archived = True
        self.new.save()
        with self.assertRaises(assignments.AssignmentError) as caught:
            leg_switch.switch_leg(self.consumption, self.new, day(20), today=TODAY)
        self.assertEqual(caught.exception.code, "leg_archived")

    def test_a_point_that_already_has_the_leg_is_left_alone(self):
        assignments.assign("leg", self.feed_in, self.new, day(-2), today=TODAY)
        before = LegAssignment.objects.filter(metering_point=self.feed_in).count()
        made = leg_switch.switch_leg(self.consumption, self.new, day(10), also=[self.feed_in], today=TODAY)
        self.assertEqual(len(made), 1)
        self.assertEqual(LegAssignment.objects.filter(metering_point=self.feed_in).count(), before)


@tag("mariadb")
class PersonSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.anna = create_active_user("anna.muster@example.org", first_name="Anna", last_name="Muster")
        cls.annabelle = create_active_user("annabelle.muster@example.org", first_name="Annabelle", last_name="Muster")
        cls.ruth = create_active_user("ruth.beispiel@example.org", first_name="Ruth", last_name="Beispiel")
        cls.company = create_active_user("office.company@example.org", first_name="Beispiel", last_name="Energie")
        cls.blocked = create_active_user("blocked.person@example.org", first_name="Bea", last_name="Muster")
        cls.blocked.set_status(User.Status.BLOCKED)

    def test_the_search_finds_by_name_customer_number_and_email_and_only_usable_accounts(self):
        names = lambda text: {p.pk for p in person_search.search(text)}  # noqa: E731
        self.assertEqual(names("muster anna"), {self.anna.pk, self.annabelle.pk})
        self.assertEqual(names("energie"), {self.company.pk})
        self.assertEqual(names("ruth.beispiel@"), {self.ruth.pk})
        self.assertEqual(names(self.ruth.customer_number_id), {self.ruth.pk})
        # A blocked account cannot be assigned; it is not offered.
        self.assertNotIn(self.blocked.pk, names("muster"))

    def test_the_result_is_limited(self):
        self.assertEqual(len(person_search.search("example", limit=2)), 2)
        self.assertLessEqual(len(person_search.search("example")), person_search.LIMIT)

    def test_a_text_names_one_person_or_it_is_refused(self):
        resolve = person_search.resolve
        self.assertEqual(resolve(person_search.label(self.anna)).person, self.anna)
        self.assertEqual(resolve("anna.muster@example.org").person, self.anna)
        number = self.ruth.customer_number_id
        self.assertEqual(resolve(f"{number[:3]} {number[3:]}").person, self.ruth)
        self.assertEqual(resolve("annabelle").person, self.annabelle)
        self.assertEqual(resolve("muster").code, "several")
        self.assertEqual(resolve("nobody here").code, "none")
        self.assertEqual(resolve("").code, "none")
        # The label of a suggestion never carries the e-mail address.
        self.assertNotIn("@", person_search.label(self.anna))


@tag("mariadb")
class ListQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.anna = create_active_user("anna.person@example.org", first_name="Anna", last_name="Muster")
        cls.leg_a, cls.leg_b = make_leg("LEG Bahnhof"), make_leg("LEG Quartier")
        cls.musterweg = make_location("Musterweg", "12", "3063", "Ittigen", remark="secret remark")
        cls.registered = make_metering_point(1, location=cls.musterweg, remark="hidden note")
        cls.notified = make_metering_point(2, location=cls.musterweg, registered_with_bkw_on=day(-9))
        cls.standby = make_metering_point(
            3, location=cls.musterweg, registered_with_bkw_on=day(-9), confirmed_on=day(-8)
        )
        cls.active = make_metering_point(
            4, location=cls.musterweg, registered_with_bkw_on=day(-9), confirmed_on=day(-8), start_date=day(-1)
        )
        cls.loose = make_metering_point(5)
        cls.archived = make_metering_point(6, location=cls.musterweg, is_archived=True)
        for point in (cls.registered, cls.notified, cls.standby):
            assign_leg(point, cls.leg_a, day(-30))
        assign_leg(cls.active, cls.leg_b, day(-30))
        assign_person(cls.active, cls.anna, day(-30))

    def numbers(self, queryset):
        return [point.number[-1] for point in queryset]

    def view(self):
        return metering_point_list.base_queryset(TODAY)

    def test_the_quick_filters_count_the_points_of_the_view(self):
        counts = metering_point_list.step_counts(self.view(), TODAY)
        self.assertEqual(counts, {"all": 5, "notify": 2, "confirm": 1, "start": 1, "archived": 1})
        # The search and the LEG filter are part of the view.
        in_leg = metering_point_list.apply_leg(self.view(), self.leg_a.pk)
        self.assertEqual(metering_point_list.step_counts(in_leg, TODAY)["all"], 3)
        found = metering_point_list.apply_search(self.view(), "ittigen", today=TODAY)
        self.assertEqual(metering_point_list.step_counts(found, TODAY)["all"], 4)

    def test_each_quick_filter_shows_its_points_and_the_archive_only_its_own(self):
        def shown(step):
            return self.numbers(
                metering_point_list.apply_sort(metering_point_list.apply_step(self.view(), step, TODAY), "number")
            )

        self.assertEqual(shown("all"), ["1", "2", "3", "4", "5"])
        self.assertEqual(shown("notify"), ["1", "5"])
        self.assertEqual(shown("confirm"), ["2"])
        self.assertEqual(shown("start"), ["3"])
        self.assertEqual(shown("archived"), ["6"])

    def test_the_problems_are_the_points_without_location_leg_or_person_and_never_archived_ones(self):
        problem = metering_point_list.apply_problems(self.view())
        self.assertEqual(sorted(self.numbers(problem)), ["1", "2", "3", "5"])  # point 4 has all three
        problem_texts = {
            point.number[-1]: [str(problems.describe(p)) for p in problems.metering_point_problems(point, TODAY)]
            for point in problem
        }
        self.assertEqual(len(problem_texts["5"]), 3)
        self.assertEqual(len(problem_texts["1"]), 1)

    def test_the_search_finds_what_is_shown_and_never_the_remarks(self):
        def search(text, see_persons=False):
            return sorted(self.numbers(metering_point_list.apply_search(self.view(), text, see_persons=see_persons, today=TODAY)))

        self.assertEqual(search("000002"), ["2"])
        self.assertEqual(search("musterweg 12"), ["1", "2", "3", "4", "6"])
        self.assertEqual(search("quartier"), ["4"])  # the name of the LEG
        self.assertEqual(search("hidden note"), [])
        self.assertEqual(search("secret remark"), [])
        # A name is found only with the right to see persons.
        self.assertEqual(search("muster", see_persons=False), ["1", "2", "3", "4", "6"])  # "Musterweg"
        self.assertEqual(search("anna", see_persons=False), [])
        self.assertEqual(search("anna", see_persons=True), ["4"])

    def test_the_sort_by_status_leg_and_location_puts_missing_values_last(self):
        def ordered(key, descending=False):
            queryset = metering_point_list.apply_step(self.view(), "all", TODAY)
            return self.numbers(metering_point_list.apply_sort(queryset, key, descending, TODAY))

        self.assertEqual(ordered("status"), ["1", "5", "2", "3", "4"])
        self.assertEqual(ordered("leg")[-1], "5")
        self.assertEqual(ordered("leg", True)[-1], "5")
        self.assertEqual(ordered("location")[-1], "5")
        self.assertEqual(ordered("number", True), ["5", "4", "3", "2", "1"])

    def test_a_location_is_found_by_typing_and_a_text_must_name_one(self):
        other = make_location("Musterstrasse", "3", "3000", "Bern")
        found = location_list.resolve(str(self.musterweg))
        self.assertEqual(found.location, self.musterweg)
        self.assertEqual(location_list.resolve("musterweg").location, self.musterweg)
        self.assertEqual(location_list.resolve("muster").code, "several")
        self.assertEqual(location_list.resolve("00000000000000000001").location, self.musterweg)
        self.assertEqual(location_list.resolve("nowhere").code, "none")
        other.is_archived = True
        other.save()
        self.assertEqual(location_list.resolve("musterstrasse").code, "none")
        self.assertLessEqual(len(location_list.location_suggestions("muster")), location_list.LOCATION_CHOICE_LIMIT)
