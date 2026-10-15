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
from django.utils.translation import gettext_lazy as _

# The groups of the start page of the administration (design package, WL-23): what a person looks
# for, not where the code lives. A model that is not named here goes to "System".
ADMIN_GROUPS = (
    ("users", _("Users"), ("User", "Group")),
    ("content", _("Content"), ("PortalSettings", "EmailText")),
    (
        "system",
        _("System"),
        (
            "GraphConfiguration",
            "SecurityEvent",
            "AccessAttempt",
            "AccessFailureLog",
            "Directory",
            "ImportRequest",
        ),
    ),
)
INDEX_HINT = _("You maintain the texts of the emails and the addresses of the footer under Content.")


class PortalAdminSite(AdminSite):
    def each_context(self, request):
        context = super().each_context(request)
        # One name for the portal and its administration: the name of the instance (D-013).
        context["site_header"] = settings.INSTANCE_NAME
        context["site_title"] = settings.INSTANCE_NAME
        return context

    def index(self, request, extra_context=None):
        return super().index(request, {"index_hint": INDEX_HINT, **(extra_context or {})})

    def get_app_list(self, request, app_label=None):
        """The start page and the side navigation show three groups instead of the apps.

        The page of one app (``app_label`` given) keeps Django's own list. A group shows only the
        models that the account may see; an empty group is left out.
        """
        app_list = super().get_app_list(request, app_label)
        if app_label is not None:
            return app_list
        entries = [model for app in app_list for model in app["models"]]
        index_url = reverse("admin:index", current_app=self.name)
        grouped = []
        for key, name, object_names in ADMIN_GROUPS:
            members = []
            for object_name in object_names:
                members += [m for m in entries if m["object_name"] == object_name]
            if key == "system":
                # Anything not named in any group ends here, so that no model gets lost.
                known = {n for _key, _name, names in ADMIN_GROUPS for n in names}
                members += [m for m in entries if m["object_name"] not in known]
            if members:
                grouped.append(
                    {
                        "name": name,
                        "app_label": key,
                        "app_url": index_url,
                        "has_module_perms": True,
                        "models": members,
                    }
                )
        return grouped

    def has_permission(self, request):
        from leg_platform.accounts.second_factor import is_second_factor_complete

        # In addition to the middleware: the administration never opens for a session without a
        # completed second factor or for an account that must replace its password first, also
        # if the middleware is changed or removed by mistake.
        return (
            super().has_permission(request)
            and not request.user.password_change_required
            and is_second_factor_complete(request.user)
        )

    def login(self, request, extra_context=None):
        from django.contrib.auth.views import redirect_to_login

        target = request.GET.get(REDIRECT_FIELD_NAME, reverse("admin:index", current_app=self.name))
        return redirect_to_login(target, settings.LOGIN_URL)

    def password_change(self, request, extra_context=None):
        return redirect("accounts:password_change")


class PortalAdminConfig(AdminConfig):
    default_site = "leg_platform.admin_site.PortalAdminSite"
