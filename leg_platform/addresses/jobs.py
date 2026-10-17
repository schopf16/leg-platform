"""Queue and claim of the address directory import (D-075, D-090).

The one ``ImportRequest`` row is the claim: only the holder of ``claim_token`` runs the import, in
the import thread of the web process (``runner``) or in the worker (``process_address_imports``).
Several web workers share it through the row lock. This module never imports the importer at the
top (web requests reach it).
"""

import logging
import uuid
from datetime import timedelta
from typing import NamedTuple

from django.db import DatabaseError, transaction
from django.utils import timezone

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent

from .models import Directory, ImportRequest

logger = logging.getLogger(__name__)
AUTOMATIC_INTERVAL = timedelta(days=90)
# While the directory is missing or empty (new instance, restore), the automatic attempt is due after this time.
EMPTY_DIRECTORY_INTERVAL = timedelta(hours=24)
# A RUNNING job older than this is taken over (D-076, hard-killed worker). Keep equal to
# ``importer.IMPORT_LEASE`` (a test checks); this module must not import the importer.
RUNNING_JOB_LEASE = timedelta(hours=6)
# A RUNNING job of the web process writes a heartbeat every few seconds (``runner``); without one for
# this long its process is gone and the job is taken over (D-090).
HEARTBEAT_STALE_AFTER = timedelta(seconds=90)
# Shown when the importer raised something it did not describe (nothing of the exception is stored).
UNEXPECTED_FAILURE = "The import failed unexpectedly."
INTERRUPTED = "The import was interrupted because the application stopped or restarted."
# The only failure texts that are stored, logged and shown: the fixed sentences of ``importer.ImportFailure``
# (a test compares them with the importer). Any other text counts as unexpected, so that no upstream data
# can reach the page, the log or the security log through a message.
SAFE_FAILURES = frozenset({
    "Unapproved download URL.", "Official download failed.", "Unexpected HTTP content encoding.",
    "Download exceeds the size limit.", "Download exceeds its resource limit.", "Incomplete download.",
    "Invalid official catalogue metadata.", "CSV exceeds its resource limit.",
    "Invalid address field length.", "Invalid address field.", "Expected exactly one CSV member.",
    "Unsafe ZIP member.", "Invalid CSV header.", "Missing approved CSV columns.",
    "Invalid CSV row count or width.", "Invalid postal label.", "Empty directory.",
    "Invalid official archive format.", "Another import is running.", "The import lease was lost.",
    "The free disk space could not be checked.", "Not enough free disk space for an import.",
    "Catalogue data date predates the published directory.",
    "A repair was tried recently and did not succeed.", "Official archive checksum mismatch.",
    "The official directory could not be imported.",
})


def directory_is_empty():
    """True when there is no published directory: no row, no addresses, or a file that cannot be read."""
    try:
        return not Directory.objects.filter(pk=1, row_count__gt=0).exists()
    except DatabaseError:
        return True


def _is_running(job, now):
    """A job that runs and is alive: a fresh heartbeat, or (a worker writes none) a lease that has not expired.

    A running job without a start time is expired.
    """
    if job.state != ImportRequest.State.RUNNING or job.started_at is None:
        return False
    if job.heartbeat_at is not None:
        return now < job.heartbeat_at + HEARTBEAT_STALE_AFTER
    return now < job.started_at + RUNNING_JOB_LEASE


def is_alive(job, now=None):
    """Whether ``job`` is running and alive (for the status page)."""
    return _is_running(job, now or timezone.now())


def request_import():
    """Coalesce pending/running clicks; completed manual imports may retry."""
    now = timezone.now()
    with transaction.atomic():
        ImportRequest.objects.get_or_create(pk=1)
        job = ImportRequest.objects.select_for_update().get(pk=1)
        if job.state == ImportRequest.State.QUEUED or _is_running(job, now):
            return False
        if job.state == ImportRequest.State.RUNNING:
            # Dead (stale heartbeat or expired lease): the heartbeat stays as the mark for ``claim``, which frees the lease.
            logger.warning("An address directory import job exceeded its lease and was replaced by a new request.")
        job.state = ImportRequest.State.QUEUED
        job.is_automatic = False
        job.requested_at = now
        job.started_at = None
        job.finished_at = None
        job.last_error = ""
        job.phase = ""
        job.save()
        return True


class Claim(NamedTuple):
    """The right to run the import: ``manual`` = also repairs, ``took_over`` = a dead job was found."""

    token: uuid.UUID
    manual: bool
    took_over: bool


def claim(*, schedule_automatic=False, web=False):
    """Claim the job for this process; None if nothing is queued or due, or a live import runs.

    A queued manual request wins. An expired RUNNING job counts as failed and is not repeated.
    ``web``: the import thread of the web process, which writes heartbeats.
    """
    took_over = False
    with transaction.atomic():
        ImportRequest.objects.get_or_create(pk=1)
        job = ImportRequest.objects.select_for_update().get(pk=1)
        now = timezone.now()
        if job.state == ImportRequest.State.RUNNING:
            if _is_running(job, now):
                return None
            logger.warning("An address directory import job exceeded its lease and was taken over.")
            took_over = True
            job.state = ImportRequest.State.FAILED
            job.finished_at = now
            job.last_error = INTERRUPTED
            job.save(update_fields=["state", "finished_at", "last_error"])
        if job.state != ImportRequest.State.QUEUED:
            if not schedule_automatic:
                return None
            interval = EMPTY_DIRECTORY_INTERVAL if directory_is_empty() else AUTOMATIC_INTERVAL
            if (
                job.last_automatic_attempt_at is not None
                and now < job.last_automatic_attempt_at + interval
            ):
                return None
            job.is_automatic = True
            job.requested_at = now
        else:
            job.is_automatic = False
        # A heartbeat that is still there while no import is alive marks a holder that died (a normal end
        # clears it): the new holder frees its lease on the directory.
        took_over = took_over or job.heartbeat_at is not None
        job.state = ImportRequest.State.RUNNING
        job.started_at = now
        job.finished_at = None
        job.claim_token = uuid.uuid4()
        job.heartbeat_at = now if web else None
        job.phase = "starting"
        job.bytes_done = job.bytes_total = job.rows_done = None
        job.last_error = ""
        if job.is_automatic:
            # Commit before any download; a failure consumes this attempt.
            job.last_automatic_attempt_at = now
        job.save()
        # A manual request also repairs a damaged directory (D-076).
        return Claim(job.claim_token, not job.is_automatic, took_over)


def _kind(claimed):
    return "manual" if claimed.manual else "automatic"


def record_success(claimed, count, *, downloaded=None):
    """Store and log a completed import (the claim must still be ours)."""
    count = count if isinstance(count, int) else None
    finished = ImportRequest.objects.filter(pk=1, claim_token=claimed.token).update(
        state=ImportRequest.State.SUCCEEDED, finished_at=timezone.now(), heartbeat_at=None,
        phase="unchanged" if downloaded is False else "", last_error="", rows_done=count,
    )
    if not finished:
        logger.warning("An address directory import finished, but its job was taken over meanwhile.")
        return
    logger.info(
        "Address directory import completed (%s): %s addresses%s.", _kind(claimed), count,
        "" if downloaded is not False else ", the directory was already up to date",
    )
    with transaction.atomic():
        security_log.record(
            SecurityEvent.Event.ADDRESS_IMPORT_SUCCEEDED, detail=f"{_kind(claimed)}, {count} addresses"
        )


def record_failure(claimed, exc):
    """Store and log a failed import: only the fixed text of an ``ImportFailure``, never upstream data."""
    from .importer import ImportFailure  # the web process reaches this only through the runner's thread

    reason = str(exc) if isinstance(exc, ImportFailure) and str(exc) in SAFE_FAILURES else UNEXPECTED_FAILURE
    finished = ImportRequest.objects.filter(pk=1, claim_token=claimed.token).update(
        state=ImportRequest.State.FAILED, finished_at=timezone.now(), heartbeat_at=None,
        phase="", last_error=reason[:255],
    )
    if not finished:
        # Taken over meanwhile: the new holder owns the row.
        logger.warning("An address directory import failed, but its job was taken over meanwhile.")
        return
    logger.error(
        "Address directory import failed (%s): %s%s", _kind(claimed), reason,
        f" ({type(exc).__name__})" if reason == UNEXPECTED_FAILURE else "",
    )
    with transaction.atomic():
        security_log.record(
            SecurityEvent.Event.ADDRESS_IMPORT_FAILED, detail=f"{_kind(claimed)}, {reason}"
        )


def execute(claimed, progress=None):
    """Run the claimed import and record the outcome; re-raises a failure (after recording it)."""
    # Imported locally so web requests never load the downloader.
    from .importer import import_directory

    options = {} if progress is None else {"progress": progress}
    try:
        if claimed.manual:
            count = import_directory(repair=True, **options)
        else:
            count = import_directory(**options)
    except Exception as exc:
        record_failure(claimed, exc)
        # Only a generic failure is stored; the exception goes to the caller (the command or the runner's log).
        raise
    record_success(claimed, count, downloaded=None if progress is None else progress.downloaded)
    return True


def process_one(*, schedule_automatic=False):
    """The worker's step: claim a queued or due job and run it (no heartbeat: its lease is six hours)."""
    claimed = claim(schedule_automatic=schedule_automatic)
    if claimed is None:
        return False
    return execute(claimed)
