"""The status of the data imports for the operator page and its polling endpoint (D-090, D-132).

One function builds everything the page shows (texts already translated), so that the page rendered
by the server and the JSON that the script polls say the same. Nothing here writes.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from . import jobs, published
from .models import ImportRequest

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


def megabytes_text(size):
    """A size in megabytes, one decimal, the Swiss thousands separator: 1’269.9 (DESIGN section 8)."""
    if size is None:
        return ""
    return f"{size / (1024 * 1024):,.1f}".replace(",", "’")


def effective_state(job, now=None):
    """The state the person should see: a "running" job without a sign of life is "interrupted"."""
    if job is None:
        return "idle"
    now = now or timezone.now()
    if job.state == ImportRequest.State.RUNNING and not jobs.is_alive(job, now):
        return "interrupted"
    return str(job.state)


def next_automatic_text(job):
    """One sentence about the automatic refresh (design review 4): when the next one is due."""
    if not settings.ADDRESS_IMPORT_AUTOMATIC:
        return gettext(
            "The automatic refresh is turned off on this server. The directory is only updated when you "
            "request it here or when the operator starts the import on the server."
        )
    if job is not None and job.update_flag_set_at is not None:
        return gettext("An address could not be found: the directory is updated at the next daily run.")
    interval = jobs.EMPTY_DIRECTORY_INTERVAL if jobs.directory_is_empty() else jobs.AUTOMATIC_INTERVAL
    last = job.last_automatic_attempt_at if job else None
    if last is None:
        return gettext("The first refresh is due now.")
    due = last + interval
    if due <= timezone.now():
        return gettext("The next refresh is due now.")
    return gettext("The next refresh is due after %(date)s.") % {"date": _when(due)}


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
                "code": code, "done": count_text(done.get(code, 0)), "total": count_text(total[code]),
            })
        else:
            lines.append(gettext("Postal code %(code)s: %(done)s buildings") % {"code": code, "done": done[code]})
    if total:
        lines.append(gettext("All postal codes: %(done)s of %(total)s buildings") % {
            "done": count_text(sum(done.get(code, 0) for code in total)), "total": count_text(sum(total.values())),
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
            result["rows_text"] = gettext("%(done)s of %(total)s rows") % {
                "done": count_text(job.rows_done), "total": count_text(rows_total),
            }
        else:
            result["rows_text"] = gettext("%(done)s rows") % {"done": count_text(job.rows_done)}
    if source == ImportRequest.Source.TRANSFORMER:
        result["buildings_lines"] = _buildings_lines(getattr(job, "progress_units", None))
    return result


def _current_job():
    return ImportRequest.objects.filter(pk=1).first()


SOURCE_ORDER = (ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT, ImportRequest.Source.TRANSFORMER)

# The names of the sources on the page (D-132): plain, no provider names.
SOURCE_NAMES = {
    ImportRequest.Source.ADDRESS: _("Address directory"),
    ImportRequest.Source.PLANT: _("PV plants"),
    ImportRequest.Source.TRANSFORMER: _("Transformer stations"),
}
STATE_LABELS = {
    "idle": _("Not run yet"),
    "queued": _("Waiting"),
    "running": _("Running"),
    "succeeded": _("Up to date"),
    "failed": _("Failed"),
    "interrupted": _("Interrupted"),
}
# A status is a dot plus a word (DESIGN section 3.5).
STATE_TONES = {
    "idle": "muted", "queued": "muted", "running": "warn", "succeeded": "ok", "failed": "bad", "interrupted": "bad",
}
# The share of the whole run that each phase covers (D-132): the percent is the start of the phase plus the part of the
# phase that is done. The bounds only ever grow, so the percent never goes back within a run.
PHASE_START = {"starting": 0, "catalogue": 0, "downloading": 2, "importing": 45, "publishing": 95, "finishing": 98}
PHASE_END = {"starting": 0, "catalogue": 2, "downloading": 45, "importing": 95, "publishing": 98, "finishing": 100}
SCAN_END = 95  # the transformer scan has no import phase: its map tiles take the whole span up to publishing
# The remaining time is not computed before the run has some progress and some time behind it.
ETA_MIN_PERCENT = 3
ETA_MIN_SECONDS = 5


def _ratio(done, total):
    if not total or done is None:
        return 0.0
    return max(0.0, min(1.0, done / total))


def _building_totals(job):
    units = job.progress_units or {}
    done = sum(int(value) for value in (units.get("done") or {}).values())
    total = sum(int(value) for value in (units.get("total") or {}).values())
    return done, total


def percent_of(job):
    """The percent of a running job, 0 to 100, from the phase and the counters; ``None`` when ``job`` is not running."""
    if job is None or job.state != ImportRequest.State.RUNNING:
        return None
    phase = job.phase if job.phase in PHASE_START else "starting"
    start, end = PHASE_START[phase], PHASE_END[phase]
    if job.source == ImportRequest.Source.TRANSFORMER and phase == "downloading":
        done, total = _building_totals(job)
        end = SCAN_END
        return int(start + (end - start) * _ratio(done, total))
    if phase == "downloading":
        return int(start + (end - start) * _ratio(job.bytes_done, job.bytes_total))
    if phase == "importing":
        return int(start + (end - start) * _ratio(job.rows_done, job.rows_total))
    return start


def remaining_seconds(job, percent, now):
    """The seconds left, from the time spent and the percent; ``None`` while it cannot be told (too early, no progress)."""
    if job is None or percent is None or job.started_at is None or percent < ETA_MIN_PERCENT or percent >= 100:
        return None
    elapsed = (now - job.started_at).total_seconds()
    if elapsed < ETA_MIN_SECONDS:
        return None
    return int(elapsed * (100 - percent) / percent)


def _result_of(job, source):
    """The last finished run of ``source``: ``(state, finished_at, error)``; the state is "" when it never ran."""
    if source == ImportRequest.Source.PLANT:
        return job.last_plant_state, job.last_plant_finished_at, job.last_plant_error
    if source == ImportRequest.Source.TRANSFORMER:
        return job.last_transformer_state, job.last_transformer_finished_at, job.last_transformer_error
    return job.last_address_state, job.last_address_finished_at, job.last_address_error


def _figure(key, label, published, pending=None):
    """One line of the detail table. ``published`` is never empty (a dash when nothing was ever published)."""
    return {"key": key, "label": str(label), "published": published or "–", "pending": pending}


def _published_figures(source, job, running, now):
    """The figures of the published data of ``source`` and, while it runs, the in-progress ones (D-132).

    ``running``: the job of this source is running now (the in-progress column is shown). Returns
    ``(figures, key text, content warning)``."""
    pending_rows = count_text(job.rows_done) if running and job.rows_done is not None else "–"
    pending_date = _day(job.pending_data_date) if running and job.pending_data_date else "–"
    if source == ImportRequest.Source.TRANSFORMER:
        has_stats = job is not None and job.last_transformer_buildings is not None
        buildings = job.last_transformer_buildings if has_stats else 0
        done, _total = _building_totals(job) if running else (0, 0)
        codes_pending = ", ".join(sorted((job.progress_units or {}).get("total") or {})) if running else ""
        figures = [
            _figure("finished_at", _("Last scan"), _when(job.last_transformer_finished_at) if has_stats else ""),
            _figure("postal_codes", _("Postal codes"), job.last_transformer_postal_codes if has_stats else "",
                    (codes_pending or "–") if running else None),
            _figure("rows", _("Buildings"), count_text(buildings) if has_stats else "",
                    count_text(done) if running and done else ("–" if running else None)),
            # Only known after the stations are matched at the end of the scan.
            _figure("with_name", _("With a transformer station"),
                    count_text(job.last_transformer_with_name or 0) if has_stats else "", "–" if running else None),
        ]
        return figures, count_text(buildings) + " " + gettext("buildings") if has_stats else "", ""
    snapshot = published.get(source)
    plant = source == ImportRequest.Source.PLANT
    data_date = _day(snapshot.data_date) if snapshot and snapshot.data_date else ""
    stored = bool(snapshot and snapshot.imported_at)
    figures = [
        _figure("data_date", _("Data date"), data_date, pending_date if running else None),
        _figure("imported_at", _("Imported at"), _when(snapshot.imported_at) if stored else "", "–" if running else None),
        _figure("rows", _("Plants") if plant else _("Addresses"), count_text(snapshot.rows) if stored else "",
                pending_rows if running else None),
    ]
    if plant:
        figures.append(_figure(
            "rejected", _("Rejected rows"),
            count_text(snapshot.rejected) if stored and snapshot.rejected is not None else "",
            count_text(job.pending_rejected) if running and job.pending_rejected is not None else ("–" if running else None),
        ))
        figures.append(_figure(
            "skipped", _("Rows without a building"),
            count_text(snapshot.skipped) if stored and snapshot.skipped is not None else "",
            count_text(job.pending_skipped) if running and job.pending_skipped is not None else ("–" if running else None),
        ))
    warning = ""
    if stored:
        warning = content_warning(snapshot.content_changed_at)
        if not warning and not plant and snapshot.data_date and snapshot.data_date < timezone.localdate() - timedelta(days=92):
            warning = gettext("The address directory is more than 92 days old.")
    key_text = ""
    if stored:
        key_text = count_text(snapshot.rows) + " " + (gettext("plants") if plant else gettext("addresses"))
    return figures, key_text, warning


def _source_status(source, job, now, waiting):
    """One row of the page: state, progress, last run and the figures of one source."""
    held = job.source == source and job.state == ImportRequest.State.RUNNING
    state = effective_state(job, now) if held else ""
    if state not in ("running", "interrupted"):
        state = ""
    position = None
    last_state, finished, error_text = _result_of(job, source)
    if not state and source in waiting:
        state, position = "queued", waiting.index(source) + 1
    if not state:
        state = {"succeeded": "succeeded", "failed": "failed"}.get(last_state, "idle")
    running = state == "running"
    percent = percent_of(job) if running else None
    lines = progress_lines(job, source, now) if running else {}
    if state == "interrupted":
        error = reason_text(jobs.INTERRUPTED)
        finished = job.finished_at or job.heartbeat_at
    elif state == "failed":
        error = reason_text(error_text) or reason_text(jobs.UNEXPECTED_FAILURE)
    else:
        error = ""
    figures, key_text, warning = _published_figures(source, job, running, now)
    return {
        "key": str(source),
        "name": str(SOURCE_NAMES[source]),
        "state": state,
        "state_label": str(STATE_LABELS[state]),
        "tone": STATE_TONES[state],
        "position": position,
        "percent": percent,
        "remaining_seconds": remaining_seconds(job, percent, now) if running else None,
        "started_at": job.started_at.isoformat() if running and job.started_at else "",
        "phase": str(PHASES.get(job.phase, "")) if running else "",
        "stall": lines.get("stall_text", ""),
        "rows_text": lines.get("rows_text", ""),
        "buildings_lines": lines.get("buildings_lines", []),
        "error": error,
        "last_run": _when(finished) if finished else "",
        "key_figures": key_text,
        "warning": warning,
        "figures": figures,
        "pending": running,
        "busy": state in ("queued", "running"),
    }


def build():
    """Everything the page and the polling endpoint show, as plain values (texts translated): one entry per source, the
    queue and the state of the whole page (D-132). Nothing here writes."""
    job = ImportRequest.objects.filter(pk=1).first() or ImportRequest(pk=1)
    now = timezone.now()
    waiting = jobs.queued_sources()
    sources = [_source_status(source, job, now, waiting) for source in SOURCE_ORDER]
    running = [row for row in sources if row["state"] == "running"]
    queued = [row for row in sources if row["state"] == "queued"]
    summary = ""
    if running or queued:
        summary = gettext("%(running)s running, %(waiting)s waiting") % {
            "running": len(running), "waiting": len(queued),
        }
    return {
        "active": bool(running or queued),
        "all_busy": all(row["busy"] for row in sources),
        "summary": summary,
        "sources": sources,
        "automatic_text": next_automatic_text(job),
        "server_time": _when(now),
    }
