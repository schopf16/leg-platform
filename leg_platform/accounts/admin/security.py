"""Read-only administration of the security log and of the django-axes lockout records."""

from axes.models import AccessAttempt, AccessFailureLog
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.utils import label_for_field, lookup_field
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models import SecurityEvent

# The one entry "Failed logins" shows both models (owner decision): "Locked" (current state) and "Log" (history).
FAILED_LOGIN_MODELS = (AccessAttempt, AccessFailureLog)

def short_identifier(value):
    """The identifier shortened for the screen: the first 8 and the last 4 characters; never the whole digest."""
    if not value:
        return "–"
    value = str(value)
    if len(value) <= 12:
        return value
    return f"{value[:8]}…{value[-4:]}"


# Plain words instead of the product name; the models are those of django-axes.
AccessAttempt._meta.verbose_name = _("blocked or failed login")
AccessAttempt._meta.verbose_name_plural = _("blocked and failed logins")
AccessFailureLog._meta.verbose_name = _("failed login in the log")
AccessFailureLog._meta.verbose_name_plural = _("log of failed logins")


# The security log is named "Security events" in the sidebar (design review E); the model keeps its name.
SecurityEvent._meta.verbose_name_plural = _("Security events")

# The event types by area, for the grouped select of the filter (design review E). A type not named here is "Other".
EVENT_GROUPS = (
    (_("Accounts and access"), (
        "PASSWORD_CHANGED", "PASSWORD_RESET", "PASSKEYS_CHANGED", "SIGN_IN_METHOD_CHANGED", "EMAIL_CHANGE_REQUESTED",
        "EMAIL_CHANGED", "SECONDARY_EMAIL_CHANGED", "TWO_FACTOR_ENABLED", "RECOVERY_CODES_RENEWED", "RECOVERY_CODE_USED",
        "REGISTRATION_COMPLETED", "ACCOUNT_BLOCKED", "ACCOUNT_UNBLOCKED", "ACCOUNT_DEACTIVATED", "ACCOUNT_REACTIVATED",
        "ACCOUNT_DELETED", "ACCOUNT_DATA_CHANGED_BY_STAFF", "PERSON_INVITED", "INVITATION_RESENT",
        "RETENTION_OBLIGATION_SET",
    )),
    (_("Sign-in and codes"), (
        "LOGIN_LOCKOUT", "CODE_ATTEMPTS_WARNING", "CODE_ATTEMPTS_PAUSED", "ALL_SESSIONS_ENDED",
    )),
    (_("Administration"), (
        "PRIVILEGES_CHANGED", "ACCESS_RELATIONS_CHANGED", "PORTAL_SETTINGS_CHANGED", "PORTAL_UPLOAD_REJECTED",
        "LOG_NOTIFICATION_CHANGED", "GRID_OPERATOR_CHANGED", "LIST_PRINTED",
    )),
    (_("Imports and maintenance"), (
        "HOUSEKEEPING_RUN", "HOUSEKEEPING_ERRORS_ACKNOWLEDGED", "ADDRESS_IMPORT_SUCCEEDED", "ADDRESS_IMPORT_FAILED",
        "PV_IMPORT_SUCCEEDED", "PV_IMPORT_FAILED",
    )),
)


class EventGroupFilter(admin.ChoicesFieldListFilter):
    """The event types as one select with a group per area (design review E), not a list of links."""

    title = _("Event type")
    template = "admin/accounts/event_group_filter.html"

    def choices(self, changelist):
        labels = dict(SecurityEvent.Event.choices)
        members = SecurityEvent.Event.__members__
        assigned = set()
        groups = []
        for title, names in EVENT_GROUPS:
            values = [members[name].value for name in names if name in members and members[name].value in labels]
            assigned.update(values)
            if values:
                groups.append({"title": title, "options": [(v, labels[v]) for v in values]})
        rest = [value for value in labels if value not in assigned]
        if rest:
            groups.append({"title": _("Other"), "options": [(v, labels[v]) for v in rest]})
        selected = changelist.params.get(self.lookup_kwarg, "")
        hidden = [(key, value) for key, value in changelist.get_filters_params().items() if key != self.lookup_kwarg]
        if changelist.query:
            hidden.append(("q", changelist.query))
        self.select = {"name": self.lookup_kwarg, "selected": selected, "groups": groups, "hidden": hidden}
        return [self.select]


class SecurityEventAdmin(admin.ModelAdmin):
    """The security log: read only, system administrators only (D-018, D-041).

    Entries leave only through the retention.
    """

    actions = None  # not even the site-wide "delete selected"
    list_display = ("occurred_at", "event", "account_number", "actor_number", "retention")
    list_filter = (("event", EventGroupFilter), "long_retention")
    ordering = ("-occurred_at", "-id")
    list_per_page = 100
    show_full_result_count = False
    fields = readonly_fields = ("occurred_at", "event", "account_id", "actor_id", "long_retention")

    @admin.display(description=_("Account"), ordering="account_id")
    def account_number(self, obj):
        return obj.account_id if obj.account_id is not None else "-"

    @admin.display(description=_("From"), ordering="actor_id")
    def actor_number(self, obj):
        return obj.actor_id if obj.actor_id is not None else "-"

    @admin.display(description=_("kept"))
    def retention(self, obj):
        days = (
            settings.SECURITY_LOG_LONG_RETENTION_DAYS
            if obj.long_retention
            else settings.SECURITY_LOG_RETENTION_DAYS
        )
        return gettext("%(days)d days") % {"days": days}

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyAxesAdmin(admin.ModelAdmin):
    """Pseudonymous lockout records, system administrators only."""

    actions = None
    list_display = ("attempt_time", "short_username", "path_info", "failures_since_start")
    list_filter = ("attempt_time", "path_info")
    ordering = ("-attempt_time",)
    list_per_page = 100
    show_full_result_count = False
    # No GET/POST data and no IP addresses, even read-only.
    fields = readonly_fields = ("username", "path_info", "failures_since_start", "attempt_time")
    search_fields = ("username", "path_info")
    change_list_template = "admin/axes_change_list.html"

    @admin.display(description=_("code (shortened)"), ordering="username")
    def short_username(self, obj):
        return short_identifier(obj.username)

    def changelist_view(self, request, extra_context=None):
        # The one page "Failed logins" shows both lists (owner decision); the old list redirects there for superusers.
        if request.user.is_superuser and request.method == "GET" and not request.GET:
            return HttpResponseRedirect(reverse("admin:failed_logins"))
        from leg_platform.accounts.login_protection import LOGIN_COOLOFF

        minutes = int(LOGIN_COOLOFF.total_seconds() // 60)
        return super().changelist_view(request, {"lockout_minutes": minutes, **(extra_context or {})})

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyLockedAdmin(ReadOnlyAxesAdmin):
    """The "Locked" section of the failed-logins page: only the current blocks, an account or an IP address at the limit
    within the cool-off. The single failed attempts stay in the "Log" section. The change view is not filtered."""

    def get_queryset(self, request):
        from django.utils import timezone

        from leg_platform.accounts.login_protection import LOGIN_COOLOFF, LOGIN_FAILURE_LIMIT

        return super().get_queryset(request).filter(
            failures_since_start__gte=LOGIN_FAILURE_LIMIT,
            attempt_time__gte=timezone.now() - LOGIN_COOLOFF,
        )


class ReadOnlyAxesFailureAdmin(ReadOnlyAxesAdmin):
    list_display = ("attempt_time", "short_username", "path_info", "locked_out")
    fields = readonly_fields = ("username", "path_info", "locked_out", "attempt_time")


def failed_logins_view(request):
    """The one page "Failed logins" (owner decision): two read-only sections, "Locked" (AccessAttempt) and "Log"
    (AccessFailureLog). Superusers only; the columns, filters, search and the cool-off note are those of the two lists."""
    from leg_platform.accounts.login_protection import LOGIN_COOLOFF

    if not request.user.is_superuser:
        raise PermissionDenied
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    minutes = int(LOGIN_COOLOFF.total_seconds() // 60)
    from django.utils import timezone

    from leg_platform.log_filters import redact_personal_data, redact_tokens
    from leg_platform.log_menu import DEFAULT_LINES, LINE_COUNTS, line_count

    count = line_count(request)
    # "lines" is not a filter of the lists: the change lists must not read it as a lookup.
    request.GET = request.GET.copy()
    request.GET.pop("lines", None)
    sections = []
    for model, title, section_id in ((AccessAttempt, _("Locked"), "locked"), (AccessFailureLog, _("Log"), "log")):
        model_admin = ReadOnlyLockedAdmin(model, admin.site) if model is AccessAttempt else ReadOnlyAxesFailureAdmin(
            model, admin.site,
        )
        cl = model_admin.get_changelist_instance(request)
        columns = list(cl.list_display)
        sections.append({
            "id": section_id,
            "title": title,
            "headers": [str(label_for_field(name, model, model_admin)) for name in columns],
            "rows": [[lookup_field(name, obj, model_admin)[2] for name in columns] for obj in cl.result_list],
            "filters": [(spec.title, list(spec.choices(cl))) for spec in cl.filter_specs],
            "query": cl.query,
            "note": minutes if section_id == "locked" else None,
            # The "Log" section is a text box (owner decision): the newest lines at the bottom, redacted.
            "lines": (
                [
                    redact_personal_data(redact_tokens(
                        f"{timezone.localtime(obj.attempt_time):%Y-%m-%d %H:%M:%S}  {short_identifier(obj.username)}  "
                        f"{obj.path_info}  locked_out={obj.locked_out}"
                    ))
                    for obj in reversed(list(cl.queryset[:count]))
                ]
                if section_id == "log" else None
            ),
        })
    context = {
        **admin.site.each_context(request),
        "title": _("Failed logins"),
        "sections": sections,
        "lockout_minutes": minutes,
        "line_counts": LINE_COUNTS,
        "lines_selected": str(count) if str(count) in LINE_COUNTS else DEFAULT_LINES,
    }
    return TemplateResponse(request, "admin/accounts/failed_logins.html", context)
