"""The log menu "Protokolle" and its e-mail notice (D-120): superusers only, redacted downloads, notices only when enabled
and at or above the severity, no log line and no secret in the mail, one mail per source and hour, validated address,
one security event per change of the setting.
"""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from leg_platform import log_menu
from leg_platform.accounts import mail_queue
from leg_platform.accounts.models import LogNotificationSettings, MailJob, SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses.models import HousekeepingError, ImportLogEntry, ImportRequest

ZURICH = ZoneInfo("Europe/Zurich")
PLANTED_EMAIL = "planted.person@example.org"
PLANTED_IBAN = "CH9300762011623852957"
PLANTED_TOKEN = "tok_PLANTEDSECRET123"
SENDER = "leg_platform.accounts.emails._deliver"


class LogMenuBase(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.superuser = create_active_user("log.superuser@example.org")
        self.superuser.set_privileges(is_staff=True, is_superuser=True)
        self.staff = create_active_user("log.staff@example.org")
        self.staff.set_privileges(is_staff=True, is_superuser=False)
        self.staff.user_permissions.add(Permission.objects.get(codename="run_housekeeping"))

    def client_for(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client

    def housekeeping_error(self, dump_name="housekeeping-20261025T0420Z.sql.gz", **fields):
        return HousekeepingError.objects.create(
            step=HousekeepingError.Step.SQLITE, reason="sqlite_vacuum_failed", dump_file_name=dump_name, **fields,
        )

    def set_notification(self, **values):
        LogNotificationSettings.objects.update_or_create(pk=1, defaults=values)


class AccessTests(LogMenuBase):
    def test_superuser_sees_the_page_and_its_sources(self):
        self.housekeeping_error()
        response = self.client_for(self.superuser).get(reverse("admin:logmenu"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="logmenu-security"')
        self.assertContains(response, 'id="logmenu-housekeeping"')
        self.assertContains(response, reverse("admin:logs_view", kwargs={"log": "security"}))

    def test_staff_with_the_housekeeping_right_is_refused_everywhere(self):
        client = self.client_for(self.staff)
        self.assertEqual(client.get(reverse("admin:logmenu")).status_code, 403)
        self.assertEqual(client.get(reverse("admin:logmenu_download", kwargs={"source": "security"})).status_code, 403)
        self.assertEqual(client.get(reverse("admin:logmenu_download", kwargs={"source": "housekeeping"})).status_code, 403)
        self.assertEqual(client.post(reverse("admin:logmenu_settings"), {"enabled": "on", "email": "a@example.org"}).status_code, 403)
        self.assertFalse(LogNotificationSettings.objects.filter(pk=1, enabled=True).exists())

    def test_a_download_is_refused_for_staff_and_redacted_for_the_superuser(self):
        self.housekeeping_error(dump_name=f"{PLANTED_EMAIL} {PLANTED_IBAN} {PLANTED_TOKEN}.sql.gz")
        url = reverse("admin:logmenu_download", kwargs={"source": "housekeeping"})
        self.assertEqual(self.client_for(self.staff).get(url).status_code, 403)
        response = self.client_for(self.superuser).get(url)
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertNotIn(PLANTED_EMAIL, body)
        self.assertNotIn(PLANTED_IBAN, body)
        self.assertIn("<email>", body)
        self.assertIn("<iban>", body)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("attachment", response["Content-Disposition"])

    def test_unknown_database_source_is_not_found(self):
        response = self.client_for(self.superuser).get(reverse("admin:logmenu_download", kwargs={"source": "mail"}))
        self.assertEqual(response.status_code, 404)

    def test_the_range_and_the_switches_are_read_from_the_query(self):
        self.housekeeping_error()
        response = self.client_for(self.superuser).get(reverse("admin:logmenu"), {"show": "housekeeping", "since": "1"})
        self.assertNotContains(response, 'id="logmenu-security"')
        self.assertContains(response, 'id="logmenu-housekeeping"')


class SettingTests(LogMenuBase):
    def test_enabling_without_an_address_is_refused(self):
        response = self.client_for(self.superuser).post(reverse("admin:logmenu_settings"), {
            "enabled": "on", "email": "",
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LogNotificationSettings.objects.filter(pk=1, enabled=True).exists())

    def test_an_invalid_address_is_refused(self):
        response = self.client_for(self.superuser).post(reverse("admin:logmenu_settings"), {
            "enabled": "on", "email": "not-an-address",
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LogNotificationSettings.objects.filter(pk=1, enabled=True).exists())

    def test_a_change_is_one_security_event_without_the_address(self):
        response = self.client_for(self.superuser).post(reverse("admin:logmenu_settings"), {
            "enabled": "on", "email": "notice.target@example.org",
        })
        self.assertEqual(response.status_code, 302)
        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.LOG_NOTIFICATION_CHANGED)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().actor_id, self.superuser.pk)
        self.assertNotIn("notice.target", str(events.get().__dict__))

    def test_the_default_is_off_and_errors_are_ticked(self):
        setting = LogNotificationSettings.objects.create(pk=1)
        self.assertFalse(setting.enabled)
        self.assertTrue(setting.notify_error)
        self.assertFalse(setting.notify_stale)


class NotificationTests(LogMenuBase):
    def run_notifier(self, now=None):
        return log_menu.notify_log_events(now or timezone.now())

    def test_no_mail_when_the_switch_is_off(self):
        self.set_notification(enabled=False, email="notice.target@example.org")
        self.housekeeping_error()
        self.assertEqual(self.run_notifier(), 0)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.LOG_NOTIFICATION).exists())


    def test_a_warning_event_mails_when_the_minimum_is_warning(self):
        self.set_notification(enabled=True, email="notice.target@example.org",
                              notify_warning=True)
        SecurityEvent.objects.create(event=SecurityEvent.Event.LOGIN_LOCKOUT)
        self.assertEqual(self.run_notifier(), 1)
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.LOG_NOTIFICATION).exists())

    def test_the_mail_holds_no_log_line_no_file_name_and_no_secret(self):
        self.set_notification(enabled=True, email="notice.target@example.org")
        self.housekeeping_error(dump_name=f"housekeeping-20261025T0420Z.sql.gz {PLANTED_EMAIL} {PLANTED_IBAN}")
        self.run_notifier()
        with patch(SENDER) as deliver:
            done, failed = mail_queue.process_pending()
        self.assertEqual((done, failed), (1, 0))
        address, subject, body = deliver.call_args.args
        self.assertEqual(address, "notice.target@example.org")
        self.assertIn("Ein Eintrag mit hohem Schweregrad ist aufgetreten", body)
        self.assertIn("| error |", body)
        for secret in (PLANTED_EMAIL, PLANTED_IBAN, "housekeeping-20261025T0420Z", "sqlite_vacuum_failed"):
            self.assertNotIn(secret, body)

    def test_one_mail_per_source_and_hour_the_rest_is_counted(self):
        self.set_notification(enabled=True, email="notice.target@example.org")
        now = timezone.now()
        self.housekeeping_error()
        self.assertEqual(self.run_notifier(now), 1)
        self.housekeeping_error()
        self.assertEqual(self.run_notifier(now + timedelta(minutes=10)), 0)
        self.assertEqual(log_menu.suppressed_counts(), {"housekeeping": 1})
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.LOG_NOTIFICATION).count(), 1)
        self.housekeeping_error()
        self.assertEqual(self.run_notifier(now + timedelta(hours=1, minutes=1)), 1)

    def test_events_before_the_switch_was_turned_on_are_not_mailed(self):
        self.set_notification(enabled=False, email="notice.target@example.org")
        self.housekeeping_error()
        self.run_notifier()
        self.set_notification(enabled=True)
        self.assertEqual(self.run_notifier(), 0)

    def test_the_mail_queue_handler_without_a_notice_makes_no_mail(self):
        self.assertIsNone(mail_queue.HANDLERS[MailJob.Kind.LOG_NOTIFICATION]("notice.target@example.org"))


class TickTests(LogMenuBase):
    """Each tick sends exactly its own event kind (D-122); the defaults send errors and nothing else."""

    MISSING = "Missing EGID column."
    UNREACHABLE = "The map server could not be reached."
    UNCHANGED = "The downloaded file is unchanged since the last run."

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.configure()

    def configure(self, **ticks):
        values = {
            "enabled": True, "email": "notice.target@example.org", "notify_error": True, "notify_warning": False,
            "notify_notice": False, "notify_stale": False, "notify_unchanged": False, "muted_failures": [],
        }
        values.update(ticks)
        LogNotificationSettings.objects.update_or_create(pk=1, defaults=values)
        # Only the rows created after this point are new: the marks of the notifier start at the last existing rows.
        marks = {
            "security": SecurityEvent.objects.aggregate(m=Max("pk"))["m"] or 0,
            "housekeeping": HousekeepingError.objects.aggregate(m=Max("pk"))["m"] or 0,
            "import": ImportLogEntry.objects.aggregate(m=Max("pk"))["m"] or 0,
        }
        LogNotificationSettings.objects.filter(pk=1).update(state={"checked": marks})
        ImportLogEntry.objects.filter(pk__gt=0).delete()
        HousekeepingError.objects.filter(pk__gt=0).delete()

    def import_entry(self, reason, kind="error"):
        ImportLogEntry.objects.create(
            source=ImportRequest.Source.ADDRESS, kind=kind, step="", reason=reason, occurred_at=self.now,
            count=1 if kind == "unchanged" else None,
        )

    def notify(self):
        return log_menu.notify_log_events(self.now)

    def test_the_defaults_send_an_error_and_nothing_for_an_unchanged_file(self):
        self.configure()
        self.import_entry(self.UNCHANGED, kind="unchanged")
        self.assertEqual(self.notify(), 0)
        self.import_entry(self.MISSING)
        self.assertEqual(self.notify(), 1)

    def test_the_unchanged_tick_alone_sends_nothing_without_the_notice_tick(self):
        self.configure(notify_error=False, notify_unchanged=True, notify_notice=False)
        self.import_entry(self.UNCHANGED, kind="unchanged")
        self.assertEqual(self.notify(), 0)
        self.configure(notify_error=False, notify_unchanged=True, notify_notice=True)
        self.import_entry(self.UNCHANGED, kind="unchanged")
        self.assertEqual(self.notify(), 1)

    def test_the_stale_tick_alone_sends_the_old_data_error(self):
        stale_day = (self.now - timedelta(days=40)).date()
        with patch.object(log_menu, "directory_changed_on", return_value=stale_day):
            self.configure(notify_error=False, notify_stale=False)
            self.assertEqual(self.notify(), 0)
            self.assertTrue(ImportLogEntry.objects.filter(kind="stale").exists())
            self.configure(notify_error=False, notify_stale=True)
            ImportLogEntry.objects.create(source=ImportRequest.Source.ADDRESS, kind="stale", step="",
                                          reason=log_menu.STALE_REASON, occurred_at=self.now)
            self.assertEqual(self.notify(), 1)

    def test_the_warning_tick_alone_sends_the_lockout(self):
        self.configure(notify_error=False, notify_warning=False)
        SecurityEvent.objects.create(event=SecurityEvent.Event.LOGIN_LOCKOUT)
        self.assertEqual(self.notify(), 0)
        self.configure(notify_error=False, notify_warning=True)
        SecurityEvent.objects.create(event=SecurityEvent.Event.LOGIN_LOCKOUT)
        self.assertEqual(self.notify(), 1)

    def test_the_error_tick_alone_sends_housekeeping_errors(self):
        self.configure(notify_error=True)
        HousekeepingError.objects.create(step=HousekeepingError.Step.SQLITE, reason="sqlite_vacuum_failed")
        self.assertEqual(self.notify(), 1)
        self.configure(notify_error=False)
        HousekeepingError.objects.create(step=HousekeepingError.Step.SQLITE, reason="sqlite_vacuum_failed")
        self.assertEqual(self.notify(), 0)

    def test_one_ticked_failure_text_sends_only_that_text(self):
        muted = [text for text in log_menu.failure_texts() if text != self.MISSING]
        self.configure(muted_failures=muted)
        self.import_entry(self.MISSING)
        self.import_entry(self.UNREACHABLE)
        self.assertEqual(self.notify(), 1)

    def test_an_unticked_failure_text_sends_nothing(self):
        self.configure(muted_failures=log_menu.failure_texts())
        self.import_entry(self.MISSING)
        self.import_entry(self.UNREACHABLE)
        self.assertEqual(self.notify(), 0)

    def test_the_failure_texts_are_the_fixed_texts_of_the_import(self):
        texts = log_menu.failure_texts()
        self.assertIn(self.MISSING, texts)
        self.assertIn(self.UNREACHABLE, texts)
        self.assertEqual(len(texts), len(set(texts)))


class MenuEntryTests(LogMenuBase):
    """The sidebar entries of the log pages: superusers only, in the group System (D-120, D-121)."""

    def test_a_superuser_sees_both_entries_with_their_urls(self):
        response = self.client_for(self.superuser).get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin:logmenu"))
        self.assertNotContains(response, reverse("admin:addresses_importrequest_log"))

    def test_staff_with_the_housekeeping_right_does_not_see_them(self):
        response = self.client_for(self.staff).get(reverse("admin:index"))
        self.assertNotContains(response, reverse("admin:logmenu"))
        self.assertNotContains(response, reverse("admin:addresses_importrequest_log"))


class LogTextBoxTests(LogMenuBase):
    """The logs are text in a box, not a table (owner decision): the chosen number of lines limits the output."""

    @staticmethod
    def section(html, start, end="</section>"):
        begin = html.index(start)
        return html[begin:html.index(end, begin)]

    def test_the_security_box_shows_the_chosen_number_of_lines_and_no_table(self):
        for _ in range(60):
            SecurityEvent.objects.create(event=SecurityEvent.Event.LOG_VIEWED)
        response = self.client_for(self.superuser).get(reverse("admin:logmenu"), {"show": "security", "lines": "50"})
        html = response.content.decode("utf-8")
        box = self.section(html, 'id="logmenu-security-box"', "</pre>")
        self.assertEqual(box.count('class="log-line"'), 50)
        self.assertNotIn("<table", self.section(html, 'id="logmenu-security"'))

    def test_the_default_is_200_lines_and_the_box_holds_the_text_of_the_events(self):
        SecurityEvent.objects.create(event=SecurityEvent.Event.LOG_VIEWED)
        response = self.client_for(self.superuser).get(reverse("admin:logmenu"), {"show": "security"})
        html = response.content.decode("utf-8")
        box = self.section(html, 'id="logmenu-security-box"', "</pre>")
        self.assertGreaterEqual(box.count('class="log-line"'), 1)
        self.assertIn('value="200" selected', html)

    def test_the_failed_login_log_is_a_text_box_with_the_chosen_lines(self):
        from axes.models import AccessFailureLog

        for index in range(60):
            AccessFailureLog.objects.create(username=f"code{index}", path_info="/accounts/login/", locked_out=False)
        response = self.client_for(self.superuser).get(reverse("admin:failed_logins"), {"lines": "50"})
        html = response.content.decode("utf-8")
        box = self.section(html, 'id="failed-logins-log-box"', "</pre>")
        self.assertEqual(box.count('class="log-line"'), 50)
        self.assertNotIn("<table", self.section(html, 'id="failed-logins-log"'))
