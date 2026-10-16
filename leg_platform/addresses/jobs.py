"""Queue of the address directory import, consumed only by the worker process (D-075).

The admin view calls ``request_import`` only, which writes the queue row. ``process_one`` is
called only by the management command ``process_address_imports``; the web process never
downloads or imports anything.
"""

import logging
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.utils import timezone

from .models import Directory, ImportRequest

logger = logging.getLogger(__name__)
AUTOMATIC_INTERVAL = timedelta(days=90)
# While the directory is missing or empty (a new instance, a restore: the file is not part of the
# backup, D-075), the automatic attempt is due again after this time instead of after 90 days, so
# that the suggestions come back without anyone pressing the button (security review of D-075, M-5).
EMPTY_DIRECTORY_INTERVAL = timedelta(hours=24)
# A RUNNING job older than this is taken over (security review of D-076, N-3): after a hard kill
# (out of memory, power loss, a stop during an import) the state stays "running" and nothing else
# would ever clear it. It is the lease of the importer on the directory
# (``importer.IMPORT_LEASE``, a test keeps the two equal; this module must not import the importer,
# the web process reaches it). An import that really runs renews its own lease with every chunk and
# takes far less than this, and a second importer is refused by that lease anyway.
RUNNING_JOB_LEASE = timedelta(hours=6)


def directory_is_empty():
    """True when there is no published directory: no row, no addresses, or a file that cannot be read."""
    try:
        return not Directory.objects.filter(pk=1, row_count__gt=0).exists()
    except DatabaseError:
        return True


def _is_running(job, now):
    """A job that runs and whose lease has not expired. A running job without a start time is expired."""
    return (
        job.state == ImportRequest.State.RUNNING
        and job.started_at is not None
        and now < job.started_at + RUNNING_JOB_LEASE
    )


def request_import():
    """Coalesce pending/running clicks; completed manual imports may retry."""
    now = timezone.now()
    with transaction.atomic():
        ImportRequest.objects.get_or_create(pk=1)
        job = ImportRequest.objects.select_for_update().get(pk=1)
        if job.state == ImportRequest.State.QUEUED or _is_running(job, now):
            return False
        job.state = ImportRequest.State.QUEUED
        job.is_automatic = False
        job.requested_at = now
        job.started_at = None
        job.finished_at = None
        job.save()
        return True


def process_one(*, schedule_automatic=False):
    # Import locally, so admin requests never load the downloader. No external parameters are
    # ever passed from a queued job.
    from .importer import import_directory

    with transaction.atomic():
        ImportRequest.objects.get_or_create(pk=1)
        job = ImportRequest.objects.select_for_update().get(pk=1)
        now = timezone.now()
        # A queued manual request always wins. A RUNNING job is not taken over while its lease is
        # valid; after a hard-killed worker its lease expires (six hours) and the job counts as
        # failed (N-3). The request that was lost with it is not repeated: whoever wants it presses
        # the button again, the automatic attempt follows its own interval.
        if job.state == ImportRequest.State.RUNNING:
            if _is_running(job, now):
                return False
            logger.warning("An address directory import job exceeded its lease and was taken over.")
            job.state = ImportRequest.State.FAILED
            job.finished_at = now
            job.save(update_fields=["state", "finished_at"])
        if job.state != ImportRequest.State.QUEUED:
            if not schedule_automatic:
                return False
            interval = EMPTY_DIRECTORY_INTERVAL if directory_is_empty() else AUTOMATIC_INTERVAL
            if (
                job.last_automatic_attempt_at is not None
                and now < job.last_automatic_attempt_at + interval
            ):
                return False
            job.is_automatic = True
            job.requested_at = now
        else:
            job.is_automatic = False
        job.state = ImportRequest.State.RUNNING
        job.started_at = now
        job.finished_at = None
        if job.is_automatic:
            # Commit BEFORE any download. Failure consumes this attempt (24 hours while the
            # directory is empty, otherwise 90 days).
            job.last_automatic_attempt_at = now
        job.save()
        # A manual request (the admin button) also repairs: an unchanged official file is skipped
        # only when the published directory passes the integrity checks, otherwise it is loaded
        # again, so that "sync again" repairs a damaged directory without a repeated request being
        # expensive (D-076). The automatic attempt trusts the recorded checksum.
        manual = not job.is_automatic
    try:
        if manual:
            import_directory(repair=True)
        else:
            import_directory()
    except Exception:
        ImportRequest.objects.filter(pk=1).update(
            state=ImportRequest.State.FAILED, finished_at=timezone.now(),
        )
        logger.error("Address directory import attempt failed.")
        # Only a generic failure reaches the command output; the row deliberately
        # stores no exception, URL, credentials or upstream data.
        raise
    ImportRequest.objects.filter(pk=1).update(
        state=ImportRequest.State.SUCCEEDED, finished_at=timezone.now(),
    )
    return True
