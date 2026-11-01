"""Install a prebuilt address directory file that was uploaded to the server (D-134).

The import of the whole directory (addresses, PV plants, transformer stations) takes hours on a one-core server. The
operator builds the file on another machine and uploads it over SSH, next to the directory file (same folder, so the
swap is one rename). ``manage.py install_address_directory`` checks the upload and puts it in place:

* the file is private and regular (no link), in the folder of the directory file, with no journal of its own;
* nothing is running that holds the claim (no live import, no housekeeping lease); the claim row stays locked until the end;
* the reset marker keeps every other process away while the file is swapped; the old file is kept until the new one passed
  the checks and is put back when it did not (the checks are the ones of the import: same migrations, same schema, the
  lookup tables, ``quick_check`` and the row count of the published generation);
* the page shows the figures of the new file and an import that setup had queued is not needed any more.

Stop the web service, the import worker and the scheduled tasks first (``docs/DEPLOYMENT.md``, section 10.3): a process
that keeps the old file open is refused by the file identity check, but SQLite removes the journal files by name when
its last connection closes, which belong to the new file by then.
"""

import gc
import hashlib
import hmac
import logging
import os
import stat
from pathlib import Path

from django.core.management import call_command
from django.db import DatabaseError, OperationalError, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from . import jobs, published
from .models import (
    Directory, HousekeepingStatus, ImportQueueEntry, ImportRequest, Plant, PlantRegister, TransformerStation,
)
from .setup import _changes_directory, _require_no_live_import, _schema_matches
from .storage import DIRECTORY_ALIAS, directory_open_guard, rebuilding_directory, reset_marker

logger = logging.getLogger(__name__)

SIDECARS = ("-wal", "-shm", "-journal")
PREVIOUS_SUFFIX = ".previous"
SQLITE_HEADER = b"SQLite format 3\x00"


class InstallError(Exception):
    """A fixed sentence for the operator; the file was not changed (or was put back)."""


def _sidecars(path):
    return [Path(str(path) + suffix) for suffix in SIDECARS]


def _require_private_regular(candidate):
    """``candidate`` is a regular file with one name that the current user owns; never a link or a device."""
    try:
        info = candidate.lstat()
    except OSError:
        raise InstallError("The file does not exist.") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise InstallError("The upload is not a private regular file.")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise InstallError("The upload belongs to another user. Upload it as the service user.")


def check_upload(source, path):
    """The upload may be installed: right place, right kind, no journal that holds data."""
    if source.parent != path.parent:
        raise InstallError("The upload must be in the folder of the address directory file.")
    if source == path or source.name.startswith(path.name + "-"):
        raise InstallError("The upload must have another name than the address directory file.")
    _require_private_regular(source)
    with open(source, "rb") as handle:
        if handle.read(16) != SQLITE_HEADER:
            raise InstallError("The upload is not an SQLite database file.")
    for sidecar in _sidecars(source):
        if sidecar.suffix != "-shm" and sidecar.exists() and sidecar.stat().st_size:
            raise InstallError("The upload has a journal file. Checkpoint the file before the upload.")
    for sidecar in _sidecars(path):
        if sidecar.exists() or sidecar.is_symlink():
            _require_private_regular(sidecar)
    if path.exists() or path.is_symlink():
        _require_private_regular(path)


def sha256_of(source):
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(connection):
    """The installed file is what the running code expects (the checks of the import, ``importer``)."""
    from .importer import _has_lookup_rows, _is_intact  # the importer is loaded only where it is used

    executor = MigrationExecutor(connection)
    loader = executor.loader
    # Not ``migration_plan``: it plans nothing while the last migration is recorded, whatever is missing before it.
    plan = [loader.graph.nodes[key] for key in sorted(loader.graph.nodes) if key[0] == "addresses" and key not in loader.applied_migrations]
    if any(_changes_directory(migration) for migration in plan):
        raise InstallError("The file was built by another version of the software. Build it again with this version.")
    if plan:
        # Migrations that only concern the application database are recorded in the file's own table.
        call_command("migrate", "addresses", database=DIRECTORY_ALIAS, interactive=False, verbosity=0)
    if not _schema_matches(connection):
        raise InstallError("The file does not have the tables of this version.")
    directory = Directory.objects.filter(pk=1).first()
    if directory is None or directory.row_count <= 0 or not directory.has_egid or not directory.imported_at:
        raise InstallError("The file holds no published address directory.")
    if not _has_lookup_rows(directory):
        raise InstallError("The file lacks the lookup tables of the address suggestions.")
    if not _is_intact(directory):
        raise InstallError("The file is damaged: the integrity check or the address count failed.")
    return directory


def _swap(source, path):
    """Move the old file aside, put the upload in its place; returns the place of the old file (or None)."""
    previous = Path(str(path) + PREVIOUS_SUFFIX)
    previous.unlink(missing_ok=True)
    kept = None
    if path.exists():
        path.rename(previous)
        kept = previous
    for sidecar in _sidecars(path):
        sidecar.unlink(missing_ok=True)
    source.replace(path)
    if os.name == "posix":
        os.chmod(path, 0o600)
    for leftover in ("-shm", ".open-lock"):
        Path(str(source) + leftover).unlink(missing_ok=True)
    return kept


def _put_back(path, kept):
    """The upload failed the checks: remove it and bring the old file back."""
    connections[DIRECTORY_ALIAS].close()
    # A connection that failed to open can stay referenced by the traceback and hold the file (Windows refuses to unlink it).
    gc.collect()
    path.unlink(missing_ok=True)
    for sidecar in _sidecars(path):
        sidecar.unlink(missing_ok=True)
    if kept is not None:
        kept.replace(path)


def _record(now):
    """What the page and the schedule know about the new file; called under the lock of the claim row."""
    published.clear()
    for source in (ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT):
        published.refresh(source)
    covered = [ImportRequest.Source.ADDRESS]
    if PlantRegister.objects.filter(pk=1, row_count__gt=0).exists() and Plant.objects.exists():
        covered.append(ImportRequest.Source.PLANT)
    if TransformerStation.objects.exists():
        covered.append(ImportRequest.Source.TRANSFORMER)
    cleared = ImportQueueEntry.objects.filter(source__in=covered).delete()[0]
    job = ImportRequest.objects.get(pk=1)
    if job.state == ImportRequest.State.QUEUED:
        # The import that setup queued for the empty file is not needed any more.
        job.state = ImportRequest.State.SUCCEEDED
        job.source = ImportRequest.Source.ADDRESS
        job.finished_at = now
        job.last_error = ""
        job.phase = ""
        for name, value in jobs.source_result(ImportRequest.Source.ADDRESS, ImportRequest.State.SUCCEEDED, now).items():
            setattr(job, name, value)
        cleared += 1
    # The next regular refresh counts from the installation, not from "never".
    job.last_automatic_attempt_at = now
    job.save()
    return cleared


def install(source, *, sha256=None, stdout=None):
    """Put the uploaded file ``source`` in place of the address directory file; returns the published ``Directory``.

    Raises ``InstallError`` (nothing changed, or the old file was put back) or ``OperationalError`` (busy). The caller marks
    the process as writer."""
    connection = connections[DIRECTORY_ALIAS]
    path = Path(connection.settings_dict["NAME"])
    source = Path(source)
    if not source.is_absolute() or not path.is_absolute():
        raise InstallError("Use an absolute path.")
    check_upload(source, path)
    if sha256 is not None and not hmac.compare_digest(sha256_of(source), sha256.strip().lower()):
        raise InstallError("The checksum of the upload does not match.")
    marker = reset_marker(path)
    try:
        with directory_open_guard(path):
            with transaction.atomic(using="default"):
                _require_no_live_import()
                if HousekeepingStatus.lease_active(timezone.now()):
                    raise InstallError("A housekeeping run is active. Try again when it has ended.")
                try:
                    os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                except FileExistsError:
                    raise InstallError(
                        "An earlier rebuild or installation did not finish (reset marker). Run setup_instance first."
                    ) from None
                # The marker stays when the old file could not be put back: the next setup rebuilds the file.
                keep_marker = True
                try:
                    connection.close()
                    kept = _swap(source, path)
                    with rebuilding_directory():
                        try:
                            try:
                                directory = _verify(connection)
                            except DatabaseError:
                                raise InstallError("The file cannot be read as an address directory database.") from None
                        except BaseException:
                            _put_back(path, kept)
                            keep_marker = False
                            raise
                        keep_marker = False
                        if kept is not None:
                            kept.unlink(missing_ok=True)
                        cleared = _record(timezone.now())
                        connection.close()
                finally:
                    if not keep_marker:
                        marker.unlink(missing_ok=True)
    except OSError as error:
        raise OperationalError("The address directory is busy or could not be replaced: nothing was installed.") from error
    logger.info("A prebuilt address directory was installed: %s addresses.", directory.row_count)
    if stdout is not None:
        stdout.write(f"Waiting imports cleared: {cleared}.\n")
    return directory
