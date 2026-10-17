"""Queue of the address directory import; the web process only writes the row, the worker consumes it (D-075)."""

import logging
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.utils import timezone

from .models import Directory, ImportRequest

logger = logging.getLogger(__name__)
AUTOMATIC_INTERVAL = timedelta(days=90)
# While the directory is missing or empty (new instance, restore), the automatic attempt is due after this time.
EMPTY_DIRECTORY_INTERVAL = timedelta(hours=24)
# A RUNNING job older than this is taken over (D-076, hard-killed worker). Keep equal to
# ``importer.IMPORT_LEASE`` (a test checks); this module must not import the importer.
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
    # Imported locally so web requests never load the downloader.
    from .importer import import_directory

    with transaction.atomic():
        ImportRequest.objects.get_or_create(pk=1)
        job = ImportRequest.objects.select_for_update().get(pk=1)
        now = timezone.now()
        # A queued manual request wins. An expired RUNNING job counts as failed and is not repeated.
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
            # Commit before any download; a failure consumes this attempt.
            job.last_automatic_attempt_at = now
        job.save()
        # A manual request also repairs a damaged directory (D-076).
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
        # Only a generic failure is stored and raised: no exception, URL or upstream data.
        raise
    ImportRequest.objects.filter(pk=1).update(
        state=ImportRequest.State.SUCCEEDED, finished_at=timezone.now(),
    )
    return True
