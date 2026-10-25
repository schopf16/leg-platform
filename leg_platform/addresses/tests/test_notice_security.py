"""The security review fixes of the notice (D-122 amendment): retry after a failed send, one proof for the switch or the
address, CSRF on the POSTs, switching on starts from now, rows that commit late, a single stale row, and the retention
of the import log."""

from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leg_platform import log_menu
from leg_platform.accounts import mail_queue
from leg_platform.accounts.models import LogNotificationSettings, MailJob, SecurityEvent
from leg_platform.accounts.tests.helpers import PASSWORD, VerifiedClient, create_active_user
from leg_platform.addresses.models import HousekeepingError, ImportLogEntry, ImportRequest

ZURICH = ZoneInfo("Europe/Zurich")
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=ZURICH)
TARGET = "security.target@example.org"
REASON = "Missing EGID column."
DELIVER = "leg_platform.accounts.emails._deliver"


def app_proof(user):
    """The proof of identity of a superuser who has the second factor: the code of the device now (D-062)."""
    from leg_platform.accounts.models import AuthenticatorDevice
    from leg_platform.accounts.tests.helpers import code_for

    device = AuthenticatorDevice.objects.filter(user=user, confirmed=True).first()
    if device is None:
        device = AuthenticatorDevice.objects.create_device(user, confirmed=True)
    return {"otp_token": code_for(device)}


class NoticeSecurityBase(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.superuser = create_active_user("notice.security.superuser@example.org")
        self.superuser.set_privileges(is_staff=True, is_superuser=True)
        self.staff = create_active_user("notice.security.staff@example.org")
        self.staff.set_privileges(is_staff=True, is_superuser=False)
        self.staff.user_permissions.add(Permission.objects.get(codename="request_directory_import"))
        LogNotificationSettings.objects.update_or_create(pk=1, defaults={
            "enabled": True, "email": TARGET, "notify_error": True, "notify_warning": False, "notify_stale": False,
            "muted_failures": [], "state": {},
        })

    def client_for(self, user, enforce_csrf=False):
        client = VerifiedClient(enforce_csrf_checks=enforce_csrf)
        client.force_login(user)
        return client

    def error(self, pk=None, reason=REASON):
        return ImportLogEntry.objects.create(
            pk=pk, source=ImportRequest.Source.ADDRESS, kind="error", step="", reason=reason, occurred_at=NOW,
        )


class RetryAfterFailedSendTests(NoticeSecurityBase):
    def test_a_failed_send_keeps_the_notices_and_the_retry_sends_them(self):
        self.error()
        self.assertEqual(log_menu.notify_log_events(NOW), 1)
        send = mail_queue.HANDLERS[MailJob.Kind.LOG_NOTIFICATION](TARGET)
        with patch(DELIVER, side_effect=OSError("mail server down")):
            with self.assertRaises(OSError):
                send()
        self.assertEqual(len(log_menu.peek_pending()), 1)
        with patch(DELIVER) as deliver:
            mail_queue.HANDLERS[MailJob.Kind.LOG_NOTIFICATION](TARGET)()
        deliver.assert_called_once()
        self.assertEqual(log_menu.peek_pending(), [])


class ProofTests(NoticeSecurityBase):
    def post(self, client, **data):
        values = {"enabled": "on", "email": "new.target@example.org", **data}
        return client.post(reverse("admin:logmenu_settings"), values)

    def test_a_change_of_the_address_without_the_proof_writes_nothing(self):
        response = self.post(self.client_for(self.superuser))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(LogNotificationSettings.objects.get(pk=1).email, TARGET)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.LOG_NOTIFICATION_CHANGED).exists())

    def test_a_change_with_the_proof_is_saved_and_recorded_once_without_the_address(self):
        response = self.post(self.client_for(self.superuser), **app_proof(self.superuser))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(LogNotificationSettings.objects.get(pk=1).email, "new.target@example.org")
        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.LOG_NOTIFICATION_CHANGED)
        self.assertEqual(events.count(), 1)
        self.assertNotIn("new.target", repr(events.get().__dict__))


class CsrfTests(NoticeSecurityBase):
    def test_the_settings_post_without_a_token_is_refused(self):
        client = self.client_for(self.superuser, enforce_csrf=True)
        response = client.post(reverse("admin:logmenu_settings"), {
            "enabled": "on", "email": "new.target@example.org", **app_proof(self.superuser),
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(LogNotificationSettings.objects.get(pk=1).email, TARGET)

    def test_the_acknowledge_post_without_a_token_is_refused(self):
        self.error()
        client = self.client_for(self.superuser, enforce_csrf=True)
        response = client.post(
            reverse("admin:addresses_housekeepingstatus_changelist"), {"action": "acknowledge"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ImportLogEntry.objects.filter(cleared_at__isnull=True).count(), 1)


class SwitchOnTests(NoticeSecurityBase):
    def test_events_before_switching_on_are_never_mailed(self):
        LogNotificationSettings.objects.filter(pk=1).update(enabled=False)
        self.error()
        HousekeepingError.objects.create(step=HousekeepingError.Step.SQLITE, reason="sqlite_vacuum_failed")
        client = self.client_for(self.superuser)
        response = client.post(reverse("admin:logmenu_settings"), {
            "enabled": "on", "email": TARGET, "notify_error": "on", "failures": log_menu.failure_texts(),
            **app_proof(self.superuser),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(log_menu.notify_log_events(NOW), 0)
        self.error()
        self.assertEqual(log_menu.notify_log_events(NOW), 1)


class LateCommitTests(NoticeSecurityBase):
    def test_a_row_with_a_lower_id_that_commits_later_is_still_handled(self):
        self.error(pk=1000)
        self.error(pk=1002)
        self.assertEqual(log_menu.notify_log_events(NOW), 1)
        self.error(pk=1001)
        self.assertEqual(log_menu.notify_log_events(NOW + timedelta(hours=2)), 1)


class StaleOnceTests(NoticeSecurityBase):
    def test_two_runs_create_one_stale_row(self):
        with patch.object(log_menu, "directory_changed_on", return_value=(NOW - timedelta(days=40)).date()):
            log_menu.notify_log_events(NOW)
            log_menu.notify_log_events(NOW)
        self.assertEqual(ImportLogEntry.objects.filter(kind="stale").count(), 1)


class RetentionTests(NoticeSecurityBase):
    def test_import_log_rows_older_than_180_days_are_deleted_by_the_scheduled_cleanup(self):
        old = ImportLogEntry.objects.create(source=ImportRequest.Source.ADDRESS, reason=REASON, step="")
        ImportLogEntry.objects.filter(pk=old.pk).update(occurred_at=timezone.now() - timedelta(days=200))
        recent = ImportLogEntry.objects.create(source=ImportRequest.Source.ADDRESS, reason=REASON, step="")
        out = StringIO()
        call_command("cleanup_import_log", stdout=out)
        self.assertFalse(ImportLogEntry.objects.filter(pk=old.pk).exists())
        self.assertTrue(ImportLogEntry.objects.filter(pk=recent.pk).exists())
        self.assertIn("Deleted 1", out.getvalue())
