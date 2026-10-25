"""The daily address directory run and the update flag (KONZEPT 3a points 5 to 7, D-090): nothing before the time,
at most one download per day, the same claim as the button, the flag cleared by a successful import."""

import time as clock_time
import uuid
from datetime import datetime, time, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import ANY, patch
from zoneinfo import ZoneInfo

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings, tag
from django.utils import timezone

from leg_platform.accounts.management.commands import run_scheduled_tasks
from leg_platform.accounts.models import SecurityEvent
from leg_platform.addresses import importer, jobs, runner, schedule
from leg_platform.addresses.models import Directory, ImportRequest

State = ImportRequest.State
ZURICH = ZoneInfo("Europe/Zurich")
IMPORT = "leg_platform.addresses.importer.import_directory"


def at(hour, minute=0, day=19):
    """A moment on 2026-10-DAY in the instance time zone."""
    return datetime(2026, 10, day, hour, minute, tzinfo=ZURICH)


def fixed_time(moment):
    """The clock of ``schedule`` stands at ``moment`` (the rest of the code keeps the real clock)."""
    return patch.object(schedule, "timezone", SimpleNamespace(now=lambda: moment, localtime=lambda value: value))


def publish_directory(*, has_egid=True, rows=3):
    Directory.objects.update_or_create(
        pk=1, defaults={
            "generation": uuid.uuid4(), "data_date": timezone.localdate(), "imported_at": timezone.now(),
            "checksum": "0" * 64, "row_count": rows, "has_egid": has_egid,
        },
    )


@tag("mariadb")
@override_settings(ADDRESS_IMPORT_AUTOMATIC=True)
class DailyRunTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        # The plant register follows the address run (D-117); these tests never download it (see test_plants).
        self.enterContext(patch.object(schedule, "run_plant_daily", return_value=schedule.DISABLED))
        # The transformer station scan follows it on Saturdays (D-118); these tests never scan (see test_transformers).
        self.enterContext(patch.object(schedule, "run_transformer_weekly", return_value=schedule.NOT_SATURDAY))
        publish_directory()
        # A recent regular refresh: only the flag (or a missing identifier column) makes the run due.
        ImportRequest.objects.create(pk=1, last_automatic_attempt_at=timezone.now() - timedelta(days=1))

    def job(self):
        return ImportRequest.objects.get(pk=1)

    def flag(self):
        self.assertTrue(jobs.set_update_flag())

    # --- the time of day -------------------------------------------------------------------------------------

    def test_the_default_time_is_three_in_the_morning_and_it_is_read_through_one_function(self):
        self.assertEqual(schedule.import_run_time(), time(3, 0))
        ImportRequest.objects.all().delete()
        self.assertEqual(schedule.import_run_time(), time(3, 0))

    def test_the_time_can_be_set_in_whole_minutes_and_only_as_a_time(self):
        schedule.set_import_run_time(time(4, 30, 59))
        self.assertEqual(schedule.import_run_time(), time(4, 30))
        for bad in ("04:30", 4, None, datetime(2026, 1, 1, 4, 30)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                schedule.set_import_run_time(bad)
        self.assertEqual(schedule.import_run_time(), time(4, 30))

    def test_setting_the_time_creates_the_row_when_it_is_missing(self):
        ImportRequest.objects.all().delete()
        schedule.set_import_run_time(time(5, 0))
        self.assertEqual(schedule.import_run_time(), time(5, 0))

    def test_it_does_nothing_before_the_configured_time(self):
        self.flag()
        with patch(IMPORT) as download:
            self.assertEqual(schedule.run_daily(at(2, 59)), schedule.BEFORE_TIME)
            self.assertEqual(schedule.run_daily(at(0, 0)), schedule.BEFORE_TIME)
        download.assert_not_called()
        job = self.job()
        self.assertEqual((job.state, job.last_scheduled_run_on), (State.IDLE, None))
        self.assertIsNotNone(job.update_flag_set_at)

    @override_settings(ADDRESS_IMPORT_AUTOMATIC=False)
    def test_a_durable_setup_request_runs_before_time_even_if_automatic_refresh_is_disabled(self):
        # The default installation has no separate importer worker. A queued setup request
        # must therefore be consumed at the next task tick, not wait for the daily policy.
        ImportRequest.objects.filter(pk=1).update(
            state=State.QUEUED, last_scheduled_run_on=at(0).date(), columns_missing_at=at(0),
        )
        with patch(IMPORT, return_value=3) as download:
            self.assertEqual(schedule.run_daily(at(0, 1)), schedule.IMPORTED)
            self.assertEqual(schedule.run_daily(at(0, 2)), schedule.DISABLED)
        download.assert_called_once_with(repair=True, progress=ANY)
        self.assertEqual(self.job().state, State.SUCCEEDED)

    def test_failure_of_a_durable_request_is_recorded_and_not_requeued_by_the_task_tick(self):
        ImportRequest.objects.filter(pk=1).update(state=State.QUEUED)
        with patch(IMPORT, side_effect=importer.ImportFailure("Official download failed.")) as download:
            with self.assertRaises(importer.ImportFailure), self.assertLogs("leg_platform.addresses.jobs", "ERROR"):
                schedule.run_daily(at(0, 1))
            self.assertEqual(schedule.run_daily(at(0, 2)), schedule.BEFORE_TIME)
            self.assertEqual(schedule.run_daily(at(3, 1)), schedule.ALREADY_RAN)
        download.assert_called_once()
        self.assertEqual(self.job().state, State.FAILED)

    def test_the_time_is_the_one_of_the_instance_not_of_utc(self):
        self.flag()
        schedule.set_import_run_time(time(3, 0))
        # 2026-10-19 is summer time: 01:30 UTC is 03:30 in Zurich, 00:30 UTC is 02:30 (before the time).
        with patch(IMPORT, return_value=3) as download:
            self.assertEqual(schedule.run_daily(datetime(2026, 10, 19, 0, 30, tzinfo=ZoneInfo("UTC"))), schedule.BEFORE_TIME)
            download.assert_not_called()
            self.assertEqual(schedule.run_daily(datetime(2026, 10, 19, 1, 30, tzinfo=ZoneInfo("UTC"))), schedule.IMPORTED)

    def test_a_later_configured_time_moves_the_run(self):
        self.flag()
        schedule.set_import_run_time(time(6, 15))
        with patch(IMPORT, return_value=3) as download:
            self.assertEqual(schedule.run_daily(at(6, 14)), schedule.BEFORE_TIME)
            self.assertEqual(schedule.run_daily(at(6, 15)), schedule.IMPORTED)
        download.assert_called_once()

    # --- the flag --------------------------------------------------------------------------------------------

    def test_with_the_flag_set_the_import_runs_once_and_clears_the_flag(self):
        self.flag()
        with patch(IMPORT, return_value=3) as download:
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.IMPORTED)
        download.assert_called_once_with(progress=ANY)  # an automatic claim: no repair
        job = self.job()
        self.assertEqual((job.state, job.is_automatic, job.last_scheduled_run_on), (State.SUCCEEDED, True, at(3).date()))
        self.assertIsNone(job.update_flag_set_at)
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.ADDRESS_IMPORT_SUCCEEDED).count(), 1,
        )

    def test_the_run_is_idempotent_a_second_call_on_the_same_day_changes_nothing(self):
        self.flag()
        with patch(IMPORT, return_value=3) as download:
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.IMPORTED)
            before = self.job()
            for moment in (at(3, 15), at(12, 0), at(23, 59)):
                self.assertEqual(schedule.run_daily(moment), schedule.ALREADY_RAN)
        download.assert_called_once()
        after = self.job()
        self.assertEqual((after.state, after.finished_at, after.last_scheduled_run_on),
                         (before.state, before.finished_at, before.last_scheduled_run_on))

    def test_at_most_one_download_per_day_even_when_the_flag_is_set_again_after_the_run(self):
        self.flag()
        with patch(IMPORT, return_value=3) as download:
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.IMPORTED)
            self.flag()  # many unknown addresses later the same day
            self.assertEqual(schedule.run_daily(at(15, 0)), schedule.ALREADY_RAN)
            self.assertEqual(download.call_count, 1)
            self.assertIsNotNone(self.job().update_flag_set_at)
            # The next day it runs again.
            self.assertEqual(schedule.run_daily(at(3, 5, day=20)), schedule.IMPORTED)
        self.assertEqual(download.call_count, 2)
        self.assertIsNone(self.job().update_flag_set_at)

    def test_without_a_flag_nothing_is_due_and_the_day_is_used_up(self):
        with patch(IMPORT) as download:
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.NOT_DUE)
            # A flag set later that day waits for the next day: one check per day.
            self.flag()
            self.assertEqual(schedule.run_daily(at(10, 0)), schedule.ALREADY_RAN)
        download.assert_not_called()
        self.assertEqual(self.job().state, State.IDLE)

    def test_the_flag_is_not_cleared_by_a_failed_import_and_a_failed_import_is_not_repeated_that_day(self):
        self.flag()
        with patch(IMPORT, side_effect=importer.ImportFailure("Official download failed.")) as download:
            with self.assertRaises(importer.ImportFailure), self.assertLogs("leg_platform.addresses.jobs", "ERROR"):
                schedule.run_daily(at(3, 0))
            self.assertEqual(schedule.run_daily(at(3, 15)), schedule.ALREADY_RAN)
        download.assert_called_once()
        job = self.job()
        self.assertEqual((job.state, job.last_error), (State.FAILED, "Official download failed."))
        self.assertIsNotNone(job.update_flag_set_at)
        # The next day it tries again.
        with patch(IMPORT, return_value=3):
            self.assertEqual(schedule.run_daily(at(3, 0, day=20)), schedule.IMPORTED)
        self.assertIsNone(self.job().update_flag_set_at)

    def test_a_flag_set_while_the_import_runs_stays_for_the_next_run(self):
        self.flag()

        def importing(**kwargs):
            ImportRequest.objects.filter(pk=1).update(update_flag_set_at=timezone.now() + timedelta(seconds=1))
            return 3

        with patch(IMPORT, side_effect=importing):
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.IMPORTED)
        self.assertIsNotNone(self.job().update_flag_set_at)

    # --- the same claim as the button ------------------------------------------------------------------------

    def test_a_running_or_queued_import_is_never_started_twice(self):
        self.flag()
        for state, extra in ((State.RUNNING, {"started_at": timezone.now(), "claim_token": uuid.uuid4()}),
                             (State.QUEUED, {})):
            with self.subTest(state=state):
                ImportRequest.objects.filter(pk=1).update(state=state, last_scheduled_run_on=None, **extra)
                with patch(IMPORT, return_value=3) as download:
                    if state == State.QUEUED:
                        # A queued request of the button is claimed by this run, as a manual one: one download.
                        self.assertEqual(schedule.run_daily(at(3, 0)), schedule.IMPORTED)
                        download.assert_called_once_with(repair=True, progress=ANY)
                    else:
                        self.assertEqual(schedule.run_daily(at(3, 0)), schedule.BUSY)
                        download.assert_not_called()
                        # The day is not used up: the next call (15 minutes later) tries again.
                        self.assertIsNone(self.job().last_scheduled_run_on)

    def test_the_run_and_the_button_share_one_claim(self):
        self.flag()
        self.assertTrue(jobs.request_import())
        manual = jobs.claim(web=True)
        self.assertIsNotNone(manual)
        with patch(IMPORT) as download:
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.BUSY)
        download.assert_not_called()
        # And the button is refused while the run holds the job.
        ImportRequest.objects.filter(pk=1).update(state=State.IDLE, claim_token=None, last_scheduled_run_on=None)
        claimed = jobs.claim(scheduled=True)
        self.assertIsNotNone(claimed)
        self.assertFalse(jobs.request_import())

    def test_a_dead_job_is_taken_over_and_its_lease_on_the_directory_is_freed(self):
        self.flag()
        now = timezone.now()
        ImportRequest.objects.filter(pk=1).update(
            state=State.RUNNING, started_at=now - timedelta(hours=1), claim_token=uuid.uuid4(),
            heartbeat_at=now - jobs.HEARTBEAT_STALE_AFTER - timedelta(seconds=5),
        )
        Directory.objects.filter(pk=1).update(import_token=uuid.uuid4(), import_lease_until=now + timedelta(hours=5))
        with patch(IMPORT, return_value=3):
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.IMPORTED)
        directory = Directory.objects.get(pk=1)
        self.assertEqual((directory.import_token, directory.import_lease_until), (None, None))

    # --- review findings 4 and 5 -----------------------------------------------------------------------------

    def test_a_file_without_the_required_columns_is_not_downloaded_again_for_a_week(self):
        self.flag()
        with patch(IMPORT, side_effect=importer.ImportFailure("Missing entrance column.")) as download:
            with self.assertRaises(importer.ImportFailure), self.assertLogs("leg_platform.addresses.jobs", "ERROR"):
                schedule.run_daily(at(3, 0))
        self.assertIsNotNone(self.job().columns_missing_at)
        self.assertEqual(self.job().last_error, "Missing entrance column.")
        # The flag stays set, the failure is recorded; now fix the clock of the failure to the day of the test.
        ImportRequest.objects.update(columns_missing_at=at(3, 0))
        with patch(IMPORT, return_value=3) as download:
            for day in (20, 21, 25):
                self.assertEqual(schedule.run_daily(at(3, 0, day=day)), schedule.BACKOFF)
            download.assert_not_called()
            # A week later it tries again (and a success clears the record).
            self.assertEqual(schedule.run_daily(at(4, 0, day=26)), schedule.IMPORTED)
        job = self.job()
        self.assertIsNone(job.columns_missing_at)
        self.assertIsNone(job.update_flag_set_at)

    def test_the_backoff_does_not_make_the_command_fail_and_the_button_is_not_held_back(self):
        ImportRequest.objects.update(columns_missing_at=at(3, 0), update_flag_set_at=at(3, 0))
        self.assertEqual(schedule.run_daily(at(5, 0, day=20)), schedule.BACKOFF)
        # The button (manual claim) still works at once and a success lifts the back-off.
        self.assertTrue(jobs.request_import())
        claimed = jobs.claim(web=True)
        self.assertIsNotNone(claimed)
        jobs.record_success(claimed, 5, downloaded=True)
        self.assertIsNone(self.job().columns_missing_at)

    def test_another_failure_does_not_start_the_back_off(self):
        self.flag()
        with patch(IMPORT, side_effect=importer.ImportFailure("Official download failed.")):
            with self.assertRaises(importer.ImportFailure), self.assertLogs("leg_platform.addresses.jobs", "ERROR"):
                schedule.run_daily(at(3, 0))
        self.assertIsNone(self.job().columns_missing_at)

    def test_the_column_failures_are_the_ones_that_back_off(self):
        self.assertEqual(
            jobs.COLUMN_FAILURES,
            {"Missing EGID column.", "Missing entrance column.", "Missing coordinate column."},
        )
        self.assertLessEqual(jobs.COLUMN_FAILURES, jobs.SAFE_FAILURES)
        self.assertEqual(schedule.COLUMNS_RETRY_INTERVAL, timedelta(days=7))

    def test_the_scheduled_run_holds_the_job_with_a_heartbeat_so_a_kill_is_noticed_after_ninety_seconds(self):
        self.flag()
        seen = {}

        def importing(**kwargs):
            job = self.job()
            seen["heartbeat"] = job.heartbeat_at
            seen["alive"] = jobs.is_alive(job)
            seen["blocked"] = jobs.request_import()
            # The process is killed: no more heartbeats. After the stale time the job is dead and can be requested.
            ImportRequest.objects.filter(pk=1).update(
                heartbeat_at=timezone.now() - jobs.HEARTBEAT_STALE_AFTER - timedelta(seconds=1),
            )
            seen["dead"] = not jobs.is_alive(self.job())
            return 3

        with patch(IMPORT, side_effect=importing):
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.IMPORTED)
        self.assertIsNotNone(seen["heartbeat"])
        self.assertTrue(seen["alive"])
        self.assertFalse(seen["blocked"])  # the button says "already running" only while it is alive
        self.assertTrue(seen["dead"])
        # The old six hours do not apply: the claim itself carries a heartbeat.
        self.assertIsNone(self.job().heartbeat_at)  # cleared by the normal end

    @override_settings(ADDRESS_IMPORT_AUTOMATIC=False)
    def test_it_does_nothing_when_the_automatic_refresh_is_turned_off(self):
        self.flag()
        with patch(IMPORT) as download:
            self.assertEqual(schedule.run_daily(at(3, 30)), schedule.DISABLED)
        download.assert_not_called()
        self.assertIsNotNone(self.job().update_flag_set_at)

    # --- the other reasons that make the directory due -------------------------------------------------------

    def test_a_directory_without_identifiers_is_due_without_a_flag(self):
        publish_directory(has_egid=False)
        with patch(IMPORT, return_value=3) as download:
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.IMPORTED)
        download.assert_called_once()

    def test_a_missing_directory_and_the_regular_refresh_are_due_as_before(self):
        # Real-time based: the run time is midnight, so every moment of the day is after it.
        schedule.set_import_run_time(time(0, 0))
        base = timezone.localtime()
        Directory.objects.all().delete()
        ImportRequest.objects.update(last_automatic_attempt_at=base - timedelta(hours=25))
        with patch(IMPORT, return_value=3) as download:
            self.assertEqual(schedule.run_daily(base), schedule.IMPORTED)
        download.assert_called_once()
        publish_directory()
        ImportRequest.objects.update(last_automatic_attempt_at=base - timedelta(days=88))
        with patch(IMPORT) as download:
            self.assertEqual(schedule.run_daily(base + timedelta(days=1)), schedule.NOT_DUE)
        download.assert_not_called()
        ImportRequest.objects.update(last_automatic_attempt_at=base - timedelta(days=91))
        with patch(IMPORT, return_value=3) as download:
            self.assertEqual(schedule.run_daily(base + timedelta(days=2)), schedule.IMPORTED)
        download.assert_called_once()


@tag("mariadb")
class FlagTests(TestCase):
    databases = {"default", "addresses"}

    def test_the_flag_is_set_once_and_keeps_its_time(self):
        self.assertTrue(jobs.set_update_flag())
        first = ImportRequest.objects.get(pk=1).update_flag_set_at
        self.assertIsNotNone(first)
        self.assertFalse(jobs.set_update_flag())
        self.assertEqual(ImportRequest.objects.get(pk=1).update_flag_set_at, first)

    def test_the_flag_holds_no_address_and_the_log_line_has_none(self):
        with self.assertLogs("leg_platform.addresses.jobs", "INFO") as logs:
            jobs.set_update_flag()
        self.assertEqual(logs.output, [
            "INFO:leg_platform.addresses.jobs:An address was not found; the update flag of the address directory is set."
        ])
        names = {field.name for field in ImportRequest._meta.get_fields()}
        self.assertFalse({"street", "address", "egid", "postal_code"} & names)

    def test_a_manual_import_clears_a_flag_that_was_set_before_it_started_and_a_failure_does_not(self):
        jobs.set_update_flag()
        jobs.request_import()
        claimed = jobs.claim(web=True)
        jobs.record_failure(claimed, importer.ImportFailure("Incomplete download."))
        self.assertIsNotNone(ImportRequest.objects.get(pk=1).update_flag_set_at)
        jobs.request_import()
        claimed = jobs.claim(web=True)
        jobs.record_success(claimed, 5, downloaded=True)
        self.assertIsNone(ImportRequest.objects.get(pk=1).update_flag_set_at)

    def test_a_flag_set_after_the_import_started_is_not_cleared_by_it(self):
        jobs.request_import()
        claimed = jobs.claim(web=True)
        jobs.set_update_flag()
        jobs.record_success(claimed, 5, downloaded=True)
        self.assertIsNotNone(ImportRequest.objects.get(pk=1).update_flag_set_at)

    def test_an_import_that_found_the_directory_up_to_date_also_clears_the_flag(self):
        jobs.set_update_flag()
        jobs.request_import()
        claimed = jobs.claim(web=True)
        jobs.record_success(claimed, 5, downloaded=False)
        self.assertIsNone(ImportRequest.objects.get(pk=1).update_flag_set_at)


@tag("mariadb")
@override_settings(ADDRESS_IMPORT_AUTOMATIC=True)
class HeartbeatThreadTests(TransactionTestCase):
    """The thread of the heartbeat really writes (its own connection, committed rows)."""

    databases = {"default", "addresses"}

    def setUp(self):
        # The plant register follows the address run (D-117); these tests never download it (see test_plants).
        self.enterContext(patch.object(schedule, "run_plant_daily", return_value=schedule.DISABLED))
        # The transformer station scan follows it on Saturdays (D-118); these tests never scan (see test_transformers).
        self.enterContext(patch.object(schedule, "run_transformer_weekly", return_value=schedule.NOT_SATURDAY))

    def test_the_heartbeat_moves_while_the_scheduled_import_runs(self):
        publish_directory()
        ImportRequest.objects.create(pk=1, update_flag_set_at=timezone.now())
        beats = []

        def importing(**kwargs):
            beats.append(ImportRequest.objects.get(pk=1).heartbeat_at)
            clock_time.sleep(0.6)
            beats.append(ImportRequest.objects.get(pk=1).heartbeat_at)
            return 3

        with patch.object(runner, "HEARTBEAT_INTERVAL", 0.05), patch(IMPORT, side_effect=importing):
            self.assertEqual(schedule.run_daily(at(3, 0)), schedule.IMPORTED)
        self.assertIsNotNone(beats[0])
        self.assertGreater(beats[1], beats[0])
        self.assertEqual(ImportRequest.objects.get(pk=1).state, State.SUCCEEDED)


@tag("mariadb")
@override_settings(ADDRESS_IMPORT_AUTOMATIC=True)
class ScheduledCommandTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        # run_daily runs the plant register after the addresses (D-117): these tests never download it.
        plant_run = patch.object(schedule, "run_plant_daily", return_value=schedule.DISABLED)
        plant_run.start()
        self.addCleanup(plant_run.stop)
        weekly_run = patch.object(schedule, "run_transformer_weekly", return_value=schedule.NOT_SATURDAY)
        weekly_run.start()
        self.addCleanup(weekly_run.stop)
        publish_directory()
        ImportRequest.objects.create(pk=1, last_automatic_attempt_at=timezone.now() - timedelta(days=1))

    def run_command(self):
        out = StringIO()
        call_command("scheduled_address_import", stdout=out)
        return out.getvalue().strip()

    def test_the_command_prints_one_fixed_sentence_and_runs_nothing_before_the_time(self):
        jobs.set_update_flag()
        with patch(IMPORT) as download, fixed_time(at(2, 0)):
            self.assertEqual(self.run_command(), "Address directory run: before the run time.")
        download.assert_not_called()

    def test_the_command_imports_when_it_is_due_and_reports_no_detail_on_a_failure(self):
        jobs.set_update_flag()
        with patch(IMPORT, return_value=3), fixed_time(at(4, 0)):
            self.assertEqual(self.run_command(), "Address directory run: imported.")
        ImportRequest.objects.update(last_scheduled_run_on=None)
        jobs.set_update_flag()
        with patch(IMPORT, side_effect=RuntimeError("Secret Street 12")), fixed_time(at(4, 0)), \
                self.assertLogs("leg_platform.addresses.jobs", "ERROR"):
            with self.assertRaises(CommandError) as raised:
                self.run_command()
        self.assertNotIn("Secret", str(raised.exception))
        self.assertEqual(ImportRequest.objects.get(pk=1).last_error, jobs.UNEXPECTED_FAILURE)


class TaskListTests(SimpleTestCase):
    def test_the_daily_run_and_the_cleanup_are_scheduled_tasks_and_the_import_is_last(self):
        tasks = run_scheduled_tasks.TASKS
        self.assertIn("cleanup_address_lookups", tasks)
        self.assertEqual(tasks[-1], "scheduled_address_import")
        self.assertLess(tasks.index("cleanup_address_lookups"), tasks.index("scheduled_address_import"))

    def test_the_unit_of_the_daily_run_is_documented(self):
        from pathlib import Path

        from django.conf import settings

        text = (Path(settings.BASE_DIR) / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
        self.assertIn("scheduled_address_import", text)
        self.assertIn("api3.geo.admin.ch", text)
