"""Read-only administration of the security log and of the django-axes lockout records."""

from axes.models import AccessAttempt, AccessFailureLog
from django.conf import settings
from django.contrib import admin
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

# Plain words instead of the product name; the models are those of django-axes.
AccessAttempt._meta.verbose_name = _("blocked or failed login")
AccessAttempt._meta.verbose_name_plural = _("blocked and failed logins")
AccessFailureLog._meta.verbose_name = _("failed login in the log")
AccessFailureLog._meta.verbose_name_plural = _("log of failed logins")


class SecurityEventAdmin(admin.ModelAdmin):
    """The security log: read only, system administrators only (D-018, D-041).

    Entries leave only through the retention.
    """

    actions = None  # not even the site-wide "delete selected"
    list_display = ("occurred_at", "event", "account_number", "actor_number", "retention")
    list_filter = ("event", "long_retention")
    ordering = ("-occurred_at", "-id")
    list_per_page = 100
    show_full_result_count = False
    fields = readonly_fields = ("occurred_at", "event", "account_id", "actor_id", "long_retention")

    @admin.display(description=_("account (internal number)"), ordering="account_id")
    def account_number(self, obj):
        return obj.account_id if obj.account_id is not None else "-"

    @admin.display(description=_("performed by (internal number)"), ordering="actor_id")
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
    list_display = ("attempt_time", "username", "path_info", "failures_since_start")
    list_filter = ("attempt_time", "path_info")
    ordering = ("-attempt_time",)
    list_per_page = 100
    show_full_result_count = False
    # No GET/POST data and no IP addresses, even read-only.
    fields = readonly_fields = ("username", "path_info", "failures_since_start", "attempt_time")
    search_fields = ("username", "path_info")
    change_list_template = "admin/axes_change_list.html"

    def changelist_view(self, request, extra_context=None):
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


class ReadOnlyAxesFailureAdmin(ReadOnlyAxesAdmin):
    list_display = ("attempt_time", "username", "path_info", "locked_out")
    fields = readonly_fields = ("username", "path_info", "locked_out", "attempt_time")
