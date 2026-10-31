"""The durable import queue, the start endpoints, the status of three sources and the published figures (D-132).

Synthetic data only: nothing is downloaded, the importers are replaced by stand-ins."""

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.db import DatabaseError
from django.test import Client, TestCase, TransactionTestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses import jobs, published, runner, schedule, status
from leg_platform.addresses.models import Directory, ImportQueueEntry, ImportRequest, PlantRegister, PublishedFigures

Source = ImportRequest.Source
State = ImportRequest.State
PAGE_URL = reverse("admin:addresses_importrequest_changelist")


def start_url(source):
    return reverse("admin:addresses_importrequest_start", kwargs={"source": source})


def make_staff(email, *, permission=True):
    person = create_active_user(email)
    person.set_privileges(is_staff=True, is_superuser=False)
    if permission:
        person.user_permissions.add(Permission.objects.get(codename="request_directory_import"))
    client = VerifiedClient()
    client.force_login(person)
    return client, person


def english_status():
    with translation.override("en"):
        return status.build()


def queue_order():
    return list(ImportQueueEntry.objects.order_by("id").values_list("source", flat=True))


@tag("mariadb")
class QueueTests(TestCase):
    databases = {"default", "addresses"}

    def test_a_source_has_at_most_one_entry(self):
        self.assertEqual(jobs.enqueue([Source.PLANT]), [Source.PLANT])
        self.assertEqual(jobs.enqueue([Source.PLANT]), [])
        self.assertEqual(queue_order(), [Source.PLANT])

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            jobs.enqueue(["bogus"])
        with self.assertRaises(ValueError):
            jobs.enqueue([])
        self.assertEqual(queue_order(), [])

    def test_a_running_source_is_not_queued_again(self):
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=State.RUNNING, source=Source.PLANT, started_at=now, claim_token=uuid.uuid4(), heartbeat_at=now,
        )
        self.assertEqual(jobs.enqueue([Source.PLANT, Source.ADDRESS]), [Source.ADDRESS])

    def test_a_legacy_address_request_counts_as_the_waiting_address_entry(self):
        ImportRequest.objects.create(pk=1, state=State.QUEUED, requested_at=timezone.now())
        self.assertEqual(jobs.enqueue([Source.ADDRESS, Source.PLANT]), [Source.PLANT])
        self.assertEqual(jobs.queued_sources(), [Source.ADDRESS, Source.PLANT])

    def test_the_full_sync_puts_the_three_sources_in_the_fixed_order(self):
        jobs.enqueue([Source.TRANSFORMER])
        added = jobs.enqueue(jobs.QUEUE_SOURCES, in_order=True)
        self.assertEqual(added, [Source.ADDRESS, Source.PLANT, Source.TRANSFORMER])
        self.assertEqual(queue_order(), [Source.ADDRESS, Source.PLANT, Source.TRANSFORMER])

    def test_the_claim_takes_the_oldest_entry_as_a_manual_job_and_keeps_the_schedule(self):
        attempt = timezone.now() - timedelta(days=3)
        ImportRequest.objects.create(pk=1, last_automatic_attempt_at=attempt)
        jobs.enqueue([Source.PLANT, Source.TRANSFORMER])
        claimed = jobs.claim(web=True, queued=True)
        self.assertEqual((claimed.source, claimed.manual), (Source.PLANT, True))
        self.assertEqual(queue_order(), [Source.TRANSFORMER])
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual((job.state, job.source, job.is_automatic), (State.RUNNING, Source.PLANT, False))
        self.assertEqual(job.last_automatic_attempt_at, attempt)

    def test_only_one_job_is_claimed_at_a_time(self):
        jobs.enqueue([Source.ADDRESS, Source.PLANT])
        self.assertIsNotNone(jobs.claim(web=True, queued=True))
        self.assertIsNone(jobs.claim(web=True, queued=True))
        self.assertEqual(queue_order(), [Source.PLANT])

    def test_an_empty_queue_claims_nothing(self):
        self.assertIsNone(jobs.claim(web=True, queued=True))

    def test_a_legacy_address_request_runs_before_the_queue(self):
        ImportRequest.objects.create(pk=1, state=State.QUEUED, requested_at=timezone.now())
        jobs.enqueue([Source.PLANT])
        claimed = jobs.claim(web=True, queued=True)
        self.assertEqual(claimed.source, Source.ADDRESS)
        self.assertEqual(queue_order(), [Source.PLANT])

    def test_a_queue_left_by_a_restart_is_worked_off_by_the_scheduled_tasks_in_order(self):
        jobs.enqueue(jobs.QUEUE_SOURCES, in_order=True)
        seen = []

        def fake(claimed):
            seen.append(claimed.source)
            jobs.record_success(claimed, 1, transformer=SimpleNamespace(postal_codes=["8000"], buildings=1, with_name=0))
            if len(seen) == 1:
                raise RuntimeError("the first one failed")

        with patch.object(schedule, "_execute_with_heartbeat", side_effect=fake):
            ran = schedule.run_queue()
        self.assertEqual(ran, 3)
        self.assertEqual(seen, [Source.ADDRESS, Source.PLANT, Source.TRANSFORMER])
        self.assertEqual(queue_order(), [])


@tag("mariadb")
class QueueRunnerTests(TransactionTestCase):
    databases = {"default", "addresses"}

    def tearDown(self):
        self.assertTrue(runner.wait_for_end(30))

    def test_the_thread_runs_the_jobs_strictly_one_after_the_other_and_a_failure_does_not_stop_it(self):
        jobs.enqueue(jobs.QUEUE_SOURCES, in_order=True)
        seen = []

        def fake(claimed, progress=None):
            # The claim is the single writer lock: at this moment nobody else is running.
            self.assertEqual(ImportRequest.objects.get(pk=1).state, State.RUNNING)
            seen.append(claimed.source)
            if claimed.source == Source.PLANT:
                jobs.record_failure(claimed, RuntimeError("synthetic"))
                raise RuntimeError("synthetic")
            jobs.record_success(claimed, 1, transformer=SimpleNamespace(postal_codes=["8000"], buildings=1, with_name=0))

        with patch.object(jobs, "execute", side_effect=fake):
            self.assertTrue(runner.start_queue())
            self.assertTrue(runner.wait_for_end(30))
        self.assertEqual(seen, [Source.ADDRESS, Source.PLANT, Source.TRANSFORMER])
        self.assertEqual(queue_order(), [])
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual(job.last_plant_state, "failed")
        self.assertEqual(job.last_transformer_state, "succeeded")

    def test_start_queue_without_entries_starts_nothing(self):
        self.assertFalse(runner.start_queue())


@tag("mariadb")
class StartEndpointTests(TestCase):
    databases = {"default", "addresses"}

    def test_anonymous_is_sent_to_the_login_and_queues_nothing(self):
        response = Client().post(start_url("plant"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
        self.assertEqual(queue_order(), [])

    def test_an_authenticated_user_without_staff_status_queues_nothing(self):
        person = create_active_user("plain.person@example.org")
        client = Client()
        client.force_login(person)
        self.assertIn(client.post(start_url("plant")).status_code, (302, 403, 404))
        self.assertEqual(queue_order(), [])

    def test_staff_without_the_permission_is_forbidden(self):
        client, _person = make_staff("nopermission.person@example.org", permission=False)
        self.assertEqual(client.post(start_url("plant")).status_code, 403)
        self.assertEqual(client.get(start_url("plant")).status_code, 403)
        self.assertEqual(queue_order(), [])
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.IMPORT_REQUESTED).exists())

    def test_a_get_never_starts_anything(self):
        client, _person = make_staff("get.person@example.org")
        with patch.object(runner, "start_queue") as start:
            response = client.get(start_url("plant"))
        self.assertEqual(response.status_code, 405)
        start.assert_not_called()
        self.assertEqual(queue_order(), [])

    def test_the_page_accepts_get_only(self):
        client, _person = make_staff("page.person@example.org")
        self.assertEqual(client.get(PAGE_URL).status_code, 200)
        self.assertEqual(client.post(PAGE_URL).status_code, 405)

    def test_a_post_without_the_csrf_token_is_refused(self):
        client, _person = make_staff("csrf.person@example.org")
        strict = VerifiedClient(enforce_csrf_checks=True)
        strict.force_login(_person)
        with patch.object(runner, "start_queue") as start:
            response = strict.post(start_url("plant"))
        self.assertEqual(response.status_code, 403)
        start.assert_not_called()
        self.assertEqual(queue_order(), [])

    def test_an_unknown_source_is_a_404(self):
        client, _person = make_staff("unknown.person@example.org")
        with patch.object(runner, "start_queue") as start:
            self.assertEqual(client.post(start_url("bogus")).status_code, 404)
        start.assert_not_called()
        self.assertEqual(queue_order(), [])

    def test_a_start_queues_the_source_logs_one_security_event_and_starts_the_thread(self):
        client, person = make_staff("start.person@example.org")
        with patch.object(runner, "start_queue") as start, \
                self.assertLogs("leg_platform.security", level="WARNING") as logs, \
                self.captureOnCommitCallbacks(execute=True):
            response = client.post(start_url("transformer"))
        self.assertRedirects(response, PAGE_URL, fetch_redirect_response=False)
        start.assert_called_once()
        self.assertEqual(queue_order(), [Source.TRANSFORMER])
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.IMPORT_REQUESTED)
        self.assertEqual((event.actor_id, event.account_id), (person.pk, None))
        self.assertEqual(logs.output, [f"WARNING:leg_platform.security:A data import was requested (actor {person.pk}, transformer)."])

    def test_the_full_sync_queues_the_three_sources_in_order(self):
        client, _person = make_staff("full.person@example.org")
        with patch.object(runner, "start_queue"):
            client.post(start_url("all"))
        self.assertEqual(queue_order(), [Source.ADDRESS, Source.PLANT, Source.TRANSFORMER])
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.IMPORT_REQUESTED).count(), 1)

    def test_a_second_start_of_a_waiting_source_adds_no_entry(self):
        client, _person = make_staff("twice.person@example.org")
        with patch.object(runner, "start_queue"):
            client.post(start_url("plant"))
            response = client.post(start_url("plant"), follow=True)
        self.assertEqual(queue_order(), [Source.PLANT])
        self.assertContains(response, "läuft bereits oder wartet")

    def test_the_request_limit_applies(self):
        client, _person = make_staff("limit.person@example.org")
        with patch("leg_platform.addresses.admin.allow_request", return_value=False), \
                patch.object(runner, "start_queue") as start:
            response = client.post(start_url("plant"))
        self.assertEqual(response.status_code, 302)
        start.assert_not_called()
        self.assertEqual(queue_order(), [])

    def test_the_buttons_are_disabled_while_the_source_waits(self):
        client, _person = make_staff("buttons.person@example.org")
        jobs.enqueue([Source.PLANT])
        html = client.get(PAGE_URL).content.decode()
        plant_form = html.split(start_url("plant"))[1].split("</form>")[0]
        self.assertIn(" disabled", plant_form)
        self.assertIn('value="Wartet"', plant_form)
        address_form = html.split(start_url("address"))[1].split("</form>")[0]
        self.assertNotIn(" disabled", address_form)


@tag("mariadb")
class StatusPayloadTests(TestCase):
    databases = {"default", "addresses"}

    def running(self, source, **fields):
        now = timezone.now()
        return ImportRequest.objects.create(
            pk=1, state=State.RUNNING, source=source, started_at=now - timedelta(seconds=60), claim_token=uuid.uuid4(),
            heartbeat_at=now, **fields,
        )

    def test_three_sources_in_the_fixed_order_with_plain_names(self):
        page = english_status()
        self.assertEqual([row["key"] for row in page["sources"]], ["address", "plant", "transformer"])
        self.assertEqual(
            [row["name"] for row in page["sources"]], ["Address directory", "PV plants", "Transformer stations"]
        )
        self.assertTrue(all(row["state"] == "idle" for row in page["sources"]))
        self.assertFalse(page["active"])

    def test_a_waiting_source_has_its_position(self):
        jobs.enqueue(jobs.QUEUE_SOURCES, in_order=True)
        page = english_status()
        self.assertEqual([(row["state"], row["position"]) for row in page["sources"]],
                         [("queued", 1), ("queued", 2), ("queued", 3)])
        self.assertTrue(page["all_busy"])
        self.assertEqual(page["summary"], "0 running, 3 waiting")

    def test_the_percent_follows_the_phase_and_never_goes_back_across_phases(self):
        values = []
        for phase, fields in (
            ("starting", {}),
            ("downloading", {"bytes_done": 50, "bytes_total": 100}),
            ("downloading", {"bytes_done": 100, "bytes_total": 100}),
            ("importing", {"rows_done": 0, "rows_total": 100}),
            ("importing", {"rows_done": 100, "rows_total": 100}),
            ("publishing", {}),
            ("finishing", {}),
        ):
            job = ImportRequest(state=State.RUNNING, source=Source.ADDRESS, phase=phase, **fields)
            values.append(status.percent_of(job))
        self.assertEqual(values, sorted(values))
        self.assertEqual((values[0], values[-1]), (0, 98))

    def test_the_percent_is_none_when_nothing_runs(self):
        self.assertIsNone(status.percent_of(ImportRequest(state=State.SUCCEEDED)))
        self.assertIsNone(status.percent_of(None))

    def test_the_remaining_time_is_hidden_early_and_computed_later(self):
        now = timezone.now()
        job = ImportRequest(state=State.RUNNING, started_at=now - timedelta(seconds=2))
        self.assertIsNone(status.remaining_seconds(job, 50, now))  # too little time spent
        job.started_at = now - timedelta(seconds=60)
        self.assertIsNone(status.remaining_seconds(job, 1, now))  # too little progress
        self.assertIsNone(status.remaining_seconds(job, 100, now))
        self.assertEqual(status.remaining_seconds(job, 50, now), 60)

    def test_a_running_job_shows_percent_and_the_clock_data_without_an_estimate_label(self):
        self.running(Source.ADDRESS, phase="importing", rows_done=500, rows_total=1000)
        row = english_status()["sources"][0]
        self.assertEqual((row["state"], row["percent"]), ("running", 70))
        self.assertEqual(row["remaining_seconds"], 25)
        self.assertTrue(row["started_at"])

    def test_a_failure_shows_the_fixed_reason_and_a_success_shows_up_to_date(self):
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=State.SUCCEEDED, source=Source.PLANT, finished_at=now,
            last_address_state="failed", last_address_finished_at=now, last_address_error="Incomplete download.",
            last_plant_state="succeeded", last_plant_finished_at=now,
        )
        rows = {row["key"]: row for row in english_status()["sources"]}
        self.assertEqual(rows["address"]["state"], "failed")
        self.assertTrue(rows["address"]["error"])
        self.assertEqual((rows["plant"]["state"], rows["plant"]["state_label"]), ("succeeded", "Up to date"))
        self.assertEqual(rows["plant"]["error"], "")


@tag("mariadb")
class PublishedFiguresTests(TestCase):
    databases = {"default", "addresses"}

    def test_a_success_copies_the_figures_and_the_page_shows_them_while_the_import_runs(self):
        Directory.objects.create(
            pk=1, data_date=timezone.localdate(), imported_at=timezone.now(), row_count=2400000, checksum="a" * 64
        )
        published.refresh(Source.ADDRESS)
        self.assertEqual(published.get(Source.ADDRESS).rows, 2400000)
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=State.RUNNING, source=Source.ADDRESS, started_at=now, claim_token=uuid.uuid4(),
            heartbeat_at=now, phase="importing", rows_done=1000, rows_total=5000,
        )
        row = english_status()["sources"][0]
        figures = {figure["key"]: figure for figure in row["figures"]}
        self.assertEqual(figures["rows"]["published"], "2’400’000")
        self.assertEqual(figures["rows"]["pending"], "1’000")
        self.assertTrue(row["pending"])

    def test_a_read_that_fails_while_the_import_writes_keeps_the_published_figures(self):
        Directory.objects.create(
            pk=1, data_date=timezone.localdate(), imported_at=timezone.now(), row_count=77, checksum="a" * 64
        )
        published.refresh(Source.ADDRESS)
        with patch.object(published, "Directory") as broken:
            broken.objects.filter.side_effect = DatabaseError("database is locked")
            snapshot = published.get(Source.ADDRESS)
        self.assertEqual(snapshot.rows, 77)

    def test_a_status_build_survives_an_unreadable_directory_file(self):
        Directory.objects.create(
            pk=1, data_date=timezone.localdate(), imported_at=timezone.now(), row_count=77, checksum="a" * 64
        )
        published.refresh(Source.ADDRESS)
        with patch.object(published, "_from_file", return_value=None):
            row = english_status()["sources"][0]
        figures = {figure["key"]: figure for figure in row["figures"]}
        self.assertEqual(figures["rows"]["published"], "77")
        self.assertNotEqual(figures["data_date"]["published"], "–")

    def test_nothing_published_shows_a_dash_and_the_plant_figures_include_the_rejects(self):
        row = english_status()["sources"][1]
        self.assertEqual({figure["key"] for figure in row["figures"]},
                         {"data_date", "imported_at", "rows", "rejected", "skipped"})
        self.assertTrue(all(figure["published"] == "–" for figure in row["figures"]))
        PlantRegister.objects.create(
            pk=1, data_date=timezone.localdate(), imported_at=timezone.now(), row_count=9, rejected_count=2,
            skipped_count=1, checksum="b" * 64,
        )
        figures = {f["key"]: f for f in english_status()["sources"][1]["figures"]}
        self.assertEqual((figures["rows"]["published"], figures["rejected"]["published"]), ("9", "2"))

    def test_reading_the_figures_writes_nothing(self):
        Directory.objects.create(
            pk=1, data_date=timezone.localdate(), imported_at=timezone.now(), row_count=5, checksum="a" * 64
        )
        self.assertEqual(published.get(Source.ADDRESS).rows, 5)
        english_status()
        self.assertFalse(PublishedFigures.objects.exists())
        with patch.object(PublishedFigures.objects, "update_or_create", side_effect=AssertionError("write")):
            self.assertEqual(published.get(Source.ADDRESS).rows, 5)
            english_status()

    def test_the_clear_after_a_rebuild_drops_the_snapshots(self):
        Directory.objects.create(
            pk=1, data_date=timezone.localdate(), imported_at=timezone.now(), row_count=5, checksum="a" * 64
        )
        published.refresh(Source.ADDRESS)
        published.clear()
        self.assertFalse(PublishedFigures.objects.exists())


class ScriptAssetTests(TestCase):
    """The script keeps the rules of the page (no browser here: the rules are checked in the source)."""

    def source(self):
        from pathlib import Path

        from django.conf import settings

        return (Path(settings.BASE_DIR) / "leg_platform" / "static" / "js" / "import-status.js").read_text(encoding="utf-8")

    def test_the_script_never_writes_markup_and_never_blanks_a_published_value(self):
        script = self.source()
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(forbidden, script)
        self.assertIn("keepFilled(row.querySelector(\"[data-published]\")", script)
        self.assertNotIn("estimate", script.lower())
        for dead in ("directory-imported-at", "directory-file-size", "import-leave"):
            self.assertNotIn(dead, script)

    def test_the_script_formats_the_remaining_time_as_minutes_and_seconds(self):
        self.assertIn("function clock(", self.source())
