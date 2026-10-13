"""The administration site: only for sessions that completed the second factor (D-040).

Django's admin login and password change pages check the password without the abuse protection
and without the second factor. They are replaced by the portal's own pages, so that there is
one login and one password change.
"""

from django.conf import settings
from django.contrib.admin.apps import AdminConfig
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.shortcuts import redirect
from django.urls import reverse


class PortalAdminSite(AdminSite):
    def has_permission(self, request):
        from leg_platform.accounts.second_factor import is_second_factor_complete

        # In addition to the middleware: the administration never opens for a session without a
        # completed second factor, also if the middleware is changed or removed by mistake.
        return super().has_permission(request) and is_second_factor_complete(request.user)

    def login(self, request, extra_context=None):
        from django.contrib.auth.views import redirect_to_login

        target = request.GET.get(REDIRECT_FIELD_NAME, reverse("admin:index", current_app=self.name))
        return redirect_to_login(target, settings.LOGIN_URL)

    def password_change(self, request, extra_context=None):
        return redirect("accounts:password_change")


class PortalAdminConfig(AdminConfig):
    default_site = "leg_platform.admin_site.PortalAdminSite"
