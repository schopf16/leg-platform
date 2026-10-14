"""Application-DB queue, consumed only by the isolated importer worker."""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import APPLICATION_ALIAS, ImportRequest

logger = logging.getLogger(__name__)
AUTOMATIC_INTERVAL = timedelta(days=90)


def request_import():
    """Coalesce pending/running clicks; completed manual imports may retry."""
    now = timezone.now()
    with transaction.atomic(using=APPLICATION_ALIAS):
        ImportRequest.objects.using(APPLICATION_ALIAS).get_or_create(pk=1)
        job = ImportRequest.objects.using(APPLICATION_ALIAS).select_for_update().get(pk=1)
        if job.state in {ImportRequest.State.QUEUED, ImportRequest.State.RUNNING}:
            return False
        job.state = ImportRequest.State.QUEUED
        job.is_automatic = False
        job.requested_at = now
        job.started_at = None
        job.finished_at = None
        job.save(using=APPLICATION_ALIAS)
        return True


def process_one(*, schedule_automatic=False):
    # Import locally, so admin requests never load the downloader or access its
    # configuration. No external parameters are ever passed from a queued job.
    from .importer import import_directory, require_writer

    require_writer()
    with transaction.atomic(using=APPLICATION_ALIAS):
        ImportRequest.objects.using(APPLICATION_ALIAS).get_or_create(pk=1)
        job = ImportRequest.objects.using(APPLICATION_ALIAS).select_for_update().get(pk=1)
        now = timezone.now()
        # A queued manual request always wins. Do not take over RUNNING work:
        # operator exclusion is required after a hard-killed worker.
        if job.state == ImportRequest.State.RUNNING:
            return False
        if job.state != ImportRequest.State.QUEUED:
            if not schedule_automatic:
                return False
            if (
                job.last_automatic_attempt_at is not None
                and now < job.last_automatic_attempt_at + AUTOMATIC_INTERVAL
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
            # Commit BEFORE any download. Failure consumes this 90-day attempt.
            job.last_automatic_attempt_at = now
        job.save(using=APPLICATION_ALIAS)
    try:
        import_directory()
    except Exception:
        ImportRequest.objects.using(APPLICATION_ALIAS).filter(pk=1).update(
            state=ImportRequest.State.FAILED, finished_at=timezone.now(),
        )
        logger.error("Address directory import attempt failed.")
        # Only a generic failure reaches the command output; the row deliberately
        # stores no exception, URL, credentials or upstream data.
        raise
    ImportRequest.objects.using(APPLICATION_ALIAS).filter(pk=1).update(
        state=ImportRequest.State.SUCCEEDED, finished_at=timezone.now(),
    )
    return True
