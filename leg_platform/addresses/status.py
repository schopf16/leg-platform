"""The status of the address directory import for the operator page and its polling endpoint (D-090).

One function builds everything the page shows (texts already translated), so that the page rendered
by the server and the JSON that the script polls say the same. Nothing here writes.
"""

import os

from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from . import jobs, schedule
from .models import ImportRequest, PlantRegister, ServicePostalCode
from .services import directory_status

# The fixed texts of ``importer.ImportFailure`` and of the jobs, in words for the page. A text that is
# not listed (it should not happen) is shown as "unexpected".
REASONS = {
    "Unapproved download URL.": _("The download address is not an approved one."),
    "Official download failed.": _("The official server did not deliver the file. Check that this server can reach data.geo.admin.ch."),
    "Official file not found.": _("The official file was not found on the server."),
    "Unexpected HTTP content encoding.": _("The official server answered in an unexpected format."),
    "Download exceeds the size limit.": _("The official file is larger than allowed."),
    "Download exceeds its resource limit.": _("The download was larger or slower than allowed."),
    "Incomplete download.": _("The download was incomplete. Try again later."),
    "Invalid official catalogue metadata.": _("The catalogue of the official server could not be understood."),
    "CSV exceeds its resource limit.": _("The official file is larger than allowed."),
    "Invalid address field length.": _("The official file contains an address that is too long."),
    "Invalid address field.": _("The official file contains an invalid address."),
    "Expected exactly one CSV member.": _("The official file does not have the expected content."),
    "Unsafe ZIP member.": _("The official file does not have the expected content."),
    "Invalid CSV header.": _("The official file does not have the expected content."),
    "Missing approved CSV columns.": _("The official file does not have the expected content."),
    "Missing EGID column.": _("The official file has no building number (EGID) column. Nothing was changed."),
    "Missing entrance column.": _("The official file has no entrance number (ADR_EDID) column. Nothing was changed."),
    "Missing coordinate column.": _("The official file has no coordinate (ADR_EASTING, ADR_NORTHING) columns. Nothing was changed."),
    "Invalid CSV row count or width.": _("The official file does not have the expected content."),
    "Invalid postal label.": _("The official file contains an invalid postal code."),
    "Empty directory.": _("The official file contains no addresses."),
    "Invalid official archive format.": _("The official file could not be read."),
    "Another import is running.": _("Another import is running."),
    "The import lease was lost.": _("The import was taken over by another process."),
    "The free disk space could not be checked.": _("The free disk space could not be checked."),
    "Not enough free disk space for an import.": _("There is not enough free disk space for the import (at least 4 GB are needed)."),
    "Catalogue data date predates the published directory.": _("The official file is older than the directory in use. Nothing was changed."),
    "A repair was tried recently and did not succeed.": _("A repair was tried a short time ago and did not succeed. Try again in a few hours."),
    "Official archive checksum mismatch.": _("The downloaded file does not match its checksum and was discarded."),
    "Unexpected HTTP content type.": _("The official server answered in an unexpected format."),
    # The plant register (D-117); the texts are the constants of ``plants``.
    "The plant register file has an unexpected format.": _("The plant register file has an unexpected format."),
    "The plant register file is larger than allowed.": _("The plant register file is larger than allowed."),
    "The number of plants changed too much. Nothing was changed.": _(
        "The number of plants changed too much. Nothing was changed."
    ),
    "The plant register contains no plants.": _("The plant register contains no plants."),
    jobs.MAP_UNREACHABLE: _("The map server could not be reached. Try again later."),
    jobs.MAP_ANSWER: _("The map server did not answer as expected."),
    jobs.MAP_TOO_LONG: _("The scan took longer than allowed."),
    jobs.MAP_TOO_MANY_TILES: _("The postal codes cover too large an area for one scan. Use fewer postal codes."),
    jobs.MAP_UNREADABLE: _("The map could not be read."),
    jobs.SCAN_NO_POSTAL_CODES: _("No postal code is set for the scan."),
    jobs.SCAN_NO_BUILDINGS: _("No buildings with a position were found for the postal codes."),
    jobs.SCAN_READER_MISSING: _("The map reader is not installed on this server."),
    "The plant register could not be downloaded or read.": _(
        "The plant register could not be downloaded or read. Check the network connection and try again."
    ),
    "The official directory could not be imported.": _("The official directory could not be downloaded or read. Check the network connection and try again."),
    jobs.UNEXPECTED_FAILURE: _("The import failed unexpectedly. Details in the import log."),
    jobs.INTERRUPTED: _("The import was interrupted because the application stopped or restarted. Request it again."),
}

PHASES = {
    "starting": _("Starting"),
    "catalogue": _("Checking the official catalogue"),
    "downloading": _("Downloading the official file"),
    "importing": _("Importing the addresses"),
    "publishing": _("Publishing the new directory"),
    "finishing": _("Finishing up"),
}


def reason_text(message):
    """The words for a stored failure text; "" for none."""
    if not message:
        return ""
    return str(REASONS.get(message, REASONS[jobs.UNEXPECTED_FAILURE]))


def _when(value):
    """A date and time in the numeric form of the design language (DESIGN section 8): 17.10.2026 02:44."""
    return date_format(timezone.localtime(value), "d.m.Y H:i") if value else ""


def _day(value):
    """A date in the numeric form of the design language (DESIGN section 8): 17.10.2026."""
    return date_format(value, "d.m.Y") if value else ""


# The hint that a source may be stale (owner decision, D-117): its file has not changed for more than this many days. A hint
# only; it changes no import outcome and writes no security event.
STALE_CONTENT_DAYS = 30


def content_warning(changed_on, today=None):
    """The hint for a source whose file has not changed for more than ``STALE_CONTENT_DAYS`` days, with the day of the last
    change in the Swiss form; "" when there is no hint (never imported, or changed within the limit)."""
    if changed_on is None:
        return ""
    today = today or timezone.localdate()
    if (today - changed_on).days <= STALE_CONTENT_DAYS:
        return ""
    return gettext("The file has not changed since %(date)s. Check whether the source is still updated.") % {
        "date": _day(changed_on),
    }


def count_text(value):
    """A count with the Swiss thousands separator, a typographic apostrophe: 341’981 (DESIGN section 8)."""
    return f"{value:,}".replace(",", "’")


def _megabytes(size):
    return f"{size / (1024 * 1024):.1f}".rstrip("0").rstrip(".") if size is not None else ""


def file_size():
    """The size of the directory file in bytes, or None if it cannot be read."""
    try:
        return os.stat(str(settings.DATABASES["addresses"]["NAME"])).st_size
    except OSError:
        return None


def effective_state(job, now=None):
    """The state the person should see: a "running" job without a sign of life is "interrupted"."""
    if job is None:
        return "idle"
    now = now or timezone.now()
    if job.state == ImportRequest.State.RUNNING and not jobs.is_alive(job, now):
        return "interrupted"
    return str(job.state)


def next_automatic_text(job):
    """A sentence about the automatic refresh (the daily run of the scheduled tasks); honest about what triggers it."""
    if not settings.ADDRESS_IMPORT_AUTOMATIC:
        return gettext(
            "The automatic refresh is turned off on this server. The directory is only updated when you "
            "request it here or when the operator starts the import on the server."
        )
    base = gettext(
        "Once a day from %(time)s the scheduled tasks of the server check whether the directory must be "
        "updated: when an address could not be found, or when the regular refresh is due. A request on this "
        "page starts at once."
    ) % {"time": schedule.import_run_time().strftime("%H:%M")}
    if job is not None and job.update_flag_set_at is not None:
        return base + " " + gettext("An address could not be found: the directory is updated at the next daily run.")
    interval = jobs.EMPTY_DIRECTORY_INTERVAL if jobs.directory_is_empty() else jobs.AUTOMATIC_INTERVAL
    last = job.last_automatic_attempt_at if job else None
    if last is None:
        return base + " " + gettext("The first refresh is due now.")
    due = last + interval
    if due <= timezone.now():
        return base + " " + gettext("The next refresh is due now.")
    return base + " " + gettext("The next refresh is due after %(date)s.") % {"date": _when(due)}


def _transformer_status_base(now=None):
    """The block "Trafostationen" of the data imports page (D-118): the postal codes of the service area, the last scan
    with its date and result, and the statistic of the last successful scan. Read only; the scan runs in the weekly run.

    The result is shown with its date whatever day it happened on, until a later scan replaces it (as for the plant
    register). The statistic keeps the values of the last success.
    """
    now = now or timezone.now()
    try:
        codes = list(ServicePostalCode.objects.order_by("postal_code").values_list("postal_code", flat=True))
    except DatabaseError:
        codes = []
    job = ImportRequest.objects.filter(pk=1).first()
    result_state = ""
    result_reason = ""
    run_at = ""
    if job is not None and job.source == ImportRequest.Source.TRANSFORMER and effective_state(job, now) in (
        "running", "interrupted",
    ):
        run_at = _when(job.started_at)
        if effective_state(job, now) == "running":
            result_state = "running"
        else:
            result_state, result_reason = "failed", reason_text(jobs.INTERRUPTED)
    elif job is not None and job.last_transformer_finished_at is not None:
        run_at = _when(job.last_transformer_finished_at)
        if job.last_transformer_state == "failed":
            result_state = "failed"
            result_reason = reason_text(job.last_transformer_error) or reason_text(jobs.UNEXPECTED_FAILURE)
        elif job.last_transformer_state == "succeeded":
            result_state = "succeeded"
    labels = {
        "running": ImportRequest.State.RUNNING.label,
        "succeeded": ImportRequest.State.SUCCEEDED.label,
        "failed": ImportRequest.State.FAILED.label,
        "": _("Not scanned yet"),
    }
    tones = {"running": "warn", "succeeded": "ok", "failed": "bad", "": "muted"}
    has_stats = job is not None and job.last_transformer_buildings is not None
    buildings = job.last_transformer_buildings if has_stats else 0
    with_name = (job.last_transformer_with_name or 0) if has_stats else 0
    return {
        "postal_codes": ", ".join(codes),
        "run_at": run_at,
        "result_label": str(labels[result_state]),
        "result_tone": tones[result_state],
        "result_reason": result_reason,
        "has_stats": has_stats,
        "scanned_postal_codes": job.last_transformer_postal_codes if has_stats else "",
        "buildings": count_text(buildings) if has_stats else "",
        "with_name": count_text(with_name) if has_stats else "",
        "without_name": count_text(buildings - with_name) if has_stats else "",
    }


def _plant_status_base(now=None):
    """The block "PV-Anlagen (BFE)" of the address page (D-117): the statistic of the last successful plant import and
    the last result of the plant run with its date and time. Read only: the plant import runs only in the daily run.

    A failure is shown whatever day it happened on, until a later run replaces it (owner decision 2026-10-25). A success
    is the result only on the day of its run (``last_plant_scheduled_run_on``); an earlier success, or no run at all,
    gives "not run today" (the time of the success stays visible). The statistic keeps the values of the last success
    (``PlantRegister``).
    """
    now = now or timezone.now()
    today = timezone.localdate(now)
    try:
        register = PlantRegister.objects.filter(pk=1).first()
    except DatabaseError:
        register = None
    job = ImportRequest.objects.filter(pk=1).first()
    imported = register is not None and register.imported_at is not None
    result_state = ""
    result_reason = ""
    run_at = ""
    if job is not None and job.source == ImportRequest.Source.PLANT and effective_state(job, now) in ("running", "interrupted"):
        # A plant run that is running, or whose process is gone (it is taken over by the next claim).
        run_at = _when(job.started_at)
        if effective_state(job, now) == "running":
            result_state = "running"
        else:
            result_state, result_reason = "failed", reason_text(jobs.INTERRUPTED)
    elif job is not None and job.last_plant_finished_at is not None:
        run_at = _when(job.last_plant_finished_at)
        if job.last_plant_state == "failed":
            result_state = "failed"
            result_reason = reason_text(job.last_plant_error) or reason_text(jobs.UNEXPECTED_FAILURE)
        elif job.last_plant_state == "succeeded" and job.last_plant_scheduled_run_on == today:
            result_state = "succeeded"
    labels = {
        "running": ImportRequest.State.RUNNING.label,
        "succeeded": ImportRequest.State.SUCCEEDED.label,
        "failed": ImportRequest.State.FAILED.label,
        "": _("Not run today"),
    }
    # The dot of the result (DESIGN section 3.5): a status is a dot plus a word.
    tones = {"running": "warn", "succeeded": "ok", "failed": "bad", "": "muted"}
    older = ""
    if imported:
        older_date = register.data_date or timezone.localtime(register.imported_at).date()
        older = _day(older_date)
    return {
        "imported": imported,
        "imported_at": _when(register.imported_at) if imported else "",
        "data_date": _day(register.data_date) if imported else "",
        "address_count": count_text(register.address_count) if imported else "",
        "file_row_count": count_text(register.file_row_count) if imported else "",
        "row_count": count_text(register.row_count) if imported else "",
        "rejected_count": count_text(register.rejected_count) if imported else "",
        "skipped_count": count_text(register.skipped_count) if imported else "",
        "run_at": run_at,
        "result_state": result_state,
        "result_label": str(labels[result_state]),
        "result_tone": tones[result_state],
        "result_reason": result_reason,
        # After a failed run the values of the last success stay valid; the date names their data.
        "older_data_date": older if result_state == "failed" and imported else "",
        "content_warning": content_warning(register.content_changed_at, today) if imported else "",
    }


# A running import without progress for this long is shown as stalled (D-121). Every step has a longer time-out
# (download and scan), so the warning comes before the time-out.
STALL_AFTER = timedelta(minutes=10)


def _buildings_lines(units):
    """One line per postal code and one for all, "x of y buildings"; a code without a known total shows x only."""
    units = units or {}
    done = {str(code): int(count) for code, count in (units.get("done") or {}).items()}
    total = {str(code): int(count) for code, count in (units.get("total") or {}).items()}
    lines = []
    for code in sorted(set(done) | set(total)):
        if code in total:
            lines.append(gettext("Postal code %(code)s: %(done)s of %(total)s buildings") % {
                "code": code, "done": done.get(code, 0), "total": total[code],
            })
        else:
            lines.append(gettext("Postal code %(code)s: %(done)s buildings") % {"code": code, "done": done[code]})
    if total:
        lines.append(gettext("All postal codes: %(done)s of %(total)s buildings") % {
            "done": sum(done.get(code, 0) for code in total), "total": sum(total.values()),
        })
    return lines


def progress_lines(job, source, now):
    """The progress of a running job of ``source`` (D-121, D-122): the last progress and, after ``STALL_AFTER``, the stall
    warning; the rows read (x of y, or x alone when the total is unknown); the buildings per postal code of the transformer
    scan. A percentage is never computed here."""
    result = {
        "progress_text": "", "stall_text": "", "progress_stalled": False, "rows_text": "", "buildings_lines": [],
    }
    if job is None or job.state != ImportRequest.State.RUNNING or job.source != source:
        return result
    if job.progress_at is not None:
        silent = now - job.progress_at
        minutes = int(silent.total_seconds() // 60)
        result["progress_text"] = gettext("Last progress %(minutes)s minutes ago.") % {"minutes": minutes}
        if silent > STALL_AFTER:
            result["stall_text"] = gettext("No movement for %(minutes)s minutes.") % {"minutes": minutes}
            result["progress_stalled"] = True
    rows_total = getattr(job, "rows_total", None)
    if source in (ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT) and getattr(job, "rows_done", None) is not None:
        if rows_total:
            result["rows_text"] = gettext("%(done)s of %(total)s rows") % {"done": job.rows_done, "total": rows_total}
        else:
            result["rows_text"] = gettext("%(done)s rows") % {"done": job.rows_done}
    if source == ImportRequest.Source.TRANSFORMER:
        result["buildings_lines"] = _buildings_lines(getattr(job, "progress_units", None))
    return result


def _current_job():
    return ImportRequest.objects.filter(pk=1).first()


def plant_status(now=None):
    now = now or timezone.now()
    return {**_plant_status_base(now), **progress_lines(_current_job(), ImportRequest.Source.PLANT, now)}


def transformer_status(now=None):
    now = now or timezone.now()
    return {**_transformer_status_base(now), **progress_lines(_current_job(), ImportRequest.Source.TRANSFORMER, now)}


def build():
    """Everything the page and the polling endpoint show, as plain values (texts translated)."""
    job = ImportRequest.objects.filter(pk=1).first()
    now = timezone.now()
    state = effective_state(job, now)
    status, directory = directory_status()
    active = state in ("queued", "running")
    percent = None
    message = ""
    phase = ""
    if job is not None:
        phase = str(PHASES.get(job.phase, "")) if state == "running" else ""
        if state == "running" and job.phase == "downloading" and job.bytes_total:
            percent = min(100, (job.bytes_done or 0) * 100 // job.bytes_total)
    if state == "idle":
        # The state label already says "no import requested": the message tells what to do (UX-013).
        message = gettext("Use the button below to load or update the directory.")
    elif state == "queued":
        message = gettext("The import is queued and waits to be started.")
    elif state == "running":
        message = phase or gettext("The import is running.")
    elif state == "interrupted":
        # The reason follows in its own line ("Reason"): the headline does not repeat it.
        message = gettext("The import did not finish.")
    elif state == "succeeded":
        message = (
            gettext("The directory was already up to date. Nothing was downloaded.")
            if job.phase == "unchanged"
            else gettext("The import completed.")
        )
    elif state == "failed":
        message = gettext("The import failed.")
    detail = ""
    if job is not None and state == "running":
        parts = []
        if job.bytes_done is not None:
            parts.append(
                gettext("%(done)s of %(total)s MB downloaded")
                % {"done": _megabytes(job.bytes_done), "total": _megabytes(job.bytes_total) or "?"}
            )
        if job.rows_done is not None:
            parts.append(gettext("%(count)s addresses read") % {"count": count_text(job.rows_done)})
        detail = ", ".join(parts)
    error = reason_text(job.last_error) if job is not None and state in ("failed", "interrupted") else ""
    if state == "interrupted" and not error:
        error = reason_text(jobs.INTERRUPTED)
    size = file_size()
    labels = {
        "idle": _("No import requested"),
        "queued": _("Queued"),
        "running": _("Running"),
        "succeeded": _("Completed"),
        "failed": _("Failed"),
        "interrupted": _("Interrupted"),
    }
    return {
        "state": state,
        "state_label": str(labels[state]),
        "active": active,
        "message": message,
        "detail": detail,
        "percent": percent,
        # The counts of the running job, the same numbers as the detail line: a percentage only where the total is known.
        "progress_rows": job.rows_done if job is not None else None,
        "progress_bytes_done": job.bytes_done if job is not None else None,
        "progress_bytes_total": job.bytes_total if job is not None else None,
        "progress_at": _when(job.progress_at) if job is not None else "",
        **progress_lines(job, ImportRequest.Source.ADDRESS, now),
        "error": error,
        "requested_at": _when(job.requested_at) if job else "",
        "started_at": _when(job.started_at) if job else "",
        "finished_at": _when(job.finished_at) if job else "",
        "last_automatic_attempt_at": _when(job.last_automatic_attempt_at) if job else "",
        "automatic_text": next_automatic_text(job),
        "directory": {
            "available": directory is not None,
            "data_date": date_format(directory.data_date, "d.m.Y") if directory and directory.data_date else "",
            "imported_at": _when(directory.imported_at) if directory else "",
            "row_count": count_text(directory.row_count) if directory else "",
            "file_megabytes": _megabytes(size),
            "warning": str(status["warning"]) if status["warning"] else "",
            "content_warning": content_warning(directory.content_changed_at) if directory else "",
        },
        "server_time": _when(now),
    }
