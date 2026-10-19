"""Reminders and deadlines (D-103): the settings and the warning number, the scheduler rules (each mail once, a second run
changes nothing, only drafts are rolled back), the command and the log lines."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, tag
from django.utils import timezone

from leg_platform.accounts.management.commands import run_scheduled_tasks
from leg_platform.accounts.models import MailJob
from leg_platform.community import (
    application_deadlines,
    application_notices,
    application_staff,
    applications,
    contracts,
    deadline_settings,
    history,
    own_iban,
)
from leg_platform.community.application_common import MAX_UNRESOLVED_NOTICES_PER_PERSON, ApplicationError
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Application,
    ApplicationMeteringPoint,
    ApplicationNotice,
    Direction,
    Location,
    ManagementSettings,
    MeteringPoint,
    PersonRecord,
)
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import (
    GOOD_IBAN,
    TODAY,
    configure_numbers,
    customer,
    full_number,
    publish_contract,
    ready_draft,
    start_with_building,
)

State = Application.State
NOW = timezone.now()


def days_ago(count):
    return NOW - timedelta(days=count)


def set_state(application, **fields):
    Application.objects.filter(pk=application.pk).update(**fields)
    application.refresh_from_db()
    return application


def mails(kind):
    return MailJob.objects.filter(kind=kind).count()


class DeadlineCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("deadline.customer@example.org")
        cls.staff = staff_user("deadline.staff@example.org")

    def setUp(self):
        configure_numbers()

    def idle_draft(self, age_days, person=None, **fields):
        self.egid_counter = getattr(self, "egid_counter", 80_000) + 1
        application, _building = start_with_building(person or self.person, self.egid_counter)
        return set_state(application, last_activity_at=days_ago(age_days), **fields)


@tag("mariadb")
class SettingsServiceTests(DeadlineCase):
    def test_the_whole_numbers_are_saved_with_history_of_the_names_only(self):
        changed = deadline_settings.update({"reminder_after_days": 3, "rollback_after_days": "21"}, self.staff)

        self.assertEqual(changed, ["reminder_after_days", "rollback_after_days"])
        row = ManagementSettings.load()
        self.assertEqual((row.reminder_after_days, row.rollback_after_days), (3, 21))
        entry = history.entries_for(row).get(event=HistoryEvent.DEADLINES_CHANGED)
        self.assertEqual((entry.actor, entry.detail), (self.staff, {"fields": ["reminder_after_days", "rollback_after_days"]}))
        self.assertNotIn("21", str(entry.detail))

    def test_an_unchanged_value_writes_nothing(self):
        self.assertEqual(deadline_settings.update({"reminder_after_days": 2}, self.staff), [])
        self.assertFalse(history.entries_for(ManagementSettings.load()).exists())

    def test_only_whole_days_from_one_are_accepted(self):
        for bad in (0, -1, "0", "-3", "1.5", "abc", "", " ", True, 2.0, 1.5, None, "٣", "1e3", [1]):
            with self.subTest(bad=bad), self.assertRaises(ValidationError) as caught:
                deadline_settings.update({"rollback_after_days": bad}, self.staff)
            self.assertIn("rollback_after_days", caught.exception.error_dict)
        self.assertEqual(ManagementSettings.load().rollback_after_days, 14)

    def test_there_is_no_upper_limit_of_our_own(self):
        deadline_settings.update({"contract_deadline_days": 100_000, "reminder_after_days": 2_000_000_000}, self.staff)
        self.assertEqual(ManagementSettings.load().contract_deadline_days, 100_000)
        # A date beyond the range simply never comes: no overflow in the scheduler.
        self.idle_draft(5)
        self.assertEqual(application_deadlines.process(NOW)["paused"], 0)

    def test_unknown_fields_and_other_accounts_are_refused(self):
        with self.assertRaises(ValueError):
            deadline_settings.update({"warning_threshold_percent": 5}, self.staff)
        for actor in (self.person, None):
            with self.assertRaises(ApplicationError):
                deadline_settings.update({"reminder_after_days": 5}, actor)
        self.assertEqual(ManagementSettings.load().reminder_after_days, 2)

    def test_the_existing_settings_service_is_unchanged(self):
        from leg_platform.community import settings_service

        self.assertFalse(set(deadline_settings.FIELDS) & set(settings_service.EDITABLE_FIELDS))


@tag("mariadb")
class AffectedDraftsTests(DeadlineCase):
    def test_the_warning_counts_only_the_drafts_that_the_new_values_delete(self):
        paused = self.idle_draft(20, state=State.PAUSED, reminded_at=days_ago(5))
        fresh = self.idle_draft(1)
        other = customer("deadline.second@example.org")
        old_paused = self.idle_draft(40, person=other, state=State.PAUSED, reminded_at=days_ago(20))

        self.assertEqual(deadline_settings.affected_drafts_count({}, NOW), 0)  # the old one is deleted anyway: not "because of" a change
        self.assertEqual(deadline_settings.affected_drafts_count({"rollback_after_days": 3}, NOW), 1)  # newly: paused
        self.assertEqual(deadline_settings.affected_drafts_count({"rollback_after_days": 30}, NOW), 0)
        self.assertEqual(deadline_settings.affected_drafts_count({"rollback_after_days": 14}, NOW), 0)
        # Nothing was written and no draft was touched.
        self.assertEqual(ManagementSettings.load().rollback_after_days, 14)
        self.assertEqual(Application.objects.count(), 3)
        self.assertIsNotNone((paused, fresh, old_paused))

    def test_the_contract_deadline_counts_too_and_a_number_is_all_it_gives(self):
        waiting = self.idle_draft(1, contract_pending_since=days_ago(10))
        count = deadline_settings.affected_drafts_count({"contract_deadline_days": 5}, NOW)
        self.assertEqual(count, 1)
        self.assertIsInstance(count, int)
        self.assertEqual(deadline_settings.affected_drafts_count({"contract_deadline_days": 11}, NOW), 0)
        self.assertIsNotNone(waiting)

    def test_a_submitted_application_is_never_counted(self):
        application, _building = ready_draft(self.person)
        applications.submit(self.person, application, today=TODAY)
        set_state(application, reminded_at=days_ago(100), contract_pending_since=days_ago(100))
        self.assertEqual(deadline_settings.affected_drafts_count({"rollback_after_days": 1, "contract_deadline_days": 1}, NOW), 0)

    def test_invalid_new_values_are_refused(self):
        with self.assertRaises(ValidationError):
            deadline_settings.affected_drafts_count({"rollback_after_days": 0}, NOW)


@tag("mariadb")
class IdleDraftTests(DeadlineCase):
    def test_an_idle_draft_is_paused_and_reminded_once_and_not_rolled_back_in_the_same_run(self):
        application = self.idle_draft(3)

        first = application_deadlines.process(NOW)
        second = application_deadlines.process(NOW)

        application.refresh_from_db()
        self.assertEqual(application.state, State.PAUSED)
        self.assertIsNotNone(application.reminded_at)
        self.assertEqual((first["paused"], first["rolled_back"], second["paused"], second["rolled_back"]), (1, 0, 0, 0))
        self.assertEqual(mails(MailJob.Kind.APPLICATION_REMINDER), 1)
        self.assertEqual(mails(MailJob.Kind.APPLICATION_ROLLED_BACK), 0)

    def test_a_draft_that_is_not_idle_long_enough_is_left_alone(self):
        self.idle_draft(1)
        self.assertEqual(application_deadlines.process(NOW)["paused"], 0)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_the_settings_decide_not_constants(self):
        self.idle_draft(3)
        deadline_settings.update({"reminder_after_days": 10}, self.staff)
        self.assertEqual(application_deadlines.process(NOW)["paused"], 0)
        deadline_settings.update({"reminder_after_days": 1}, self.staff)
        self.assertEqual(application_deadlines.process(NOW)["paused"], 1)

    def test_the_rollback_comes_after_the_rollback_period_counted_from_the_reminder(self):
        application = self.idle_draft(30, state=State.PAUSED, reminded_at=days_ago(13))
        self.assertEqual(application_deadlines.process(NOW)["rolled_back"], 0)
        set_state(application, reminded_at=days_ago(15))

        first = application_deadlines.process(NOW)
        second = application_deadlines.process(NOW)

        self.assertEqual((first["rolled_back"], second["rolled_back"]), (1, 0))
        self.assertFalse(Application.objects.filter(pk=application.pk).exists())
        self.assertEqual(mails(MailJob.Kind.APPLICATION_ROLLED_BACK), 1)
        entry = history.entries_for(application).get(event=HistoryEvent.APPLICATION_ROLLED_BACK)
        self.assertEqual((entry.actor, entry.detail), (None, {"reason": "idle"}))

    def test_a_change_ends_the_pause_and_the_next_idle_phase_gets_its_own_reminder(self):
        application = self.idle_draft(3)
        application_deadlines.process(NOW)
        building = application.buildings.get()
        applications.add_metering_point(
            self.person, application, building.pk, direction=Direction.CONSUMPTION, entered_number="1"
        )
        application.refresh_from_db()
        self.assertEqual((application.state, application.reminded_at), (State.DRAFT, None))

        set_state(application, last_activity_at=days_ago(3))
        application_deadlines.process(NOW)

        self.assertEqual(mails(MailJob.Kind.APPLICATION_REMINDER) + mails(MailJob.Kind.CONTRACT_REMINDER), 2)

    def test_the_rollback_deletes_only_the_draft_never_the_account_the_iban_or_stored_data(self):
        stored = make_location(street="Stored", house_number="1", egid=91_001)
        point = make_metering_point(80, location=stored)
        assign_person(point, self.person)
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        draft = self.idle_draft(30, state=State.PAUSED, reminded_at=days_ago(20))
        other = customer("deadline.neighbour@example.org")
        neighbour = self.idle_draft(1, person=other)

        application_deadlines.process(NOW)

        self.assertFalse(Application.objects.filter(pk=draft.pk).exists())
        self.assertTrue(Application.objects.filter(pk=neighbour.pk).exists())
        self.assertEqual(ApplicationMeteringPoint.objects.count(), 0)
        self.assertEqual(Location.objects.get(), stored)
        self.assertEqual(MeteringPoint.objects.get(), point)
        self.assertEqual(own_iban.own_iban(self.person), GOOD_IBAN)
        self.assertTrue(type(self.person).objects.filter(pk=self.person.pk).exists())
        self.assertTrue(PersonRecord.objects.filter(person=self.person).exclude(iban_encrypted="").exists())

    def test_nothing_that_was_submitted_or_finished_is_ever_rolled_back(self):
        application, _building = ready_draft(self.person)
        applications.submit(self.person, application, today=TODAY)
        for state in (State.SUBMITTED, State.IN_REVIEW, State.OPERATOR_CONFIRMED, State.COMPLETED):
            with self.subTest(state=state):
                set_state(
                    application, state=state, reminded_at=days_ago(500), last_activity_at=days_ago(500),
                    contract_pending_since=days_ago(500), operator_started_at=None,
                )
                result = application_deadlines.process(NOW)
                self.assertEqual((result["rolled_back"], result["paused"]), (0, 0))
                self.assertTrue(Application.objects.filter(pk=application.pk).exists())
        self.assertEqual(MailJob.objects.exclude(kind=MailJob.Kind.APPLICATION_SUBMITTED).count(), 0)

    def test_the_mail_goes_to_every_address_of_the_account(self):
        self.person.set_secondary_email("deadline.second@example.org")
        self.idle_draft(3)
        application_deadlines.process(NOW)
        self.assertEqual(
            sorted(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_REMINDER).values_list("address", flat=True)),
            ["deadline.customer@example.org", "deadline.second@example.org"],
        )


@tag("mariadb")
class ContractDeadlineTests(DeadlineCase):
    def waiting_draft(self, age_days, pending_days, **fields):
        application, _building = ready_draft(self.person, accept=False, start=False)
        return set_state(
            application, last_activity_at=days_ago(age_days), contract_pending_since=days_ago(pending_days), **fields
        )

    def test_the_contract_reminder_replaces_the_general_one(self):
        publish_contract(self.staff)
        application = self.waiting_draft(3, 3)
        application_deadlines.process(NOW)
        self.assertEqual((mails(MailJob.Kind.CONTRACT_REMINDER), mails(MailJob.Kind.APPLICATION_REMINDER)), (1, 0))
        application_deadlines.process(NOW)
        self.assertEqual(mails(MailJob.Kind.CONTRACT_REMINDER), 1)
        self.assertIsNotNone(application)

    def test_the_contract_deadline_rolls_the_draft_back_with_its_own_mail(self):
        publish_contract(self.staff)
        application = self.waiting_draft(1, 15)

        first = application_deadlines.process(NOW)
        second = application_deadlines.process(NOW)

        self.assertEqual((first["rolled_back"], second["rolled_back"]), (1, 0))
        self.assertFalse(Application.objects.filter(pk=application.pk).exists())
        self.assertEqual((mails(MailJob.Kind.CONTRACT_ROLLED_BACK), mails(MailJob.Kind.APPLICATION_ROLLED_BACK)), (1, 0))
        entry = history.entries_for(application).get(event=HistoryEvent.APPLICATION_ROLLED_BACK)
        self.assertEqual(entry.detail, {"reason": "contract_deadline"})

    def test_a_paused_draft_that_waits_for_the_contract_gets_the_contract_mail_when_rolled_back(self):
        publish_contract(self.staff)
        self.waiting_draft(30, 5, state=State.PAUSED, reminded_at=days_ago(15))
        application_deadlines.process(NOW)
        self.assertEqual(mails(MailJob.Kind.CONTRACT_ROLLED_BACK), 1)

    def test_a_draft_before_its_deadline_stays(self):
        publish_contract(self.staff)
        application = self.waiting_draft(1, 13)
        application_deadlines.process(NOW)
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())


@tag("mariadb")
class BankConnectionReminderTests(DeadlineCase):
    def submitted_with_feed_in(self):
        publish_contract(self.staff)
        application, _building = ready_draft(self.person, feed_in=True)
        applications.submit(self.person, application, today=TODAY)
        return set_state(application, submitted_at=days_ago(8))

    def test_one_reminder_per_period_of_missing(self):
        application = self.submitted_with_feed_in()
        PersonRecord.objects.filter(person=self.person).update(iban_encrypted="")

        first = application_deadlines.process(NOW)
        second = application_deadlines.process(NOW)

        self.assertEqual((first["bank_reminded"], second["bank_reminded"]), (1, 0))
        self.assertEqual(mails(MailJob.Kind.BANK_CONNECTION_MISSING), 1)
        # The connection is there again: the period ends; missing again later gives a new reminder.
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        application_deadlines.process(NOW)
        application.refresh_from_db()
        self.assertIsNone(application.iban_reminded_at)
        PersonRecord.objects.filter(person=self.person).update(iban_encrypted="")
        self.assertEqual(application_deadlines.process(NOW)["bank_reminded"], 1)
        self.assertEqual(mails(MailJob.Kind.BANK_CONNECTION_MISSING), 2)

    def test_no_reminder_before_the_period_or_with_an_iban_or_without_a_feed_in(self):
        application = self.submitted_with_feed_in()
        self.assertEqual(application_deadlines.process(NOW)["bank_reminded"], 0)  # the IBAN is stored
        PersonRecord.objects.filter(person=self.person).update(iban_encrypted="")
        set_state(application, submitted_at=days_ago(3))
        self.assertEqual(application_deadlines.process(NOW)["bank_reminded"], 0)
        other = customer("deadline.nofeedin@example.org")
        plain, _building = ready_draft(other, egid=90004271, number=9)
        applications.submit(other, plain, today=TODAY)
        set_state(plain, submitted_at=days_ago(30))
        self.assertEqual(application_deadlines.process(NOW)["bank_reminded"], 0)


@tag("mariadb")
class OperatorConfirmationTests(DeadlineCase):
    def at_operator(self, age_days, person=None, **kwargs):
        if contracts.current_version() is None:
            publish_contract(self.staff)
        person = person or self.person
        application, _building = ready_draft(person, **kwargs)
        applications.submit(person, application, today=TODAY)
        application_staff.mark_operator_started(application, self.staff, TODAY, today=TODAY)
        return set_state(application, operator_started_at=days_ago(age_days))

    def test_a_missing_confirmation_rolls_back_notifies_the_staff_and_keeps_a_notice(self):
        second_staff = staff_user("deadline.staff2@example.org")
        retired = staff_user("deadline.retired@example.org")
        retired.set_privileges(is_staff=False, is_superuser=False)
        application = self.at_operator(15, feed_in=True)

        first = application_deadlines.process(NOW)
        second = application_deadlines.process(NOW)

        self.assertEqual((first["operator_lapsed"], second["operator_lapsed"]), (1, 0))
        self.assertFalse(Application.objects.filter(pk=application.pk).exists())
        jobs = MailJob.objects.filter(kind=MailJob.Kind.OPERATOR_CONFIRMATION_LAPSED)
        self.assertEqual(
            sorted(jobs.values_list("address", flat=True)),
            ["deadline.staff2@example.org", "deadline.staff@example.org"],
        )
        notice = ApplicationNotice.objects.get()
        self.assertEqual((notice.person, notice.kind), (self.person, "operator_lapsed"))
        self.assertEqual(notice.numbers, sorted([full_number(1), full_number(2)]))
        entry = history.entries_for(application).get(event=HistoryEvent.APPLICATION_ROLLED_BACK)
        self.assertEqual(entry.detail, {"reason": "operator_not_confirmed"})
        # The customer's own data stays.
        self.assertEqual(own_iban.own_iban(self.person), GOOD_IBAN)
        self.assertIsNotNone(second_staff)

    def test_before_the_deadline_or_once_confirmed_nothing_happens(self):
        early = self.at_operator(13)
        self.assertEqual(application_deadlines.process(NOW)["operator_lapsed"], 0)
        set_state(early, operator_started_at=days_ago(30))
        application_staff.record_operator_confirmation(early, self.staff, TODAY, today=TODAY)
        self.assertEqual(application_deadlines.process(NOW)["operator_lapsed"], 0)
        self.assertTrue(Application.objects.filter(pk=early.pk).exists())

    def test_the_deadline_is_a_setting(self):
        self.at_operator(5)
        affected = deadline_settings.affected_counts({"operator_confirmation_days": 3}, NOW)
        deadline_settings.update({"operator_confirmation_days": 3}, self.staff, expected=affected.token)
        self.assertEqual(application_deadlines.process(NOW)["operator_lapsed"], 1)

    def test_a_lapse_is_recorded_and_deleted_even_with_twenty_open_notices(self):
        for index in range(MAX_UNRESOLVED_NOTICES_PER_PERSON):
            ApplicationNotice.objects.create(
                person=self.person, kind=application_notices.Kind.NUMBER_UNAVAILABLE, numbers=[str(index)]
            )
        application = self.at_operator(15)
        self.assertEqual(application_deadlines.process(NOW)["operator_lapsed"], 1)
        self.assertFalse(Application.objects.filter(pk=application.pk).exists())
        self.assertEqual(ApplicationNotice.objects.filter(kind="operator_lapsed").count(), 1)
        # A second lapse of the same person is recorded as well (no duplicate suppression for this kind).
        again = self.at_operator(15)
        application_deadlines.process(NOW)
        self.assertFalse(Application.objects.filter(pk=again.pk).exists())

    def test_without_a_recorded_notice_nothing_is_deleted(self):
        application = self.at_operator(15)
        with mock.patch.object(application_notices, "record", return_value=None):
            result = application_deadlines.process(NOW)
        self.assertEqual((result["operator_lapsed"], result["failed"]), (0, 1))
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())
        self.assertEqual(mails(MailJob.Kind.OPERATOR_CONFIRMATION_LAPSED), 0)


@tag("mariadb")
class AffectedAtOperatorTests(DeadlineCase):
    def at_operator(self, age_days):
        publish_contract(self.staff)
        application, _building = ready_draft(self.person)
        applications.submit(self.person, application, today=TODAY)
        application_staff.mark_operator_started(application, self.staff, TODAY, today=TODAY)
        return set_state(application, operator_started_at=days_ago(age_days))

    def test_the_second_number_counts_applications_at_the_grid_operator(self):
        self.at_operator(10)
        paused = self.idle_draft(20, person=customer("deadline.other@example.org"), state=State.PAUSED, reminded_at=days_ago(5))

        both = deadline_settings.affected_counts({"operator_confirmation_days": 5, "rollback_after_days": 3}, NOW)

        self.assertEqual((both.drafts, both.at_operator, both.token, bool(both)), (1, 1, "1:1", True))
        only = deadline_settings.affected_counts({"operator_confirmation_days": 5}, NOW)
        self.assertEqual((only.drafts, only.at_operator), (0, 1))
        self.assertEqual(deadline_settings.affected_counts({"operator_confirmation_days": 30}, NOW), deadline_settings.Affected())
        self.assertFalse(deadline_settings.Affected())
        self.assertEqual(deadline_settings.affected_drafts_count({"operator_confirmation_days": 5}, NOW), 0)
        self.assertIsNotNone(paused)

    def test_an_application_that_lapses_anyway_is_not_counted(self):
        self.at_operator(30)
        self.assertEqual(deadline_settings.affected_counts({"operator_confirmation_days": 5}, NOW).at_operator, 0)

    def test_only_applications_at_the_operator_are_counted(self):
        publish_contract(self.staff)
        application, _building = ready_draft(self.person)
        applications.submit(self.person, application, today=TODAY)
        set_state(application, operator_started_at=days_ago(100))
        self.assertEqual(deadline_settings.affected_counts({"operator_confirmation_days": 1}, NOW).at_operator, 0)


@tag("mariadb")
class UpdateConfirmationTests(DeadlineCase):
    def paused(self, person=None, **kwargs):
        return self.idle_draft(30, person=person, state=State.PAUSED, reminded_at=days_ago(10), **kwargs)

    def test_a_change_that_rolls_back_something_needs_the_token_of_the_numbers_it_was_shown(self):
        self.paused()
        for token in (None, "", "0:0", "1", "2:0", "1:1"):
            with self.subTest(token=token), self.assertRaises(deadline_settings.CountChanged) as caught:
                deadline_settings.update({"rollback_after_days": 5}, self.staff, expected=token, now=NOW)
            self.assertEqual(caught.exception.affected.token, "1:0")
        self.assertEqual(ManagementSettings.load().rollback_after_days, 14)
        self.assertFalse(history.entries_for(ManagementSettings.load()).exists())
        self.assertEqual(
            deadline_settings.update({"rollback_after_days": 5}, self.staff, expected="1:0", now=NOW),
            ["rollback_after_days"],
        )

    def test_the_numbers_are_computed_again_after_the_row_lock(self):
        self.paused()
        seen = deadline_settings.affected_counts({"rollback_after_days": 5}, NOW).token
        # Between the question and the answer another draft became affected.
        self.paused(person=customer("deadline.late@example.org"))
        with self.assertRaises(deadline_settings.CountChanged) as caught:
            deadline_settings.update({"rollback_after_days": 5}, self.staff, expected=seen, now=NOW)
        self.assertEqual(caught.exception.affected.token, "2:0")
        self.assertEqual(ManagementSettings.load().rollback_after_days, 14)

    def test_the_run_time_is_saved_in_the_same_transaction(self):
        from datetime import time

        from leg_platform.addresses import schedule

        self.paused()
        before = schedule.import_run_time()
        with self.assertRaises(deadline_settings.CountChanged):
            deadline_settings.update({"rollback_after_days": 5}, self.staff, run_time=time(4, 15), now=NOW)
        self.assertEqual(schedule.import_run_time(), before)
        deadline_settings.update(
            {"rollback_after_days": 5}, self.staff, expected="1:0", run_time=time(4, 15, 30), now=NOW
        )
        self.assertEqual(schedule.import_run_time(), time(4, 15))
        self.assertEqual(history.entries_for(ManagementSettings.load()).count(), 2)
        # Only the time: no deadline field changes, no confirmation needed.
        self.assertEqual(deadline_settings.update({}, self.staff, run_time=time(5, 0)), [])
        self.assertEqual(schedule.import_run_time(), time(5, 0))
        with self.assertRaises(ValueError):
            deadline_settings.update({}, self.staff, run_time="05:00")

    def test_the_whole_number_matrix_never_raises_anything_but_a_validation_error(self):
        too_big = (2**32, 4_294_967_296, "4294967296", "99999999999", "9" * 5000, "1" + "0" * 5000, "0" * 5000 + "5" * 11)
        for value in too_big:
            with self.subTest(length=len(str(value)) if isinstance(value, str) else value):
                with self.assertRaises(ValidationError) as caught:
                    deadline_settings.update({"reminder_after_days": value}, self.staff)
                self.assertIn("reminder_after_days", caught.exception.error_dict)
        for value in (2_147_483_648, "2147483648", "0000000000000000000000000000000003", 4_294_967_295):
            with self.subTest(ok=value):
                self.assertEqual(deadline_settings.clean_values({"reminder_after_days": value})["reminder_after_days"], int(value))
        self.assertEqual(ManagementSettings.load().reminder_after_days, 2)
        # A very large number does not reach the scheduler as an error either.
        deadline_settings.update({"reminder_after_days": 2_147_483_648}, self.staff)
        self.assertEqual(application_deadlines.process(NOW)["failed"], 0)


@tag("mariadb")
class FailingRowTests(DeadlineCase):
    def paused(self, person=None):
        return self.idle_draft(30, person=person, state=State.PAUSED, reminded_at=days_ago(20))

    def test_a_failing_row_does_not_stop_the_others(self):
        first = self.paused()
        second = self.paused(customer("deadline.next@example.org"))
        real = application_deadlines.delete_application
        calls = []

        def flaky(application, *args, **kwargs):
            calls.append(application.pk)
            if len(calls) == 1:
                raise RuntimeError("secret detail that must not be logged")
            return real(application, *args, **kwargs)

        with self.assertLogs("leg_platform.community.application_deadlines", "ERROR") as logs:
            with mock.patch.object(application_deadlines, "delete_application", side_effect=flaky):
                result = application_deadlines.process(NOW)

        self.assertEqual((result["failed"], result["rolled_back"]), (1, 1))
        self.assertEqual(Application.objects.filter(pk__in=[first.pk, second.pk]).count(), 1)
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("secret detail", text)

    def test_the_command_finishes_all_rows_and_then_exits_with_an_error(self):
        self.paused()
        self.paused(customer("deadline.next@example.org"))
        out = StringIO()
        with mock.patch.object(application_deadlines, "delete_application", side_effect=RuntimeError("x")):
            with self.assertRaises(CommandError):
                call_command("process_application_deadlines", stdout=out)
        self.assertIn("failed rows: 2", out.getvalue())


@tag("mariadb")
class InactiveAccountMailTests(DeadlineCase):
    def account(self, email, status):
        person = customer(email)
        if status != type(person).Status.ACTIVE:
            person.set_status(status)
        return person

    def test_a_blocked_or_deactivated_account_gets_no_mail_but_the_rollback_runs(self):
        user_model = type(self.person)
        for index, status in enumerate((user_model.Status.BLOCKED, user_model.Status.DEACTIVATED)):
            with self.subTest(status=status):
                person = self.account(f"deadline.inactive{index}@example.org", user_model.Status.ACTIVE)
                paused = self.idle_draft(30, person=person, state=State.PAUSED, reminded_at=days_ago(20))
                person.set_status(status)
                self.assertEqual(application_deadlines.process(NOW)["rolled_back"], 1)
                self.assertFalse(Application.objects.filter(pk=paused.pk).exists())
        self.assertEqual(MailJob.objects.count(), 0)

    def test_reminders_contract_and_bank_mails_are_skipped_too(self):
        user_model = type(self.person)
        publish_contract(self.staff)
        blocked = self.account("deadline.blocked@example.org", user_model.Status.ACTIVE)
        draft = self.idle_draft(3, person=blocked)
        blocked.set_status(user_model.Status.BLOCKED)
        result = application_deadlines.process(NOW)
        draft.refresh_from_db()
        # The pause itself happens (a state, not a mail); no mail is queued.
        self.assertEqual((result["paused"], draft.state), (1, State.PAUSED))
        waiting_person = customer("deadline.waiting@example.org")
        waiting, _building = ready_draft(waiting_person, accept=False, start=False, egid=90004272, number=7)
        set_state(waiting, contract_pending_since=days_ago(15), last_activity_at=days_ago(1))
        waiting_person.set_status(user_model.Status.DEACTIVATED)
        self.assertEqual(application_deadlines.process(NOW)["rolled_back"], 1)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_the_staff_rejection_mail_goes_to_active_accounts_only(self):
        publish_contract(self.staff)
        application, _building = ready_draft(self.person)
        applications.submit(self.person, application, today=TODAY)
        self.person.set_status(type(self.person).Status.BLOCKED)
        application_staff.reject(application, self.staff)
        self.assertFalse(Application.objects.filter(pk=application.pk).exists())
        self.assertEqual(mails(MailJob.Kind.APPLICATION_ROLLED_BACK), 0)

    def test_an_active_account_still_gets_the_mail(self):
        paused = self.idle_draft(30, state=State.PAUSED, reminded_at=days_ago(20))
        application_deadlines.process(NOW)
        self.assertFalse(Application.objects.filter(pk=paused.pk).exists())
        self.assertEqual(mails(MailJob.Kind.APPLICATION_ROLLED_BACK), 1)


class CoveredSubqueryTests(TestCase):
    def test_the_covered_points_are_a_subquery_never_a_list(self):
        from leg_platform.community import deregistrations

        covered = deregistrations._covered()
        self.assertNotIsInstance(covered, (list, set, tuple))
        sql = str(MeteringPoint.objects.exclude(pk__in=covered).query)
        self.assertEqual(sql.upper().count("SELECT"), 2)


@tag("mariadb")
class CommandTests(DeadlineCase):
    def test_the_command_runs_the_rules_prints_numbers_and_logs_no_data(self):
        self.idle_draft(3)
        out = StringIO()

        with self.assertLogs("leg_platform.community.application_deadlines", "INFO") as logs:
            call_command("process_application_deadlines", stdout=out)

        self.assertIn("drafts reminded: 1", out.getvalue())
        text = out.getvalue() + "\n".join(logs.output)
        for secret in ("deadline.customer", "example.org", "CH1A2B", "Ittigen", GOOD_IBAN):
            self.assertNotIn(secret, text)
        self.assertIn("reminded 1", "\n".join(logs.output))

    def test_it_is_part_of_the_scheduled_tasks_and_idempotent_there(self):
        self.assertIn("process_application_deadlines", run_scheduled_tasks.TASKS)
        self.idle_draft(3)
        call_command("run_scheduled_tasks", stdout=StringIO())
        call_command("run_scheduled_tasks", stdout=StringIO())
        self.assertEqual(mails(MailJob.Kind.APPLICATION_REMINDER), 1)
