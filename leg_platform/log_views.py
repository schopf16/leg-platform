"""The log pages of the administration (D-092): list, live view, polling endpoint and download.

For system administrators only (``is_superuser``, like the security log; no permission can give it to
a staff member). Every request names one of two fixed logs and optionally the number of a rotated
backup; the file name is built here from those two values, a client never supplies a path. Every
opening of a page and every download is a security event. Responses are size-bounded and never cached.
"""

import logging
from datetime import datetime

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseNotAllowed, JsonResponse, StreamingHttpResponse
from django.template.response import TemplateResponse
from django.urls import register_converter, reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from leg_platform import logfiles
from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent

logger = logging.getLogger(__name__)

LOGS = {
    "application": logfiles.APPLICATION_LOG,
    "security": logfiles.SECURITY_LOG,
}


class LogKeyConverter:
    """The URL part that names a log: exactly one of the fixed keys, nothing else matches."""

    regex = "|".join(LOGS)

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


# Registered once, when the module is first imported (``PortalAdminSite.get_urls`` imports it).
register_converter(LogKeyConverter, "logkey")


def log_titles():
    return {"application": _("Application log"), "security": _("Security log (file)")}


def require_administrator(request):
    if not request.user.is_superuser:
        raise PermissionDenied


def backup_number(request):
    """The number of the rotated backup (0 = the current file) from ``?n=``; anything else is a 404."""
    raw = request.GET.get("n", "0")
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 2 or int(raw) > logfiles.BACKUPS:
        raise Http404
    return int(raw)


def file_name(log, number):
    """The fixed file name of a log and a backup number; the only way a name is built."""
    base = LOGS[log]
    return base if number == 0 else f"{base}.{number}"


def find_file(log, request):
    """``(name, path or None)``; the path is None while the file does not exist yet (nothing was logged)."""
    name = file_name(log, backup_number(request))
    return name, logfiles.resolve(settings.LOG_DIRECTORY, name)


VIEW_EVENT_INTERVAL = 300  # seconds: opening the same page again within this time is not a new event (L-4)


def record(request, event, name):
    with transaction.atomic():
        security_log.record(event, actor=request.user, detail=name)


def record_view(request, name):
    """The event "log viewed" for ``name``, at most once per session and file in five minutes: a reload or a return to
    the page is not a new event (the security log keeps these events for a year; L-4 of the review of 2026-10-17).
    A download is always an event."""
    now = int(timezone.now().timestamp())
    seen = request.session.get("log_views_seen")
    seen = {key: value for key, value in seen.items() if isinstance(value, int) and 0 <= now - value < VIEW_EVENT_INTERVAL} if isinstance(seen, dict) else {}
    if name in seen:
        return
    seen[name] = now
    request.session["log_views_seen"] = seen
    record(request, SecurityEvent.Event.LOG_VIEWED, name)


def describe(files):
    """Rows for the list page: the file, its log key, its backup number, links and size."""
    rows = []
    for item in files:
        base, _dot, suffix = item["name"].partition(".log")
        number = int(suffix[1:]) if suffix else 0
        if number > logfiles.BACKUPS or base not in LOGS:
            continue
        query = f"?n={number}" if number else ""
        rows.append(
            {
                "name": item["name"],
                "title": log_titles()[base],
                "backup": number,
                "size_kb": f"{item['size'] / 1024:.1f}",
                "modified": datetime.fromtimestamp(item["modified"], tz=timezone.get_current_timezone()),
                "view_url": reverse("admin:logs_view", kwargs={"log": base}) + query,
                "download_url": reverse("admin:logs_download", kwargs={"log": base}) + query,
            }
        )
    return rows


def log_list(request):
    require_administrator(request)
    record_view(request, "list")
    directory = settings.LOG_DIRECTORY
    context = {
        **admin.site.each_context(request),
        "title": _("Log files"),
        "directory_configured": bool(directory),
        "rows": describe(logfiles.list_files(directory)),
        "max_mb": logfiles.MAX_BYTES // (1024 * 1024),
        "backups": logfiles.BACKUPS,
    }
    return TemplateResponse(request, "admin/logs/list.html", context)


def log_view(request, log):
    require_administrator(request)
    name, path = find_file(log, request)
    record_view(request, name)
    number = backup_number(request)
    query = f"?n={number}" if number else ""
    context = {
        **admin.site.each_context(request),
        "title": log_titles()[log],
        "file_name": name,
        "exists": path is not None,
        "tail_url": reverse("admin:logs_tail", kwargs={"log": log}) + query,
        "download_url": reverse("admin:logs_download", kwargs={"log": log}) + query,
        "list_url": reverse("admin:logs"),
        # A rotated backup does not grow: no polling.
        "live": number == 0,
        "poll_seconds": 2,
        "tail_lines": logfiles.TAIL_LINES,
    }
    return TemplateResponse(request, "admin/logs/viewer.html", context)


def log_tail(request, log):
    """JSON with the last lines (no ``offset``) or the new lines since ``offset``; the size of an answer is bounded.

    A poll is not an activity of the session and not a security event of its own (the page opening is).
    """
    require_administrator(request)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    _name, path = find_file(log, request)
    body = {"text": "", "offset": 0, "file_id": "", "reset": False, "exists": path is not None}
    if path is not None:
        try:
            offset = request.GET.get("offset")
            if offset is None:
                text, new_offset, file_id = logfiles.tail(path)
                reset = True
            else:
                if not offset.isascii() or not offset.isdecimal() or len(offset) > 12:
                    raise Http404
                text, new_offset, file_id, reset = logfiles.read_since(
                    path, int(offset), request.GET.get("file_id", "")[:64]
                )
        except OSError:
            logger.warning("A log file could not be read.")
            body["exists"] = False
        else:
            body.update(text=text, offset=new_offset, file_id=file_id, reset=reset)
    response = JsonResponse(body)
    response["Cache-Control"] = "no-store"
    return response


def log_download(request, log):
    """The whole file, redacted line by line, streamed in pieces; the file name of the download is the fixed one."""
    require_administrator(request)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    name, path = find_file(log, request)
    if path is None:
        raise Http404
    record(request, SecurityEvent.Event.LOG_DOWNLOADED, name)
    try:
        chunks = logfiles.redacted_chunks(path)
    except OSError:
        raise Http404 from None
    response = StreamingHttpResponse(chunks, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["Cache-Control"] = "no-store"
    return response
