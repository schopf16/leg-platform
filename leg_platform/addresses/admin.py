from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.translation import gettext as _

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent

from .jobs import request_import
from .limits import allow_request
from .models import ImportRequest
from .services import directory_status

PERMISSION = "addresses.request_directory_import"


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
        # Only a status page and the CSRF-protected enqueue; no generic model editing.
        return [
            path("", self.admin_site.admin_view(self.directory_view),
                 name="addresses_importrequest_changelist"),
        ]

    def directory_view(self, request):
        if not request.user.has_perm(PERMISSION):
            raise PermissionDenied
        if request.method == "POST":
            if not allow_request(request, admin=True):
                self.message_user(request, _("Too many import requests. Please try again later."), messages.WARNING)
            else:
                # Every request is recorded with the account in the same transaction as the queue row (D-041, D-076).
                with transaction.atomic():
                    security_log.record(SecurityEvent.Event.ADDRESS_IMPORT_REQUESTED, actor=request.user)
                    queued = request_import()
                if queued:
                    self.message_user(request, _("The address directory import has been queued."), messages.SUCCESS)
                else:
                    self.message_user(request, _("An import is already queued or running."), messages.WARNING)
            return HttpResponseRedirect(reverse("admin:addresses_importrequest_changelist"))
        status, directory = directory_status()
        context = {
            **self.admin_site.each_context(request),
            "title": _("Address directory"),
            "opts": self.model._meta,
            "directory_status": status,
            "directory": directory,
            "import_job": ImportRequest.objects.filter(pk=1).first(),
        }
        return TemplateResponse(request, "admin/addresses/directory.html", context)
