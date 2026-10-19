"""The list of the processes and the small services behind the management pages of the onboarding (D-103): the rows of
``process_list`` (running applications and deregistrations, never a draft), their filters, search and sorting; the contract
history and the newest acceptance for the staff; the time of the daily address directory run."""

from datetime import date, time, timedelta
from unittest.mock import patch

from django.test import TestCase, tag
from django.utils import timezone

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.addresses import schedule
from leg_platform.community import (
    application_staff,
    applications,
    contracts,
    deadline_settings,
    deregistrations,
    history,
    process_list,
)
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Application, ContractAcceptance, ManagementSettings
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import TODAY, configure_numbers, customer, pdf, publish_contract, ready_draft

State = Application.State
STARTED = date(2026, 6, 1)


@tag("mariadb")
class ProcessListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        configure_numbers()
        cls.staff = staff_user("list.staff@example.org")
        publish_contract(cls.staff)
        cls.anna = customer("anna.list@example.org", first_name="Anna", last_name="Muster")
        cls.bert = customer("bert.list@example.org", first_name="Bert", last_name="Beispiel")
        cls.carl = customer("carl.list@example.org", first_name="Carl", last_name="Probe")

    def submitted(self, person, egid, number, street="Ittigenstrasse", house="51", **kwargs):
        application, _building = ready_draft(
            person, egid=egid, number=number, address={"street": street, "house_number": house}, **kwargs
        )
        applications.submit(person, application, today=TODAY)
        application.refresh_from_db()
        return application

    def test_only_running_applications_are_rows_and_never_a_draft(self):
        anna = self.submitted(self.anna, 90004269, 1)
        ready_draft(self.bert, egid=90004270, number=5)
        paused, _building = ready_draft(self.carl, egid=90004271, number=9)
        Application.objects.filter(pk=paused.pk).update(state=State.PAUSED)
        rows = process_list.rows()
        self.assertEqual([(row.kind, row.pk) for row in rows], [("application", anna.pk)])
        completed = self.submitted(self.bert, 90004272, 13, house="53")
        application_staff.mark_operator_started(completed, self.staff, STARTED, today=TODAY)
        application_staff.record_operator_confirmation(completed, self.staff, date(2026, 7, 1), today=TODAY)
        application_staff.complete(completed, self.staff, date(2026, 9, 1), today=TODAY)
        self.assertNotIn(completed.pk, [row.pk for row in process_list.rows()])

    def test_the_row_carries_what_the_list_shows(self):
        application = self.submitted(self.anna, 90004269, 1, feed_in=True)
        (row,) = process_list.rows()
        self.assertEqual(
            (row.person_name, row.person_number, row.address, row.egid, row.points, row.state),
            ("Anna Muster", self.anna.customer_number_display, "Ittigenstrasse 51, 3063 Ittigen", 90004269, 2, "submitted"),
        )
        self.assertEqual((row.progress_done, row.progress_total), (1, 5))
        self.assertEqual((row.date, row.start_choice), (None, Application.StartChoice.ASAP))
        self.assertEqual(row.changed, application.updated_at)

    def test_the_filters_by_kind_and_by_state(self):
        self.submitted(self.anna, 90004269, 1)
        bert = self.submitted(self.bert, 90004270, 5, house="52")
        application_staff.start_review(bert, self.staff)
        carl = self.submitted(self.carl, 90004271, 9, house="53")
        application_staff.mark_operator_started(carl, self.staff, STARTED, today=TODAY)
        states = {
            state: sorted(row.person_name for row in process_list.rows(state=state))
            for state in ("all", "submitted", "in_review", "at_operator", "deregistration_submitted")
        }
        self.assertEqual(states["all"], ["Anna Muster", "Bert Beispiel", "Carl Probe"])
        self.assertEqual(states["submitted"], ["Anna Muster"])
        self.assertEqual(states["in_review"], ["Bert Beispiel"])
        self.assertEqual(states["at_operator"], ["Carl Probe"])
        self.assertEqual(states["deregistration_submitted"], [])
        self.assertEqual(process_list.rows(kind="deregistration"), [])
        self.assertEqual(len(process_list.rows(kind="application")), 3)
        # An unknown value is the default.
        self.assertEqual(len(process_list.rows(kind="x", state="y", sort="z")), 3)
        # The row state keeps the exact word of the two states at the operator.
        self.assertEqual({row.state for row in process_list.rows(state="at_operator")}, {"operator_started"})

    def test_the_search_finds_the_name_the_number_the_address_and_the_egid_every_word_must_match(self):
        self.submitted(self.anna, 90004269, 1)
        self.submitted(self.bert, 90004270, 5, street="Bernstrasse", house="52")
        names = lambda text: sorted(row.person_name for row in process_list.rows(text=text))  # noqa: E731
        self.assertEqual(names("muster"), ["Anna Muster"])
        self.assertEqual(names("BEISPIEL"), ["Bert Beispiel"])
        self.assertEqual(names("Bernstrasse"), ["Bert Beispiel"])
        self.assertEqual(names("Bernstrasse 52"), ["Bert Beispiel"])
        self.assertEqual(names("Bernstrasse Muster"), [])
        self.assertEqual(names("90004269"), ["Anna Muster"])
        self.assertEqual(names("3063"), ["Anna Muster", "Bert Beispiel"])
        self.assertEqual(names(self.anna.customer_number_display), ["Anna Muster"])
        self.assertEqual(names("   "), ["Anna Muster", "Bert Beispiel"])
        # Never a note, an IBAN or the email address.
        self.assertEqual(names("anna.list@example.org"), [])

    def test_a_search_that_joins_several_buildings_lists_the_application_once(self):
        application, _building = ready_draft(self.anna, egid=90004269, number=1)
        applications.submit(self.anna, application, today=TODAY)
        self.assertEqual(len(process_list.rows(text="Ittigenstrasse 3063")), 1)

    def test_the_sorting_puts_empty_values_last_also_descending(self):
        first = self.submitted(self.anna, 90004269, 1)
        second = self.submitted(self.bert, 90004270, 5, house="52")
        Application.objects.filter(pk=first.pk).update(
            start_choice=Application.StartChoice.DATE, desired_start_date=date(2027, 3, 1)
        )
        Application.objects.filter(pk=second.pk).update(start_choice="", desired_start_date=None)
        for descending in (False, True):
            order = [row.person_name for row in process_list.rows(sort="date", descending=descending)]
            self.assertEqual(order, ["Anna Muster", "Bert Beispiel"], descending)
        by_name = [row.person_name for row in process_list.rows(sort="person", descending=True)]
        self.assertEqual(by_name, ["Bert Beispiel", "Anna Muster"])
        for key in process_list.SORT_KEYS:
            with self.subTest(key=key):
                self.assertEqual(len(process_list.rows(sort=key)), 2)

    def test_the_default_order_is_the_oldest_change_first(self):
        first = self.submitted(self.anna, 90004269, 1)
        second = self.submitted(self.bert, 90004270, 5, house="52")
        Application.objects.filter(pk=first.pk).update(updated_at=timezone.now())
        Application.objects.filter(pk=second.pk).update(updated_at=timezone.now() - timedelta(days=2))
        self.assertEqual([row.pk for row in process_list.rows()], [second.pk, first.pk])

    def test_each_query_is_bounded(self):
        for index in range(3):
            self.submitted(customer(f"many{index}.list@example.org"), 90004300 + index, 20 + index, house=str(60 + index))
        with patch.object(process_list, "MAX_ROWS", 2):
            self.assertEqual(len(process_list.rows()), 2)

    def test_a_deregistration_is_a_row_with_the_date_of_the_confirmation(self):
        today = timezone.localdate()
        place = make_location("Abmeldeweg", "4", "3000", "Bern", egid=88880001)
        point = make_metering_point(
            80, location=place, registered_with_operator_on=today - timedelta(days=40),
            confirmed_on=today - timedelta(days=39), start_date=today - timedelta(days=30),
        )
        assign_person(point, self.anna, today - timedelta(days=30))
        dereg = deregistrations.request_deregistration(self.anna, metering_point_ids=[point.pk], today=today)
        (row,) = process_list.rows()
        self.assertEqual((row.kind, row.state, row.address, row.egid, row.points, row.date),
                         ("deregistration", "deregistration_submitted", "Abmeldeweg 4, 3000 Bern", 88880001, 1, None))
        self.assertEqual((row.progress_done, row.progress_total), (1, 3))
        from leg_platform.community.application_common import month_end

        confirmed = month_end(today + timedelta(days=130))
        deregistrations.confirm(dereg, self.staff, confirmed, today=today)
        (row,) = process_list.rows()
        self.assertEqual((row.state, row.date, row.progress_done), ("deregistration_confirmed", confirmed, 2))
        self.assertEqual(process_list.rows(state="deregistration_submitted"), [])
        self.assertEqual(len(process_list.rows(state="deregistration_confirmed")), 1)
        self.assertEqual(process_list.rows(kind="application"), [])
        self.assertEqual(len(process_list.rows(kind="deregistration", text="Abmeldeweg")), 1)
        self.assertEqual(process_list.rows(kind="deregistration", text="Elsewhere"), [])
        # A finished deregistration is no longer running.
        deregistrations.finish_due(confirmed)
        self.assertEqual(process_list.rows(), [])


@tag("mariadb")
class ContractReadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("read.staff@example.org")
        cls.anna = customer("anna.read@example.org")

    def test_the_versions_carry_their_acceptance_count_and_no_file(self):
        first = contracts.upload_version(self.staff, pdf(b"\n% one"), date(2026, 1, 1))
        second = contracts.upload_version(self.staff, pdf(b"\n% two"), date(2026, 2, 1))
        contracts.make_current(self.staff, first)
        ContractAcceptance.objects.create(person=self.anna, version=first, sha256=first.sha256, data_exchange_accepted=True)
        rows = {version.pk: version for version in contracts.versions()}
        self.assertEqual(rows[first.pk].acceptance_count, 1)
        self.assertEqual(rows[second.pk].acceptance_count, 0)
        self.assertIn("pdf", rows[first.pk].get_deferred_fields())

    def test_the_history_of_the_versions_names_the_file_also_of_a_deleted_one_and_holds_no_content(self):
        one = contracts.upload_version(self.staff, pdf(b"\n% one"), date(2026, 1, 1))
        two = contracts.upload_version(self.staff, pdf(b"\n% two"), date(2026, 2, 1))
        contracts.make_current(self.staff, one)
        contracts.delete_version(self.staff, two)
        rows = contracts.history_rows(self.staff)
        pairs = [(entry.event, name) for entry, name in rows]
        self.assertEqual(
            pairs,
            [
                (HistoryEvent.CONTRACT_DELETED, "contract-2026-02-01.pdf"),
                (HistoryEvent.CONTRACT_MADE_CURRENT, "contract-2026-01-01.pdf"),
                (HistoryEvent.CONTRACT_UPLOADED, "contract-2026-02-01.pdf"),
                (HistoryEvent.CONTRACT_UPLOADED, "contract-2026-01-01.pdf"),
            ],
        )
        for entry, _name in rows:
            self.assertNotIn("pdf", str(entry.detail).lower().replace("version_date", ""))
        self.assertEqual(len(contracts.history_rows(self.staff, limit=2)), 2)

    def test_the_history_and_the_acceptance_are_for_the_staff_only(self):
        for call in (lambda actor: contracts.history_rows(actor),):
            for actor in (self.anna, None):
                with self.assertRaises(ApplicationError):
                    call(actor)

    def test_the_version_with_the_same_bytes_is_found_for_the_staff_only(self):
        data = pdf(b"\n% same")
        stored = contracts.upload_version(self.staff, data, date(2026, 3, 1))
        found = contracts.stored_version_of(self.staff, data)
        self.assertEqual(found.pk, stored.pk)
        self.assertIn("pdf", found.get_deferred_fields())
        self.assertIsNone(contracts.stored_version_of(self.staff, pdf(b"\n% other")))
        for actor in (self.anna, None):
            with self.assertRaises(ApplicationError):
                contracts.stored_version_of(actor, data)

    def test_the_newest_acceptance_of_an_account_without_the_file(self):
        self.assertIsNone(contracts.latest_acceptance(self.anna))
        first = contracts.upload_version(self.staff, pdf(b"\n% one"), date(2026, 1, 1))
        ContractAcceptance.objects.create(person=self.anna, version=first, sha256=first.sha256, data_exchange_accepted=True)
        found = contracts.latest_acceptance(self.anna)
        self.assertEqual((found["version_date"], found["data_exchange_accepted"]), (date(2026, 1, 1), True))
        self.assertEqual(set(found), {"accepted_at", "data_exchange_accepted", "version_date"})


@tag("mariadb")
class AddressRunTimeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("run.staff@example.org")
        cls.plain = create_active_user("run.plain@example.org")

    def test_the_time_is_stored_in_whole_minutes_with_a_history_entry_without_a_value(self):
        self.assertEqual(deadline_settings.address_run_time(), time(3, 0))
        self.assertTrue(deadline_settings.set_address_run_time(time(4, 30, 59, 7), self.staff))
        self.assertEqual(schedule.import_run_time(), time(4, 30))
        entry = history.entries_for(ManagementSettings.load()).get(event=HistoryEvent.IMPORT_RUN_TIME_CHANGED)
        self.assertEqual((entry.actor, entry.detail), (self.staff, {}))

    def test_the_same_time_again_changes_nothing(self):
        deadline_settings.set_address_run_time(time(4, 30), self.staff)
        self.assertFalse(deadline_settings.set_address_run_time(time(4, 30, 20), self.staff))
        self.assertEqual(history.entries_for(ManagementSettings.load()).filter(
            event=HistoryEvent.IMPORT_RUN_TIME_CHANGED).count(), 1)

    def test_only_a_time_of_day_and_only_staff(self):
        for bad in ("04:30", None, 4, 4.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                deadline_settings.set_address_run_time(bad, self.staff)
        for actor in (self.plain, None):
            with self.subTest(actor=actor), self.assertRaises(ApplicationError):
                deadline_settings.set_address_run_time(time(5, 0), actor)
        self.assertEqual(schedule.import_run_time(), time(3, 0))
