from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseNotAllowed, HttpResponseRedirect, JsonResponse
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.translation import gettext as _

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent

from . import runner, status
from .limits import allow_request
from .models import ImportRequest

PERMISSION = "addresses.request_directory_import"
# How often the page asks for the status while an import is queued or running (seconds).
POLL_SECONDS = 2


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
            "title": _("Address directory"),
            "opts": self.model._meta,
            "import_status": status.build(),
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
