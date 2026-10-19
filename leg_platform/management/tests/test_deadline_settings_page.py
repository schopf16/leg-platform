"""The chapter "Reminders and deadlines" of the settings page (KONZEPT 5.8, D-103): what it shows, the whole-number rule,
the warning with the number of drafts a change would delete and the second step, the history without values, the time of
the daily address directory run, and the closed doors.
"""

from datetime import time, timedelta

from django.contrib.auth.models import Permission
from django.test import Client, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses import schedule
from leg_platform.community import deadline_settings, history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Application, ManagementSettings
from leg_platform.community.tests.helpers import staff_user
from leg_platform.community.tests.onboarding import configure_numbers, customer, publish_contract, ready_draft

SETTINGS = reverse("management:settings")
URL = reverse("management:settings_deadlines")
FIELDS = deadline_settings.FIELDS
VALID = {
    "reminder_after_days": "3", "rollback_after_days": "15", "contract_deadline_days": "16",
    "operator_confirmation_days": "17", "iban_missing_reminder_days": "8", "run_time": "04:30",
}


@tag("mariadb")
class DeadlinesBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        configure_numbers()
        cls.staff = staff_user("deadline.staff@example.org")
        cls.plain = create_active_user("deadline.plain@example.org")
        cls.anna = customer("anna.deadline@example.org", first_name="Zora", last_name="Quelle")
        publish_contract(cls.staff)

    def setUp(self):
        self.client = self.login(self.staff)

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def values(self):
        return {name: str(value) for name, value in deadline_settings.current_values().items()}

    def paused_draft(self, person=None, reminded_days_ago=10, **kwargs):
        """A paused draft whose reminder was sent that many days ago (it would be rolled back by a shorter rollback
        period than that)."""
        application, _building = ready_draft(person or self.anna, accept=False, start=False, **kwargs)
        Application.objects.filter(pk=application.pk).update(
            state=Application.State.PAUSED, reminded_at=timezone.now() - timedelta(days=reminded_days_ago)
        )
        return application


@tag("mariadb")
class ChapterTests(DeadlinesBase):
    def test_the_chapter_lists_every_deadline_with_name_trigger_days_recipient_mail_text_and_effect(self):
        page = self.client.get(SETTINGS)
        for text in (
            "Erinnerungen und Fristen", "Anmeldung nicht abgeschlossen", "Anmeldung rückabgewickelt",
            "Vertrag nicht akzeptiert", "Bestätigung durch Netzbetreiber", "Bankverbindung fehlt",
            "Auslöser", "Empfänger", "Mailtext", "Wirkung", "Haupt- und Zweitadresse des Kunden",
            "Jedes aktive Mitarbeiterkonto", "Die letzte Änderung eines Entwurfs durch den Kunden",
            "Anmeldung: Erinnerung, noch nicht abgeschlossen", "Anmeldung: rückabgewickelt",
            "Personal: Bestätigung des Netzbetreibers fehlt, Anmeldung zurückziehen",
        ):
            self.assertContains(page, text)
        for name, value in deadline_settings.current_values().items():
            self.assertContains(page, f'name="{name}" value="{value}"')
        self.assertContains(page, 'action="/management/settings/deadlines/"')

    def test_the_mail_texts_are_a_link_only_for_an_account_that_may_open_them(self):
        self.assertNotContains(self.client.get(SETTINGS), 'href="/admin/accounts/emailtext/"')
        editor = staff_user("deadline.mail@example.org")
        editor.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="change_emailtext"))
        editor = type(editor).objects.get(pk=editor.pk)
        self.assertContains(self.login(editor).get(SETTINGS), 'href="/admin/accounts/emailtext/"')

    def test_the_time_of_the_daily_run_is_shown_and_defaults_to_the_one_of_the_schedule(self):
        page = self.client.get(SETTINGS)
        self.assertContains(page, "Uhrzeit des täglichen Laufs")
        self.assertContains(page, 'name="run_time" value="03:00"')
        self.assertEqual(schedule.import_run_time(), time(3, 0))

    def test_every_staff_account_sees_and_may_save_the_chapter_and_a_get_leads_back(self):
        self.assertRedirects(self.client.get(URL), SETTINGS, fetch_redirect_response=False)
        self.assertEqual(self.login(self.plain).get(SETTINGS).status_code, 404)


@tag("mariadb")
class SaveTests(DeadlinesBase):
    def test_a_valid_set_is_saved_with_a_history_of_field_names_and_the_run_time(self):
        response = self.client.post(URL, VALID)
        self.assertRedirects(response, SETTINGS, fetch_redirect_response=False)
        self.assertEqual(deadline_settings.current_values(), {
            "reminder_after_days": 3, "rollback_after_days": 15, "contract_deadline_days": 16,
            "operator_confirmation_days": 17, "iban_missing_reminder_days": 8,
        })
        self.assertEqual(schedule.import_run_time(), time(4, 30))
        row = ManagementSettings.load()
        events = {entry.event: entry for entry in history.entries_for(row)}
        self.assertEqual(sorted(events[HistoryEvent.DEADLINES_CHANGED].detail["fields"]), sorted(FIELDS))
        self.assertEqual(events[HistoryEvent.DEADLINES_CHANGED].actor, self.staff)
        self.assertEqual(events[HistoryEvent.IMPORT_RUN_TIME_CHANGED].detail, {})
        # Never a value in the history.
        for entry in events.values():
            for value in VALID.values():
                self.assertNotIn(f'"{value}"', str(entry.detail))
        self.assertContains(self.client.get(SETTINGS), "Gespeichert.")

    def test_saving_the_same_values_writes_no_history(self):
        data = {**self.values(), "run_time": "03:00"}
        before = history.entries_for(ManagementSettings.load()).count()
        self.assertEqual(self.client.post(URL, data).status_code, 302)
        self.assertEqual(history.entries_for(ManagementSettings.load()).count(), before)

    def test_only_the_changed_fields_are_named(self):
        self.client.post(URL, {**self.values(), "run_time": "03:00", "reminder_after_days": "5"})
        entry = history.entries_for(ManagementSettings.load()).filter(event=HistoryEvent.DEADLINES_CHANGED).get()
        self.assertEqual(entry.detail, {"fields": ["reminder_after_days"]})

    def test_a_sentence_of_the_other_sections_is_not_touched(self):
        before = ManagementSettings.load()
        self.client.post(URL, VALID)
        after = ManagementSettings.load()
        for name in ("country_code", "identifier", "warning_threshold_percent", "sender_street"):
            self.assertEqual(getattr(before, name), getattr(after, name))

    def test_the_run_time_must_be_a_time_of_day(self):
        for bad in ("", "25:00", "noon", "03:61"):
            with self.subTest(bad=bad):
                response = self.client.post(URL, {**VALID, "run_time": bad})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Uhrzeit")
        self.assertEqual(schedule.import_run_time(), time(3, 0))
        self.assertEqual(self.values(), {"reminder_after_days": "2", "rollback_after_days": "14",
                                         "contract_deadline_days": "14", "operator_confirmation_days": "14",
                                         "iban_missing_reminder_days": "7"})


@tag("mariadb")
class WholeNumberTests(DeadlinesBase):
    MESSAGE = "Geben Sie eine ganze Zahl von Tagen ein, mindestens 1."

    def test_only_whole_numbers_from_one_are_accepted(self):
        for bad in ("0", "-3", "1.5", "1,5", "abc", "", "  ", "3 days", "+4", "1e3", "٣", "１２", "0x10", "١٢"):
            with self.subTest(bad=bad):
                response = self.client.post(URL, {**VALID, "reminder_after_days": bad})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, self.MESSAGE)
                self.assertEqual(deadline_settings.current_values()["reminder_after_days"], 2)

    def test_the_message_stands_at_the_field_and_in_the_summary_and_nothing_is_saved(self):
        response = self.client.post(URL, {**VALID, "rollback_after_days": "x", "contract_deadline_days": "0"})
        self.assertContains(response, "Bitte prüfen Sie Ihre Angaben")
        self.assertContains(response, 'class="mgmt-field-error"', count=2)
        self.assertEqual(deadline_settings.current_values()["reminder_after_days"], 2)
        self.assertEqual(schedule.import_run_time(), time(3, 0))
        # The entries the person typed stay in the fields.
        self.assertContains(response, 'value="x"')

    def test_a_huge_number_is_a_message_and_never_an_error(self):
        for huge in ("4294967296", "99999999999", "9" * 5000):
            with self.subTest(length=len(huge)):
                response = self.client.post(URL, {**VALID, "reminder_after_days": huge})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "class=\"mgmt-field-error\"")
        # The largest value the column holds is accepted (no limit of our own, owner).
        self.assertEqual(self.client.post(URL, {**VALID, "reminder_after_days": "4294967295"}).status_code, 302)
        self.assertEqual(deadline_settings.current_values()["reminder_after_days"], 4294967295)

    def test_a_field_that_is_missing_from_the_post_is_a_message(self):
        data = dict(VALID)
        del data["iban_missing_reminder_days"]
        self.assertContains(self.client.post(URL, data), self.MESSAGE)


@tag("mariadb")
class DraftWarningTests(DeadlinesBase):
    def question(self, **changes):
        return self.client.post(URL, {**self.values(), "run_time": "03:00", **changes})

    def test_a_shorter_value_that_would_delete_drafts_shows_the_number_and_saves_nothing(self):
        self.paused_draft()
        response = self.question(rollback_after_days="5")
        self.assertContains(response, "Diese Änderung löscht 1 Entwurf.")
        self.assertContains(response, 'name="confirmed_count" value="1:0"')
        self.assertEqual(deadline_settings.current_values()["rollback_after_days"], 14)
        # A number only: no name and no address of the draft.
        for fragment in ("Zora", "Quelle", "Ittigenstrasse", "anna.deadline"):
            self.assertNotContains(response, fragment)

    def test_the_number_equals_the_service_and_the_plural_is_used(self):
        self.paused_draft(reminded_days_ago=10)
        self.paused_draft(customer("bert.deadline@example.org"), egid=90004270, number=5, address={"house_number": "52"})
        response = self.question(rollback_after_days="5")
        self.assertEqual(deadline_settings.affected_drafts_count({"rollback_after_days": 5}), 2)
        self.assertContains(response, "Diese Änderung löscht 2 Entwürfe.")

    def test_the_second_step_with_the_same_number_saves(self):
        self.paused_draft()
        response = self.question(rollback_after_days="5", confirmed_count="1:0")
        self.assertRedirects(response, SETTINGS, fetch_redirect_response=False)
        self.assertEqual(deadline_settings.current_values()["rollback_after_days"], 5)
        entry = history.entries_for(ManagementSettings.load()).filter(event=HistoryEvent.DEADLINES_CHANGED).get()
        self.assertEqual(entry.detail, {"fields": ["rollback_after_days"]})

    def test_a_number_that_changed_between_the_steps_asks_again_with_the_new_number(self):
        self.paused_draft()
        self.paused_draft(customer("bert.deadline@example.org"), egid=90004270, number=5, address={"house_number": "52"})
        response = self.question(rollback_after_days="5", confirmed_count="1:0")
        self.assertContains(response, "Diese Änderung löscht 2 Entwürfe.")
        self.assertContains(response, 'name="confirmed_count" value="2:0"')
        self.assertEqual(deadline_settings.current_values()["rollback_after_days"], 14)

    def test_a_forged_or_missing_confirmation_does_not_save(self):
        self.paused_draft()
        for forged in ("", "0", "2", "x", "1 ", "-1", "1", "0:0", "1:1", "1:0:0", "2:0"):
            with self.subTest(forged=forged):
                response = self.question(rollback_after_days="5", confirmed_count=forged)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Diese Änderung löscht 1 Entwurf.")
                self.assertEqual(deadline_settings.current_values()["rollback_after_days"], 14)

    def at_operator(self, days_ago=10):
        application, _building = ready_draft(
            customer("operator.deadline@example.org"), accept=True, start=True, egid=90004280, number=7,
            address={"house_number": "60"},
        )
        Application.objects.filter(pk=application.pk).update(
            state=Application.State.OPERATOR_STARTED, operator_started_at=timezone.now() - timedelta(days=days_ago),
        )
        return application

    def test_an_application_at_the_grid_operator_is_a_second_number_in_the_warning(self):
        self.at_operator()
        response = self.question(operator_confirmation_days="5")
        self.assertContains(response, "1 Anmeldung, die schon beim Netzbetreiber ist")
        self.assertNotContains(response, "löscht")
        self.assertContains(response, 'name="confirmed_count" value="0:1"')
        self.assertEqual(deadline_settings.current_values()["operator_confirmation_days"], 14)
        for fragment in ("Zora", "Quelle", "Ittigenstrasse", "anna.deadline"):
            self.assertNotContains(response, fragment)
        self.assertRedirects(
            self.question(operator_confirmation_days="5", confirmed_count="0:1"), SETTINGS,
            fetch_redirect_response=False,
        )
        self.assertEqual(deadline_settings.current_values()["operator_confirmation_days"], 5)

    def test_drafts_and_applications_at_the_grid_operator_show_both_numbers(self):
        self.paused_draft()
        self.at_operator()
        response = self.question(rollback_after_days="5", operator_confirmation_days="5")
        self.assertContains(response, "Diese Änderung löscht 1 Entwurf.")
        self.assertContains(response, "1 Anmeldung, die schon beim Netzbetreiber ist")
        self.assertContains(response, 'name="confirmed_count" value="1:1"')

    def test_a_change_that_deletes_nothing_is_saved_at_once(self):
        self.paused_draft(reminded_days_ago=1)
        self.assertEqual(self.question(rollback_after_days="5").status_code, 302)
        self.assertEqual(deadline_settings.current_values()["rollback_after_days"], 5)
        # A longer value never deletes anything.
        self.assertEqual(self.question(rollback_after_days="30").status_code, 302)

    def test_the_contract_deadline_is_counted_too(self):
        application = self.paused_draft(reminded_days_ago=1)
        Application.objects.filter(pk=application.pk).update(
            contract_pending_since=timezone.now() - timedelta(days=10)
        )
        response = self.question(contract_deadline_days="5")
        self.assertContains(response, "Diese Änderung löscht 1 Entwurf.")

    def test_the_question_is_a_form_with_the_token_and_a_way_back(self):
        self.paused_draft()
        page = self.question(rollback_after_days="5")
        self.assertContains(page, "csrfmiddlewaretoken")
        self.assertContains(page, "Änderung speichern")
        self.assertContains(page, f'href="{SETTINGS}"')


@tag("mariadb")
class ChapterAccessTests(DeadlinesBase):
    def test_a_customer_an_anonymous_visitor_and_a_staff_account_without_the_second_factor_cannot_save(self):
        before = deadline_settings.current_values()
        self.assertEqual(self.login(self.plain).post(URL, VALID).status_code, 404)
        self.assertEqual(Client().post(URL, VALID).status_code, 302)
        late = Client()
        late.force_login(staff_user("deadline.nosecond@example.org"))
        self.assertRedirects(late.post(URL, VALID), reverse("accounts:two_factor_setup"), fetch_redirect_response=False)
        self.assertEqual(deadline_settings.current_values(), before)

    def test_the_token_is_needed_and_other_methods_are_refused(self):
        client = self.login(self.staff, enforce_csrf_checks=True)
        self.assertEqual(client.post(URL, VALID).status_code, 403)
        for method in ("put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(URL).status_code, 405)
        self.assertEqual(schedule.import_run_time(), time(3, 0))

    def test_the_service_refuses_an_account_that_is_no_staff_account(self):
        from leg_platform.community.application_common import ApplicationError

        with self.assertRaises(ApplicationError):
            deadline_settings.set_address_run_time(time(5, 0), self.plain)
        with self.assertRaises(ValueError):
            deadline_settings.set_address_run_time("05:00", self.staff)
        self.assertEqual(schedule.import_run_time(), time(3, 0))
