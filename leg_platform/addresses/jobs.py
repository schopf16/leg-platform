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

from django.db import DatabaseError, connections, transaction
from django.db.models import F
from django.utils import timezone

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent

from .models import Directory, HousekeepingStatus, ImportLogEntry, ImportRequest
from .storage import DIRECTORY_ALIAS, reset_marker

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
    "Unapproved download URL.", "Official download failed.", "Official file not found.",
    "Unexpected HTTP content encoding.",
    "Download exceeds the size limit.", "Download exceeds its resource limit.", "Incomplete download.",
    "Invalid official catalogue metadata.", "CSV exceeds its resource limit.",
    "Invalid address field length.", "Invalid address field.", "Expected exactly one CSV member.",
    "Unsafe ZIP member.", "Invalid CSV header.", "Missing approved CSV columns.", "Missing EGID column.",
    "Missing entrance column.", "Missing coordinate column.",
    "Invalid CSV row count or width.", "Invalid postal label.", "Empty directory.",
    "Invalid official archive format.", "Another import is running.", "The import lease was lost.",
    "The free disk space could not be checked.", "Not enough free disk space for an import.",
    "Catalogue data date predates the published directory.",
    "A repair was tried recently and did not succeed.", "Official archive checksum mismatch.",
    "The official directory could not be imported.", "Unexpected HTTP content type.",
})
# The official file lacks a required column: the same file fails again, so the daily run waits a week (``schedule``).
COLUMN_FAILURES = frozenset({"Missing EGID column.", "Missing entrance column.", "Missing coordinate column."})
# The fixed texts of the plant register import (``plants``, D-117): stored, logged and shown like SAFE_FAILURES.
PLANT_FAILURES = frozenset({
    "The plant register file has an unexpected format.", "The plant register file is larger than allowed.",
    "The number of plants changed too much. Nothing was changed.", "The plant register contains no plants.",
    "The plant register could not be downloaded or read.",
})


# The fixed texts of the transformer station scan (``transformers``, D-118), stored, logged and shown like SAFE_FAILURES.
MAP_UNREACHABLE = "The map server could not be reached."
MAP_ANSWER = "The map server did not answer as expected."
MAP_TOO_LONG = "The transformer scan takes too long."
MAP_TOO_MANY_TILES = "Too many map tiles for one run."
MAP_UNREADABLE = "The map could not be read."
SCAN_NO_POSTAL_CODES = "No postal code is set for the transformer scan."
SCAN_NO_BUILDINGS = "No buildings with coordinates for the postal codes."
SCAN_READER_MISSING = "The map reader is not installed on this server."
TRANSFORMER_FAILURES = frozenset({
    MAP_UNREACHABLE, MAP_ANSWER, MAP_TOO_LONG, MAP_TOO_MANY_TILES, MAP_UNREADABLE,
    SCAN_NO_POSTAL_CODES, SCAN_NO_BUILDINGS, SCAN_READER_MISSING,
})


def _plant_result(state, finished, error=""):
    """The fields of the last plant run (D-117, ``ImportRequest.last_plant_*``): set when a plant run ends."""
    return {"last_plant_state": state, "last_plant_finished_at": finished, "last_plant_error": error}


def _transformer_result(state, finished, error=""):
    """The fields of the last transformer scan (D-118, ``ImportRequest.last_transformer_*``): set when a scan ends."""
    return {"last_transformer_state": state, "last_transformer_finished_at": finished, "last_transformer_error": error}


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


def set_update_flag():
    """Note that an address was found neither in the directory nor at swisstopo (the update flag, KONZEPT 3a).

    Set at most once: a flag that is set keeps its time, so many failed searches change nothing. The flag holds
    no address. True if this call set it.
    """
    ImportRequest.objects.get_or_create(pk=1)
    newly_set = bool(
        ImportRequest.objects.filter(pk=1, update_flag_set_at__isnull=True).update(update_flag_set_at=timezone.now())
    )
    if newly_set:
        logger.info("An address was not found; the update flag of the address directory is set.")
    return newly_set


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
        job.source = ImportRequest.Source.ADDRESS
        job.is_automatic = False
        job.requested_at = now
        job.started_at = None
        job.finished_at = None
        job.last_error = ""
        job.phase = ""
        job.save()
        return True


class Claim(NamedTuple):
    """The right to run the import: ``manual`` = also repairs, ``took_over`` = a dead job was found, ``source`` = the
    address directory or the plant register (D-117)."""

    token: uuid.UUID
    manual: bool
    took_over: bool
    source: str = ImportRequest.Source.ADDRESS


def claim(*, schedule_automatic=False, scheduled=False, web=False, source=ImportRequest.Source.ADDRESS):
    """Claim the job for this process; None if nothing is queued or due, or a live import runs.

    A queued manual request wins. An expired RUNNING job counts as failed and is not repeated.
    ``web``: the caller writes heartbeats (the import thread of the web process, the daily run of ``schedule``): a
    job without a heartbeat for ``HEARTBEAT_STALE_AFTER`` is dead and can be requested again at once. Without it
    (the worker) the job is taken as alive for its six-hour lease.
    ``scheduled``: the daily run (``schedule``) has decided that an import is due; the 90-day interval of
    ``schedule_automatic`` is not asked again. The claim is an automatic one (no repair).
    ``source`` (D-117): the plant register runs only as the daily run (``scheduled``), never while an address request
    is queued, and it does not touch the address schedule. One claim for both sources (D-090).
    """
    took_over = False
    with transaction.atomic():
        ImportRequest.objects.get_or_create(pk=1)
        job = ImportRequest.objects.select_for_update().get(pk=1)
        now = timezone.now()
        # Setup commits the replacement queue before publishing the rebuilt schema. Never
        # consume that request (or start an automatic run) until its marker is removed.
        # Checking under this row lock also closes the race with setup taking the same lock.
        if reset_marker(connections[DIRECTORY_ALIAS].settings_dict["NAME"]).exists():
            return None
        # A housekeeping run holds its lease under the same row lock (D-119): no import starts until it ends.
        if HousekeepingStatus.lease_active(now):
            return None
        if job.state == ImportRequest.State.RUNNING:
            if _is_running(job, now):
                return None
            logger.warning("An address directory import job exceeded its lease and was taken over.")
            took_over = True
            job.state = ImportRequest.State.FAILED
            job.finished_at = now
            job.last_error = INTERRUPTED
            fields = ["state", "finished_at", "last_error"]
            if job.source == ImportRequest.Source.PLANT:
                # The plant run that held the claim is over: its result is kept apart from the shared fields (D-117).
                for name, value in _plant_result(ImportRequest.State.FAILED, now, INTERRUPTED).items():
                    setattr(job, name, value)
                fields += ["last_plant_state", "last_plant_finished_at", "last_plant_error"]
            if job.source == ImportRequest.Source.TRANSFORMER:
                for name, value in _transformer_result(ImportRequest.State.FAILED, now, INTERRUPTED).items():
                    setattr(job, name, value)
                fields += ["last_transformer_state", "last_transformer_finished_at", "last_transformer_error"]
            job.save(update_fields=fields)
            # The interrupted run is a failure of its source too (D-121).
            _log_failure(job.source, INTERRUPTED, "", now)
        # The plant register and the transformer scan both run only as the scheduled run, never while an address request
        # is queued, and neither touches the address schedule (D-117, D-118).
        separate = source in (ImportRequest.Source.PLANT, ImportRequest.Source.TRANSFORMER)
        if separate:
            if job.state == ImportRequest.State.QUEUED or not scheduled:
                return None
            job.is_automatic = True
            job.requested_at = now
        elif job.state != ImportRequest.State.QUEUED:
            if not (schedule_automatic or scheduled):
                return None
            if not scheduled:
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
        job.source = source
        job.heartbeat_at = now if web else None
        job.phase = "starting"
        job.bytes_done = job.bytes_total = job.rows_done = None
        job.last_error = ""
        if job.is_automatic and not separate:
            # Commit before any download; a failure consumes this attempt.
            job.last_automatic_attempt_at = now
        job.save()
        # A manual request also repairs a damaged directory (D-076).
        return Claim(job.claim_token, not job.is_automatic, took_over, source)


def _kind(claimed):
    return "manual" if claimed.manual else "automatic"


def _log_failure(source, reason, step, now):
    """One row of the import error log (D-121): the source, the step and the fixed reason text only."""
    ImportLogEntry.objects.create(
        source=source, step=step if step.isidentifier() and len(step) <= 20 else "",
        reason=reason[:255], occurred_at=now,
    )


def _clear_failures(source, now, keep_kinds=()):
    """A successful run of the source clears the open rows of that source only (D-121). ``keep_kinds``: rows that
    stay open (the stale event stays until the file changes, D-122)."""
    ImportLogEntry.objects.filter(source=source, cleared_at__isnull=True).exclude(kind__in=keep_kinds).update(
        cleared_at=now,
    )


# The information event of an unchanged file (D-122): level notice, not an error.
UNCHANGED_TEXT = "The downloaded file is unchanged since the last run."


def record_success(claimed, count, *, downloaded=None, transformer=None):
    """Store and log a completed import (the claim must still be ours). ``transformer``: the result of a scan (D-118)."""
    count = count if isinstance(count, int) else None
    plant = claimed.source == ImportRequest.Source.PLANT
    scan = claimed.source == ImportRequest.Source.TRANSFORMER
    fields = {
        "state": ImportRequest.State.SUCCEEDED, "finished_at": timezone.now(), "heartbeat_at": None,
        "phase": "unchanged" if downloaded is False else "", "last_error": "", "rows_done": count,
    }
    if plant:
        fields.update(_plant_result(ImportRequest.State.SUCCEEDED, fields["finished_at"]))
    elif scan:
        fields.update(_transformer_result(ImportRequest.State.SUCCEEDED, fields["finished_at"]))
        fields.update({
            "last_transformer_postal_codes": ", ".join(transformer.postal_codes)[:255],
            "last_transformer_buildings": transformer.buildings,
            "last_transformer_with_name": transformer.with_name,
        })
    else:
        fields["columns_missing_at"] = None
    unchanged = downloaded is False and claimed.source == ImportRequest.Source.ADDRESS
    if claimed.source == ImportRequest.Source.ADDRESS:
        # Consecutive runs that found the file unchanged (D-122); a changed download starts again at zero.
        previous = ImportRequest.objects.filter(pk=1).values_list("unchanged_runs", flat=True).first() or 0
        fields["unchanged_runs"] = previous + 1 if unchanged else 0
    finished = ImportRequest.objects.filter(pk=1, claim_token=claimed.token).update(**fields)
    if not finished:
        logger.warning("An import finished, but its job was taken over meanwhile.")
        return
    # The stale event stays open until the file changes again (D-122): an unchanged run does not clear it.
    _clear_failures(claimed.source, fields["finished_at"], keep_kinds=("stale",) if unchanged else ())
    if unchanged:
        ImportLogEntry.objects.create(
            source=claimed.source, kind="unchanged", step="", reason=UNCHANGED_TEXT, count=fields["unchanged_runs"],
            occurred_at=fields["finished_at"],
        )
    if scan:
        logger.info("Transformer station scan completed (%s): %s buildings.", _kind(claimed), transformer.buildings)
        with transaction.atomic():
            security_log.record(
                SecurityEvent.Event.TRANSFORMER_SCAN_SUCCEEDED,
                detail=f"{_kind(claimed)}, {transformer.buildings} buildings",
            )
        return
    if plant:
        logger.info("Plant register import completed (%s): %s plants.", _kind(claimed), count)
        with transaction.atomic():
            security_log.record(
                SecurityEvent.Event.PV_IMPORT_SUCCEEDED, detail=f"{_kind(claimed)}, {count} plants"
            )
        return
    logger.info(
        "Address directory import completed (%s): %s addresses%s.", _kind(claimed), count,
        "" if downloaded is not False else ", the directory was already up to date",
    )
    # The update flag (KONZEPT 3a): an import that started after it was set has answered it. A flag set while this
    # import ran stays for the next daily run.
    ImportRequest.objects.filter(
        pk=1, claim_token=claimed.token, update_flag_set_at__lte=F("started_at"),
    ).update(update_flag_set_at=None)
    with transaction.atomic():
        security_log.record(
            SecurityEvent.Event.ADDRESS_IMPORT_SUCCEEDED, detail=f"{_kind(claimed)}, {count} addresses"
        )


def record_failure(claimed, exc):
    """Store and log a failed import: only the fixed text of an ``ImportFailure``, never upstream data."""
    from .importer import ImportFailure  # the web process reaches this only through the runner's thread

    plant = claimed.source == ImportRequest.Source.PLANT
    scan = claimed.source == ImportRequest.Source.TRANSFORMER
    known = (SAFE_FAILURES | PLANT_FAILURES) if plant else SAFE_FAILURES
    if scan:
        known = SAFE_FAILURES | TRANSFORMER_FAILURES
    reason = str(exc) if isinstance(exc, ImportFailure) and str(exc) in known else UNEXPECTED_FAILURE
    now = timezone.now()
    step = ImportRequest.objects.filter(pk=1).values_list("phase", flat=True).first() or ""
    finished = ImportRequest.objects.filter(pk=1, claim_token=claimed.token).update(
        state=ImportRequest.State.FAILED, finished_at=now, heartbeat_at=None,
        phase="", last_error=reason[:255],
        # The file itself lacks a required column: the daily run waits (``schedule.COLUMNS_RETRY_INTERVAL``).
        **({"columns_missing_at": now} if not plant and not scan and reason in COLUMN_FAILURES else {}),
        # The result of the plant run (D-117) or of the transformer scan (D-118), kept apart from the shared fields above.
        **(_plant_result(ImportRequest.State.FAILED, now, reason[:255]) if plant else {}),
        **(_transformer_result(ImportRequest.State.FAILED, now, reason[:255]) if scan else {}),
    )
    if not finished:
        # Taken over meanwhile: the new holder owns the row.
        logger.warning("An import failed, but its job was taken over meanwhile.")
        return
    _log_failure(claimed.source, reason, step, now)
    what = "Plant register import" if plant else "Transformer station scan" if scan else "Address directory import"
    logger.error(
        "%s failed (%s): %s%s", what, _kind(claimed), reason,
        f" ({type(exc).__name__})" if reason == UNEXPECTED_FAILURE else "",
    )
    with transaction.atomic():
        security_log.record(
            SecurityEvent.Event.PV_IMPORT_FAILED if plant
        else SecurityEvent.Event.TRANSFORMER_SCAN_FAILED if scan else SecurityEvent.Event.ADDRESS_IMPORT_FAILED,
            detail=f"{_kind(claimed)}, {reason}",
        )


def execute(claimed, progress=None):
    """Run the claimed import and record the outcome; re-raises a failure (after recording it)."""
    # Imported locally so web requests never load the downloader.
    from .importer import clear_stale_lease, import_directory

    options = {} if progress is None else {"progress": progress}
    transformer = None
    try:
        if claimed.source == ImportRequest.Source.TRANSFORMER:
            from .transformers import run_scan

            transformer = run_scan(claimed.token, progress=progress)
            count = transformer.buildings
        elif claimed.source == ImportRequest.Source.PLANT:
            from .plants import import_plants

            count = import_plants(claim_token=claimed.token, **options)
        else:
            if claimed.took_over:
                # The process that held the lease on the directory is gone (its heartbeat or lease ran out, D-090).
                clear_stale_lease()
            if claimed.manual:
                count = import_directory(repair=True, **options)
            else:
                count = import_directory(**options)
    except Exception as exc:
        record_failure(claimed, exc)
        # Only a generic failure is stored; the exception goes to the caller (the command or the runner's log).
        raise
    record_success(
        claimed, count, downloaded=None if progress is None else progress.downloaded, transformer=transformer,
    )
    return True


def process_one(*, schedule_automatic=False):
    """The worker's step: claim a queued or due job and run it (no heartbeat: its lease is six hours)."""
    claimed = claim(schedule_automatic=schedule_automatic)
    if claimed is None:
        return False
    return execute(claimed)
