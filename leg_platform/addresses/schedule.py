"""The daily run of the address directory (KONZEPT 3a points 5 to 7, D-090).

``manage.py run_scheduled_tasks`` (a systemd timer every 15 minutes) calls ``scheduled_address_import``, which calls
``run_daily``. From the configured time of day on (``import_run_time``, default 03:00 in the instance time zone) it
looks, once per day, whether the directory is due for an update and then claims and runs the import in its own
process, with the same claim as the button (``jobs.claim``): one import at a time over the web workers and this run,
so no double download. At most one download per day.

Due is: the update flag is set (a building was found neither locally nor at swisstopo, ``jobs.set_update_flag``);
or the published directory has no building identifiers yet; or the regular refresh is due (90 days, 24 hours while
there is no directory; D-090). A successful import clears the flag (``jobs.record_success``). The administrator's
button starts immediately and does not depend on this run. ``ADDRESS_IMPORT_AUTOMATIC=False`` turns the run off.

The run writes heartbeats like the web thread (``_execute_with_heartbeat``). When the official file lacks a required
column the run waits a week before it downloads again (``COLUMNS_RETRY_INTERVAL``, ``ImportRequest.columns_missing_at``).

The run is idempotent: a second call on the same day, or before the time, changes nothing.
"""

import logging
from datetime import time, timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.connection import ConnectionDoesNotExist

from . import jobs
from .models import DEFAULT_RUN_TIME, Directory, ImportRequest

logger = logging.getLogger(__name__)

# What ``run_daily`` reports (fixed words, no data).
DISABLED = "disabled"
BEFORE_TIME = "before the run time"
ALREADY_RAN = "already ran today"
NOT_DUE = "nothing due"
BUSY = "an import is running or queued"
IMPORTED = "imported"
BACKOFF = "waiting after a file without the required columns"
# After an import that failed because the official file lacks BDG_EGID or ADR_EDID, the daily run tries again after
# this long (the file is not going to change by itself every day; 143 MB per attempt).
COLUMNS_RETRY_INTERVAL = timedelta(days=7)


def import_run_time():
    """The time of day (instance time zone) from which the daily run may start the import."""
    value = ImportRequest.objects.filter(pk=1).values_list("run_time", flat=True).first()
    return value or DEFAULT_RUN_TIME


def set_import_run_time(value):
    """Store the time of day of the daily run (whole minutes); the settings page of the management area calls it."""
    if not isinstance(value, time):
        raise ValueError("A time of day is required.")
    ImportRequest.objects.get_or_create(pk=1)
    ImportRequest.objects.filter(pk=1).update(run_time=value.replace(second=0, microsecond=0, tzinfo=None))


def _directory_needs_identifiers():
    """True when a directory is published but was imported without the building identifiers."""
    try:
        directory = Directory.objects.filter(pk=1).first()
    except (DatabaseError, ConnectionDoesNotExist):
        # An unreadable file is the regular refresh's concern (``jobs.directory_is_empty``).
        return False
    return directory is not None and directory.row_count > 0 and not directory.has_egid


def refresh_is_due(job, now):
    """Whether an import is due: flag set, directory without identifiers, or the regular refresh (D-090)."""
    if job.update_flag_set_at is not None or _directory_needs_identifiers():
        return True
    interval = jobs.EMPTY_DIRECTORY_INTERVAL if jobs.directory_is_empty() else jobs.AUTOMATIC_INTERVAL
    last = job.last_automatic_attempt_at
    return last is None or now >= last + interval


def run_daily(now=None):
    """Check once per day, from the configured time on, and run the import if it is due; return one of the words above.

    Raises what the import raised (it is recorded in the job row and the logs by ``jobs.execute``).
    """
    if not settings.ADDRESS_IMPORT_AUTOMATIC:
        return DISABLED
    now = timezone.localtime(now or timezone.now())
    if now.time() < import_run_time():
        return BEFORE_TIME
    today = now.date()
    with transaction.atomic():
        ImportRequest.objects.get_or_create(pk=1)
        job = ImportRequest.objects.select_for_update().get(pk=1)
        if job.last_scheduled_run_on == today:
            return ALREADY_RAN
        if job.columns_missing_at is not None and now < job.columns_missing_at + COLUMNS_RETRY_INTERVAL:
            # The official file itself lacks a required column (jobs.COLUMN_FAILURES): the same download would
            # fail again. Wait a week; the button is not held back.
            ImportRequest.objects.filter(pk=1).update(last_scheduled_run_on=today)
            return BACKOFF
        if not refresh_is_due(job, now):
            ImportRequest.objects.filter(pk=1).update(last_scheduled_run_on=today)
            return NOT_DUE
        claimed = jobs.claim(scheduled=True, web=True)
        if claimed is None:
            # Another import runs or is queued: the day is not used up, the next call tries again.
            return BUSY
        # Committed with the claim, before the download: a failed import is not repeated today.
        ImportRequest.objects.filter(pk=1).update(last_scheduled_run_on=today)
    _execute_with_heartbeat(claimed)
    return IMPORTED


def _execute_with_heartbeat(claimed):
    """Run the claimed import with a heartbeat like the web thread, so that a killed run is seen as dead after about
    90 seconds (``jobs.HEARTBEAT_STALE_AFTER``) and the button is not blocked for the six hours of the lease."""
    from . import runner
    from .progress import Progress

    progress = Progress(check=runner._claim_check(claimed.token))
    heartbeat = runner.Heartbeat(claimed.token, progress)
    heartbeat.start()
    try:
        jobs.execute(claimed, progress)
    finally:
        heartbeat.stop()
