"""Deregistration (D-103): the notice period, the choice of the date, who may deregister what, the staff's
confirmation, the effect and the release of the account deletion."""

from datetime import date

from django.test import SimpleTestCase, TestCase, tag

from leg_platform.accounts.models import MailJob
from leg_platform.community import application_deadlines, deregistrations, history, own_iban
from leg_platform.community.application_common import ApplicationError, add_months, month_end
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Application, Deregistration, Direction, MeteringPoint
from leg_platform.community.status import MeteringPointStatus
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import GOOD_IBAN, TODAY, customer

State = Deregistration.State


def code_of(call):
    try:
        call()
    except ApplicationError as error:
        return error.code
    raise AssertionError("ApplicationError was not raised.")


def active_point(last, person, location=None, direction=Direction.CONSUMPTION):
    point = make_metering_point(
        last, direction=direction, location=location,
        registered_with_operator_on=date(2025, 1, 1), confirmed_on=date(2025, 2, 1), start_date=date(2025, 6, 1),
    )
    assign_person(point, person, valid_from=date(2025, 6, 1))
    return point


class DateArithmeticTests(SimpleTestCase):
    def test_the_notice_is_four_months_to_the_end_of_that_month(self):
        for day, expected in (
            (date(2026, 10, 19), date(2027, 2, 28)),
            (date(2026, 10, 1), date(2027, 2, 28)),
            (date(2026, 10, 31), date(2027, 2, 28)),
            (date(2027, 1, 31), date(2027, 5, 31)),
            (date(2027, 10, 15), date(2028, 2, 29)),  # leap year
            (date(2026, 9, 30), date(2027, 1, 31)),
        ):
            with self.subTest(day=day):
                self.assertEqual(deregistrations.earliest_termination_date(day), expected)

    def test_month_arithmetic_keeps_to_the_last_day_of_a_shorter_month(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2026, 12, 15), 3), date(2027, 3, 15))
        self.assertEqual(month_end(date(2028, 2, 3)), date(2028, 2, 29))


@tag("mariadb")
class RequestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("dereg.customer@example.org")
        cls.other = customer("dereg.other@example.org")
        cls.staff = staff_user("dereg.staff@example.org")

    def setUp(self):
        self.location = make_location(egid=70_001)
        self.draw = active_point(1, self.person, self.location)
        self.feed_in = active_point(2, self.person, self.location, Direction.FEED_IN)
        own_iban.set_own_iban(self.person, GOOD_IBAN)

    def request(self, **kwargs):
        return deregistrations.request_deregistration(self.person, today=TODAY, **kwargs)

    def test_a_feed_in_needs_an_iban_on_the_account_and_a_draw_does_not(self):
        own_iban.remove_own_iban(self.person)

        for kwargs in ({"location_id": self.location.pk}, {"metering_point_ids": [self.feed_in.pk]},
                       {"metering_point_ids": [self.draw.pk, self.feed_in.pk]}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(code_of(lambda: self.request(**kwargs)), "iban_required")
        self.assertEqual(Deregistration.objects.count(), 0)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.DEREGISTRATION_SUBMITTED).count(), 0)

        dereg = self.request(metering_point_ids=[self.draw.pk])
        self.assertEqual(dereg.items.count(), 1)
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        self.assertEqual(self.request(metering_point_ids=[self.feed_in.pk]).items.count(), 1)

    def test_a_whole_building_takes_all_its_active_points(self):
        self.person.set_secondary_email("dereg.second@example.org")

        dereg = self.request(location_id=self.location.pk)

        self.assertEqual(dereg.state, State.SUBMITTED)
        self.assertEqual(sorted(dereg.items.values_list("metering_point_id", flat=True)), [self.draw.pk, self.feed_in.pk])
        self.assertEqual((dereg.earliest_date, dereg.desired_date, dereg.wants_earliest), (date(2027, 2, 28),) * 2 + (True,))
        self.assertEqual(
            sorted(MailJob.objects.filter(kind=MailJob.Kind.DEREGISTRATION_SUBMITTED).values_list("address", flat=True)),
            ["dereg.customer@example.org", "dereg.second@example.org"],
        )
        entry = history.entries_for(dereg).get(event=HistoryEvent.DEREGISTRATION_SUBMITTED)
        self.assertEqual((entry.actor, entry.detail), (self.person, {"metering_points": 2, "desired_date": "2027-02-28"}))
        # Nothing is terminated by the request.
        self.assertTrue(all(point.terminated_on is None for point in MeteringPoint.objects.all()))

    def test_a_single_point_with_a_later_desired_month_end(self):
        dereg = self.request(metering_point_ids=[self.feed_in.pk], desired_date=date(2027, 6, 30))

        self.assertEqual(list(dereg.items.values_list("metering_point_id", flat=True)), [self.feed_in.pk])
        self.assertEqual((dereg.desired_date, dereg.wants_earliest), (date(2027, 6, 30), False))

    def test_the_desired_date_is_a_month_end_and_never_before_the_earliest(self):
        for wish, expected in (
            (date(2027, 2, 27), "date_before_earliest"),
            (date(2026, 10, 31), "date_before_earliest"),
            (date(2020, 1, 31), "date_before_earliest"),
            (date(2027, 3, 15), "not_month_end"),
        ):
            with self.subTest(wish=wish):
                self.assertEqual(code_of(lambda: self.request(location_id=self.location.pk, desired_date=wish)), expected)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_a_point_or_building_of_another_account_is_not_found_like_a_missing_one(self):
        foreign_location = make_location(street="Foreign", egid=70_002)
        foreign = active_point(3, self.other, foreign_location)
        for kwargs in (
            {"metering_point_ids": [foreign.pk]},
            {"metering_point_ids": [999_999]},
            {"metering_point_ids": [self.draw.pk, foreign.pk]},
            {"location_id": foreign_location.pk},
            {"location_id": 999_999},
            {},
            {"metering_point_ids": [self.draw.pk], "location_id": self.location.pk},
            {"metering_point_ids": []},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(code_of(lambda: self.request(**kwargs)), "not_found")
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_a_point_that_is_not_active_yet_cannot_be_deregistered(self):
        waiting = make_metering_point(
            4, location=self.location, registered_with_operator_on=date(2026, 8, 1), confirmed_on=date(2026, 9, 1),
            start_date=date(2027, 1, 1),
        )
        assign_person(waiting, self.person, valid_from=date(2027, 1, 1))
        notified = make_metering_point(5, registered_with_operator_on=date(2026, 8, 1))
        assign_person(notified, self.person, valid_from=date(2027, 1, 1))
        for ids in ([waiting.pk], [notified.pk], [self.draw.pk, waiting.pk]):
            with self.subTest(ids=ids):
                self.assertEqual(code_of(lambda: self.request(metering_point_ids=ids)), "not_active_yet")
        # The whole building takes only what is active.
        dereg = self.request(location_id=self.location.pk)
        self.assertEqual(dereg.items.count(), 2)

    def test_a_point_is_in_one_deregistration_at_a_time(self):
        self.request(metering_point_ids=[self.draw.pk])
        self.assertEqual(code_of(lambda: self.request(metering_point_ids=[self.draw.pk])), "not_active_yet")
        dereg = self.request(location_id=self.location.pk)
        self.assertEqual(list(dereg.items.values_list("metering_point_id", flat=True)), [self.feed_in.pk])

    def test_a_building_without_an_active_point_has_nothing_to_deregister(self):
        self.request(location_id=self.location.pk)
        self.assertEqual(code_of(lambda: self.request(location_id=self.location.pk)), "nothing_to_deregister")

    def test_an_ended_assignment_or_an_archived_or_terminated_point_is_not_offered(self):
        ended = active_point(6, self.person)
        MeteringPoint.objects.filter(pk=ended.pk).update(terminated_on=date(2026, 8, 31))
        archived = active_point(7, self.person)
        MeteringPoint.objects.filter(pk=archived.pk).update(is_archived=True)
        for point in (ended, archived):
            with self.subTest(point=point.pk):
                self.assertEqual(code_of(lambda: self.request(metering_point_ids=[point.pk])), "not_active_yet")

    def test_the_open_deregistrations_per_account_are_capped(self):
        from unittest import mock

        with mock.patch.object(deregistrations, "MAX_OPEN_DEREGISTRATIONS_PER_PERSON", 1):
            self.request(metering_point_ids=[self.draw.pk])
            self.assertEqual(code_of(lambda: self.request(metering_point_ids=[self.feed_in.pk])), "too_many_deregistrations")

    def test_an_inactive_account_cannot_request(self):
        self.person.set_status(type(self.person).Status.BLOCKED)
        self.assertEqual(code_of(lambda: self.request(location_id=self.location.pk)), "not_allowed")

    def test_the_own_deregistration_is_found_through_the_account(self):
        dereg = self.request(location_id=self.location.pk)
        self.assertEqual(deregistrations.own_deregistration(self.person, dereg.pk), dereg)
        with self.assertRaises(Deregistration.DoesNotExist):
            deregistrations.own_deregistration(self.other, dereg.pk)


@tag("mariadb")
class ConfirmAndEffectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("effect.customer@example.org")
        cls.staff = staff_user("effect.staff@example.org")

    def setUp(self):
        self.location = make_location(egid=71_001)
        self.draw = active_point(1, self.person, self.location)
        self.feed_in = active_point(2, self.person, self.location, Direction.FEED_IN)
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        self.dereg = deregistrations.request_deregistration(self.person, location_id=self.location.pk, today=TODAY)

    def test_the_staff_confirms_and_the_termination_is_recorded_at_every_point(self):
        deregistrations.confirm(self.dereg, self.staff, date(2027, 3, 31), today=TODAY)

        self.dereg.refresh_from_db()
        self.assertEqual((self.dereg.state, self.dereg.confirmed_date), (State.CONFIRMED, date(2027, 3, 31)))
        for point in MeteringPoint.objects.all():
            self.assertEqual(point.terminated_on, date(2027, 3, 31))
            self.assertEqual(point.status(TODAY), MeteringPointStatus.ACTIVE)  # until the date is reached
            self.assertEqual(point.status(date(2027, 3, 31)), MeteringPointStatus.TERMINATED)
        entry = history.entries_for(self.dereg).get(event=HistoryEvent.DEREGISTRATION_CONFIRMED)
        self.assertEqual((entry.actor, entry.detail), (self.staff, {"confirmed_date": "2027-03-31"}))
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.DEREGISTRATION_EFFECTIVE).count(), 0)

    def test_the_date_is_a_month_end_and_not_in_the_past_and_the_state_is_checked(self):
        self.assertEqual(code_of(lambda: deregistrations.confirm(self.dereg, self.staff, date(2027, 3, 30), TODAY)), "not_month_end")
        self.assertEqual(code_of(lambda: deregistrations.confirm(self.dereg, self.staff, date(2026, 9, 30), TODAY)),
                         "date_before_earliest")
        deregistrations.confirm(self.dereg, self.staff, date(2027, 3, 31), today=TODAY)
        self.assertEqual(code_of(lambda: deregistrations.confirm(self.dereg, self.staff, date(2027, 4, 30), TODAY)), "wrong_state")

    def test_a_point_that_cannot_be_terminated_stops_the_whole_confirmation(self):
        # The feed-in is not active any more when the staff confirms (start date moved): ``terminate`` refuses it.
        MeteringPoint.objects.filter(pk=self.feed_in.pk).update(start_date=date(2027, 1, 1))
        self.assertEqual(code_of(lambda: deregistrations.confirm(self.dereg, self.staff, date(2026, 10, 31), TODAY)),
                         "cannot_terminate")
        self.assertTrue(all(point.terminated_on is None for point in MeteringPoint.objects.all()))
        self.dereg.refresh_from_db()
        self.assertEqual(self.dereg.state, State.SUBMITTED)

    def test_only_active_staff_confirm(self):
        other = customer("effect.other@example.org")
        blocked = staff_user("effect.blocked@example.org")
        blocked.set_status(type(blocked).Status.BLOCKED)
        for actor in (self.person, other, blocked, None):
            with self.subTest(actor=getattr(actor, "email", None)):
                self.assertEqual(code_of(lambda: deregistrations.confirm(self.dereg, actor, date(2027, 3, 31), TODAY)),
                                 "not_allowed")
        self.assertEqual(code_of(lambda: deregistrations.confirm(Deregistration(pk=999_999), self.staff, date(2027, 3, 31), TODAY)),
                         "not_found")

    def test_it_becomes_effective_once_the_date_is_reached_with_one_mail(self):
        self.person.set_secondary_email("effect.second@example.org")
        deregistrations.confirm(self.dereg, self.staff, date(2027, 3, 31), today=TODAY)

        self.assertEqual(deregistrations.finish_due(date(2027, 3, 30)), 0)
        self.assertEqual(deregistrations.finish_due(date(2027, 3, 31)), 1)
        self.assertEqual(deregistrations.finish_due(date(2027, 4, 15)), 0)

        self.dereg.refresh_from_db()
        self.assertEqual(self.dereg.state, State.EFFECTIVE)
        self.assertEqual(
            sorted(MailJob.objects.filter(kind=MailJob.Kind.DEREGISTRATION_EFFECTIVE).values_list("address", flat=True)),
            ["effect.customer@example.org", "effect.second@example.org"],
        )
        self.assertTrue(history.entries_for(self.dereg).filter(event=HistoryEvent.DEREGISTRATION_EFFECTIVE).exists())

    def test_the_scheduler_makes_it_effective_and_a_second_run_changes_nothing(self):
        deregistrations.confirm(self.dereg, self.staff, date(2027, 3, 31), today=TODAY)
        first = application_deadlines.process(today=date(2027, 4, 1))
        second = application_deadlines.process(today=date(2027, 4, 1))
        self.assertEqual((first["deregistrations_effective"], second["deregistrations_effective"]), (1, 0))
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.DEREGISTRATION_EFFECTIVE).count(), 1)

    def test_the_confirmation_dialog_is_prefilled_with_the_customers_wish_and_the_earliest_date(self):
        defaults = deregistrations.confirmation_defaults(self.dereg, self.staff, today=TODAY)
        self.assertEqual(
            (defaults.desired_date, defaults.wants_earliest, defaults.earliest_date, defaults.suggested_date),
            (date(2027, 2, 28), True, date(2027, 2, 28), date(2027, 2, 28)),
        )
        # The default is a date that ``confirm`` accepts.
        deregistrations.confirm(self.dereg, self.staff, defaults.suggested_date, today=TODAY)

    def test_a_wish_that_has_passed_is_replaced_by_the_end_of_the_current_month(self):
        Deregistration.objects.filter(pk=self.dereg.pk).update(desired_date=date(2026, 9, 30), wants_earliest=False)
        defaults = deregistrations.confirmation_defaults(self.dereg, self.staff, today=TODAY)
        self.assertEqual((defaults.desired_date, defaults.wants_earliest), (date(2026, 9, 30), False))
        self.assertEqual(defaults.suggested_date, date(2026, 10, 31))
        deregistrations.confirm(self.dereg, self.staff, defaults.suggested_date, today=TODAY)

    def test_the_defaults_are_for_staff_and_for_a_deregistration_that_waits_for_its_date(self):
        other = customer("effect.defaults@example.org")
        for actor in (self.person, other, None):
            with self.subTest(actor=getattr(actor, "email", None)):
                self.assertEqual(code_of(lambda: deregistrations.confirmation_defaults(self.dereg, actor)), "not_allowed")
        self.assertEqual(
            code_of(lambda: deregistrations.confirmation_defaults(Deregistration(pk=999_999), self.staff)), "not_found"
        )
        deregistrations.confirm(self.dereg, self.staff, date(2027, 3, 31), today=TODAY)
        self.assertEqual(code_of(lambda: deregistrations.confirmation_defaults(self.dereg, self.staff)), "wrong_state")


class EarliestDatePreviewTests(SimpleTestCase):
    def test_the_preview_is_the_earliest_month_end_of_a_notice_given_on_that_day(self):
        for day, expected in ((date(2026, 10, 19), date(2027, 2, 28)), (date(2027, 1, 31), date(2027, 5, 31))):
            with self.subTest(day=day):
                self.assertEqual(deregistrations.earliest_date_preview(day), expected)
                self.assertEqual(deregistrations.earliest_date_preview(day), deregistrations.earliest_termination_date(day))

    def test_without_a_day_it_is_today(self):
        from django.utils import timezone

        self.assertEqual(
            deregistrations.earliest_date_preview(), deregistrations.earliest_termination_date(timezone.localdate())
        )


@tag("mariadb")
class DeletionReleaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("release.customer@example.org")

    def test_an_account_without_metering_points_is_released(self):
        self.assertTrue(deregistrations.deletion_released(self.person, TODAY))

    def test_running_points_block_until_a_deregistration_was_actively_started(self):
        location = make_location(egid=72_001)
        active_point(1, self.person, location)
        active_point(2, self.person, location, Direction.FEED_IN)
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        self.assertEqual(
            deregistrations.deletion_blockers(self.person, TODAY),
            {"running_metering_points": 2, "applications_in_flight": 0},
        )
        self.assertFalse(deregistrations.deletion_released(self.person, TODAY))

        deregistrations.request_deregistration(self.person, metering_point_ids=[MeteringPoint.objects.first().pk], today=TODAY)
        self.assertEqual(deregistrations.deletion_blockers(self.person, TODAY)["running_metering_points"], 1)

        deregistrations.request_deregistration(self.person, location_id=location.pk, today=TODAY)
        # Started, not necessarily confirmed by the operator: released.
        self.assertTrue(deregistrations.deletion_released(self.person, TODAY))

    def test_a_point_that_is_not_active_yet_and_an_application_in_flight_block(self):
        waiting = make_metering_point(3, confirmed_on=date(2026, 9, 1), registered_with_operator_on=date(2026, 8, 1),
                                      start_date=date(2027, 1, 1))
        assign_person(waiting, self.person, valid_from=date(2027, 1, 1))
        self.assertEqual(deregistrations.deletion_blockers(self.person, TODAY)["running_metering_points"], 1)
        MeteringPoint.objects.filter(pk=waiting.pk).update(terminated_on=date(2026, 9, 30))
        self.assertTrue(deregistrations.deletion_released(self.person, TODAY))
        Application.objects.create(person=self.person, state=Application.State.SUBMITTED)
        self.assertEqual(deregistrations.deletion_blockers(self.person, TODAY)["applications_in_flight"], 1)
        # A draft holds nothing back: it is deleted with the account.
        Application.objects.all().delete()
        Application.objects.create(person=self.person)
        self.assertTrue(deregistrations.deletion_released(self.person, TODAY))

    def test_the_blockers_are_those_of_the_account_asked_about(self):
        other = customer("release.other@example.org")
        active_point(1, other)
        self.assertTrue(deregistrations.deletion_released(self.person, TODAY))
        self.assertFalse(deregistrations.deletion_released(other, TODAY))
