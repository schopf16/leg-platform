"""The log menu "Protokolle" (D-120, D-121, D-122): one page for system administrators only, and the e-mail notice.

Sources: the database sources (security events, housekeeping errors, import errors) with a time range and a redacted text
download; the two log files are linked to their existing pages (``log_views``). Every page view and download is a security
event (``log_views.record_view`` / ``log_views.record``). The notifier looks at the new rows of the database sources once
per scheduled run (``notify_log_events``), applies the ticks of the setting and queues at most one mail per source and
hour. The mail holds no log line, no reason text and no file name.
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

DATABASE_SOURCES = ("security", "housekeeping", "import")
TIME_RANGES = {"1": timedelta(hours=1), "24": timedelta(hours=24), "168": timedelta(days=7), "720": timedelta(days=30)}
DEFAULT_RANGE = "24"
ROW_LIMIT = 200
# One mail per source and this long at most; further events in that time are only counted (owner decision, D-120).
MAIL_INTERVAL = timedelta(hours=1)
HIGH_SEVERITY_SENTENCE = _("A high-severity entry was recorded. Please sign in to the server and check the log.")
NOTICE_SENTENCE = _("A notice was recorded.")
# The security events that notify, with their fixed source label and severity. Other events never notify.
NOTIFYING_EVENTS = {
    SecurityEvent.Event.LOGIN_LOCKOUT: ("security", LogNotificationSettings.Severity.WARNING),
}
HOUSEKEEPING_SEVERITY = LogNotificationSettings.Severity.ERROR
# The import error log (D-121) notifies as the source "import". Its kinds (D-122): an error; the data is old (stale: an
# error when it opens); the downloaded file is unchanged (an information event, level notice).
IMPORT_SEVERITY = LogNotificationSettings.Severity.ERROR
UNCHANGED_SEVERITY = LogNotificationSettings.Severity.NOTICE
KIND_ERROR, KIND_STALE, KIND_UNCHANGED = "error", "stale", "unchanged"
STALE_REASON = _("The address directory has not changed for a long time.")


def failure_texts():
    """Every fixed failure text of the import (addresses, plant register, transformer scan): one tick each (D-122)."""
    from leg_platform.addresses import jobs

    texts = set(jobs.SAFE_FAILURES) | set(jobs.PLANT_FAILURES) | set(jobs.TRANSFORMER_FAILURES)
    texts |= {jobs.UNEXPECTED_FAILURE, jobs.INTERRUPTED}
    return sorted(texts)


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


def _import_rows(since):
    from leg_platform.addresses.models import ImportLogEntry

    return ImportLogEntry.objects.filter(occurred_at__gte=since).order_by("-occurred_at", "-pk")[:ROW_LIMIT]


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
        "import_rows": _import_rows(since) if "import" in shown else None,
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
    elif source == "import":
        for row in _import_rows(since):
            yield "\t".join([
                f"{timezone.localtime(row.occurred_at):%Y-%m-%d %H:%M:%S}", str(row.get_source_display()),
                row.step or "-", str(_(row.reason)),
            ])
    else:
        for row in _housekeeping_rows(since):
            yield "\t".join([
                f"{timezone.localtime(row['occurred_at']):%Y-%m-%d %H:%M:%S}", str(row["step"]),
                str(row["reason"]), row["dump"] or "-",
            ])


def settings_row():
    return LogNotificationSettings.objects.get_or_create(pk=1)[0]


class NotificationForm(forms.ModelForm):
    """The notice setting (D-122): the switch, the one address, the ticks by severity and by event kind, and one tick
    per fixed failure text of the import. A ticked text is mailed; an unticked one is not."""

    failures = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple, label=_("Import errors"))

    class Meta:
        model = LogNotificationSettings
        fields = [
            "enabled", "email", "notify_error", "notify_warning", "notify_notice", "notify_stale", "notify_unchanged",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        muted = set(self.instance.muted_failures or [])
        self.fields["failures"].choices = [(text, _(text)) for text in failure_texts()]
        self.fields["failures"].initial = [text for text in failure_texts() if text not in muted]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("enabled") and not cleaned.get("email"):
            self.add_error("email", _("Enter an email address, or switch the email off."))
        return cleaned

    def save_settings(self):
        """Save the setting fields only: the bookkeeping of the notifier (``state``) stays as it is."""
        row = self.save(commit=False)
        ticked = set(self.cleaned_data.get("failures") or [])
        row.muted_failures = [text for text in failure_texts() if text not in ticked]
        row.save(update_fields=[*self.Meta.fields, "muted_failures"])
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
        "import_rows": None,
        "file_links": [],
        "suppressed": suppressed_counts(),
        "log_list_url": reverse("admin:logs"),
    }
    return TemplateResponse(request, "admin/logmenu/page.html", context, status=400)


# --- notification (D-120, D-122) --------------------------------------------------------------------------------

def _state(row):
    state = row.state if isinstance(row.state, dict) else {}
    state.setdefault("checked", {})
    state.setdefault("last_mail", {})
    state.setdefault("suppressed", {})
    state.setdefault("pending", [])
    return state


def suppressed_counts():
    return dict(_state(settings_row())["suppressed"])


def directory_changed_on():
    """The day the address directory last changed (None when there is no directory)."""
    from leg_platform.addresses.models import Directory

    return Directory.objects.filter(pk=1).values_list("content_changed_at", flat=True).first()


def check_stale(now):
    """The data is old (no change for more than ``STALE_CONTENT_DAYS``): one error event when it opens (D-122).

    The event is cleared by the next successful import of the source that changes the file (``jobs``)."""
    from leg_platform.addresses import status
    from leg_platform.addresses.models import ImportLogEntry, ImportRequest

    changed = directory_changed_on()
    if changed is None:
        return
    stale = (now.date() - changed).days > status.STALE_CONTENT_DAYS
    open_stale = ImportLogEntry.objects.filter(kind=KIND_STALE, cleared_at__isnull=True)
    if stale and not open_stale.exists():
        ImportLogEntry.objects.create(
            source=ImportRequest.Source.ADDRESS, kind=KIND_STALE, step="", reason=STALE_REASON, occurred_at=now,
        )


def _new_items(state):
    """Items after the last checked row: (key, row pk, source, severity, time, kind, reason), in row order.

    ``reason`` is the fixed failure text of an import error (its tick decides); None for the other sources."""
    checked = state["checked"]
    items = []
    for event in SecurityEvent.objects.filter(
        pk__gt=checked.get("security", 0), event__in=list(NOTIFYING_EVENTS),
    ).order_by("pk"):
        source, severity = NOTIFYING_EVENTS[event.event]
        items.append(("security", event.pk, source, severity, event.occurred_at, "security", None))
    from leg_platform.addresses.models import HousekeepingError, ImportLogEntry

    for error in HousekeepingError.objects.filter(pk__gt=checked.get("housekeeping", 0)).order_by("pk"):
        items.append(("housekeeping", error.pk, "housekeeping", HOUSEKEEPING_SEVERITY, error.occurred_at,
                      KIND_ERROR, None))
    for entry in ImportLogEntry.objects.filter(pk__gt=checked.get("import", 0)).order_by("pk"):
        if entry.kind == KIND_UNCHANGED:
            items.append(("import", entry.pk, "import", UNCHANGED_SEVERITY, entry.occurred_at, KIND_UNCHANGED, None))
        elif entry.kind == KIND_STALE:
            items.append(("import", entry.pk, "import", IMPORT_SEVERITY, entry.occurred_at, KIND_STALE, None))
        else:
            items.append(("import", entry.pk, "import", IMPORT_SEVERITY, entry.occurred_at, KIND_ERROR,
                          entry.reason))
    return items


def wanted(row, severity, kind, reason):
    """The rule of the ticks (D-122): every kind is sent only by its own tick(s), nothing else."""
    level = LogNotificationSettings.Severity
    if kind == "security":
        return severity == level.WARNING and row.notify_warning
    if kind == KIND_STALE:
        return row.notify_stale
    if kind == KIND_UNCHANGED:
        return row.notify_unchanged and row.notify_notice
    if kind == KIND_ERROR:
        if severity != level.ERROR or not row.notify_error:
            return False
        # An import error is sent only if its own failure text is ticked (one tick per text).
        return reason is None or reason not in set(row.muted_failures or [])
    return False


def notify_log_events(now=None):
    """Check the database sources once; queue at most one mail per source and hour (D-120, D-122).

    The high-water marks always advance, so a switch turned on later does not mail old events. Returns the number of
    notices queued in this run.
    """
    from leg_platform.accounts import mail_queue

    now = now or timezone.now()
    check_stale(now)
    with transaction.atomic():
        LogNotificationSettings.objects.get_or_create(pk=1)
        row = LogNotificationSettings.objects.select_for_update().get(pk=1)
        state = _state(row)
        queued = 0
        for source_key, pk, source, severity, occurred, kind, reason in _new_items(state):
            state["checked"][source_key] = max(pk, state["checked"].get(source_key, 0))
            if not row.enabled or not row.email or not wanted(row, severity, kind, reason):
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
