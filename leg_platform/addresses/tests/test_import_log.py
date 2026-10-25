"""The import page and the import error log (D-121): superusers only for the log, one action per block, the errors open
until a later success of the same source, the notice of import errors (no reason text in the mail), and the progress of
a running import (last progress, stall warning after ten minutes, no percentage without a known total)."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leg_platform import log_menu
from leg_platform.accounts import mail_queue
from leg_platform.accounts.models import LogNotificationSettings, MailJob
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses import importer, jobs, status
from leg_platform.addresses.models import ImportLogEntry, ImportRequest

ZURICH = ZoneInfo("Europe/Zurich")
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=ZURICH)
TOKEN = "11111111-2222-3333-4444-555555555555"
REASON = "Missing EGID column."
SENDER = "leg_platform.accounts.emails._deliver"


class ImportLogBase(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.superuser = create_active_user("import.superuser@example.org")
        self.superuser.set_privileges(is_staff=True, is_superuser=True)
        self.staff = create_active_user("import.staff@example.org")
        self.staff.set_privileges(is_staff=True, is_superuser=False)
        self.staff.user_permissions.add(Permission.objects.get(codename="request_directory_import"))

    def client_for(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client

    def failed_run(self, source=ImportRequest.Source.ADDRESS, reason=REASON):
        """A failed run of ``source`` through the job functions (the claim is ours: the row carries the token)."""
        ImportRequest.objects.update_or_create(pk=1, defaults={
            "claim_token": TOKEN, "state": ImportRequest.State.RUNNING, "source": source,
            "started_at": NOW, "heartbeat_at": NOW, "phase": "downloading",
        })
        claimed = SimpleNamespace(source=source, token=TOKEN, manual=True)
        jobs.record_failure(claimed, importer.ImportFailure(reason))
        return claimed

    def successful_run(self, source=ImportRequest.Source.ADDRESS):
        ImportRequest.objects.update_or_create(pk=1, defaults={
            "claim_token": TOKEN, "state": ImportRequest.State.RUNNING, "source": source, "started_at": NOW,
            "heartbeat_at": NOW,
        })
        claimed = SimpleNamespace(source=source, token=TOKEN, manual=True)
        jobs.record_success(claimed, 3)


class ImportLogAccessTests(ImportLogBase):
    def test_superuser_sees_the_import_log(self):
        self.failed_run()
        response = self.client_for(self.superuser).get(reverse("admin:addresses_importrequest_log"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="importlog-table"')
        self.assertEqual(ImportLogEntry.objects.count(), 1)

    def test_staff_with_the_directory_right_is_refused_on_the_log_and_its_download(self):
        self.failed_run()
        client = self.client_for(self.staff)
        self.assertEqual(client.get(reverse("admin:addresses_importrequest_log")).status_code, 403)
        for source in ("all", "address", "plant", "transformer"):
            with self.subTest(source=source):
                url = reverse("admin:addresses_importrequest_log_download", kwargs={"source": source})
                self.assertEqual(client.get(url).status_code, 403)

    def test_the_download_is_text_and_redacted_for_the_superuser(self):
        self.failed_run(reason=REASON)
        url = reverse("admin:addresses_importrequest_log_download", kwargs={"source": "all"})
        response = self.client_for(self.superuser).get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response["Content-Type"])
        lines = b"".join(response.streaming_content).decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0].split("	")), 4)

    def test_the_directory_page_links_the_log_to_the_superuser_only(self):
        self.failed_run()
        log_url = reverse("admin:addresses_importrequest_log")
        self.assertContains(self.client_for(self.superuser).get(reverse("admin:addresses_importrequest_changelist")), log_url)
        self.assertNotContains(self.client_for(self.staff).get(reverse("admin:addresses_importrequest_changelist")), log_url)


class ImportErrorStateTests(ImportLogBase):
    def test_a_failed_run_opens_an_error_with_source_and_fixed_reason(self):
        self.failed_run()
        entry = ImportLogEntry.objects.get()
        self.assertEqual(entry.source, ImportRequest.Source.ADDRESS)
        self.assertEqual(entry.reason, REASON)
        self.assertIsNone(entry.cleared_at)

    def test_a_later_success_of_the_same_source_clears_only_that_source(self):
        self.failed_run(ImportRequest.Source.ADDRESS)
        self.failed_run(ImportRequest.Source.PLANT, reason=REASON)
        self.successful_run(ImportRequest.Source.ADDRESS)
        self.assertIsNotNone(ImportLogEntry.objects.get(source=ImportRequest.Source.ADDRESS).cleared_at)
        self.assertIsNone(ImportLogEntry.objects.get(source=ImportRequest.Source.PLANT).cleared_at)


class ImportNoticeTests(ImportLogBase):
    def setUp(self):
        super().setUp()
        LogNotificationSettings.objects.update_or_create(pk=1, defaults={
            "enabled": True, "email": "notice.target@example.org",
        })

    def test_an_import_error_queues_one_mail_without_the_reason(self):
        self.failed_run()
        self.assertEqual(log_menu.notify_log_events(NOW), 1)
        with patch(SENDER) as deliver:
            done, failed = mail_queue.process_pending()
        self.assertEqual((done, failed), (1, 0))
        address, subject, body = deliver.call_args.args
        self.assertEqual(address, "notice.target@example.org")
        self.assertIn("| error |", body)
        self.assertNotIn(REASON, body)
        self.assertNotIn("Missing", body)
        self.assertNotIn("EGID", body)

    def test_no_mail_when_disabled_or_below_the_severity(self):
        self.failed_run()
        LogNotificationSettings.objects.filter(pk=1).update(enabled=False)
        self.assertEqual(log_menu.notify_log_events(NOW), 0)
        LogNotificationSettings.objects.filter(pk=1).update(
            enabled=True, notify_error=False,
        )
        self.assertEqual(log_menu.notify_log_events(NOW + timedelta(hours=2)), 0)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.LOG_NOTIFICATION).exists())

    def test_one_mail_per_source_and_hour(self):
        self.failed_run()
        self.assertEqual(log_menu.notify_log_events(NOW), 1)
        self.failed_run()
        self.assertEqual(log_menu.notify_log_events(NOW + timedelta(minutes=5)), 0)
        self.assertEqual(log_menu.suppressed_counts(), {"import": 1})


class ImportPageLayoutTests(ImportLogBase):
    def test_the_action_button_appears_once_and_only_in_the_address_block(self):
        response = self.client_for(self.staff).get(reverse("admin:addresses_importrequest_changelist"))
        html = response.content.decode("utf-8")
        self.assertEqual(html.count('id="import-start"'), 1)
        self.assertEqual(html.count('<input type="submit"'), 1)
        plant = html[html.index('id="plant-status"'):html.index('id="transformer-status"')]
        transformer = html[html.index('id="transformer-status"'):]
        self.assertNotIn("<input", plant)
        self.assertNotIn("<input", transformer)


class ImportProgressTests(ImportLogBase):
    def job(self, progress_at, **fields):
        values = {"source": ImportRequest.Source.ADDRESS, "progress_at": progress_at}
        values.update(fields)
        return SimpleNamespace(state=ImportRequest.State.RUNNING, **values)

    def test_the_warning_shows_after_the_threshold_with_a_fake_clock(self):
        job = self.job(NOW)
        quiet = status.progress_lines(job, ImportRequest.Source.ADDRESS, NOW + timedelta(minutes=9))
        self.assertFalse(quiet["progress_stalled"])
        self.assertEqual(quiet["stall_text"], "")
        late = status.progress_lines(job, ImportRequest.Source.ADDRESS, NOW + timedelta(minutes=11))
        self.assertTrue(late["progress_stalled"])
        self.assertIn("11", late["stall_text"])
        self.assertIn("11", late["progress_text"])

    def test_no_percentage_when_the_total_is_unknown(self):
        ImportRequest.objects.update_or_create(pk=1, defaults={
            "claim_token": TOKEN, "state": ImportRequest.State.RUNNING, "source": ImportRequest.Source.ADDRESS,
            "started_at": timezone.now(), "heartbeat_at": timezone.now(), "phase": "downloading",
            "bytes_done": 5_000, "bytes_total": None, "rows_done": 7, "progress_at": timezone.now(),
        })
        payload = status.build()
        self.assertIsNone(payload["percent"])
        self.assertEqual(payload["progress_rows"], 7)
        self.assertEqual(payload["progress_bytes_total"], None)
        self.assertNotEqual(payload["progress_at"], "")

    def test_the_status_holds_the_counts_and_the_last_update(self):
        last = timezone.now()
        ImportRequest.objects.update_or_create(pk=1, defaults={
            "claim_token": TOKEN, "state": ImportRequest.State.RUNNING, "source": ImportRequest.Source.ADDRESS,
            "started_at": last, "heartbeat_at": last, "phase": "downloading",
            "bytes_done": 500, "bytes_total": 1000, "rows_done": 0, "progress_at": last,
        })
        payload = status.build()
        self.assertEqual(payload["percent"], 50)
        self.assertEqual(payload["progress_bytes_done"], 500)
        self.assertEqual(payload["progress_bytes_total"], 1000)
        self.assertIn(":", payload["progress_at"])
