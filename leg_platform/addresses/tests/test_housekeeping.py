"""The weekly housekeeping of both databases (D-119): fake clock and fake dump status file.

Covered: dump missing, dump failed, dump older than two hours, fresh dump (OPTIMIZE runs), an import that holds the claim
(nothing runs), the lease against an import, low disk (no VACUUM), a failing step that does not stop the other, the
admin right (page, POST, status endpoint), once per ISO week for the scheduled run, one security event per run.
"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import mail_queue
from leg_platform.accounts.models import MailJob, SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses import housekeeping, jobs
from leg_platform.addresses.models import HousekeepingError, HousekeepingStatus, ImportRequest

ZURICH = ZoneInfo("Europe/Zurich")
# 2026-10-25 is a Sunday; 2026-10-19 is the Monday of the same ISO week.
SUNDAY_0430 = datetime(2026, 10, 25, 4, 30, tzinfo=ZURICH)
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=ZURICH)
OPTIMISE = "leg_platform.addresses.housekeeping._optimise"
IS_MARIADB = "leg_platform.addresses.housekeeping._is_mariadb"
SQLITE_STEP = "leg_platform.addresses.housekeeping._sqlite_step"
TABLE_SIZES = "leg_platform.addresses.housekeeping._table_sizes"


class DumpStatusFile:
    """A temporary dump status file (the fake of ``/var/lib/leg-platform/housekeeping-backup.json``)."""

    def __init__(self, test):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "housekeeping-backup.json"

    def write(self, finished_at=NOW - timedelta(minutes=30), ok=True, file_name="housekeeping-20261025T0420Z.sql.gz"):
        self.path.write_text(json.dumps({
            "finished_at": finished_at.isoformat(), "ok": ok, "file_name": file_name,
        }), encoding="utf-8")


class HousekeepingBase(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.dump = DumpStatusFile(self)
        self.patchers = {}
        for name, target in (
            ("BACKUP_STATUS_PATH", self.dump.path),
        ):
            patcher = patch.object(housekeeping, name, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The steps are replaced unless a test runs them: no test touches a real directory file or real tables.
        self.sqlite = self._patch(SQLITE_STEP, return_value=housekeeping._sqlite_result(
            HousekeepingStatus.State.SKIPPED, housekeeping.SQLITE_MISSING))
        self.is_mariadb = self._patch(IS_MARIADB, return_value=True)
        self.optimise = self._patch(OPTIMISE)
        self.sizes = self._patch(TABLE_SIZES, return_value={})

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.patchers[target] = patcher
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def run_now(self, now=NOW):
        return housekeeping.run_now("manual", now=now)


class DumpGateTests(HousekeepingBase):
    def mariadb(self):
        return HousekeepingStatus.objects.get_or_create(pk=1)[0]

    def test_dump_missing_skips_the_optimisation(self):
        self.assertEqual(self.run_now(), housekeeping.DONE)
        status = self.mariadb()
        self.assertEqual(status.mariadb_state, HousekeepingStatus.State.SKIPPED)
        self.assertEqual(status.mariadb_message, housekeeping.BACKUP_MISSING)
        self.optimise.assert_not_called()

    def test_failed_dump_skips_the_optimisation(self):
        self.dump.write(ok=False)
        self.run_now()
        self.assertEqual(self.mariadb().mariadb_message, housekeeping.BACKUP_FAILED)
        self.optimise.assert_not_called()

    def test_dump_older_than_two_hours_skips_the_optimisation(self):
        self.dump.write(finished_at=NOW - timedelta(hours=2, minutes=1))
        self.run_now()
        self.assertEqual(self.mariadb().mariadb_message, housekeeping.BACKUP_OLD)
        self.optimise.assert_not_called()

    def test_dump_in_the_future_is_not_trusted(self):
        self.dump.write(finished_at=NOW + timedelta(hours=1))
        self.run_now()
        self.assertEqual(self.mariadb().mariadb_message, housekeeping.BACKUP_INVALID)
        self.optimise.assert_not_called()

    def test_unreadable_status_text_is_invalid(self):
        self.dump.path.write_text("{not json", encoding="utf-8")
        self.run_now()
        self.assertEqual(self.mariadb().mariadb_message, housekeeping.BACKUP_INVALID)
        self.optimise.assert_not_called()

    def test_fresh_dump_runs_the_optimisation_on_the_chosen_tables(self):
        self.dump.write()
        self.run_now()
        status = self.mariadb()
        self.assertEqual(status.mariadb_state, HousekeepingStatus.State.SUCCEEDED)
        self.assertEqual(status.mariadb_message, housekeeping.MARIADB_OPTIMISED)
        self.optimise.assert_called_once()
        tables = self.optimise.call_args.args[1]
        self.assertEqual(tables, housekeeping.optimised_tables())
        self.assertIn("accounts_mailjob", tables)
        self.assertIn("addresses_buildinglookup", tables)

    def test_the_status_file_is_only_read(self):
        self.dump.write()
        before = self.dump.path.read_bytes()
        self.run_now()
        self.assertEqual(self.dump.path.read_bytes(), before)

    def test_database_other_than_mariadb_is_skipped(self):
        self.is_mariadb.return_value = False
        self.dump.write()
        self.run_now()
        self.assertEqual(self.mariadb().mariadb_message, housekeeping.MARIADB_NOT_MARIADB)
        self.optimise.assert_not_called()


class SqliteStepTests(HousekeepingBase):
    """The address directory step with a real temporary file; the disk and the SQLite calls are replaced."""

    def setUp(self):
        super().setUp()
        self.patchers[SQLITE_STEP].stop()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file = Path(directory.name) / "address-directory.sqlite3"
        self.file.write_bytes(b"x" * 4096)
        self.file_path = self._patch("leg_platform.addresses.housekeeping._directory_path", return_value=self.file)
        self.vacuum = self._patch("leg_platform.addresses.housekeeping._vacuum")
        self.check = self._patch("leg_platform.addresses.housekeeping._quick_check", return_value=True)
        self.disk = self._patch("shutil.disk_usage", return_value=SimpleNamespace(free=10 * 1024 ** 3))

    def test_low_disk_skips_the_vacuum(self):
        # Free space must be at least the file size plus 1 GiB: 1 GiB exactly is not enough for a 4 KiB file plus margin.
        self.disk.return_value = SimpleNamespace(free=1024 ** 3)
        result = housekeeping._sqlite_step(NOW)
        self.assertEqual(result["sqlite_state"], HousekeepingStatus.State.SKIPPED)
        self.assertEqual(result["sqlite_message"], housekeeping.SQLITE_DISK_SPACE)
        self.vacuum.assert_not_called()

    def test_enough_space_runs_check_vacuum_and_check(self):
        result = housekeeping._sqlite_step(NOW)
        self.assertEqual(result["sqlite_state"], HousekeepingStatus.State.SUCCEEDED)
        self.assertEqual(result["sqlite_message"], housekeeping.SQLITE_COMPACTED)
        self.assertEqual(self.check.call_count, 2)
        self.vacuum.assert_called_once()

    def test_failed_check_before_skips_the_vacuum(self):
        self.check.return_value = False
        result = housekeeping._sqlite_step(NOW)
        self.assertEqual(result["sqlite_message"], housekeeping.SQLITE_CHECK_FAILED)
        self.vacuum.assert_not_called()

    def test_missing_file_is_skipped_and_nothing_is_created(self):
        self.file.unlink()
        result = housekeeping._sqlite_step(NOW)
        self.assertEqual(result["sqlite_message"], housekeeping.SQLITE_MISSING)
        self.assertFalse(self.file.exists())
        self.vacuum.assert_not_called()


class ClaimTests(HousekeepingBase):
    def test_an_import_that_holds_the_claim_stops_the_run(self):
        ImportRequest.objects.update_or_create(pk=1, defaults={
            "state": ImportRequest.State.RUNNING, "started_at": NOW, "heartbeat_at": NOW,
        })
        self.dump.write()
        self.assertEqual(self.run_now(), housekeeping.IMPORT_RUNNING)
        self.sqlite.assert_not_called()
        self.optimise.assert_not_called()
        self.assertFalse(HousekeepingStatus.objects.filter(pk=1, started_at__isnull=False).exists())
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.HOUSEKEEPING_RUN).exists())

    def test_a_running_housekeeping_refuses_an_import(self):
        self.dump.write()
        with patch.object(housekeeping, "_execute"):
            self.assertIsNone(housekeeping._claim("manual", None, NOW))
        self.assertIsNone(jobs.claim(web=True))
        self.assertEqual(HousekeepingStatus.objects.get(pk=1).trigger, "manual")

    def test_a_second_run_is_refused_while_the_lease_is_active(self):
        HousekeepingStatus.objects.update_or_create(pk=1, defaults={"running_until": NOW + timedelta(hours=1)})
        self.assertEqual(self.run_now(), housekeeping.BUSY)
        self.sqlite.assert_not_called()

    def test_the_lease_is_released_after_the_run(self):
        self.dump.write()
        self.run_now()
        self.assertIsNone(HousekeepingStatus.objects.get(pk=1).running_until)


class StepIsolationTests(HousekeepingBase):
    def test_a_failing_step_does_not_stop_the_other(self):
        self.sqlite.side_effect = RuntimeError("secret detail")
        self.dump.write()
        self.run_now()
        status = HousekeepingStatus.objects.get(pk=1)
        self.assertEqual(status.sqlite_state, HousekeepingStatus.State.FAILED)
        self.assertEqual(status.sqlite_message, housekeeping.STEP_FAILED)
        self.assertEqual(status.mariadb_state, HousekeepingStatus.State.SUCCEEDED)
        self.optimise.assert_called_once()

    def test_a_database_error_in_optimise_is_recorded_with_a_fixed_text(self):
        self.optimise.side_effect = DatabaseError("table name and SQL")
        self.dump.write()
        self.run_now()
        status = HousekeepingStatus.objects.get(pk=1)
        self.assertEqual(status.mariadb_state, HousekeepingStatus.State.FAILED)
        self.assertEqual(status.mariadb_message, housekeeping.MARIADB_OPTIMISE_FAILED)
        self.assertNotIn("SQL", status.mariadb_message)


class ScheduleTests(HousekeepingBase):
    def test_scheduled_run_only_on_sunday_from_0430(self):
        self.assertEqual(housekeeping.run_scheduled(datetime(2026, 10, 24, 5, 0, tzinfo=ZURICH)), housekeeping.NOT_SUNDAY)
        self.assertEqual(housekeeping.run_scheduled(datetime(2026, 10, 25, 4, 29, tzinfo=ZURICH)), housekeeping.BEFORE_TIME)

    def test_scheduled_run_once_per_iso_week(self):
        self.dump.write(finished_at=SUNDAY_0430 - timedelta(minutes=10))
        self.assertEqual(housekeeping.run_scheduled(SUNDAY_0430), housekeeping.DONE)
        self.assertEqual(housekeeping.run_scheduled(SUNDAY_0430 + timedelta(minutes=15)), housekeeping.ALREADY_RAN)
        self.assertEqual(self.optimise.call_count, 1)

    def test_a_manual_run_counts_for_the_week(self):
        self.dump.write(finished_at=datetime(2026, 10, 19, 10, 0, tzinfo=ZURICH))
        monday = datetime(2026, 10, 19, 10, 0, tzinfo=ZURICH)
        self.assertEqual(housekeeping.run_now("manual", now=monday), housekeeping.DONE)
        self.assertEqual(housekeeping.run_scheduled(SUNDAY_0430), housekeeping.ALREADY_RAN)

    def test_a_failed_scheduled_run_is_not_repeated_in_the_same_week(self):
        self.sqlite.side_effect = RuntimeError()
        self.dump.write(finished_at=SUNDAY_0430 - timedelta(minutes=10))
        housekeeping.run_scheduled(SUNDAY_0430)
        self.assertEqual(housekeeping.run_scheduled(SUNDAY_0430 + timedelta(minutes=15)), housekeeping.ALREADY_RAN)

    def test_each_run_writes_one_security_event(self):
        self.dump.write(finished_at=SUNDAY_0430 - timedelta(minutes=10))
        housekeeping.run_scheduled(SUNDAY_0430)
        housekeeping.run_scheduled(SUNDAY_0430 + timedelta(minutes=15))
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.HOUSEKEEPING_RUN).count(), 1)

    def test_the_command_runs_the_weekly_check_by_default(self):
        with patch.object(housekeeping, "run_scheduled", return_value=housekeeping.NOT_SUNDAY) as scheduled,                 patch("leg_platform.addresses.storage.mark_writer"):
            call_command("housekeeping")
        scheduled.assert_called_once_with()


class AdminPermissionTests(HousekeepingBase):
    def setUp(self):
        super().setUp()
        self.person = create_active_user("housekeeping.staff@example.org")
        self.person.set_privileges(is_staff=True, is_superuser=False)

    def client_for(self, with_permission):
        if with_permission:
            self.person.user_permissions.add(Permission.objects.get(codename="run_housekeeping"))
        client = VerifiedClient()
        client.force_login(self.person)
        return client

    def test_the_page_post_and_status_need_the_right(self):
        client = self.client_for(False)
        page = reverse("admin:addresses_housekeepingstatus_changelist")
        status = reverse("admin:addresses_housekeepingstatus_status")
        self.assertEqual(client.get(page).status_code, 403)
        self.assertEqual(client.post(page).status_code, 403)
        self.assertEqual(client.get(status).status_code, 403)
        self.assertFalse(HousekeepingStatus.objects.filter(started_at__isnull=False).exists())
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.HOUSEKEEPING_RUN).exists())

    def test_the_right_shows_the_page_and_the_status(self):
        client = self.client_for(True)
        page = reverse("admin:addresses_housekeepingstatus_changelist")
        self.assertEqual(client.get(page).status_code, 200)
        response = client.get(reverse("admin:addresses_housekeepingstatus_status"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("steps", response.json())

    def test_the_button_starts_a_thread_once_and_records_one_event(self):
        client = self.client_for(True)
        page = reverse("admin:addresses_housekeepingstatus_changelist")
        with patch("leg_platform.addresses.housekeeping.threading.Thread") as thread:
            response = client.post(page)
        self.assertEqual(response.status_code, 302)
        thread.assert_called_once()
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.HOUSEKEEPING_RUN).count(), 1)
        with patch("leg_platform.addresses.housekeeping.threading.Thread") as thread:
            client.post(page)
        thread.assert_not_called()
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.HOUSEKEEPING_RUN).count(), 1)


class ReviewFixTests(HousekeepingBase):
    """Review findings: setup marker, free space for the temporary copy, deep JSON, a late run."""

    def setUp(self):
        super().setUp()
        self.patchers[SQLITE_STEP].stop()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file = Path(directory.name) / "address-directory.sqlite3"
        self.file.write_bytes(b"x" * 4096)
        self._patch("leg_platform.addresses.housekeeping._directory_path", return_value=self.file)
        self.vacuum = self._patch("leg_platform.addresses.housekeeping._vacuum")
        self._patch("leg_platform.addresses.housekeeping._quick_check", return_value=True)

    def free(self, bytes_free):
        self._patch("shutil.disk_usage", return_value=SimpleNamespace(free=bytes_free))

    def test_free_space_must_cover_the_temporary_copy(self):
        # One copy of the file plus the margin is not enough: VACUUM needs about twice the file size.
        self.free(1024 ** 3 + 4096)
        self.assertEqual(housekeeping._sqlite_step(NOW)["sqlite_message"], housekeeping.SQLITE_DISK_SPACE)
        self.vacuum.assert_not_called()

    def test_twice_the_file_plus_the_margin_is_enough(self):
        self.free(2 * 4096 + 1024 ** 3)
        self.assertEqual(housekeeping._sqlite_step(NOW)["sqlite_message"], housekeeping.SQLITE_COMPACTED)
        self.vacuum.assert_called_once()

    def test_a_setup_of_the_directory_file_refuses_the_run(self):
        marker = SimpleNamespace(exists=lambda: True)
        with patch("leg_platform.addresses.storage.reset_marker", return_value=marker):
            self.assertEqual(self.run_now(), housekeeping.SETUP_RUNNING)
        self.sqlite.assert_not_called()
        self.optimise.assert_not_called()

    def test_deeply_nested_status_file_is_invalid_not_an_error(self):
        self.dump.path.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
        self.assertEqual(housekeeping.read_backup_status(self.dump.path), (None, housekeeping.BACKUP_INVALID))

    def test_a_late_run_does_not_overwrite_a_newer_result(self):
        later = NOW + timedelta(hours=1)
        HousekeepingStatus.objects.update_or_create(pk=1, defaults={
            "started_at": later, "running_until": later + timedelta(hours=3),
        })
        self.dump.write()
        housekeeping._execute(NOW)
        status = HousekeepingStatus.objects.get(pk=1)
        self.assertEqual(status.sqlite_state, "")
        self.assertIsNotNone(status.running_until)


class ErrorStateTests(HousekeepingBase):
    """The error flag (no weekly run while an error is open), the error log, the error mail and its body."""

    def setUp(self):
        super().setUp()
        self.dump.write()
        self.sqlite.return_value = housekeeping._sqlite_result(
            HousekeepingStatus.State.FAILED, housekeeping.SQLITE_VACUUM_FAILED, 4096)

    def succeed(self):
        self.sqlite.return_value = housekeeping._sqlite_result(
            HousekeepingStatus.State.SUCCEEDED, housekeeping.SQLITE_COMPACTED, 4096, 2048)

    def open_errors(self):
        return HousekeepingError.objects.filter(cleared_at__isnull=True).count()

    def test_a_failed_step_opens_an_error_with_step_reason_and_dump_name(self):
        self.run_now()
        error = HousekeepingError.objects.get()
        self.assertEqual(error.step, HousekeepingError.Step.SQLITE)
        self.assertEqual(error.reason, housekeeping.SQLITE_VACUUM_FAILED)
        self.assertEqual(error.dump_file_name, "housekeeping-20261025T0420Z.sql.gz")

    def test_the_weekly_run_does_not_run_again_while_an_error_is_open(self):
        self.run_now()
        self.sqlite.reset_mock()
        # Same week: no second attempt (the week is already used).
        self.assertEqual(housekeeping.run_scheduled(SUNDAY_0430), housekeeping.ALREADY_RAN)
        # Next week: the open error still holds the weekly run back.
        next_sunday = datetime(2026, 11, 1, 4, 30, tzinfo=ZURICH)
        self.assertEqual(housekeeping.run_scheduled(next_sunday), housekeeping.ERROR_OPEN)
        self.sqlite.assert_not_called()

    def test_a_manual_run_is_allowed_while_an_error_is_open(self):
        self.run_now()
        self.sqlite.reset_mock()
        self.assertEqual(self.run_now(NOW + timedelta(minutes=5)), housekeeping.DONE)
        self.sqlite.assert_called_once()

    def test_a_successful_run_clears_the_error(self):
        self.run_now()
        self.succeed()
        self.run_now(NOW + timedelta(hours=1))
        self.assertEqual(self.open_errors(), 0)
        self.assertEqual(HousekeepingError.objects.count(), 1)

    def test_the_error_log_lists_only_housekeeping_errors(self):
        self.run_now()
        SecurityEvent.objects.create(event=SecurityEvent.Event.LOG_VIEWED)
        rows = housekeeping.error_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"occurred_at", "step", "reason", "dump", "open"})

    def test_the_error_mail_is_queued_only_when_enabled_and_once_per_open_error(self):
        self.run_now()  # switch off by default
        with patch("leg_platform.accounts.mail_queue.enqueue") as enqueue:
            self.run_now(NOW + timedelta(minutes=5))
            enqueue.assert_not_called()
        HousekeepingStatus.objects.update_or_create(pk=1, defaults={
            "notify_enabled": True, "notify_email": "housekeeping@example.org",
        })
        self.succeed()
        self.run_now(NOW + timedelta(hours=1))  # clears the errors
        self.failing()
        with patch("leg_platform.accounts.mail_queue.enqueue") as enqueue:
            self.run_now(NOW + timedelta(hours=2))
            enqueue.assert_called_once_with(MailJob.Kind.HOUSEKEEPING_ERROR, "housekeeping@example.org")
            self.run_now(NOW + timedelta(hours=2, minutes=5))  # error still open: no second mail
            enqueue.assert_called_once()

    def failing(self):
        self.sqlite.return_value = housekeeping._sqlite_result(
            HousekeepingStatus.State.FAILED, housekeeping.SQLITE_VACUUM_FAILED, 4096)

    def test_the_error_mail_body_holds_the_log_lines_and_no_dump_content(self):
        dump_file = self.dump.path.parent / "housekeeping-20261025T0420Z.sql.gz"
        dump_file.write_text("SECRET-DUMP-CONTENT INSERT INTO accounts_user", encoding="utf-8")
        self.run_now()
        send = mail_queue.HANDLERS[MailJob.Kind.HOUSEKEEPING_ERROR]("housekeeping@example.org")
        self.assertIsNotNone(send)
        with patch("leg_platform.accounts.emails._deliver") as deliver:
            send()
        address, subject, body = deliver.call_args.args
        self.assertEqual(address, "housekeeping@example.org")
        self.assertIn("housekeeping-20261025T0420Z.sql.gz", body)
        self.assertIn(" | ", body)
        self.assertNotIn("SECRET-DUMP-CONTENT", body)
        self.assertNotIn("INSERT", body)

    def test_no_mail_job_is_made_without_an_open_error(self):
        self.assertIsNone(mail_queue.HANDLERS[MailJob.Kind.HOUSEKEEPING_ERROR]("housekeeping@example.org"))


class ErrorAdminTests(HousekeepingBase):
    def setUp(self):
        super().setUp()
        self.dump.write()
        self.sqlite.return_value = housekeeping._sqlite_result(
            HousekeepingStatus.State.FAILED, housekeeping.SQLITE_VACUUM_FAILED, 4096)
        self.person = create_active_user("housekeeping.errors@example.org")
        self.person.set_privileges(is_staff=True, is_superuser=False)

    def client_for(self, with_permission):
        if with_permission:
            self.person.user_permissions.add(Permission.objects.get(codename="run_housekeeping"))
        client = VerifiedClient()
        client.force_login(self.person)
        return client

    def test_acknowledge_clears_the_flag_and_starts_and_changes_no_run(self):
        self.run_now()
        calls = self.sqlite.call_count
        client = self.client_for(True)
        with patch("leg_platform.addresses.housekeeping.threading.Thread") as thread:
            response = client.post(reverse("admin:addresses_housekeepingstatus_changelist"), {"action": "acknowledge"})
        self.assertEqual(response.status_code, 302)
        thread.assert_not_called()
        self.assertEqual(self.sqlite.call_count, calls)
        self.assertEqual(HousekeepingError.objects.filter(cleared_at__isnull=True).count(), 0)
        self.assertEqual(HousekeepingStatus.objects.get(pk=1).running_until, None)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.HOUSEKEEPING_ERRORS_ACKNOWLEDGED).count(), 1)

    def test_acknowledge_is_denied_without_the_right(self):
        self.run_now()
        client = self.client_for(False)
        response = client.post(reverse("admin:addresses_housekeepingstatus_changelist"), {"action": "acknowledge"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(HousekeepingError.objects.filter(cleared_at__isnull=True).count(), 1)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.HOUSEKEEPING_ERRORS_ACKNOWLEDGED).exists())

    def test_the_notification_setting_needs_the_right_and_a_valid_address(self):
        page = reverse("admin:addresses_housekeepingstatus_changelist")
        data = {"action": "notify", "notify_enabled": "on", "notify_email": "housekeeping@example.org"}
        response = self.client_for(False).post(page, data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(HousekeepingStatus.objects.filter(pk=1, notify_enabled=True).exists())
        client = self.client_for(True)
        invalid = client.post(page, {**data, "notify_email": "not-an-address"})
        self.assertEqual(invalid.status_code, 200)
        self.assertFalse(HousekeepingStatus.objects.filter(pk=1, notify_enabled=True).exists())
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.HOUSEKEEPING_NOTIFICATION_CHANGED).exists())
        valid = client.post(page, data)
        self.assertEqual(valid.status_code, 302)
        self.assertTrue(HousekeepingStatus.objects.filter(pk=1, notify_enabled=True).exists())
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.HOUSEKEEPING_NOTIFICATION_CHANGED).count(), 1)
