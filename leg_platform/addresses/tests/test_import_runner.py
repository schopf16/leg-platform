"""The import of the address directory in a thread of the web process (D-090): the claim in the database,
the heartbeat, the outcome in the row, the logs and the security log, the status page and its JSON.

The thread uses its own database connections, so the tests that run it are transaction tests."""

import ast
import threading
import uuid
from datetime import time, timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone, translation

from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses import importer, jobs, runner, status, storage
from leg_platform.addresses.models import Directory, ImportRequest
from leg_platform.addresses.progress import Progress

State = ImportRequest.State
STATUS_URL = reverse("admin:addresses_importrequest_status")
PAGE_URL = reverse("admin:addresses_importrequest_changelist")


def english_build():
    """The status in English: the texts of these tests are the source texts (the German ones are tested through the pages)."""
    with translation.override("en"):
        return status.build()


def make_staff(email, *, permission=True):
    person = create_active_user(email)
    person.set_privileges(is_staff=True, is_superuser=False)
    if permission:
        person.user_permissions.add(Permission.objects.get(codename="request_directory_import"))
    client = VerifiedClient()
    client.force_login(person)
    return client, person


class FailureTextTests(SimpleTestCase):
    """Only the fixed sentences of the importer can reach the row, the logs and the page."""

    def importer_messages(self):
        tree = ast.parse(Path(importer.__file__).read_text(encoding="utf-8"))
        messages = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ImportFailure"
                and node.args and isinstance(node.args[0], ast.Constant)
            ):
                messages.add(node.args[0].value)
        return messages

    def test_every_sentence_of_the_importer_is_allowed_and_has_words_for_the_page(self):
        messages = self.importer_messages()
        self.assertGreater(len(messages), 20)
        self.assertEqual(messages, set(jobs.SAFE_FAILURES))
        self.assertEqual(
            set(status.REASONS),
            set(jobs.SAFE_FAILURES) | set(jobs.PLANT_FAILURES) | {jobs.UNEXPECTED_FAILURE, jobs.INTERRUPTED},
        )

    def test_every_reason_has_a_swiss_german_text_without_the_sharp_s(self):
        with translation.override("de-ch"):
            for message in status.REASONS:
                with self.subTest(message=message):
                    text = status.reason_text(message)
                    self.assertTrue(text)
                    self.assertNotIn("\u00df", text)
                    # Translated: the German text differs from the English source.
                    self.assertNotEqual(text, message)

    def test_an_unknown_text_is_shown_as_unexpected(self):
        self.assertEqual(status.reason_text("secret upstream detail"), str(status.REASONS[jobs.UNEXPECTED_FAILURE]))
        self.assertEqual(status.reason_text(""), "")


@tag("mariadb")
class ClaimTests(TestCase):
    databases = {"default", "addresses"}

    def test_a_web_claim_writes_a_heartbeat_and_a_manual_one_repairs(self):
        self.assertTrue(jobs.request_import())
        claimed = jobs.claim(web=True)
        job = ImportRequest.objects.get(pk=1)
        self.assertTrue(claimed.manual)
        self.assertFalse(claimed.took_over)
        self.assertEqual((job.state, job.claim_token, job.phase), (State.RUNNING, claimed.token, "starting"))
        self.assertIsNotNone(job.heartbeat_at)

    def test_a_live_import_is_not_claimed_again_and_a_click_is_coalesced(self):
        jobs.request_import()
        self.assertIsNotNone(jobs.claim(web=True))
        self.assertIsNone(jobs.claim(web=True))
        self.assertFalse(jobs.request_import())

    def test_a_job_without_a_heartbeat_for_too_long_is_dead_and_the_next_claim_takes_over(self):
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=State.RUNNING, started_at=now - timedelta(minutes=10), claim_token=uuid.uuid4(),
            heartbeat_at=now - jobs.HEARTBEAT_STALE_AFTER - timedelta(seconds=1),
        )
        self.assertFalse(jobs.is_alive(ImportRequest.objects.get(pk=1)))
        self.assertEqual(status.effective_state(ImportRequest.objects.get(pk=1)), "interrupted")
        self.assertTrue(jobs.request_import())
        claimed = jobs.claim(web=True)
        self.assertTrue(claimed.took_over)

    def test_a_fresh_heartbeat_keeps_the_job_alive(self):
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=State.RUNNING, started_at=now - timedelta(hours=2), claim_token=uuid.uuid4(),
            heartbeat_at=now - jobs.HEARTBEAT_STALE_AFTER + timedelta(seconds=5),
        )
        self.assertTrue(jobs.is_alive(ImportRequest.objects.get(pk=1)))
        self.assertFalse(jobs.request_import())

    def test_a_worker_job_without_a_heartbeat_keeps_its_lease_of_six_hours(self):
        now = timezone.now()
        ImportRequest.objects.create(pk=1, state=State.RUNNING, started_at=now - timedelta(hours=5))
        self.assertTrue(jobs.is_alive(ImportRequest.objects.get(pk=1)))
        ImportRequest.objects.filter(pk=1).update(started_at=now - timedelta(hours=7))
        self.assertFalse(jobs.is_alive(ImportRequest.objects.get(pk=1)))

    def test_a_thread_whose_job_was_taken_over_stops_at_the_next_phase(self):
        # L-1: an old thread checks its claim at every phase, so it does not download and parse for nothing.
        jobs.request_import()
        old = jobs.claim(web=True)
        progress = Progress(check=runner._claim_check(old.token))
        progress.phase("catalogue")
        self.assertEqual(progress.current_phase, "catalogue")
        ImportRequest.objects.filter(pk=1).update(claim_token=uuid.uuid4())
        with self.assertRaisesMessage(importer.ImportFailure, "The import lease was lost."):
            progress.phase("downloading")
        self.assertEqual(progress.current_phase, "catalogue")  # nothing was announced for the lost job
        # A job that ended is no longer held either; a progress without a check (the worker) never asks.
        ImportRequest.objects.filter(pk=1).update(claim_token=old.token, state=State.FAILED)
        with self.assertRaises(importer.ImportFailure):
            runner._claim_check(old.token)()
        Progress().phase("downloading")

    def test_a_failure_of_a_job_that_was_taken_over_does_not_touch_the_new_holder(self):
        jobs.request_import()
        old = jobs.claim(web=True)
        ImportRequest.objects.filter(pk=1).update(claim_token=uuid.uuid4())
        with self.assertLogs("leg_platform.addresses.jobs", "WARNING"):
            jobs.record_failure(old, importer.ImportFailure("Incomplete download."))
        self.assertEqual(ImportRequest.objects.get(pk=1).state, State.RUNNING)

    def test_the_stored_failure_is_the_fixed_sentence_only(self):
        jobs.request_import()
        claimed = jobs.claim(web=True)
        with self.assertLogs("leg_platform.addresses.jobs", "ERROR") as logs:
            jobs.record_failure(claimed, importer.ImportFailure("Official download failed."))
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual((job.state, job.last_error, job.heartbeat_at), (State.FAILED, "Official download failed.", None))
        self.assertEqual(
            logs.output, ["ERROR:leg_platform.addresses.jobs:Address directory import failed (manual): Official download failed."]
        )


@tag("mariadb")
class RunnerTests(TransactionTestCase):
    """The thread, with a stand-in for the importer (nothing is downloaded)."""

    databases = {"default", "addresses"}

    def tearDown(self):
        self.assertTrue(runner.wait_for_end(30))

    def start_and_wait(self):
        self.assertTrue(runner.start_manual())
        self.assertTrue(runner.wait_for_end(30))
        return ImportRequest.objects.get(pk=1)

    def test_a_successful_import_is_recorded_logged_and_written_to_the_security_log(self):
        def fake(*, repair=False, progress=None):
            progress.phase("downloading")
            progress.download(50, 100)
            progress.downloaded = True
            progress.rows(1234)
            return 1234

        with patch("leg_platform.addresses.importer.import_directory", side_effect=fake) as imported, \
                self.assertLogs("leg_platform.addresses.jobs", "INFO") as logs:
            job = self.start_and_wait()
        imported.assert_called_once()
        self.assertTrue(imported.call_args.kwargs["repair"])
        self.assertEqual((job.state, job.phase, job.last_error, job.rows_done), (State.SUCCEEDED, "", "", 1234))
        self.assertIsNone(job.heartbeat_at)
        self.assertIsNotNone(job.finished_at)
        self.assertIn("Address directory import completed (manual): 1234 addresses.", logs.output[0])
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.ADDRESS_IMPORT_SUCCEEDED)
        self.assertEqual((event.actor_id, event.account_id), (None, None))

    def test_an_import_that_found_the_directory_current_says_so(self):
        with patch("leg_platform.addresses.importer.import_directory", return_value=5):
            job = self.start_and_wait()
        self.assertEqual((job.state, job.phase), (State.SUCCEEDED, "unchanged"))
        self.assertIn("already up to date", english_build()["message"])

    def test_a_failure_is_visible_with_its_reason_in_the_log_and_the_security_log(self):
        with patch(
            "leg_platform.addresses.importer.import_directory",
            side_effect=importer.ImportFailure("Not enough free disk space for an import."),
        ), self.assertLogs("leg_platform.addresses.jobs", "ERROR") as logs:
            job = self.start_and_wait()
        self.assertEqual(
            (job.state, job.last_error), (State.FAILED, "Not enough free disk space for an import.")
        )
        self.assertIn("Not enough free disk space", logs.output[0])
        self.assertTrue(SecurityEvent.objects.filter(event=SecurityEvent.Event.ADDRESS_IMPORT_FAILED).exists())
        page = english_build()
        self.assertEqual((page["state"], page["active"]), ("failed", False))
        self.assertIn("4 GB", page["error"])

    def test_an_unexpected_error_is_logged_with_its_trace_and_shown_without_detail(self):
        with patch("leg_platform.addresses.importer.import_directory", side_effect=ValueError("secret detail")), \
                self.assertLogs("leg_platform.addresses", "ERROR") as logs:
            job = self.start_and_wait()
        self.assertEqual((job.state, job.last_error), (State.FAILED, jobs.UNEXPECTED_FAILURE))
        self.assertNotIn("secret detail", job.last_error)
        self.assertTrue(any("ended with an error" in line for line in logs.output))
        self.assertNotIn("secret detail", english_build()["error"])

    def test_only_one_import_runs_and_a_second_request_is_refused(self):
        release = threading.Event()
        entered = threading.Event()

        def slow(*, repair=False, progress=None):
            entered.set()
            release.wait(20)
            return 1

        with patch("leg_platform.addresses.importer.import_directory", side_effect=slow):
            self.assertTrue(runner.start_manual())
            self.assertTrue(entered.wait(20))
            self.assertFalse(runner.start_manual())
            self.assertTrue(status.build()["active"])
            release.set()
            self.assertTrue(runner.wait_for_end(30))
        self.assertEqual(ImportRequest.objects.get(pk=1).state, State.SUCCEEDED)

    def test_the_heartbeat_writes_the_progress_while_the_import_runs(self):
        release = threading.Event()
        entered = threading.Event()

        def slow(*, repair=False, progress=None):
            progress.phase("downloading")
            progress.download(25 * 1024 * 1024, 100 * 1024 * 1024)
            entered.set()
            release.wait(20)
            return 1

        with patch("leg_platform.addresses.importer.import_directory", side_effect=slow), \
                patch.object(runner, "HEARTBEAT_INTERVAL", 0.05):
            self.assertTrue(runner.start_manual())
            self.assertTrue(entered.wait(20))
            for _ in range(200):
                job = ImportRequest.objects.get(pk=1)
                if job.phase == "downloading":
                    break
                threading.Event().wait(0.05)
            page = english_build()
            release.set()
            self.assertTrue(runner.wait_for_end(30))
        self.assertEqual((job.phase, job.bytes_done, job.bytes_total), ("downloading", 25 * 1024 * 1024, 100 * 1024 * 1024))
        self.assertEqual((page["state"], page["percent"]), ("running", 25))
        self.assertIn("25 of 100 MB downloaded", page["detail"])

    def test_only_the_import_thread_may_write_the_directory_file(self):
        seen = {}

        def fake(*, repair=False, progress=None):
            seen["thread"] = storage.is_writer()
            return 1

        with patch.object(storage, "_writer", False), \
                patch("leg_platform.addresses.importer.import_directory", side_effect=fake):
            self.assertFalse(storage.is_writer())
            self.start_and_wait()
            # The request threads of the process stay read-only; the flag of the thread is gone again.
            self.assertFalse(storage.is_writer())
        self.assertTrue(seen["thread"])

    def test_the_lease_of_a_dead_import_is_freed_when_a_new_one_takes_over(self):
        now = timezone.now()
        Directory.objects.create(pk=1, import_token=uuid.uuid4(), import_lease_until=now + timedelta(hours=5))
        ImportRequest.objects.create(
            pk=1, state=State.RUNNING, started_at=now - timedelta(hours=1), claim_token=uuid.uuid4(),
            heartbeat_at=now - timedelta(minutes=10),
        )
        seen = {}

        def fake(*, repair=False, progress=None):
            seen["lease"] = Directory.objects.get(pk=1).import_token
            return 1

        with patch("leg_platform.addresses.importer.import_directory", side_effect=fake):
            self.start_and_wait()
        self.assertIsNone(seen["lease"])

    def test_the_lease_of_a_live_holder_is_not_freed(self):
        # A second import started by the operator command stays refused by the lease on the directory.
        now = timezone.now()
        token = uuid.uuid4()
        Directory.objects.create(pk=1, import_token=token, import_lease_until=now + timedelta(hours=5))
        with patch("leg_platform.addresses.importer.import_directory", return_value=1):
            self.start_and_wait()
        self.assertEqual(Directory.objects.get(pk=1).import_token, token)

    def test_a_thread_that_cannot_start_marks_the_job_failed(self):
        with patch("leg_platform.addresses.runner.threading.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("can't start new thread")
            self.assertFalse(runner.start_manual())
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual((job.state, job.last_error), (State.FAILED, jobs.UNEXPECTED_FAILURE))


@tag("mariadb")
class AutomaticRefreshTextTests(TestCase):
    """The refresh is the daily run of the scheduled tasks (KONZEPT 3a); the page says so."""

    databases = {"default", "addresses"}

    @override_settings(ADDRESS_IMPORT_AUTOMATIC=True)
    def test_the_text_of_the_page_names_the_trigger_and_the_time(self):
        with translation.override("de-ch"):
            text = status.next_automatic_text(None)
        self.assertIn("geplanten Aufgaben", text)
        self.assertIn("ab 03:00", text)
        self.assertNotIn("niemand das Portal benutzt", text)

    @override_settings(ADDRESS_IMPORT_AUTOMATIC=True)
    def test_the_text_follows_the_configured_time_and_the_flag(self):
        ImportRequest.objects.create(pk=1, run_time=time(4, 30), update_flag_set_at=timezone.now())
        with translation.override("en"):
            text = status.next_automatic_text(ImportRequest.objects.get(pk=1))
        self.assertIn("from 04:30", text)
        self.assertIn("An address could not be found", text)

    @override_settings(ADDRESS_IMPORT_AUTOMATIC=False)
    def test_the_text_of_the_page_says_when_it_is_turned_off(self):
        with translation.override("en"):
            self.assertIn("turned off", status.next_automatic_text(None))

    def test_the_first_visit_of_a_staff_member_starts_nothing_any_more(self):
        # The old trigger (D-090: the first visit after the due time) is replaced by the daily run.
        self.assertFalse(hasattr(runner, "maybe_start_automatic"))
        client, person = make_staff("visit.person@example.org")
        person.set_privileges(is_staff=True, is_superuser=True)
        with override_settings(ADDRESS_IMPORT_AUTOMATIC=True), patch.object(runner, "_start") as start, \
                patch.object(jobs, "claim") as claim:
            self.assertEqual(client.get(reverse("admin:index")).status_code, 200)
            client.get(reverse("management:overview"))
        start.assert_not_called()
        claim.assert_not_called()


@tag("mariadb")
class StatusEndpointTests(TestCase):
    databases = {"default", "addresses"}

    def test_anonymous_is_sent_to_the_login_and_gets_no_data(self):
        response = Client().get(STATUS_URL)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b"state", response.content)

    def test_without_the_right_it_is_forbidden_for_staff(self):
        client, _person = make_staff("nobody.person@example.org", permission=False)
        self.assertEqual(client.get(STATUS_URL).status_code, 403)

    def test_an_ordinary_user_has_no_access(self):
        person = create_active_user("ordinary.person@example.org")
        client = Client()
        client.force_login(person)
        self.assertIn(client.get(STATUS_URL).status_code, (302, 403, 404))

    def test_the_answer_is_json_never_cached_and_get_only(self):
        client, _person = make_staff("status.person@example.org")
        response = client.get(STATUS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("no-store", response["Cache-Control"])
        body = response.json()
        for key in ("state", "state_label", "active", "message", "percent", "error", "directory", "automatic_text"):
            self.assertIn(key, body)
        self.assertEqual(body["state"], "idle")
        self.assertEqual(client.post(STATUS_URL).status_code, 405)
        self.assertEqual(client.delete(STATUS_URL).status_code, 405)

    def test_a_post_to_the_status_address_cannot_start_an_import(self):
        client, _person = make_staff("status.two@example.org")
        with patch.object(runner, "_start") as start:
            client.post(STATUS_URL)
        start.assert_not_called()
        self.assertFalse(ImportRequest.objects.exists())

    def test_the_answer_shows_the_counts_and_the_file_of_the_directory(self):
        client, _person = make_staff("status.three@example.org")
        Directory.objects.create(
            pk=1, data_date=timezone.localdate(), imported_at=timezone.now(), row_count=2400000, checksum="a" * 64
        )
        body = client.get(STATUS_URL).json()
        # The count is the Swiss text of count_text (DESIGN section 8), the same as in the page.
        self.assertEqual(body["directory"]["row_count"], "2’400’000")
        self.assertTrue(body["directory"]["available"])

    def test_a_running_job_without_a_heartbeat_is_reported_as_interrupted_with_a_reason(self):
        client, _person = make_staff("status.four@example.org")
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=State.RUNNING, started_at=now - timedelta(hours=1), claim_token=uuid.uuid4(),
            heartbeat_at=now - timedelta(minutes=5),
        )
        body = client.get(STATUS_URL).json()
        self.assertEqual((body["state"], body["active"]), ("interrupted", False))
        self.assertIn("unterbrochen", body["error"])

    def test_the_page_shows_the_status_and_the_polling_script_and_not_the_old_sentence(self):
        client, _person = make_staff("status.five@example.org")
        response = client.get(PAGE_URL)
        self.assertContains(response, 'data-status-url="%s"' % STATUS_URL)
        self.assertContains(response, "js/import-status.js")
        self.assertContains(response, "Eine Anforderung auf dieser Seite läuft im Portal selbst")
        self.assertNotContains(response, "separaten Hintergrundprozess")
        self.assertNotContains(response, "Hintergrund-Worker")
        # No inline script or handler on the page (Content Security Policy).
        html = response.content.decode()
        self.assertNotIn("<script>", html)
        self.assertNotIn(" onclick=", html)

    def test_the_page_uses_the_admin_button_an_alert_for_failures_numeric_dates_and_no_worker_text(self):
        client, _person = make_staff("status.seven@example.org")
        idle = client.get(PAGE_URL).content.decode()
        # UX-003, UX-013: the filled admin button; the idle state does not repeat itself; no "Import-Worker".
        self.assertIn('<input type="submit" class="default" id="import-start"', idle)
        self.assertIn('value="Aktualisierung des Verzeichnisses anfordern"', idle)
        self.assertNotIn('<button type="submit" id="import-start"', idle)
        self.assertNotIn("Es wurde noch kein Import angefordert", idle)
        self.assertNotIn("Import-Worker", idle)
        self.assertIn("Sie können diese Seite verlassen", idle)
        self.assertIn('id="import-leave" hidden>', idle)  # only while an import is queued or running
        # UX-006: the reason of a failure is an alert.
        from datetime import datetime

        failed_at = timezone.make_aware(datetime(2026, 10, 17, 2, 44))
        ImportRequest.objects.create(
            pk=1, state=State.FAILED, last_error="Incomplete download.", requested_at=failed_at, finished_at=failed_at,
        )
        failed = client.get(PAGE_URL).content.decode()
        self.assertIn('id="import-error" role="alert"', failed)
        self.assertIn('value="Erneut versuchen"', failed)  # UX-013
        self.assertIn("17.10.2026 02:44", failed)  # DESIGN section 8: numeric dates
        self.assertNotIn("Oktober", failed)
        self.assertIn('id="import-leave" hidden>', failed)

    def test_an_interrupted_import_does_not_say_the_same_sentence_twice(self):
        client, _person = make_staff("status.eight@example.org")
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=State.RUNNING, started_at=now - timedelta(hours=1), claim_token=uuid.uuid4(),
            heartbeat_at=now - timedelta(minutes=5),
        )
        body = client.get(STATUS_URL).json()
        self.assertEqual(body["message"], "Der Import wurde nicht abgeschlossen.")
        self.assertIn("unterbrochen", body["error"])
        self.assertNotEqual(body["message"], body["error"])

    def test_the_stylesheet_has_no_motion_under_reduced_motion_and_44_px_targets_on_touch(self):
        from pathlib import Path

        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "leg_platform" / "static" / "css" / "admin-portal.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"@media \(prefers-reduced-motion: reduce\) \{[^{}]*\{[^{}]*animation: none;")  # UX-006
        # The block that holds the buttons of the import and the logs: found by its content, not by its place (the
        # file has more than one media query for touch screens, e.g. the links of the header, D-113).
        touch_blocks = [
            block for block in css.split("@media (pointer: coarse), (max-width: 767px)")[1:]
            if ".import-form input[type=\"submit\"]" in block.split("\n}\n", 1)[0]
        ]
        self.assertEqual(len(touch_blocks), 1, "exactly one touch block holds the buttons of the import and the logs")
        touch = touch_blocks[0].split("\n}\n", 1)[0]
        for needle in (".import-form input[type=\"submit\"]", ".log-controls .button", ".log-files a", "min-height: 44px"):
            self.assertIn(needle, touch, needle)  # UX-003
        self.assertIn(".log-files thead", css)  # UX-002: the phone shows each file as a block
        self.assertIn("align-items: baseline;", css)  # UX-015

    def test_the_page_of_a_failed_import_names_the_reason(self):
        client, _person = make_staff("status.six@example.org")
        ImportRequest.objects.create(
            pk=1, state=State.FAILED, last_error="Official archive checksum mismatch.",
            requested_at=timezone.now(), finished_at=timezone.now(),
        )
        response = client.get(PAGE_URL)
        self.assertContains(response, "Fehlgeschlagen")
        self.assertContains(response, "Prüfsumme")

    def test_the_polling_endpoint_is_not_an_activity_of_the_session(self):
        from leg_platform.accounts import session_timeout

        self.assertIn("admin:addresses_importrequest_status", session_timeout.PASSIVE_VIEWS)


class ProgressTests(SimpleTestCase):
    def test_the_progress_is_plain_data(self):
        progress = Progress()
        progress.phase("downloading")
        progress.download(10, 20)
        progress.rows(5)
        self.assertEqual(
            (progress.current_phase, progress.bytes_done, progress.bytes_total, progress.rows_done),
            ("downloading", 10, 20, 5),
        )
        self.assertFalse(progress.downloaded)
