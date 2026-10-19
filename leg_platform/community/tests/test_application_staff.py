"""The staff's side of an application (D-103): review, return, the registration at the grid operator, completion and
rejection; who may do it; what completion creates; and that a rejection deletes only draft rows."""

from datetime import date
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase, tag

from leg_platform.accounts.models import MailJob, SecurityEvent
from leg_platform.community import application_staff, applications, history
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Application,
    ApplicationBuilding,
    ApplicationMeteringPoint,
    ApplicationNotice,
    CustomerMessage,
    Direction,
    Location,
    MeteringPoint,
    PersonAssignment,
)
from leg_platform.community.status import MeteringPointStatus
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import (
    GOOD_IBAN,
    TODAY,
    configure_numbers,
    customer,
    full_number,
    ready_draft,
)

State = Application.State
STARTED = date(2026, 6, 1)
CONFIRMED = date(2026, 7, 1)
START = date(2026, 9, 1)  # three months after the registration at the operator


def code_of(call):
    try:
        call()
    except ApplicationError as error:
        return error.code
    raise AssertionError("ApplicationError was not raised.")


class StaffCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("review.customer@example.org")
        cls.staff = staff_user("review.staff@example.org")

    def setUp(self):
        configure_numbers()

    def submitted(self, person=None, **kwargs):
        person = person or self.person
        application, building = ready_draft(person, **kwargs)
        applications.submit(person, application, today=TODAY)
        application.refresh_from_db()
        return application

    def at_operator(self, **kwargs):
        application = self.submitted(**kwargs)
        application_staff.mark_operator_started(application, self.staff, STARTED, today=TODAY)
        application_staff.record_operator_confirmation(application, self.staff, CONFIRMED, today=TODAY)
        application.refresh_from_db()
        return application


@tag("mariadb")
class TransitionTests(StaffCase):
    def test_review_return_and_resubmit(self):
        application = self.submitted()
        application_staff.start_review(application, self.staff)
        application.refresh_from_db()
        self.assertEqual(application.state, State.IN_REVIEW)

        application_staff.return_for_correction(application, self.staff, "Please check the number of the draw point.")

        application.refresh_from_db()
        self.assertEqual(application.state, State.DRAFT)
        self.assertIsNotNone(application.returned_at)
        self.assertEqual(application.returned_reason, "Please check the number of the draw point.")
        self.assertEqual(applications.returned_reason(application), "Please check the number of the draw point.")
        self.assertIsNone(application.submitted_at)
        # The submission mail and the mail about the return (action required).
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED).count(), 1)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_RETURNED).count(), 1)
        self.assertEqual(CustomerMessage.objects.get().kind, CustomerMessage.Kind.APPLICATION_RETURNED)
        applications.submit(self.person, application, today=TODAY)
        application.refresh_from_db()
        self.assertIsNone(application.returned_at)
        self.assertEqual((application.returned_reason, applications.returned_reason(application)), ("", ""))
        events = list(history.entries_for(application).values_list("event", flat=True))
        self.assertEqual(
            events,
            [HistoryEvent.APPLICATION_STARTED, HistoryEvent.APPLICATION_BUILDING_SET, HistoryEvent.APPLICATION_SUBMITTED,
             HistoryEvent.APPLICATION_IN_REVIEW, HistoryEvent.APPLICATION_RETURNED, HistoryEvent.APPLICATION_SUBMITTED],
        )

    def test_steps_cannot_be_taken_twice_or_out_of_order(self):
        application = self.submitted()
        self.assertEqual(code_of(lambda: application_staff.record_operator_confirmation(application, self.staff, TODAY, TODAY)),
                         "wrong_state")
        self.assertEqual(code_of(lambda: application_staff.complete(application, self.staff, START, today=TODAY)),
                         "wrong_state")
        application_staff.start_review(application, self.staff)
        self.assertEqual(code_of(lambda: application_staff.start_review(application, self.staff)), "wrong_state")
        application_staff.mark_operator_started(application, self.staff, STARTED, today=TODAY)
        self.assertEqual(code_of(lambda: application_staff.mark_operator_started(application, self.staff, STARTED, today=TODAY)),
                         "wrong_state")
        self.assertEqual(code_of(lambda: application_staff.return_for_correction(application, self.staff, "x")), "wrong_state")

    def test_the_dates_follow_the_rules(self):
        application = self.submitted()
        self.assertEqual(
            code_of(lambda: application_staff.mark_operator_started(application, self.staff, date(2030, 1, 1), today=TODAY)),
            "date_in_future",
        )
        application_staff.mark_operator_started(application, self.staff, STARTED, today=TODAY)
        for day, expected in ((date(2030, 1, 1), "date_in_future"), (date(2026, 5, 31), "date_before_start")):
            with self.subTest(day=day):
                self.assertEqual(
                    code_of(lambda: application_staff.record_operator_confirmation(application, self.staff, day, TODAY)),
                    expected,
                )
        application_staff.record_operator_confirmation(application, self.staff, STARTED, today=TODAY)
        application.refresh_from_db()
        self.assertEqual(application.state, State.OPERATOR_CONFIRMED)

    def test_history_names_ids_and_dates_only(self):
        application = self.at_operator()
        details = {
            entry.event: entry.detail
            for entry in history.entries_for(application).filter(
                event__in=[HistoryEvent.APPLICATION_OPERATOR_STARTED, HistoryEvent.APPLICATION_OPERATOR_CONFIRMED]
            )
        }
        self.assertEqual(details[HistoryEvent.APPLICATION_OPERATOR_STARTED], {"started_on": "2026-06-01"})
        self.assertEqual(details[HistoryEvent.APPLICATION_OPERATOR_CONFIRMED], {"confirmed_on": "2026-07-01"})

    def test_once_at_the_operator_the_customer_cannot_change_anything(self):
        application = self.submitted()
        application_staff.mark_operator_started(application, self.staff, STARTED, today=TODAY)
        self.assertEqual(code_of(lambda: applications.withdraw(self.person, application)), "not_editable")
        self.assertEqual(code_of(lambda: applications.cancel(self.person, application)), "not_editable")


@tag("mariadb")
class AuthorizationTests(StaffCase):
    def test_only_an_active_staff_account_may_act(self):
        application = self.submitted()
        blocked = staff_user("review.blocked@example.org")
        blocked.set_status(type(blocked).Status.BLOCKED)
        other_customer = customer("review.other@example.org")
        for actor in (self.person, other_customer, blocked, None):
            calls = {
                "review": lambda: application_staff.start_review(application, actor),
                "return": lambda: application_staff.return_for_correction(application, actor, "Reason"),
                "start": lambda: application_staff.mark_operator_started(application, actor, STARTED, today=TODAY),
                "confirm": lambda: application_staff.record_operator_confirmation(application, actor, STARTED, TODAY),
                "complete": lambda: application_staff.complete(application, actor, START, today=TODAY),
                "reject": lambda: application_staff.reject(application, actor),
            }
            for name, call in calls.items():
                with self.subTest(actor=getattr(actor, "email", None), call=name):
                    self.assertEqual(code_of(call), "not_allowed")
        application.refresh_from_db()
        self.assertEqual(application.state, State.SUBMITTED)

    def test_a_staff_account_that_lost_its_staff_status_is_refused(self):
        stale = staff_user("review.stale@example.org")
        application = self.submitted()
        stale_copy = type(stale).objects.get(pk=stale.pk)
        stale.set_privileges(is_staff=False, is_superuser=False)
        # The actor is read again: the object the caller holds does not decide.
        self.assertEqual(code_of(lambda: application_staff.start_review(application, stale_copy)), "not_allowed")

    def test_an_unknown_application_is_not_found(self):
        ghost = Application(pk=999_999)
        self.assertEqual(code_of(lambda: application_staff.start_review(ghost, self.staff)), "not_found")


@tag("mariadb")
class CompleteTests(StaffCase):
    def complete(self, application, **kwargs):
        return application_staff.complete(application, self.staff, kwargs.pop("start", START), today=TODAY, **kwargs)

    def test_completion_creates_the_location_the_points_and_the_assignments(self):
        application = self.at_operator(feed_in=True)

        self.complete(application)

        application.refresh_from_db()
        self.assertEqual(application.state, State.COMPLETED)
        location = Location.objects.get()
        self.assertEqual((location.egid, location.needs_confirmation), (90004269, True))
        points = {point.direction: point for point in MeteringPoint.objects.all()}
        self.assertEqual(set(points), {Direction.CONSUMPTION, Direction.FEED_IN})
        for point in points.values():
            self.assertEqual(
                (point.location, point.registered_with_operator_on, point.confirmed_on, point.start_date),
                (location, STARTED, CONFIRMED, START),
            )
            self.assertEqual(point.status(TODAY), MeteringPointStatus.ACTIVE)
            assignment = PersonAssignment.objects.get(metering_point=point)
            self.assertEqual((assignment.person, assignment.valid_from), (self.person, START))
        self.assertEqual(points[Direction.CONSUMPTION].number, full_number(1))
        for row in ApplicationMeteringPoint.objects.all():
            self.assertEqual(row.metering_point, MeteringPoint.objects.get(number=row.number))
        self.assertEqual(ApplicationBuilding.objects.get().location, location)
        # The assignment is in force: the account is kept (D-016) and the security log says so.
        self.person.refresh_from_db()
        self.assertTrue(self.person.retention_obligation)
        self.assertTrue(SecurityEvent.objects.filter(event=SecurityEvent.Event.RETENTION_OBLIGATION_SET).exists())
        entry = history.entries_for(application).get(event=HistoryEvent.APPLICATION_COMPLETED)
        self.assertEqual(entry.detail, {"start_date": "2026-09-01", "metering_points": 2})
        self.assertTrue(history.entries_for(location).filter(event=HistoryEvent.CREATED, actor=self.staff).exists())

    def test_the_start_is_not_before_three_months_after_the_registration(self):
        application = self.at_operator()
        with self.assertRaises(ApplicationError) as caught:
            self.complete(application, start=date(2026, 8, 31))
        self.assertEqual((caught.exception.code, caught.exception.details["earliest"]), ("start_too_early", START))
        self.assertEqual(MeteringPoint.objects.count(), 0)
        self.assertEqual(Location.objects.count(), 0)

    def test_a_confirmed_address_is_locked_at_once_on_request(self):
        application = self.at_operator()
        self.complete(application, confirm_addresses=True)
        self.assertFalse(Location.objects.get().needs_confirmation)
        self.assertTrue(history.entries_for(Location.objects.get()).filter(event=HistoryEvent.LOCATION_CONFIRMED).exists())

    def test_the_acceptance_sets_the_retention_flag_even_when_the_start_is_in_the_future(self):
        application = self.at_operator()
        self.complete(application, start=date(2027, 6, 1))
        self.person.refresh_from_db()
        # Set by the contract acceptance (owner decision 2026-10-19); the future assignment adds nothing.
        self.assertTrue(self.person.retention_obligation)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.RETENTION_OBLIGATION_SET).count(), 1)

    def test_a_stored_building_is_used_not_duplicated(self):
        location = make_location(street="Mine", house_number="2", egid=77_001)
        assign_person(make_metering_point(90, location=location), self.person)
        draft = applications.start_draft(self.person, location=location)
        applications.add_metering_point(
            self.person, draft, draft.buildings.get().pk, direction=Direction.FEED_IN, entered_number="91"
        )
        applications.save_bank_connection(self.person, draft, GOOD_IBAN)
        from leg_platform.community.tests.onboarding import publish_contract

        version = publish_contract(self.staff)
        applications.accept_contract_step(self.person, draft, version.pk, agrees_contract=True, agrees_data_exchange=True)
        applications.set_start(self.person, draft, asap=True)
        applications.submit(self.person, draft, today=TODAY)
        application_staff.mark_operator_started(draft, self.staff, STARTED, today=TODAY)
        application_staff.record_operator_confirmation(draft, self.staff, CONFIRMED, today=TODAY)

        self.complete(draft)

        self.assertEqual(Location.objects.count(), 1)
        self.assertEqual(MeteringPoint.objects.filter(location=location).count(), 2)

    def test_a_number_stored_meanwhile_rolls_everything_back(self):
        application = self.at_operator(feed_in=True)
        # The second number appears (raw edit) after the checks of the customer's side.
        MeteringPoint.objects.create(number=full_number(2), direction=Direction.CONSUMPTION)

        self.assertEqual(code_of(lambda: self.complete(application)), "number_exists")

        application.refresh_from_db()
        self.assertEqual(application.state, State.OPERATOR_CONFIRMED)
        self.assertEqual(MeteringPoint.objects.count(), 1)  # only the one that was stored meanwhile
        self.assertEqual(Location.objects.count(), 0)
        self.assertEqual(PersonAssignment.objects.count(), 0)

    def test_an_existing_building_of_another_account_is_shared_not_refused(self):
        application = self.at_operator()
        other = customer("review.owner@example.org")
        stored = make_location(street="Taken", house_number="1", egid=90004269)
        other_point = make_metering_point(60, location=stored)
        assign_person(other_point, other)

        self.complete(application)

        self.assertEqual(Location.objects.get(), stored)
        self.assertEqual(MeteringPoint.objects.filter(location=stored).count(), 2)
        mine = MeteringPoint.objects.get(number=full_number(1))
        self.assertEqual(PersonAssignment.objects.get(metering_point=mine).person, self.person)
        self.assertEqual(PersonAssignment.objects.get(metering_point=other_point).person, other)

    def test_an_archived_building_is_refused(self):
        application = self.at_operator()
        make_location(street="Taken", house_number="1", egid=90004269, is_archived=True)

        self.assertEqual(code_of(lambda: self.complete(application)), "building_unavailable")
        self.assertEqual(MeteringPoint.objects.count(), 0)

    def test_the_customers_own_words_for_the_building_are_saved_per_account(self):
        from leg_platform.community import buildings

        first = self.at_operator()
        first_building = first.buildings.get()
        ApplicationBuilding.objects.filter(pk=first_building.pk).update(name="Meins", description="oben")
        other = customer("review.shared@example.org")
        second = self.submitted(
            person=other, number=7, egid=90004269,
            address={"street": "Ittigenstrasse", "house_number": "51", "postal_code": "3063", "town": "Ittigen"},
        )
        application_staff.mark_operator_started(second, self.staff, STARTED, today=TODAY)
        application_staff.record_operator_confirmation(second, self.staff, CONFIRMED, today=TODAY)
        ApplicationBuilding.objects.filter(application=second).update(name="Deins")

        self.complete(first)
        self.complete(second)

        location = Location.objects.get()
        self.assertEqual(buildings.building_details(self.person, location), ("Meins", "oben"))
        self.assertEqual(buildings.building_details(other, location), ("Deins", ""))

    def test_completion_needs_the_confirmation_first(self):
        application = self.submitted()
        self.assertEqual(code_of(lambda: self.complete(application)), "wrong_state")

    def test_a_person_that_cannot_be_assigned_stops_the_completion(self):
        application = self.at_operator()
        self.person.set_status(type(self.person).Status.BLOCKED)

        self.assertEqual(code_of(lambda: self.complete(application)), "person_not_active")
        self.assertEqual(MeteringPoint.objects.count(), 0)
        self.assertEqual(Location.objects.count(), 0)


@tag("mariadb")
class RejectTests(StaffCase):
    def test_a_rejection_deletes_only_the_draft_rows_and_tells_the_customer(self):
        stored = make_location(street="Stored", house_number="3", egid=88_001)
        assign_person(make_metering_point(70, location=stored), self.person)
        application = self.at_operator()

        application_staff.reject(application, self.staff, "operator_rejected", "  Not   registered there. ")

        self.assertEqual(Application.objects.count(), 0)
        self.assertEqual(ApplicationBuilding.objects.count(), 0)
        self.assertEqual(Location.objects.get(), stored)
        self.assertEqual(MeteringPoint.objects.count(), 1)
        self.assertEqual(PersonAssignment.objects.count(), 1)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_REJECTED_OPERATOR).count(), 1)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_REJECTED).count(), 0)
        message = CustomerMessage.objects.get()
        self.assertEqual(
            (message.person, message.kind, message.text),
            (self.person, CustomerMessage.Kind.APPLICATION_REJECTED_OPERATOR, "Not registered there."),
        )
        entry = history.entries_for(application).get(event=HistoryEvent.APPLICATION_ROLLED_BACK)
        self.assertEqual((entry.actor, entry.detail), (self.staff, {"reason": "operator_rejected"}))

    def test_a_rejection_by_the_leg_has_its_own_mail_and_message_and_the_note_is_optional(self):
        application = self.at_operator()
        application_staff.reject(application, self.staff)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_REJECTED).count(), 1)
        message = CustomerMessage.objects.get()
        self.assertEqual((message.kind, message.text), (CustomerMessage.Kind.APPLICATION_REJECTED, ""))

    def test_the_note_is_limited_and_never_in_the_history_or_the_queue(self):
        application = self.at_operator()
        for bad in ("x" * 301, "a\x00b"):
            with self.subTest(bad=len(bad)), self.assertRaises(ValidationError):
                application_staff.reject(application, self.staff, "rejected", bad)
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())
        application_staff.reject(application, self.staff, "rejected", "x" * 300)
        for entry in history.entries_for(application):
            self.assertNotIn("xxxx", str(entry.detail))
        self.assertEqual(CustomerMessage.objects.get().text, "x" * 300)

    def test_a_draft_a_completed_application_and_an_unknown_reason_are_refused(self):
        application, _building = ready_draft(self.person)
        self.assertEqual(code_of(lambda: application_staff.reject(application, self.staff)), "wrong_state")
        applications.submit(self.person, application, today=TODAY)
        self.assertEqual(code_of(lambda: application_staff.reject(application, self.staff, "because")), "wrong_state")
        Application.objects.filter(pk=application.pk).update(state=State.COMPLETED)
        self.assertEqual(code_of(lambda: application_staff.reject(application, self.staff)), "wrong_state")
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())


@tag("mariadb")
class TwoAccountsTests(StaffCase):
    def test_two_accounts_with_the_same_number_in_drafts_cannot_both_complete(self):
        other = customer("review.second@example.org")
        first = self.at_operator(number=5)
        # The second account's number is changed to the first one's by a raw edit after its own checks and after its
        # own steps at the grid operator (the checks cannot see a number that appears later).
        second = self.submitted(
            person=other, number=7, egid=90004270,
            address={"street": "Zweiteweg", "house_number": "2", "postal_code": "3000", "town": "Bern"},
        )
        application_staff.mark_operator_started(second, self.staff, STARTED, today=TODAY)
        application_staff.record_operator_confirmation(second, self.staff, CONFIRMED, today=TODAY)
        ApplicationMeteringPoint.objects.filter(building__application=second).update(number=full_number(5))

        application_staff.complete(first, self.staff, START, today=TODAY)
        self.assertEqual(code_of(lambda: application_staff.complete(second, self.staff, START, today=TODAY)), "number_exists")
        self.assertEqual(MeteringPoint.objects.filter(number=full_number(5)).count(), 1)


@tag("mariadb")
class NumberRaceTests(StaffCase):
    """Two accounts submit the same number at the same time: the conflict surfaces before the registration at the grid
    operator, and exactly one of them goes on."""

    def setUp(self):
        super().setUp()
        self.other = customer("review.race@example.org")
        self.first = self.submitted(number=5)
        self.second = self.submitted(
            person=self.other, number=7, egid=90004270,
            address={"street": "Zweiteweg", "house_number": "2", "postal_code": "3000", "town": "Bern"},
        )
        # Both were submitted with a free number; now the second one names the first one's number.
        ApplicationMeteringPoint.objects.filter(building__application=self.second).update(number=full_number(5))

    def test_the_later_submission_is_refused_at_the_review_and_leaves_a_notice_for_the_staff(self):
        self.assertEqual(code_of(lambda: application_staff.start_review(self.second, self.staff)), "number_exists")

        self.second.refresh_from_db()
        self.assertEqual(self.second.state, State.SUBMITTED)
        notice = ApplicationNotice.objects.get()
        self.assertEqual((notice.person, notice.kind, notice.numbers), (self.other, "number_unavailable", [full_number(5)]))
        self.assertEqual(application_staff.start_review(self.first, self.staff).state, State.IN_REVIEW)

    def test_the_later_submission_is_refused_before_the_registration_at_the_grid_operator(self):
        self.assertEqual(
            code_of(lambda: application_staff.mark_operator_started(self.second, self.staff, STARTED, today=TODAY)),
            "number_exists",
        )
        self.second.refresh_from_db()
        self.assertEqual((self.second.state, self.second.operator_started_at), (State.SUBMITTED, None))
        application_staff.mark_operator_started(self.first, self.staff, STARTED, today=TODAY)
        # The earlier one at the grid operator keeps the number against the later one, also afterwards.
        self.assertEqual(code_of(lambda: application_staff.start_review(self.second, self.staff)), "number_exists")

    def test_an_earlier_submission_is_not_blocked_by_a_later_one(self):
        application_staff.mark_operator_started(self.first, self.staff, STARTED, today=TODAY)
        self.first.refresh_from_db()
        self.assertEqual(self.first.state, State.OPERATOR_STARTED)

    def test_a_number_stored_for_a_metering_point_meanwhile_refuses_both_steps(self):
        MeteringPoint.objects.create(number=full_number(5), direction=Direction.CONSUMPTION)
        self.assertEqual(code_of(lambda: application_staff.start_review(self.first, self.staff)), "number_exists")
        self.assertEqual(
            code_of(lambda: application_staff.mark_operator_started(self.first, self.staff, STARTED, today=TODAY)),
            "number_exists",
        )
        self.first.refresh_from_db()
        self.assertEqual(self.first.state, State.SUBMITTED)

    def test_the_notice_does_not_name_the_other_account(self):
        code_of(lambda: application_staff.start_review(self.second, self.staff))
        notice = ApplicationNotice.objects.get()
        self.assertNotIn("review.customer", str(notice.__dict__))

    def test_a_staff_member_is_still_required(self):
        self.assertEqual(code_of(lambda: application_staff.start_review(self.second, self.person)), "not_allowed")


@tag("mariadb")
class ConcurrentBuildingTests(StaffCase):
    """Two completions at the same new building: a fixed lock order and a retry when the unique constraint was lost."""

    def new_building_application(self, person, number, egid=90004269):
        application = self.at_operator(person=person, number=number, egid=egid)
        # The application names a building that does not exist yet.
        self.assertIsNone(application.buildings.get().location_id)
        return application

    def test_a_lost_race_on_the_new_building_attaches_to_the_winners_building(self):
        application = self.new_building_application(self.person, 1)
        winner = {}

        def lose(location):
            winner["location"] = Location.objects.create(
                street=location.street, house_number=location.house_number, postal_code=location.postal_code,
                town=location.town, egid=location.egid,
            )
            raise IntegrityError("duplicate")

        with mock.patch.object(application_staff, "_insert", side_effect=lose) as insert:
            application_staff.complete(application, self.staff, START, today=TODAY)

        self.assertEqual(insert.call_count, 1)
        self.assertEqual(Location.objects.get(), winner["location"])
        application.refresh_from_db()
        self.assertEqual(application.state, State.COMPLETED)
        self.assertEqual(application.buildings.get().location, winner["location"])
        point = MeteringPoint.objects.get()
        self.assertEqual(point.location, winner["location"])
        self.assertEqual(PersonAssignment.objects.get().person, self.person)

    def test_an_integrity_error_without_a_building_to_attach_to_is_building_unavailable_and_keeps_nothing(self):
        application = self.new_building_application(self.person, 1)
        with mock.patch.object(application_staff, "_insert", side_effect=IntegrityError("duplicate")):
            self.assertEqual(code_of(lambda: application_staff.complete(application, self.staff, START, today=TODAY)),
                             "building_unavailable")
        application.refresh_from_db()
        self.assertEqual(application.state, State.OPERATOR_CONFIRMED)
        self.assertEqual((Location.objects.count(), MeteringPoint.objects.count(), PersonAssignment.objects.count()), (0, 0, 0))

    def test_the_unique_constraint_is_real_and_the_savepoint_keeps_the_transaction_usable(self):
        stored = Location.objects.create(street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen", egid=1)
        duplicate = Location(street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen", egid=1)
        with self.assertRaises(IntegrityError):
            application_staff._insert(duplicate)
        self.assertEqual(Location.objects.get(), stored)

    def test_two_completions_at_the_same_new_building_share_it(self):
        other = customer("review.sharing@example.org")
        first = self.new_building_application(self.person, 1)
        second = self.new_building_application(other, 3)
        application_staff.complete(first, self.staff, START, today=TODAY)
        application_staff.complete(second, self.staff, START, today=TODAY)
        self.assertEqual(Location.objects.count(), 1)
        self.assertEqual(MeteringPoint.objects.count(), 2)

    def test_the_buildings_are_taken_in_a_fixed_order_by_egid_then_id(self):
        application = self.at_operator(egid=90004299)
        second = ApplicationBuilding.objects.create(
            application=application, egid=90004270, street="Zweiteweg", house_number="2", postal_code="3000", town="Bern",
        )
        order = []
        real = application_staff._location_for

        def spy(app, building, *args):
            order.append(building.egid)
            return real(app, building, *args)

        with mock.patch.object(application_staff, "_location_for", side_effect=spy):
            application_staff.complete(application, self.staff, START, today=TODAY)

        self.assertEqual(order, [90004270, 90004299])
        self.assertIsNotNone(second)


@tag("mariadb")
class ReturnForCorrectionTests(StaffCase):
    def test_the_reason_is_required_cleaned_and_limited(self):
        application = self.submitted()
        for bad, error in (("", ApplicationError), ("   \n ", ApplicationError), (None, ApplicationError),
                           ("x" * 501, ValidationError), ("a\x00b", ValidationError), ("a\u202eb", ValidationError)):
            with self.subTest(bad=bad if bad is None else len(bad)), self.assertRaises(error) as caught:
                application_staff.return_for_correction(application, self.staff, bad)
            if error is ApplicationError:
                self.assertEqual(caught.exception.code, "reason_required")
        application.refresh_from_db()
        self.assertEqual((application.state, application.returned_reason), (State.SUBMITTED, ""))
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_RETURNED).count(), 0)

        application_staff.return_for_correction(application, self.staff, "  Check   the\nnumber ")
        self.assertEqual(applications.returned_reason(application), "Check the number")
        self.assertEqual(
            code_of(lambda: application_staff.return_for_correction(application, self.staff, "Again")), "wrong_state"
        )

    def test_exactly_500_characters_are_accepted(self):
        application = self.submitted()
        application_staff.return_for_correction(application, self.staff, "x" * 500)
        self.assertEqual(len(applications.returned_reason(application)), 500)

    def test_the_history_and_the_mail_never_hold_the_text(self):
        application = self.submitted()
        application_staff.return_for_correction(application, self.staff, "Geheimer Grund 4711")
        entry = history.entries_for(application).get(event=HistoryEvent.APPLICATION_RETURNED)
        self.assertEqual(entry.detail, {"fields": ["returned_reason"]})
        self.assertNotIn("Geheimer", str(entry.detail))
        # The mail is a notification without payload: its text comes from the default text and holds no reason.
        from leg_platform.accounts import email_texts

        subject, body = email_texts.default_texts(email_texts.Key.APPLICATION_RETURNED)
        self.assertNotIn("Geheimer", subject + body)
        self.assertEqual(CustomerMessage.objects.get().text, "")

    def test_the_reason_is_shown_on_the_draft_only_and_belongs_to_the_account(self):
        application = self.submitted()
        application_staff.return_for_correction(application, self.staff, "Please correct")
        other = customer("review.stranger@example.org")
        self.assertEqual(applications.returned_reason(applications.own_application(self.person, application.pk)),
                         "Please correct")
        with self.assertRaises(Application.DoesNotExist):
            applications.own_application(other, application.pk)
        # A change of the customer keeps it; the submission clears it; a pause does not hide a returned draft.
        Application.objects.filter(pk=application.pk).update(state=State.PAUSED)
        self.assertEqual(applications.returned_reason(application), "Please correct")
        Application.objects.filter(pk=application.pk).update(state=State.DRAFT)
        applications.submit(self.person, application, today=TODAY)
        self.assertEqual(applications.returned_reason(application), "")

    def test_a_draft_that_was_never_returned_has_no_reason(self):
        application, _building = ready_draft(self.person)
        self.assertEqual(applications.returned_reason(application), "")
        applications.submit(self.person, application, today=TODAY)
        self.assertEqual(applications.returned_reason(application), "")

    def test_the_withdrawal_cap_does_not_stop_a_return(self):
        application = self.submitted()
        with mock.patch.object(applications, "MAX_DRAFTS_PER_PERSON", 0):
            application_staff.return_for_correction(application, self.staff, "Reason")
        application.refresh_from_db()
        self.assertEqual(application.state, State.DRAFT)

    def test_a_blocked_customer_gets_no_mail_but_the_return_happens(self):
        application = self.submitted()
        self.person.set_status(type(self.person).Status.BLOCKED)
        application_staff.return_for_correction(application, self.staff, "Reason")
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_RETURNED).count(), 0)
        self.assertEqual(CustomerMessage.objects.count(), 1)


@tag("mariadb")
class CompletionDefaultsTests(StaffCase):
    def test_the_wish_the_earliest_date_and_the_unconfirmed_addresses_are_given(self):
        application = self.at_operator()  # asap; registered on STARTED
        defaults = application_staff.completion_defaults(application, self.staff, today=TODAY)

        self.assertEqual(defaults.start_choice, "asap")
        self.assertIsNone(defaults.desired_start_date)
        self.assertEqual(defaults.earliest_start_date, START)
        self.assertTrue(defaults.registered)
        self.assertEqual(defaults.suggested_start_date, START)
        self.assertEqual(defaults.unconfirmed_addresses, 1)  # a new building is "not yet confirmed"

    def test_a_date_wish_is_kept_but_never_before_the_earliest_date(self):
        application = self.at_operator()
        later, earlier = date(2027, 2, 1), date(2026, 8, 1)
        for wish, expected in ((later, later), (earlier, START)):
            with self.subTest(wish=wish):
                Application.objects.filter(pk=application.pk).update(
                    start_choice=Application.StartChoice.DATE, desired_start_date=wish
                )
                defaults = application_staff.completion_defaults(application, self.staff, today=TODAY)
                self.assertEqual((defaults.desired_start_date, defaults.suggested_start_date), (wish, expected))
                self.assertEqual(defaults.start_choice, "date")

    def test_a_confirmed_stored_building_is_not_counted(self):
        make_location(street="Taken", house_number="1", egid=90004269)
        application = self.at_operator()
        self.assertEqual(application_staff.completion_defaults(application, self.staff, TODAY).unconfirmed_addresses, 0)
        Location.objects.update(needs_confirmation=True)
        self.assertEqual(application_staff.completion_defaults(application, self.staff, TODAY).unconfirmed_addresses, 1)

    def test_without_a_registration_date_the_earliest_is_three_months_from_today(self):
        application = self.at_operator()
        Application.objects.filter(pk=application.pk).update(operator_started_on=None)
        defaults = application_staff.completion_defaults(application, self.staff, today=date(2026, 10, 19))
        self.assertEqual((defaults.registered, defaults.earliest_start_date), (False, date(2027, 1, 19)))

    def test_the_default_is_accepted_by_complete(self):
        application = self.at_operator()
        defaults = application_staff.completion_defaults(application, self.staff, today=TODAY)
        application_staff.complete(application, self.staff, defaults.suggested_start_date, today=TODAY)
        application.refresh_from_db()
        self.assertEqual(application.state, State.COMPLETED)

    def test_staff_only_and_only_in_the_state_of_the_completion(self):
        application = self.at_operator()
        other = customer("review.defaults@example.org")
        for actor in (self.person, other, None):
            with self.subTest(actor=getattr(actor, "email", None)):
                self.assertEqual(code_of(lambda: application_staff.completion_defaults(application, actor)), "not_allowed")
        early = self.submitted(person=other, number=7, egid=90004270,
                               address={"street": "Zweiteweg", "house_number": "2", "postal_code": "3000", "town": "Bern"})
        self.assertEqual(code_of(lambda: application_staff.completion_defaults(early, self.staff)), "wrong_state")
        missing = Application(pk=0)
        self.assertEqual(code_of(lambda: application_staff.completion_defaults(missing, self.staff)), "not_found")

