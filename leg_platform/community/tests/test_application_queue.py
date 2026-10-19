"""The data of the staff overview and the queue (D-103): counts, lists, notices; no drafts; staff only."""

from datetime import date

from django.test import TestCase, tag

from leg_platform.community import application_notices, application_queue, applications, application_staff, deregistrations
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import Application, ApplicationNotice, ContractVersion
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import TODAY, configure_numbers, customer, publish_contract, ready_draft

State = Application.State


@tag("mariadb")
class QueueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("queue.staff@example.org")
        cls.first = customer("queue.first@example.org", first_name="Erika", last_name="Muster")
        cls.second = customer("queue.second@example.org")

    def setUp(self):
        configure_numbers()

    def submit(self, person, egid, number):
        application, _building = ready_draft(person, egid=egid, number=number)
        applications.submit(person, application, today=TODAY)
        return application

    def test_without_a_contract_the_count_says_so_and_nothing_else_is_open(self):
        self.assertEqual(
            application_queue.queue_counts(),
            {"applications_submitted": 0, "applications_in_review": 0, "applications_at_operator": 0,
             "deregistrations_waiting": 0, "contract_missing": 1, "problems": 0},
        )

    def test_the_counts_follow_the_states_and_never_include_a_draft(self):
        publish_contract(self.staff)
        draft, _building = ready_draft(self.second, egid=61_000, number=9)
        submitted = self.submit(self.first, 61_001, 1)
        reviewing = self.submit(self.first, 61_002, 3)
        application_staff.start_review(reviewing, self.staff)
        at_operator = self.submit(self.first, 61_003, 5)
        application_staff.mark_operator_started(at_operator, self.staff, TODAY, today=TODAY)

        counts = application_queue.queue_counts()

        self.assertEqual(
            counts,
            {"applications_submitted": 1, "applications_in_review": 1, "applications_at_operator": 1,
             "deregistrations_waiting": 0, "contract_missing": 0, "problems": 0},
        )
        self.assertIsNotNone((draft, submitted))

    def test_the_lists_hold_only_what_staff_may_see_and_no_drafts(self):
        publish_contract(self.staff)
        ready_draft(self.second, egid=61_000, number=9)
        submitted = self.submit(self.first, 61_001, 1)

        (row,) = application_queue.pending_applications(self.staff)

        self.assertEqual((row.application_id, row.state, row.person_id), (submitted.pk, State.SUBMITTED, self.first.pk))
        self.assertEqual((row.buildings, row.metering_points), (1, 1))
        self.assertIn("Erika Muster", row.person_label)
        self.assertNotIn("@", row.person_label)  # never the email address
        # A draft cannot be asked for by listing its state.
        self.assertEqual(application_queue.pending_applications(self.staff, states=(State.DRAFT, State.PAUSED)), [])

    def test_the_applications_at_the_operator(self):
        publish_contract(self.staff)
        application = self.submit(self.first, 61_001, 1)
        self.assertEqual(application_queue.applications_at_operator(self.staff), [])
        application_staff.mark_operator_started(application, self.staff, TODAY, today=TODAY)
        self.assertEqual([row.application_id for row in application_queue.applications_at_operator(self.staff)], [application.pk])

    def test_waiting_deregistrations(self):
        location = make_location(egid=61_100)
        point = make_metering_point(
            1, location=location, registered_with_operator_on=date(2025, 1, 1), confirmed_on=date(2025, 2, 1),
            start_date=date(2025, 6, 1),
        )
        assign_person(point, self.first, valid_from=date(2025, 6, 1))
        dereg = deregistrations.request_deregistration(self.first, location_id=location.pk, today=TODAY)

        (row,) = application_queue.waiting_deregistrations(self.staff)
        self.assertEqual((row.deregistration_id, row.metering_points, row.desired_date), (dereg.pk, 1, date(2027, 2, 28)))
        self.assertEqual(application_queue.queue_counts()["deregistrations_waiting"], 1)
        deregistrations.confirm(dereg, self.staff, date(2027, 3, 31), today=TODAY)
        self.assertEqual(application_queue.waiting_deregistrations(self.staff), [])

    def test_notices_are_counted_listed_and_resolved_without_their_numbers(self):
        notice = application_notices.record(self.first, application_notices.Kind.NUMBER_UNAVAILABLE, numbers=["CH123"])
        self.assertEqual(application_queue.queue_counts()["problems"], 1)
        self.assertEqual(application_queue.problems(self.staff), [notice])

        application_notices.resolve(notice, self.staff)

        notice.refresh_from_db()
        self.assertEqual((notice.numbers, notice.resolved_by), ([], self.staff))
        self.assertIsNotNone(notice.resolved_at)
        self.assertEqual(application_queue.queue_counts()["problems"], 0)

    def test_the_same_notice_is_written_once_and_the_number_per_account_is_capped(self):
        kind = application_notices.Kind.BUILDING_UNAVAILABLE
        self.assertIsNotNone(application_notices.record(self.first, kind, egid=5))
        self.assertIsNone(application_notices.record(self.first, kind, egid=5))
        for egid in range(10, 10 + 30):
            application_notices.record(self.first, kind, egid=egid)
        self.assertEqual(ApplicationNotice.objects.filter(person=self.first).count(), 20)

    def test_only_staff_see_the_queue(self):
        for actor in (self.first, None):
            for call in (
                lambda: application_queue.pending_applications(actor),
                lambda: application_queue.applications_at_operator(actor),
                lambda: application_queue.waiting_deregistrations(actor),
                lambda: application_queue.problems(actor),
                lambda: application_notices.resolve(ApplicationNotice(pk=1), actor),
            ):
                with self.assertRaises(ApplicationError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "not_allowed")
        self.assertEqual(ContractVersion.objects.count(), 0)
