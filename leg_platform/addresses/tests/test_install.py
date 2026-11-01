"""Installing a prebuilt address directory file that was uploaded to the server (D-134).

Synthetic data only: the uploads are small files built with the importer from a synthetic table. What is proven: a good upload
replaces the file, clears the import that setup queued and shows its figures; every refusal leaves the old file as it was
(checksum, place, kind, journal, live import, housekeeping, reset marker); an upload that fails the checks is removed and the old
file is put back; the command reports the refusals as errors without a trace."""

import contextlib
import hashlib
import io
import os
import tempfile
import types
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock, skipUnless

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connections
from django.test import TransactionTestCase, tag
from django.utils import timezone

from leg_platform.addresses import importer, install, jobs, storage
from leg_platform.addresses.models import (
    DIRECTORY_ALIAS, Directory, HousekeepingStatus, ImportLogEntry, ImportQueueEntry, ImportRequest,
    PublishedFigures, TransformerStation,
)
from leg_platform.addresses.tests.test_directory import HEADER, archive_bytes

Source = ImportRequest.Source
State = ImportRequest.State
OLD_CSV = HEADER + "Old Street;1;9999 Old Town;x;x\n"
NEW_CSV = HEADER + "New Street;1;9999 New Town;x;x\nNew Street;2;9999 New Town;x;x\nOther Road;4;9998 Other Place;x;x\n"
OLD_CHECKSUM = "a" * 64
NEW_CHECKSUM = "b" * 64


@contextlib.contextmanager
def directory_file(path):
    """Point the alias of the address directory at ``path`` (a file that may not exist yet)."""
    alias = connections[DIRECTORY_ALIAS]
    alias.close()
    with mock.patch.dict(alias.settings_dict, {"NAME": str(path)}):
        try:
            yield alias
        finally:
            alias.close()


@tag("mariadb")
class InstallTests(TransactionTestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.target = self.folder / "address-directory.sqlite3"
        self.upload = self.folder / "address-directory.sqlite3.upload"
        disk = mock.patch.object(
            importer.shutil, "disk_usage",
            return_value=types.SimpleNamespace(total=10**12, used=0, free=importer.MIN_FREE_DISK_BYTES * 10),
        )
        disk.start()
        self.addCleanup(disk.stop)
        self.addCleanup(connections[DIRECTORY_ALIAS].close)

    def build(self, path, csv, checksum):
        """A directory file with one published generation of ``csv``."""
        with directory_file(path):
            call_command("migrate", "addresses", database=DIRECTORY_ALIAS, interactive=False, verbosity=0)
            Directory.objects.create(pk=1)
            importer._publish(io.BytesIO(archive_bytes(csv)), checksum, timezone.localdate())

    def build_old_target(self):
        self.build(self.target, OLD_CSV, OLD_CHECKSUM)
        self.addCleanup(connections[DIRECTORY_ALIAS].close)

    def build_upload(self, csv=NEW_CSV, checksum=NEW_CHECKSUM):
        self.build(self.upload, csv, checksum)

    def run_install(self, **options):
        with directory_file(self.target):
            return install.install(self.upload, **options)

    def published(self):
        with directory_file(self.target):
            directory = Directory.objects.get(pk=1)
            return directory.checksum, directory.row_count

    def assertTargetIsTheOldFile(self):
        self.assertEqual(self.published(), (OLD_CHECKSUM, 1))
        self.assertFalse(storage.reset_marker(self.target).exists())

    def test_a_good_upload_replaces_the_file_and_leaves_nothing_behind(self):
        self.build_old_target()
        self.build_upload()

        directory = self.run_install(sha256=hashlib.sha256(self.upload.read_bytes()).hexdigest())

        self.assertEqual(directory.row_count, 3)
        self.assertEqual(self.published(), (NEW_CHECKSUM, 3))
        self.assertEqual(sorted(path.name for path in self.folder.iterdir() if not path.name.endswith(("-wal", "-shm"))),
                         ["address-directory.sqlite3", "address-directory.sqlite3.open-lock"])
        if os.name == "posix":
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_a_first_installation_needs_no_old_file(self):
        self.build_upload()

        self.run_install()

        self.assertEqual(self.published(), (NEW_CHECKSUM, 3))

    def test_the_import_that_setup_queued_is_not_needed_any_more_and_the_page_shows_the_new_figures(self):
        self.build_old_target()
        self.build_upload()
        with directory_file(self.target):
            jobs.request_import()
            jobs.enqueue([Source.PLANT, Source.TRANSFORMER])

        self.run_install()

        job = ImportRequest.objects.get(pk=1)
        self.assertEqual((job.state, job.source, job.last_address_state), (State.SUCCEEDED, Source.ADDRESS, "succeeded"))
        self.assertIsNotNone(job.last_automatic_attempt_at)
        # The synthetic file holds no plants and no stations: their entries stay.
        self.assertEqual(list(ImportQueueEntry.objects.values_list("source", flat=True)), [Source.PLANT, Source.TRANSFORMER])
        self.assertEqual(PublishedFigures.objects.get(source=Source.ADDRESS).rows, 3)

    def test_a_wrong_checksum_changes_nothing(self):
        self.build_old_target()
        self.build_upload()

        with self.assertRaisesMessage(install.InstallError, "checksum"):
            self.run_install(sha256="0" * 64)

        self.assertTargetIsTheOldFile()
        self.assertTrue(self.upload.exists())

    def test_an_upload_in_another_folder_or_with_the_name_of_the_file_is_refused(self):
        self.build_old_target()
        with tempfile.TemporaryDirectory() as other:
            elsewhere = Path(other) / "upload.sqlite3"
            elsewhere.write_bytes(b"x")
            with directory_file(self.target), self.assertRaisesMessage(install.InstallError, "folder"):
                install.install(elsewhere)
        with directory_file(self.target), self.assertRaisesMessage(install.InstallError, "another name"):
            install.install(self.target)
        with directory_file(self.target), self.assertRaisesMessage(install.InstallError, "absolute"):
            install.install("upload.sqlite3")
        self.assertTargetIsTheOldFile()

    def test_a_missing_upload_is_refused(self):
        self.build_old_target()

        with self.assertRaisesMessage(install.InstallError, "does not exist"):
            self.run_install()

        self.assertTargetIsTheOldFile()

    @skipUnless(os.name == "posix", "Symbolic links need privileges on Windows.")
    def test_a_link_is_refused(self):
        self.build_old_target()
        real = self.folder / "real.sqlite3"
        real.write_bytes(b"x")
        self.upload.symlink_to(real)

        with self.assertRaisesMessage(install.InstallError, "private regular file"):
            self.run_install()

        self.assertTargetIsTheOldFile()

    def test_an_upload_with_a_journal_that_holds_data_is_refused(self):
        self.build_old_target()
        self.build_upload()
        Path(str(self.upload) + "-wal").write_bytes(b"unwritten pages")

        with self.assertRaisesMessage(install.InstallError, "journal"):
            self.run_install()

        self.assertTargetIsTheOldFile()
        self.assertTrue(self.upload.exists())

    def test_a_live_import_keeps_the_file(self):
        self.build_old_target()
        self.build_upload()
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=State.RUNNING, source=Source.ADDRESS, started_at=now, heartbeat_at=now, claim_token=uuid.uuid4(),
        )

        with self.assertRaisesMessage(OperationalError, "still running"):
            self.run_install()

        self.assertTargetIsTheOldFile()
        self.assertTrue(self.upload.exists())

    def test_an_active_housekeeping_run_keeps_the_file(self):
        self.build_old_target()
        self.build_upload()
        HousekeepingStatus.objects.create(pk=1, running_until=timezone.now() + timedelta(hours=1))

        with self.assertRaisesMessage(install.InstallError, "housekeeping"):
            self.run_install()

        self.assertTargetIsTheOldFile()

    def test_an_unfinished_rebuild_must_be_finished_by_setup_first(self):
        self.build_old_target()
        self.build_upload()
        marker = storage.reset_marker(self.target)
        marker.write_bytes(b"")

        with self.assertRaisesMessage(install.InstallError, "did not finish"):
            self.run_install()

        self.assertTrue(marker.exists())
        self.assertTrue(self.upload.exists())

    def test_a_file_that_is_not_a_database_is_refused_and_stays_where_it_is(self):
        self.build_old_target()
        self.upload.write_bytes(b"this is not a database" * 500)

        with self.assertRaisesMessage(install.InstallError, "not an SQLite database"):
            self.run_install()

        self.assertTargetIsTheOldFile()
        self.assertTrue(self.upload.exists())
        self.assertFalse(Path(str(self.target) + install.PREVIOUS_SUFFIX).exists())

    def test_a_file_without_a_published_directory_is_removed_and_the_old_file_comes_back(self):
        self.build_old_target()
        with directory_file(self.upload):
            call_command("migrate", "addresses", database=DIRECTORY_ALIAS, interactive=False, verbosity=0)

        with self.assertRaisesMessage(install.InstallError, "holds no published address directory"):
            self.run_install()

        self.assertTargetIsTheOldFile()
        self.assertFalse(self.upload.exists())

    def test_a_file_built_by_another_version_is_refused(self):
        self.build_old_target()
        self.build_upload()
        with directory_file(self.upload):
            with connections[DIRECTORY_ALIAS].cursor() as cursor:
                cursor.execute("DELETE FROM django_migrations WHERE app = 'addresses' AND name LIKE '0010%'")

        with self.assertRaisesMessage(install.InstallError, "another version"):
            self.run_install()

        self.assertTargetIsTheOldFile()

    def test_a_failure_while_the_old_file_cannot_be_put_back_keeps_the_reset_marker(self):
        self.build_old_target()
        self.build_upload()

        with mock.patch.object(install, "_verify", side_effect=install.InstallError("broken")), \
                mock.patch.object(install, "_put_back", side_effect=OSError("disk gone")):
            with self.assertRaises(OperationalError):
                self.run_install()

        self.assertTrue(storage.reset_marker(self.target).exists())

    def test_the_command_installs_and_reports_a_refusal_without_a_trace(self):
        self.build_old_target()
        self.build_upload()
        out = io.StringIO()

        with directory_file(self.target):
            call_command("install_address_directory", str(self.upload), "--sha256", "0" * 64, stdout=out) \
                if False else None
            with self.assertRaisesMessage(CommandError, "Nothing was changed"):
                call_command("install_address_directory", str(self.upload), "--sha256", "0" * 64, stdout=out)
            call_command("install_address_directory", str(self.upload), stdout=out)

        self.assertIn("Installed the address directory: 3 addresses.", out.getvalue())
        self.assertEqual(self.published(), (NEW_CHECKSUM, 3))

    def add_stations(self):
        """Two scanned buildings in the upload, one with a transformer name; returns the time of the latest scan."""
        first = timezone.now() - timedelta(days=2)
        latest = timezone.now() - timedelta(days=1)
        with directory_file(self.upload):
            TransformerStation.objects.create(egid=1, postal_code="9999", name="Test_TRA 1", match="exact", scanned_at=first)
            TransformerStation.objects.create(egid=2, postal_code="9998", name="", match="none", scanned_at=latest)
        return latest

    def fail_transformer_scan(self):
        ImportRequest.objects.update_or_create(pk=1, defaults={
            "last_transformer_state": "failed", "last_transformer_error": jobs.INTERRUPTED,
            "last_transformer_finished_at": timezone.now(),
        })
        return ImportLogEntry.objects.create(
            source=Source.TRANSFORMER, reason=jobs.INTERRUPTED, occurred_at=timezone.now(),
        )

    def assertTransformerResultIsTheFiles(self, latest):
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual(job.last_transformer_state, "succeeded")
        self.assertEqual(job.last_transformer_error, "")
        self.assertEqual(job.last_transformer_finished_at, latest)
        self.assertEqual(
            (job.last_transformer_postal_codes, job.last_transformer_buildings, job.last_transformer_with_name),
            ("9998, 9999", 2, 1),
        )

    def test_the_result_of_the_scan_in_the_file_replaces_an_older_failure(self):
        self.build_old_target()
        self.build_upload()
        latest = self.add_stations()
        entry = self.fail_transformer_scan()

        self.run_install()

        self.assertTransformerResultIsTheFiles(latest)
        entry.refresh_from_db()
        self.assertIsNotNone(entry.cleared_at)

    def test_record_only_takes_over_the_results_of_the_file_in_place(self):
        self.build_old_target()
        self.build_upload()
        latest = self.add_stations()
        self.run_install()
        self.fail_transformer_scan()

        with directory_file(self.target):
            call_command("install_address_directory", "--record-only", stdout=io.StringIO())

        self.assertTransformerResultIsTheFiles(latest)
        self.assertEqual(self.published(), (NEW_CHECKSUM, 3))

    def test_record_only_is_refused_while_an_import_is_alive_and_needs_no_file(self):
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=State.RUNNING, source=Source.ADDRESS, started_at=now, heartbeat_at=now, claim_token=uuid.uuid4(),
        )
        with self.assertRaisesMessage(CommandError, "still running"):
            call_command("install_address_directory", "--record-only", stdout=io.StringIO())
        for arguments in ((), ("--record-only", "upload.sqlite3")):
            with self.assertRaisesMessage(CommandError, "either a file or --record-only"):
                call_command("install_address_directory", *arguments, stdout=io.StringIO())
