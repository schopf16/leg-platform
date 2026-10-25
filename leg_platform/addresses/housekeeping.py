"""Weekly housekeeping of both databases (D-119).

SQLite, address directory file (alias ``addresses``): ``PRAGMA quick_check`` before and after, then ``VACUUM`` in
place. Only when no import runs and the free disk space is at least the file size plus 1 GiB. Never rebuilds,
deletes or changes data; a rebuild happens only in the setup after a schema change (``setup.prepare_directory``).

MariaDB, application database: ``OPTIMIZE TABLE`` on the tables with many deletes, only when the status file of the
housekeeping dump (written by a root unit shortly before the run, ``docs/DEPLOYMENT.md``, section 12.5) reports a
successful dump that is not older than ``BACKUP_MAX_AGE``. The application only reads that status file. It never
creates, deletes or changes a dump; the daily backup is never read or changed (D-119).

One run at a time. The run takes a lease in ``HousekeepingStatus`` under the row lock of ``ImportRequest`` (the import
claim, ``jobs.claim``); an import is refused while the lease is active, and a run is refused while an import runs.
A failed step records a fixed message code and does not stop the other step. Every run writes one security event.
"""

import json
import logging
import os
import shutil
import stat
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

from django.db import DatabaseError, connections, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from leg_platform.accounts import security_log
from leg_platform.accounts.models import ConfirmationFailure, LoginIpAttempt, MailJob, SecurityEvent, SessionCodeFailure

from . import jobs, storage
from .models import BuildingLookup, HousekeepingError, HousekeepingStatus, ImportRequest

logger = logging.getLogger(__name__)

DEFAULT_ALIAS = "default"
# The status file of the housekeeping dump, written by the root unit (docs/DEPLOYMENT.md, section 12.5). Read only:
# the web service user cannot read the backup directory itself.
BACKUP_STATUS_PATH = Path("/var/lib/leg-platform/housekeeping-backup.json")
BACKUP_MAX_AGE = timedelta(hours=2)
BACKUP_STATUS_MAX_BYTES = 4096
# A backup time more than this in the future is not trusted (clock skew tolerance).
FUTURE_TOLERANCE = timedelta(minutes=5)
FREE_SPACE_MARGIN = 1024 ** 3
# The lease of a run: a run that is killed is taken over after this time.
RUN_LEASE = timedelta(hours=3)
# The scheduled run: Sunday from this time of day (instance time). The daily address import runs at 03:00 by default.
ERROR_LINE_COUNTS = (50, 200, 500)
ERROR_LINES_DEFAULT = 200
SUNDAY = 6
SCHEDULED_TIME = time(4, 30)

# Run results (returned by the functions below, not stored).
DONE = "done"
STARTED = "started"
BUSY = "busy"
SETUP_RUNNING = "setup_running"
ERROR_OPEN = "error_open"
IMPORT_RUNNING = "import_running"
NOT_SUNDAY = "not_sunday"
BEFORE_TIME = "before_time"
ALREADY_RAN = "already_ran"

# The fixed message codes of the steps (stored, shown and logged); the texts are translated in ``MESSAGES``.
SQLITE_COMPACTED = "sqlite_compacted"
SQLITE_MISSING = "sqlite_missing"
SQLITE_DISK_SPACE = "sqlite_disk_space"
SQLITE_CHECK_FAILED = "sqlite_check_failed"
SQLITE_CHECK_FAILED_AFTER = "sqlite_check_failed_after"
SQLITE_VACUUM_FAILED = "sqlite_vacuum_failed"
MARIADB_NOT_MARIADB = "mariadb_not_mariadb"
MARIADB_OPTIMISED = "mariadb_optimised"
MARIADB_OPTIMISE_FAILED = "mariadb_optimise_failed"
BACKUP_MISSING = "backup_missing"
BACKUP_UNREADABLE = "backup_unreadable"
BACKUP_INVALID = "backup_invalid"
BACKUP_FAILED = "backup_failed"
BACKUP_OLD = "backup_old"
STEP_FAILED = "step_failed"


def message_text(code):
    """The translated text of a message code ("" for none)."""
    texts = {
        SQLITE_COMPACTED: _("Free pages were returned to the file system."),
        SQLITE_MISSING: _("The address directory file is missing. Nothing was done."),
        SQLITE_DISK_SPACE: _("Not enough free disk space. The file was not compacted."),
        SQLITE_CHECK_FAILED: _("The integrity check failed. The file was not compacted."),
        SQLITE_CHECK_FAILED_AFTER: _("The integrity check failed after the compaction."),
        SQLITE_VACUUM_FAILED: _("The compaction failed."),
        MARIADB_NOT_MARIADB: _("The application database is not MariaDB. Nothing was done."),
        MARIADB_OPTIMISED: _("The tables were optimised."),
        MARIADB_OPTIMISE_FAILED: _("The optimisation failed."),
        BACKUP_MISSING: _("No dump status of the housekeeping is available. Nothing was optimised."),
        BACKUP_UNREADABLE: _("The dump status of the housekeeping cannot be read. Nothing was optimised."),
        BACKUP_INVALID: _("The dump status of the housekeeping is invalid. Nothing was optimised."),
        BACKUP_FAILED: _("The last dump of the housekeeping failed. Nothing was optimised."),
        BACKUP_OLD: _("The last dump of the housekeeping is too old. Nothing was optimised."),
        STEP_FAILED: _("The step failed unexpectedly."),
    }
    return texts.get(code, "")


def optimised_tables():
    """The tables of the MariaDB step: the ones with many deletes (sessions, login attempts, mail queue, lookups)."""
    from django.contrib.sessions.models import Session

    return [
        Session._meta.db_table,
        LoginIpAttempt._meta.db_table,
        SessionCodeFailure._meta.db_table,
        ConfirmationFailure._meta.db_table,
        MailJob._meta.db_table,
        BuildingLookup._meta.db_table,
    ]


def _week(now):
    year, week, _day = timezone.localtime(now).isocalendar()
    return f"{year}-W{week:02d}"


def _claim(trigger, actor, now, once_per_week=False):
    """Take the lease of a run under the row lock of the import claim; ``None`` when started, else the refusal."""
    HousekeepingStatus.objects.get_or_create(pk=1)
    with transaction.atomic():
        ImportRequest.objects.get_or_create(pk=1)
        job = ImportRequest.objects.select_for_update().get(pk=1)
        status = HousekeepingStatus.objects.select_for_update().get(pk=1)
        if status.running_until is not None and status.running_until > now:
            return BUSY
        if jobs.is_alive(job, now):
            return IMPORT_RUNNING
        # The setup of the directory file replaces it (D-075): nothing runs meanwhile (as in jobs.claim).
        if storage.reset_marker(connections[storage.DIRECTORY_ALIAS].settings_dict["NAME"]).exists():
            return SETUP_RUNNING
        week = _week(now)
        if once_per_week and status.last_run_week == week:
            return ALREADY_RAN
        # The weekly run never repeats while an error is open: the failed dump is not replaced (owner decision).
        if once_per_week and open_error_exists():
            return ERROR_OPEN
        status.running_until = now + RUN_LEASE
        status.started_at = now
        status.finished_at = None
        status.trigger = trigger
        status.last_run_week = week
        status.save()
        security_log.record(SecurityEvent.Event.HOUSEKEEPING_RUN, actor=actor)
    return None


def _file_size(path):
    """Bytes of the database file and its WAL file."""
    total = 0
    for candidate in (path, Path(f"{path}-wal")):
        try:
            total += candidate.stat().st_size
        except FileNotFoundError:
            pass
    return total


def _quick_check(alias):
    with connections[alias].cursor() as cursor:
        cursor.execute("PRAGMA quick_check")
        rows = cursor.fetchall()
    return rows == [("ok",)]


def _vacuum(alias):
    with connections[alias].cursor() as cursor:
        cursor.execute("VACUUM")


def _directory_path():
    return Path(connections.databases[storage.DIRECTORY_ALIAS]["NAME"])


def _sqlite_step(now):
    """The address directory step: returns the result fields of ``HousekeepingStatus``."""
    alias = storage.DIRECTORY_ALIAS
    path = _directory_path()
    if not path.is_file():
        return _sqlite_result(HousekeepingStatus.State.SKIPPED, SQLITE_MISSING)
    size_before = _file_size(path)
    # VACUUM writes a temporary copy of about the same size next to the file: SQLite puts its temporary files in the
    # folder of the file (SQLITE_TMPDIR, addresses/sqlite/base.py), so the free space there must cover the copy too.
    if shutil.disk_usage(path.parent).free < 2 * size_before + FREE_SPACE_MARGIN:
        return _sqlite_result(HousekeepingStatus.State.SKIPPED, SQLITE_DISK_SPACE, size_before)
    if not _quick_check(alias):
        return _sqlite_result(HousekeepingStatus.State.FAILED, SQLITE_CHECK_FAILED, size_before)
    try:
        _vacuum(alias)
    except DatabaseError:
        return _sqlite_result(HousekeepingStatus.State.FAILED, SQLITE_VACUUM_FAILED, size_before, _file_size(path))
    size_after = _file_size(path)
    if not _quick_check(alias):
        return _sqlite_result(HousekeepingStatus.State.FAILED, SQLITE_CHECK_FAILED_AFTER, size_before, size_after)
    return _sqlite_result(HousekeepingStatus.State.SUCCEEDED, SQLITE_COMPACTED, size_before, size_after)


def _sqlite_result(state, message, size_before=None, size_after=None):
    return {
        "sqlite_state": state, "sqlite_message": message, "sqlite_finished_at": timezone.now(),
        "sqlite_size_before": size_before, "sqlite_size_after": size_after,
    }


def read_backup_status(path=None):
    """The backup status file: ``(dict, "")`` or ``(None, message code)``. Read only; never changes the file."""
    path = path or BACKUP_STATUS_PATH
    try:
        # O_NONBLOCK: an open of a FIFO does not wait for a writer; the S_ISREG check below refuses it.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, BACKUP_MISSING
    except OSError:
        return None, BACKUP_UNREADABLE
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None, BACKUP_INVALID
            raw = handle.read(BACKUP_STATUS_MAX_BYTES + 1)
    except OSError:
        return None, BACKUP_UNREADABLE
    if len(raw) > BACKUP_STATUS_MAX_BYTES:
        return None, BACKUP_INVALID
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, BACKUP_INVALID
    if not isinstance(data, dict):
        return None, BACKUP_INVALID
    finished, ok, name = (data.get(key) for key in ("finished_at", "ok", "file_name"))
    if not (isinstance(finished, str) and isinstance(ok, bool) and isinstance(name, str)):
        return None, BACKUP_INVALID
    try:
        finished_at = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None, BACKUP_INVALID
    if timezone.is_naive(finished_at):
        return None, BACKUP_INVALID
    return {"finished_at": finished_at, "ok": ok, "file_name": name}, ""


def backup_verdict(now, path=None):
    """``None`` when the last backup succeeded and is recent enough; else the message code of the reason."""
    data, code = read_backup_status(path)
    if code:
        return code
    if not data["ok"]:
        return BACKUP_FAILED
    if data["finished_at"] > now + FUTURE_TOLERANCE:
        return BACKUP_INVALID
    if now - data["finished_at"] > BACKUP_MAX_AGE:
        return BACKUP_OLD
    return None


def _table_sizes(connection, tables):
    """Bytes (data and index) of the tables; {} when the server does not tell (cheap read only)."""
    if not tables:
        return {}
    placeholders = ", ".join(["%s"] * len(tables))
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT TABLE_NAME, DATA_LENGTH + INDEX_LENGTH FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})",
                tables,
            )
            return {name: int(size or 0) for name, size in cursor.fetchall()}
    except DatabaseError:
        return {}


def _is_mariadb(connection):
    return connection.vendor == "mysql"


def _optimise(connection, tables):
    """OPTIMIZE TABLE of each table (locks the table while it runs: docs/DEPLOYMENT.md, section 12.5)."""
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f"OPTIMIZE TABLE {connection.ops.quote_name(table)}")
            cursor.fetchall()


def _mariadb_step(now, path=None):
    """The application database step: returns the result fields of ``HousekeepingStatus``."""
    connection = connections[DEFAULT_ALIAS]
    if not _is_mariadb(connection):
        return _mariadb_result(HousekeepingStatus.State.SKIPPED, MARIADB_NOT_MARIADB)
    reason = backup_verdict(now, path)
    if reason:
        return _mariadb_result(HousekeepingStatus.State.SKIPPED, reason)
    tables = optimised_tables()
    before = _table_sizes(connection, tables)
    try:
        _optimise(connection, tables)
    except DatabaseError:
        return _mariadb_result(HousekeepingStatus.State.FAILED, MARIADB_OPTIMISE_FAILED, before, _table_sizes(connection, tables))
    after = _table_sizes(connection, tables)
    sizes = {name: {"before": before.get(name), "after": after.get(name)} for name in tables}
    return _mariadb_result(HousekeepingStatus.State.SUCCEEDED, MARIADB_OPTIMISED, sizes=sizes)


def _mariadb_result(state, message, before=None, after=None, sizes=None):
    if sizes is None:
        sizes = {name: {"before": before.get(name), "after": (after or {}).get(name)} for name in (before or {})}
    return {
        "mariadb_state": state, "mariadb_message": message, "mariadb_finished_at": timezone.now(),
        "mariadb_sizes": sizes,
    }


def _guarded(step, now, failed_fields):
    """Run one step; an exception is logged by its class only and recorded with a fixed message."""
    try:
        return step(now)
    except Exception as error:  # noqa: BLE001 - only the class is logged, never the text (it may hold data)
        logger.error("A housekeeping step failed (%s).", type(error).__name__)
        return failed_fields(STEP_FAILED)


DUMP_REASONS = frozenset({BACKUP_MISSING, BACKUP_UNREADABLE, BACKUP_INVALID, BACKUP_FAILED, BACKUP_OLD})


def _errors_of(sqlite_fields, mariadb_fields):
    """The failed steps of a run as ``(step, message code)``. A MariaDB step that is not applicable is no error."""
    found = []
    if sqlite_fields["sqlite_state"] != HousekeepingStatus.State.SUCCEEDED:
        found.append((HousekeepingError.Step.SQLITE, sqlite_fields["sqlite_message"]))
    state, message = mariadb_fields["mariadb_state"], mariadb_fields["mariadb_message"]
    if state == HousekeepingStatus.State.FAILED:
        found.append((HousekeepingError.Step.MARIADB, message))
    elif state == HousekeepingStatus.State.SKIPPED and message in DUMP_REASONS:
        found.append((HousekeepingError.Step.DUMP, message))
    return found


def _dump_name():
    """The name of the dump the status file names (no content is read), "" when there is none."""
    backup, _code = read_backup_status()
    return backup["file_name"][:255] if backup else ""


def _record_outcome(errors):
    """Open the errors of a failed run, or clear the open ones after a successful run; the error mail on opening."""
    with transaction.atomic():
        if not errors:
            HousekeepingError.objects.filter(cleared_at__isnull=True).update(cleared_at=timezone.now())
            return
        dump_name = _dump_name()
        for step, reason in errors:
            HousekeepingError.objects.create(step=step, reason=reason, dump_file_name=dump_name)


def open_error_exists():
    return HousekeepingError.objects.filter(cleared_at__isnull=True).exists()


def acknowledge_errors(actor, now=None):
    """Acknowledge the errors: clears the open-error flag only. It touches no dump, no database table, no file and
    starts no run; the next weekly run may then run again (D-119). Returns the number of errors cleared."""
    now = now or timezone.now()
    with transaction.atomic():
        cleared = HousekeepingError.objects.filter(cleared_at__isnull=True).update(cleared_at=now)
        if cleared:
            security_log.record(SecurityEvent.Event.HOUSEKEEPING_ERRORS_ACKNOWLEDGED, actor=actor)
    return cleared


def error_rows(limit=None, open_only=False):
    """The housekeeping errors, newest first: step, fixed reason text, dump file name (never content or SQL)."""
    query = HousekeepingError.objects.order_by("-occurred_at", "-id")
    if open_only:
        query = query.filter(cleared_at__isnull=True)
    if limit:
        query = query[:limit]
    return [
        {
            "occurred_at": error.occurred_at, "step": error.get_step_display(),
            "reason": message_text(error.reason), "dump": error.dump_file_name, "open": error.cleared_at is None,
        }
        for error in query
    ]


def error_lines(count):
    """The newest ``count`` housekeeping errors as text lines, oldest first (the newest at the bottom).

    Each line: time, step, fixed reason text, dump file name (never content or SQL), open or acknowledged.
    """
    rows = error_rows(limit=count)
    rows.reverse()
    return [
        f"{timezone.localtime(row['occurred_at']):%Y-%m-%d %H:%M:%S}  {row['step']}  {row['reason']}  "
        f"{row['dump'] or '-'}  {_('Open') if row['open'] else _('Acknowledged')}"
        for row in rows
    ]


def _execute(now):
    """Both steps, then the results and the release of the lease in one write.

    The write is limited to the run that holds the lease (``started_at``): a run that ends late cannot overwrite the
    results of a newer run. Errors are recorded only for the run that wrote its results.
    """
    fields = {}
    errors = []
    try:
        sqlite_fields = _guarded(
            _sqlite_step, now,
            lambda code: _sqlite_result(HousekeepingStatus.State.FAILED, code),
        )
        mariadb_fields = _guarded(
            _mariadb_step, now,
            lambda code: _mariadb_result(HousekeepingStatus.State.FAILED, code),
        )
        fields = {**sqlite_fields, **mariadb_fields}
        errors = _errors_of(sqlite_fields, mariadb_fields)
    finally:
        written = HousekeepingStatus.objects.filter(pk=1, started_at=now).update(
            running_until=None, finished_at=timezone.now(), **fields,
        )
    if fields and written:
        _record_outcome(errors)
        logger.info(
            "Housekeeping finished: address directory %s, application database %s.",
            fields["sqlite_state"], fields["mariadb_state"],
        )


def _release_lease():
    HousekeepingStatus.objects.filter(pk=1).update(running_until=None, finished_at=timezone.now())


def run_now(trigger, actor=None, now=None):
    """Run the housekeeping in this process (the command, the scheduled run). Returns ``DONE`` or a refusal."""
    now = now or timezone.now()
    refusal = _claim(trigger, actor, now)
    if refusal:
        return refusal
    _execute(now)  # releases the lease in every case
    return DONE


def run_scheduled(now=None):
    """The weekly scheduled run (``run_scheduled_tasks``): Sunday from ``SCHEDULED_TIME``, once per ISO week.

    A manual run in the same week counts as the week's run (its ``last_run_week`` is set), so the scheduled run
    then does not start again.
    """
    now = now or timezone.now()
    local = timezone.localtime(now)
    if local.weekday() != SUNDAY:
        return NOT_SUNDAY
    if local.time() < SCHEDULED_TIME:
        return BEFORE_TIME
    refusal = _claim("scheduled", None, now, once_per_week=True)
    if refusal:
        return refusal
    _execute(now)  # releases the lease in every case
    return DONE


def _thread_body(now):
    storage.mark_thread_writer()
    try:
        _execute(now)
    except BaseException as error:  # noqa: BLE001 - only the class is logged (no traceback, no text)
        logger.error("The housekeeping thread stopped unexpectedly (%s).", type(error).__name__)
    finally:
        storage.unmark_thread_writer()
        connections.close_all()


def start_manual(actor):
    """The button of the administration: take the lease here, run in a thread of this web process (D-090).

    The page shows the result after the run; the status endpoint shows the progress. Returns ``STARTED`` or a refusal.
    """
    now = timezone.now()
    refusal = _claim("manual", actor, now)
    if refusal:
        return refusal
    thread = threading.Thread(target=_thread_body, args=(now,), name="housekeeping", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        _release_lease()
        return BUSY
    return STARTED


def refusal_text(result):
    """The text of a refused start (the page shows it as a warning)."""
    texts = {
        BUSY: _("Another housekeeping run is active."),
        IMPORT_RUNNING: _("An import is running. Try again when it has finished."),
        SETUP_RUNNING: _("The address directory is being set up. Try again later."),
        ERROR_OPEN: _("An error of an earlier run is open. The weekly run waits; start a run here."),
    }
    return texts.get(result, _("The housekeeping did not start."))


def _tone(state):
    """The colour word of a step (a dot plus the word, never colour alone): the tokens of the admin stylesheet."""
    return {"succeeded": "ok", "failed": "bad", "skipped": "warn"}.get(state, "muted")


def _state_label(state):
    """The word of a step: a dot and the word, never colour alone (design review 5)."""
    words = {
        "": _("Not run yet"),
        HousekeepingStatus.State.SUCCEEDED: _("Successful"),
        HousekeepingStatus.State.FAILED: _("Failed"),
        HousekeepingStatus.State.SKIPPED: _("Skipped"),
    }
    return str(words.get(state or "", words[""]))


def _step_rows(status):
    return [
        {
            "label": _("Address directory"), "state": status.sqlite_state, "state_label": _state_label(status.sqlite_state), "tone": _tone(status.sqlite_state),
            "finished_at": status.sqlite_finished_at, "text": message_text(status.sqlite_message),
        },
        {
            "label": _("Application database"), "state": status.mariadb_state, "tone": _tone(status.mariadb_state),
            "state_label": _state_label(status.mariadb_state),
            "finished_at": status.mariadb_finished_at, "text": message_text(status.mariadb_message),
        },
    ]


def page_context(now=None, lines=ERROR_LINES_DEFAULT):
    """The data of the administration page (read only)."""
    now = now or timezone.now()
    status = HousekeepingStatus.objects.get_or_create(pk=1)[0]
    backup, backup_code = read_backup_status()
    sizes = [
        {"name": name, "before": values["before"], "after": values["after"]}
        for name, values in (status.mariadb_sizes or {}).items()
    ]
    return {
        "status": status,
        "running": status.running_until is not None and status.running_until > now,
        "steps": _step_rows(status),
        "sizes": sizes,
        "backup": backup,
        "backup_text": message_text(backup_code) if backup_code else "",
        "backup_fresh": backup_verdict(now) is None,
        "error_lines": error_lines(lines),
        "error_line_counts": ERROR_LINE_COUNTS,
        "lines_selected": lines,
        "open_error": open_error_exists(),
    }


def status_payload(now=None):
    """The JSON of the status endpoint: step states and fixed texts, no table content, no file name."""
    now = now or timezone.now()
    status = HousekeepingStatus.objects.get_or_create(pk=1)[0]
    return {
        "running": status.running_until is not None and status.running_until > now,
        "steps": [
            {"state": row["state"], "text": str(row["text"]),
             "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None}
            for row in _step_rows(status)
        ],
    }
