"""The log menu "Protokolle" (D-120): one page for system administrators only, the e-mail notification of log events.

Sources: the database sources (security events, housekeeping errors) with a time range and a redacted text download;
the two log files are linked to their existing pages (``log_views``). Every page view and download is a security event
(``log_views.record_view`` / ``log_views.record``). The notifier looks at the new rows of the database sources once per
scheduled run (``notify_log_events``) and queues at most one mail per source and hour; the mail holds no log line.
"""

from datetime import datetime, timedelta

from django import forms
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseNotAllowed, HttpResponseRedirect, StreamingHttpResponse
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from leg_platform import log_views
from leg_platform.accounts import security_log
from leg_platform.accounts.models import LogNotificationSettings, MailJob, SecurityEvent
from leg_platform.log_filters import redact_personal_data, redact_tokens

DATABASE_SOURCES = ("security", "housekeeping")
TIME_RANGES = {"1": timedelta(hours=1), "24": timedelta(hours=24), "168": timedelta(days=7), "720": timedelta(days=30)}
DEFAULT_RANGE = "24"
ROW_LIMIT = 200
# One mail per source and this many seconds at most; further events in that time are only counted (owner decision).
MAIL_INTERVAL = timedelta(hours=1)
SEVERITY_RANK = {
    LogNotificationSettings.Severity.WARNING: 1,
    LogNotificationSettings.Severity.ERROR: 2,
    LogNotificationSettings.Severity.CRITICAL: 3,
}
HIGH_SEVERITY_SENTENCE = _(
    "A high-severity entry was recorded. Please sign in to the server and check the log."
)
NOTICE_SENTENCE = _("A notice was recorded.")
# The security events that notify, with their fixed source label and severity. Other events never notify.
NOTIFYING_EVENTS = {
    SecurityEvent.Event.LOGIN_LOCKOUT: ("security", LogNotificationSettings.Severity.WARNING),
    SecurityEvent.Event.ADDRESS_IMPORT_FAILED: ("import", LogNotificationSettings.Severity.ERROR),
    SecurityEvent.Event.PV_IMPORT_FAILED: ("import", LogNotificationSettings.Severity.ERROR),
    SecurityEvent.Event.TRANSFORMER_SCAN_FAILED: ("import", LogNotificationSettings.Severity.ERROR),
}
HOUSEKEEPING_SEVERITY = LogNotificationSettings.Severity.ERROR


def source_label(key):
    return {
        "application": _("application"), "security": _("security"), "housekeeping": _("housekeeping"),
        "import": _("import"), "mail": _("mail"),
    }[key]


def _since(request):
    key = request.GET.get("since", DEFAULT_RANGE)
    if key not in TIME_RANGES:
        key = DEFAULT_RANGE
    return key, timezone.now() - TIME_RANGES[key]


def _shown(request):
    chosen = set(request.GET.getlist("show")) & set(DATABASE_SOURCES)
    return chosen or set(DATABASE_SOURCES)


def require_superuser(request):
    """System administrators only; no permission gives access (owner decision; D-099)."""
    if not request.user.is_superuser:
        raise PermissionDenied


def _security_rows(since):
    return SecurityEvent.objects.filter(occurred_at__gte=since).order_by("-occurred_at", "-pk")[:ROW_LIMIT]


def _housekeeping_rows(since):
    # Imported here: the addresses app is loaded by the admin; the rows are the housekeeping error log (D-119).
    from leg_platform.addresses import housekeeping

    return [row for row in housekeeping.error_rows(limit=ROW_LIMIT) if row["occurred_at"] >= since]


def page(request):
    require_superuser(request)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    log_views.record_view(request, "logmenu")
    key, since = _since(request)
    shown = _shown(request)
    context = {
        **log_views.admin.site.each_context(request),
        "title": _("Logs"),
        "range": key,
        "ranges": [(value, label) for value, label in (
            ("1", _("Last hour")), ("24", _("Last 24 hours")), ("168", _("Last 7 days")), ("720", _("Last 30 days")),
        )],
        "shown": shown,
        "security_rows": _security_rows(since) if "security" in shown else None,
        "housekeeping_rows": _housekeeping_rows(since) if "housekeeping" in shown else None,
        "file_links": [
            ("application", reverse("admin:logs_view", kwargs={"log": "application"})),
            ("security", reverse("admin:logs_view", kwargs={"log": "security"})),
        ],
        "settings_form": NotificationForm(instance=settings_row()),
        "suppressed": suppressed_counts(),
        "log_list_url": reverse("admin:logs"),
    }
    return TemplateResponse(request, "admin/logmenu/page.html", context)


def download(request, source):
    """A text file of one database source: the rows of the chosen range, redacted like the log files."""
    require_superuser(request)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if source not in DATABASE_SOURCES:
        raise Http404
    key, since = _since(request)
    log_views.record(request, SecurityEvent.Event.LOG_DOWNLOADED, f"logmenu-{source}")
    lines = _text_lines(source, since)
    response = StreamingHttpResponse(
        (redact_personal_data(redact_tokens(line)) + "\n" for line in lines),
        content_type="text/plain; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="protokoll-{source}.txt"'
    response["Cache-Control"] = "no-store"
    return response


def _text_lines(source, since):
    if source == "security":
        for row in _security_rows(since):
            yield f"{timezone.localtime(row.occurred_at):%Y-%m-%d %H:%M:%S}\t{row.get_event_display()}"
    else:
        for row in _housekeeping_rows(since):
            yield "\t".join([
                f"{timezone.localtime(row['occurred_at']):%Y-%m-%d %H:%M:%S}", str(row["step"]),
                str(row["reason"]), row["dump"] or "-",
            ])


def settings_row():
    return LogNotificationSettings.objects.get_or_create(pk=1)[0]


class NotificationForm(forms.ModelForm):
    class Meta:
        model = LogNotificationSettings
        fields = ["enabled", "email", "minimum_severity"]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("enabled") and not cleaned.get("email"):
            self.add_error("email", _("Enter an email address, or switch the email off."))
        return cleaned

    def save_settings(self):
        """Save the setting fields only: the bookkeeping of the notifier (``state``) stays as it is."""
        row = self.save(commit=False)
        row.save(update_fields=["enabled", "email", "minimum_severity"])
        return row


def settings_view(request):
    """POST only: change the notification setting. One security event per change, no address in it."""
    require_superuser(request)
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    form = NotificationForm(request.POST, instance=settings_row())
    if form.is_valid():
        with transaction.atomic():
            form.save_settings()
            security_log.record(SecurityEvent.Event.LOG_NOTIFICATION_CHANGED, actor=request.user)
        return HttpResponseRedirect(reverse("admin:logmenu"))
    context = {
        **log_views.admin.site.each_context(request),
        "title": _("Logs"),
        "settings_form": form,
        "range": DEFAULT_RANGE,
        "ranges": [],
        "shown": set(DATABASE_SOURCES),
        "security_rows": None,
        "housekeeping_rows": None,
        "file_links": [],
        "suppressed": suppressed_counts(),
        "log_list_url": reverse("admin:logs"),
    }
    return TemplateResponse(request, "admin/logmenu/page.html", context, status=400)


# --- notification -----------------------------------------------------------------------------------------------

def _state(row):
    state = row.state if isinstance(row.state, dict) else {}
    state.setdefault("checked", {})
    state.setdefault("last_mail", {})
    state.setdefault("suppressed", {})
    state.setdefault("pending", [])
    return state


def suppressed_counts():
    return dict(_state(settings_row())["suppressed"])


def _new_items(state):
    """Notifying items after the last checked row, as (row key, source, severity, time), in row order."""
    checked = state["checked"]
    items = []
    for event in SecurityEvent.objects.filter(
        pk__gt=checked.get("security", 0), event__in=list(NOTIFYING_EVENTS),
    ).order_by("pk"):
        source, severity = NOTIFYING_EVENTS[event.event]
        items.append(("security", event.pk, source, severity, event.occurred_at))
    from leg_platform.addresses.models import HousekeepingError

    for error in HousekeepingError.objects.filter(pk__gt=checked.get("housekeeping", 0)).order_by("pk"):
        items.append(("housekeeping", error.pk, "housekeeping", HOUSEKEEPING_SEVERITY, error.occurred_at))
    return items


def notify_log_events(now=None):
    """Check the new rows of the database sources once; queue at most one mail per source and hour.

    The high-water marks always advance, so a switch turned on later does not mail old events. Returns the number of
    notices queued in this run.
    """
    from leg_platform.accounts import mail_queue

    now = now or timezone.now()
    with transaction.atomic():
        LogNotificationSettings.objects.get_or_create(pk=1)
        row = LogNotificationSettings.objects.select_for_update().get(pk=1)
        state = _state(row)
        queued = 0
        for source_key, pk, source, severity, occurred in _new_items(state):
            state["checked"][source_key] = max(pk, state["checked"].get(source_key, 0))
            if not row.enabled or not row.email:
                continue
            if SEVERITY_RANK[severity] < SEVERITY_RANK[row.minimum_severity]:
                continue
            last = state["last_mail"].get(source)
            if last and now - datetime.fromisoformat(last) < MAIL_INTERVAL:
                state["suppressed"][source] = state["suppressed"].get(source, 0) + 1
                continue
            state["last_mail"][source] = now.isoformat()
            state["pending"].append({"time": occurred.isoformat(), "severity": severity, "source": source})
            queued += 1
        if state["pending"] and not MailJob.objects.filter(kind=MailJob.Kind.LOG_NOTIFICATION).exists():
            mail_queue.enqueue(MailJob.Kind.LOG_NOTIFICATION, row.email)
        row.state = state
        row.save(update_fields=["state"])
    return queued


def take_pending():
    """The notices waiting for the mail, and clears them (called by the mail queue handler when the mail is made)."""
    with transaction.atomic():
        LogNotificationSettings.objects.get_or_create(pk=1)
        row = LogNotificationSettings.objects.select_for_update().get(pk=1)
        state = _state(row)
        pending, state["pending"] = state["pending"], []
        row.state = state
        row.save(update_fields=["state"])
    return pending
