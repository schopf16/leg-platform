from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseNotAllowed, HttpResponseRedirect, JsonResponse, StreamingHttpResponse
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from leg_platform import log_views
from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent

from leg_platform.log_filters import redact_personal_data, redact_tokens

from . import housekeeping, jobs, status
from .limits import allow_request
from .models import HousekeepingStatus, ImportLogEntry, ImportRequest, ServicePostalCode

PERMISSION = "addresses.request_directory_import"
# The postal codes of the service area of the transformer station scan (D-118); its own right, like the one above.
SERVICE_AREA_PERMISSION = "addresses.manage_service_areas"
# How often the page asks for the status while an import is queued or running (seconds); a hidden tab asks not at all
# (D-133).
POLL_SECONDS = 5


@admin.register(ServicePostalCode)
class ServicePostalCodeAdmin(admin.ModelAdmin):
    # Only the right ``addresses.manage_service_areas`` (D-118): view, add, change and delete of the postal codes.
    list_display = ("postal_code",)
    search_fields = ("postal_code",)

    def has_module_permission(self, request):
        return request.user.has_perm(SERVICE_AREA_PERMISSION)

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm(SERVICE_AREA_PERMISSION)

    def has_add_permission(self, request):
        return request.user.has_perm(SERVICE_AREA_PERMISSION)

    def has_change_permission(self, request, obj=None):
        return request.user.has_perm(SERVICE_AREA_PERMISSION)

    def has_delete_permission(self, request, obj=None):
        return request.user.has_perm(SERVICE_AREA_PERMISSION)

    # Every change is a security event (D-118) with the postal codes of the service area after the change, nothing else.
    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            self._record_change(request)

    def delete_model(self, request, obj):
        with transaction.atomic():
            super().delete_model(request, obj)
            self._record_change(request)

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            super().delete_queryset(request, queryset)
            self._record_change(request)

    @staticmethod
    def _record_change(request):
        codes = ServicePostalCode.objects.order_by("postal_code").values_list("postal_code", flat=True)
        detail = "postal codes: " + (", ".join(codes) or "none")
        security_log.record(SecurityEvent.Event.SERVICE_AREA_CHANGED, actor=request.user, detail=detail)


@admin.register(ImportRequest)
class ImportRequestAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.has_perm(PERMISSION)

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm(PERMISSION)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        # Only a status page, its polling endpoint and the CSRF-protected start; no generic model editing.
        return [
            path("", self.admin_site.admin_view(self.directory_view),
                 name="addresses_importrequest_changelist"),
            path("status/", self.admin_site.admin_view(self.status_view),
                 name="addresses_importrequest_status"),
            path("start/<str:source>/", self.admin_site.admin_view(self.start_view),
                 name="addresses_importrequest_start"),
            path("log/", self.admin_site.admin_view(self.log_view), name="addresses_importrequest_log"),
            path("log/download/<str:source>/", self.admin_site.admin_view(self.log_download),
                 name="addresses_importrequest_log_download"),
        ]

    def log_view(self, request):
        """The old address of the import log: it redirects to its section on the import page (owner decision, D-121).
        Only system administrators; the download and its redaction are unchanged."""
        from leg_platform.log_menu import require_superuser

        require_superuser(request)
        return HttpResponseRedirect(reverse("admin:addresses_importrequest_changelist") + "#import-log")

    def log_download(self, request, source):
        """The rows of the import error log as text, redacted like the log files (D-121, D-099)."""
        from leg_platform.log_menu import require_superuser

        require_superuser(request)
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        if source != "all" and source not in IMPORT_LOG_SOURCES:
            raise Http404
        log_views.record(request, SecurityEvent.Event.LOG_DOWNLOADED, f"importlog-{source}")
        lines = (
            "\t".join([
                f"{timezone.localtime(row.occurred_at):%Y-%m-%d %H:%M:%S}", str(IMPORT_LOG_SOURCES[row.source]),
                row.step or "-", str(_(row.reason)),
            ])
            for row in import_log_queryset(source if source != "all" else "")
        )
        response = StreamingHttpResponse(
            (redact_personal_data(redact_tokens(line)) + "\n" for line in lines),
            content_type="text/plain; charset=utf-8",
        )
        response["Content-Disposition"] = 'attachment; filename="protokoll-import.txt"'
        response["Cache-Control"] = "no-store"
        return response

    def start_view(self, request, source):
        """Start one import or the full sync (D-132): POST only, CSRF (middleware), the right of the page, the request
        limit of the admin pages, one security event, then the durable queue (the import worker runs it, D-133). ``source`` is one of ``START_SOURCES``;
        anything else is a 404. The answer is always a redirect to the page (post, redirect, get)."""
        if not request.user.has_perm(PERMISSION):
            raise PermissionDenied
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if source not in START_SOURCES:
            raise Http404
        page = HttpResponseRedirect(reverse("admin:addresses_importrequest_changelist"))
        if not allow_request(request, admin=True):
            self.message_user(request, _("Too many import requests. Please try again later."), messages.WARNING)
            return page
        sources = jobs.QUEUE_SOURCES if source == FULL_SYNC else (source,)
        # Every request is recorded with the account (D-041, D-076); a request that cannot be recorded is not queued.
        with transaction.atomic():
            security_log.record(SecurityEvent.Event.IMPORT_REQUESTED, actor=request.user, detail=source)
            added = jobs.enqueue(sources, in_order=source == FULL_SYNC)
        if not added:
            self.message_user(request, _("This import is already running or waiting."), messages.WARNING)
            return page
        # The import worker works off the queue one job after the other (D-133); the web process never runs an import.
        self.message_user(request, _("The import was queued. This page shows the progress."), messages.SUCCESS)
        return page

    def directory_view(self, request):
        if not request.user.has_perm(PERMISSION):
            raise PermissionDenied
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        context = {
            **self.admin_site.each_context(request),
            "title": _("Data imports"),
            "opts": self.model._meta,
            "import_status": status.build(),
            "status_url": reverse("admin:addresses_importrequest_status"),
            "full_sync": FULL_SYNC,
            "poll_seconds": POLL_SECONDS,
            "import_log": self.import_log_section(request) if request.user.is_superuser else None,
        }
        return TemplateResponse(request, "admin/addresses/directory.html", context)

    def import_log_section(self, request):
        """The import error log as a table, at the bottom of the import page (superusers only; D-121, D-132)."""
        from leg_platform import log_views
        from leg_platform.log_menu import LINE_COUNTS, line_count

        log_views.record_view(request, "importlog")
        source = request.GET.get("log_source", "")
        if source not in ("", *IMPORT_LOG_SOURCES):
            source = ""
        count = line_count(request)
        return {
            "entries": import_log_entries(source, count),
            "source": source,
            "sources": [("", _("All sources")), *[(key, label) for key, label in IMPORT_LOG_SOURCES.items()]],
            "line_counts": LINE_COUNTS,
            "lines_selected": str(count),
            "download_url": reverse("admin:addresses_importrequest_log_download", kwargs={"source": source or "all"}),
        }

    def status_view(self, request):
        """The status as JSON for the script of the page: signed in, same right as the page, GET only, never cached."""
        if not request.user.has_perm(PERMISSION):
            raise PermissionDenied
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        response = JsonResponse(status.build())
        response["Cache-Control"] = "no-store"
        return response


# The import error log (D-121): the sources and their labels, the row limit of the page and of the download.
IMPORT_LOG_SOURCES = {
    ImportRequest.Source.ADDRESS: gettext_lazy("Address directory"),
    ImportRequest.Source.PLANT: gettext_lazy("PV plants"),
    # The key of the transformer source as a string: the label only, nothing here starts a scan (D-121).
    "transformer": gettext_lazy("Transformer stations"),
}
IMPORT_LOG_LIMIT = 500
# What the start buttons may ask for (D-132): one source or all three in order (the full sync).
FULL_SYNC = "all"
START_SOURCES = frozenset({*jobs.QUEUE_SOURCES, FULL_SYNC})


def import_log_queryset(source=""):
    rows = ImportLogEntry.objects.all()
    if source:
        rows = rows.filter(source=source)
    return rows.order_by("-occurred_at", "-pk")[:IMPORT_LOG_LIMIT]


def import_log_entries(source, count):
    """The newest ``count`` entries of the import error log for the table, newest first: time, source, step and the
    fixed reason text, redacted like the download (D-099). Plain strings, no markup."""
    return [
        {
            "time": f"{timezone.localtime(row.occurred_at):%d.%m.%Y %H:%M:%S}",
            "source": str(IMPORT_LOG_SOURCES[row.source]),
            "step": str(status.PHASES.get(row.step, "")) if row.step else "",
            "reason": redact_personal_data(redact_tokens(str(_(row.reason)))),
        }
        for row in list(import_log_queryset(source))[:count]
    ]


# The housekeeping of both databases (D-119): its own right, checked on the page, the POST and the status endpoint.
HOUSEKEEPING_PERMISSION = "addresses.run_housekeeping"


def _error_line_count(request):
    """The number of lines of the error box from the query (50, 200 or 500; the default 200)."""
    try:
        value = int(request.GET.get("lines", housekeeping.ERROR_LINES_DEFAULT))
    except ValueError:
        return housekeeping.ERROR_LINES_DEFAULT
    return value if value in housekeeping.ERROR_LINE_COUNTS else housekeeping.ERROR_LINES_DEFAULT


@admin.register(HousekeepingStatus)
class HousekeepingStatusAdmin(admin.ModelAdmin):
    """The last results of the housekeeping, the dump status and the button to run now. No generic model editing."""

    def has_module_permission(self, request):
        return request.user.has_perm(HOUSEKEEPING_PERMISSION)

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm(HOUSEKEEPING_PERMISSION)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [
            path("", self.admin_site.admin_view(self.housekeeping_view),
                 name="addresses_housekeepingstatus_changelist"),
            path("status/", self.admin_site.admin_view(self.status_view),
                 name="addresses_housekeepingstatus_status"),
        ]

    def housekeeping_view(self, request):
        if not request.user.has_perm(HOUSEKEEPING_PERMISSION):
            raise PermissionDenied
        page = reverse("admin:addresses_housekeepingstatus_changelist")
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "acknowledge":
                housekeeping.acknowledge_errors(request.user)
                self.message_user(request, _("The open errors were acknowledged. The weekly run may run again."), messages.SUCCESS)
                return HttpResponseRedirect(page)
            result = housekeeping.start_manual(request.user)
            if result == housekeeping.STARTED:
                self.message_user(request, _("The housekeeping has started. This page shows the result."), messages.SUCCESS)
            else:
                self.message_user(request, housekeeping.refusal_text(result), messages.WARNING)
            return HttpResponseRedirect(page)
        if request.method not in ("GET", "POST"):
            return HttpResponseNotAllowed(["GET", "POST"])
        context = {
            **self.admin_site.each_context(request),
            **housekeeping.page_context(lines=_error_line_count(request)),
            "title": _("Housekeeping"),
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/addresses/housekeeping.html", context)

    def status_view(self, request):
        """The step results as JSON for the page: signed in, same right, GET only, never cached."""
        if not request.user.has_perm(HOUSEKEEPING_PERMISSION):
            raise PermissionDenied
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        response = JsonResponse(housekeeping.status_payload())
        response["Cache-Control"] = "no-store"
        return response
