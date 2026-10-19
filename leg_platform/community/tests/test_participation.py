"""The participation status lines of the start page (D-103, design walkthrough W1-18): at most two plain lines as data,
read through the account only."""

from datetime import date

from django.test import TestCase, tag

from leg_platform.community import applications, application_staff, deregistrations, own_iban, participation
from leg_platform.community.models import Application, Deregistration, Direction
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import GOOD_IBAN, TODAY, configure_numbers, customer, publish_contract, ready_draft

Line = participation.StatusLine


def line(kind, day=None):
    return Line(kind, day)


@tag("mariadb")
class StatusLineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("status.customer@example.org")
        cls.other = customer("status.other@example.org")
        cls.staff = staff_user("status.staff@example.org")

    def point(self, last, person=None, valid_from=date(2025, 6, 1), **extra):
        location = make_location(street=f"Weg {last}", egid=72_000 + last)
        point = make_metering_point(
            last, location=location, registered_with_operator_on=date(2025, 1, 1), confirmed_on=date(2025, 2, 1),
            start_date=valid_from, **extra,
        )
        assign_person(point, person or self.person, valid_from=valid_from)
        return point

    def lines(self, person=None, today=TODAY):
        return participation.status_lines(person or self.person, today)

    def test_nothing_to_say_gives_no_line(self):
        self.assertEqual(self.lines(), [])

    def test_a_participation_that_runs_says_since_when(self):
        self.point(1, valid_from=date(2026, 3, 1))
        self.point(2, valid_from=date(2025, 11, 1))
        self.assertEqual(self.lines(), [line(participation.PARTICIPATION_STARTED, date(2025, 11, 1))])

    def test_a_participation_that_starts_later_says_when(self):
        self.point(1, valid_from=date(2027, 1, 19))
        self.point(2, valid_from=date(2027, 3, 1))
        self.assertEqual(self.lines(), [line(participation.PARTICIPATION_STARTS, date(2027, 1, 19))])

    def test_a_terminated_or_archived_point_is_no_participation_any_more(self):
        self.point(1, terminated_on=date(2026, 9, 30))
        archived = self.point(2)
        type(archived).objects.filter(pk=archived.pk).update(is_archived=True)
        self.assertEqual(self.lines(), [])
        # Until the day before its termination it still runs.
        self.assertEqual(
            self.lines(today=date(2026, 9, 29))[0].kind, participation.PARTICIPATION_STARTED
        )

    def test_an_application_at_the_leg_and_at_the_grid_operator(self):
        configure_numbers()
        publish_contract(self.staff)
        application, _building = ready_draft(self.person)
        self.assertEqual(self.lines(), [])  # a draft is no status
        applications.submit(self.person, application, today=TODAY)
        self.assertEqual(self.lines(), [line(participation.APPLICATION_AT_LEG)])
        application_staff.start_review(application, self.staff)
        self.assertEqual(self.lines(), [line(participation.APPLICATION_AT_LEG)])
        application_staff.mark_operator_started(application, self.staff, TODAY, today=TODAY)
        self.assertEqual(self.lines(), [line(participation.APPLICATION_AT_OPERATOR)])
        Application.objects.filter(pk=application.pk).update(state=Application.State.OPERATOR_CONFIRMED)
        self.assertEqual(self.lines(), [line(participation.APPLICATION_AT_OPERATOR)])

    def test_a_deregistration_comes_first_the_confirmed_one_with_its_last_day(self):
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        draw = self.point(1)
        feed_in = self.point(2, direction=Direction.FEED_IN)
        dereg = deregistrations.request_deregistration(self.person, metering_point_ids=[draw.pk], today=TODAY)
        self.assertEqual(
            self.lines(),
            [line(participation.DEREGISTRATION_REQUESTED), line(participation.PARTICIPATION_STARTED, date(2025, 6, 1))],
        )
        deregistrations.confirm(dereg, self.staff, date(2027, 2, 28), today=TODAY)
        self.assertEqual(
            self.lines()[0], line(participation.DEREGISTRATION_RUNNING, date(2027, 2, 28))
        )
        # A second, earlier one: the nearest last day is shown.
        second = deregistrations.request_deregistration(self.person, metering_point_ids=[feed_in.pk], today=TODAY)
        deregistrations.confirm(second, self.staff, date(2027, 2, 28), today=TODAY)
        self.assertEqual(self.lines()[0].kind, participation.DEREGISTRATION_RUNNING)
        self.assertEqual(Deregistration.objects.count(), 2)

    def test_never_more_than_two_lines_and_in_the_order_of_importance(self):
        configure_numbers()
        publish_contract(self.staff)
        draw = self.point(1)
        deregistrations.request_deregistration(self.person, metering_point_ids=[draw.pk], today=TODAY)
        application, _building = ready_draft(self.person, egid=90004270, address={"house_number": "52"}, number=5)
        applications.submit(self.person, application, today=TODAY)
        kinds = [entry.kind for entry in self.lines()]
        self.assertEqual(kinds, [participation.DEREGISTRATION_REQUESTED, participation.APPLICATION_AT_LEG])
        self.assertLessEqual(len(self.lines()), participation.MAX_LINES)

    def test_nothing_of_another_account_shows(self):
        self.point(1, person=self.other, valid_from=date(2027, 1, 19))
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.lines(person=self.other), [line(participation.PARTICIPATION_STARTS, date(2027, 1, 19))])

    def test_a_line_holds_only_a_kind_and_a_date(self):
        self.point(1)
        (entry,) = self.lines()
        self.assertEqual(set(vars(entry)), {"kind", "date"})
        self.assertEqual(entry.date, date(2025, 6, 1))
