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
from django.urls import reverse
from django.utils import timezone

from leg_platform import log_menu
from leg_platform.accounts import mail_queue
from leg_platform.accounts.models import LogNotificationSettings, MailJob, SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses.models import HousekeepingError

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
            "enabled": "on", "email": "", "minimum_severity": "error",
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LogNotificationSettings.objects.filter(pk=1, enabled=True).exists())

    def test_an_invalid_address_is_refused(self):
        response = self.client_for(self.superuser).post(reverse("admin:logmenu_settings"), {
            "enabled": "on", "email": "not-an-address", "minimum_severity": "error",
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LogNotificationSettings.objects.filter(pk=1, enabled=True).exists())

    def test_a_change_is_one_security_event_without_the_address(self):
        response = self.client_for(self.superuser).post(reverse("admin:logmenu_settings"), {
            "enabled": "on", "email": "notice.target@example.org", "minimum_severity": "critical",
        })
        self.assertEqual(response.status_code, 302)
        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.LOG_NOTIFICATION_CHANGED)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().actor_id, self.superuser.pk)
        self.assertNotIn("notice.target", str(events.get().__dict__))

    def test_the_default_is_off_with_severity_error(self):
        setting = LogNotificationSettings.objects.create(pk=1)
        self.assertFalse(setting.enabled)
        self.assertEqual(setting.minimum_severity, LogNotificationSettings.Severity.ERROR)


class NotificationTests(LogMenuBase):
    def run_notifier(self, now=None):
        return log_menu.notify_log_events(now or timezone.now())

    def test_no_mail_when_the_switch_is_off(self):
        self.set_notification(enabled=False, email="notice.target@example.org")
        self.housekeeping_error()
        self.assertEqual(self.run_notifier(), 0)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.LOG_NOTIFICATION).exists())

    def test_a_mail_only_at_or_above_the_severity(self):
        self.set_notification(enabled=True, email="notice.target@example.org",
                              minimum_severity=LogNotificationSettings.Severity.CRITICAL)
        self.housekeeping_error()  # an error is below critical
        self.assertEqual(self.run_notifier(), 0)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.LOG_NOTIFICATION).exists())

    def test_a_warning_event_mails_when_the_minimum_is_warning(self):
        self.set_notification(enabled=True, email="notice.target@example.org",
                              minimum_severity=LogNotificationSettings.Severity.WARNING)
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
