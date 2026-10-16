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
from django.utils.translation import gettext
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
INDEX_HINT = _(
    "You maintain the name, the logo, the colours and the addresses of the portal and the "
    "texts of the emails under Content."
)


class PortalAdminSite(AdminSite):
    def group_of(self, object_name):
        """The group of the start page that a model belongs to: (key, name)."""
        for key, name, object_names in ADMIN_GROUPS:
            if object_name in object_names:
                return key, name
        return "system", dict((k, n) for k, n, _o in ADMIN_GROUPS)["system"]

    def each_context(self, request):
        from leg_platform.accounts.portal_settings import portal_values

        context = super().each_context(request)
        # One name for the portal and its administration: the name of the instance, as the
        # administrator set it or else the server configuration (D-013, D-073).
        name = portal_values(request).name
        context["site_header"] = name
        context["site_title"] = name
        # The pages of a model show the group of the start page in the breadcrumb ("Inhalte >
        # Mailtexte"), not the name of the app (design package, final spot check, 3).
        context["portal_group"] = self._group_of_request(request)
        return context

    def _group_of_request(self, request):
        match = getattr(request, "resolver_match", None)
        url_name = getattr(match, "url_name", None) or ""
        for model in self._registry:
            if url_name.startswith(f"{model._meta.app_label}_{model._meta.model_name}_"):
                _key, name = self.group_of(model._meta.object_name)
                return {"name": name, "url": reverse("admin:index", current_app=self.name)}
        return None

    def index(self, request, extra_context=None):
        # The hint names the group "Content": only an account that sees that group gets it. An
        # account that sees nothing gets Django's own sentence for it (reworded in the catalogue).
        groups = self.get_app_list(request)
        sees_content = any(group["app_label"] == "content" for group in groups)
        context = {
            "index_hint": INDEX_HINT if sees_content else "",
            "first_steps": self.first_steps(request),
        }
        return super().index(request, {**context, **(extra_context or {})})

    def first_steps(self, request):
        """The live status of the settings that a new administrator must make (acceptance, P1-1).

        Reads only, shows no secret (no key, no address of an account), and lists an item only
        when the account may open its page. Each item: label, state (True, False or None for a
        plain fact), status text and the link.
        """
        from leg_platform.accounts.models import (
            CERTIFICATE_EXPIRED,
            EmailText,
            GraphConfiguration,
            PortalSettings,
        )

        def may_open(model):
            model_admin = self._registry.get(model)
            return model_admin is not None and model_admin.has_view_permission(request)

        steps = []
        if may_open(PortalSettings):
            row = PortalSettings.objects.defer(*PortalSettings.LOGO_DATA_FIELDS).filter(pk=1).first()
            url = reverse("admin:accounts_portalsettings_change", args=(1,), current_app=self.name)
            # A plain fact, not a task: name, logo and colours always work, with the settings of
            # the portal or with the server configuration (D-073).
            own = bool(
                row
                and (
                    row.name
                    or row.logo_light_sha256
                    or row.logo_dark_sha256
                    or any(getattr(row, field) for field in PortalSettings.COLOUR_FIELDS)
                )
            )
            steps.append(
                {
                    "label": _("Name, logo and colours"),
                    "ok": None,
                    "status": _("own settings") if own else _("server configuration"),
                    "url": url,
                }
            )
            for label, field in (
                (_("Address of the privacy notice"), "privacy_url"),
                (_("Address of the legal notice"), "imprint_url"),
            ):
                is_set = bool(row and getattr(row, field))
                steps.append(
                    {"label": label, "ok": is_set, "status": _("set") if is_set else _("not set yet"), "url": url}
                )
        if may_open(EmailText):
            total = len(EmailText.Key.values)
            customised = EmailText.objects.exclude(subject="", body="").count()
            steps.append(
                {
                    "label": _("Texts of the emails"),
                    "ok": None,
                    "status": gettext("%(total)d texts, %(customised)d customised")
                    % {"total": total, "customised": customised},
                    "url": reverse("admin:accounts_emailtext_changelist", current_app=self.name),
                }
            )
        if may_open(GraphConfiguration):
            row = GraphConfiguration.objects.filter(pk=1).first()
            expired = bool(row and row.certificate_pem and row.certificate_status() == CERTIFICATE_EXPIRED)
            ready = bool(row and row.is_configured) and not expired
            if expired:
                status = _("certificate expired")
            else:
                status = _("ready") if ready else _("not ready")
            steps.append(
                {
                    "label": _("Sending of emails (Microsoft Graph)"),
                    "ok": ready,
                    "status": status,
                    "url": reverse(
                        "admin:accounts_graphconfiguration_changelist", current_app=self.name
                    ),
                }
            )
        return steps

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
