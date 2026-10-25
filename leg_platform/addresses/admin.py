from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseNotAllowed, HttpResponseRedirect, JsonResponse
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.translation import gettext as _

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent

from . import housekeeping, runner, status
from .limits import allow_request
from .models import HousekeepingStatus, ImportRequest, ServicePostalCode

PERMISSION = "addresses.request_directory_import"
# The postal codes of the service area of the transformer station scan (D-118); its own right, like the one above.
SERVICE_AREA_PERMISSION = "addresses.manage_service_areas"
# How often the page asks for the status while an import is queued or running (seconds).
POLL_SECONDS = 2


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
        ]

    def directory_view(self, request):
        if not request.user.has_perm(PERMISSION):
            raise PermissionDenied
        if request.method == "POST":
            if not allow_request(request, admin=True):
                self.message_user(request, _("Too many import requests. Please try again later."), messages.WARNING)
            else:
                # Every request is recorded with the account (D-041, D-076); the import then starts in a
                # thread of this process (D-090): one at a time over all web workers (the row of the job).
                with transaction.atomic():
                    security_log.record(SecurityEvent.Event.ADDRESS_IMPORT_REQUESTED, actor=request.user)
                if runner.start_manual():
                    self.message_user(
                        request, _("The address directory import has started. This page shows the progress."),
                        messages.SUCCESS,
                    )
                else:
                    self.message_user(request, _("An import is already queued or running."), messages.WARNING)
            return HttpResponseRedirect(reverse("admin:addresses_importrequest_changelist"))
        context = {
            **self.admin_site.each_context(request),
            "title": _("Data imports"),
            "opts": self.model._meta,
            "import_status": status.build(),
            "plant_status": status.plant_status(),
            "transformer_status": status.transformer_status(),
            "status_url": reverse("admin:addresses_importrequest_status"),
            "poll_seconds": POLL_SECONDS,
        }
        return TemplateResponse(request, "admin/addresses/directory.html", context)

    def status_view(self, request):
        """The status as JSON for the script of the page: signed in, same right as the page, GET only, never cached."""
        if not request.user.has_perm(PERMISSION):
            raise PermissionDenied
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        response = JsonResponse(status.build())
        response["Cache-Control"] = "no-store"
        return response


# The housekeeping of both databases (D-119): its own right, checked on the page, the POST and the status endpoint.
HOUSEKEEPING_PERMISSION = "addresses.run_housekeeping"


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
        if request.method == "POST":
            result = housekeeping.start_manual(request.user)
            if result == housekeeping.STARTED:
                self.message_user(request, _("The housekeeping has started. This page shows the result."), messages.SUCCESS)
            else:
                self.message_user(request, housekeeping.refusal_text(result), messages.WARNING)
            return HttpResponseRedirect(reverse("admin:addresses_housekeepingstatus_changelist"))
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET", "POST"])
        context = {
            **self.admin_site.each_context(request),
            **housekeeping.page_context(),
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
