"""Administration of the portal settings (D-073): one row, values of the server read only."""

import json
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import second_factor, security_log
from leg_platform.accounts.models import PortalSettings, SecurityEvent
from leg_platform.accounts.portal_settings import environment_values, portal_values
from leg_platform.accounts.portal_settings_form import PortalSettingsForm
from leg_platform.branding import HEX_COLOUR
from leg_platform.presentation import accent_rules

Event = SecurityEvent.Event


class PortalSettingsAdmin(admin.ModelAdmin):
    """One row, no add and no delete.

    The rights to see and to change it are ordinary rights that a role gets at runtime (D-012);
    superusers pass automatically. Every change is a security event and is in the history.
    """

    form = PortalSettingsForm
    change_form_template = "admin/accounts/portalsettings/change_form.html"
    readonly_fields = (
        "privacy_preview",
        "imprint_preview",
        "logo_status",
        "favicon_status",
        "preview",
        "server_configuration",
    )
    # The own preview link opens a new tab; the standard "Currently" link of the widget is not
    # needed.
    formfield_overrides = {models.URLField: {"widget": forms.URLInput()}}
    address_examples = {
        "privacy_url": _("https://www.example.org/privacy"),
        "imprint_url": _("https://www.example.org/imprint"),
    }

    def get_fieldsets(self, request, obj=None):
        can_change = self.has_change_permission(request, obj)
        fieldsets = [
            (_("Name and website"), {"fields": ("name", "website_url")}),
            (
                _("Legal notices"),
                {
                    "description": _(
                        "The texts are maintained on your website. Enter only the addresses here."
                    ),
                    "fields": ("privacy_url", "privacy_preview", "imprint_url", "imprint_preview"),
                },
            ),
            (
                _("Logo"),
                {
                    "fields": (
                        "logo_status",
                        *(("logo_light_file", "logo_dark_file", "remove_logos") if can_change else ()),
                    )
                },
            ),
            (
                _("Browser tab icon"),
                {
                    "fields": (
                        "favicon_status",
                        *(("favicon_file", "remove_favicon") if can_change else ()),
                    )
                },
            ),
            (
                _("Colours"),
                {"fields": (*PortalSettings.COLOUR_FIELDS, *(("reset_colours",) if can_change else ()))},
            ),
            (_("Preview"), {"fields": ("preview",)}),
        ]
        if request.user.is_superuser:
            # Least disclosure (D-073): only system administrators see the server configuration.
            fieldsets.append(
                (
                    _("Server configuration (read only)"),
                    {
                        "classes": ("collapse",),
                        "description": _(
                            "These values are set by the operator in the server configuration and "
                            "cannot be changed here: the host name, the allowed hosts and the base "
                            "address decide where the links in emails and the security settings "
                            "point, so they are not stored where the portal settings can change them."
                        ),
                        "fields": ("server_configuration",),
                    },
                )
            )
        if can_change:
            fieldsets.append(
                (
                    _("Confirmation"),
                    {"fields": (
                        "passkey_assertion" if request.user.authentication_method == request.user.AuthenticationMethod.PASSKEY
                        else "otp_token" if second_factor.has_second_factor(request.user) else "current_password",
                    )},
                )
            )
        return fieldsets

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # The acting account of a rejected upload and the request for the proof of identity; the
        # class is built for this request only.
        return type(form.__name__, (form,), {"actor": request.user, "request": request})

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        response = super().changeform_view(request, object_id, form_url, extra_context)
        if getattr(request, second_factor.SESSION_ENDED_ATTRIBUTE, False):
            # Too many wrong passwords or codes (D-062): end this session and say so at the login page.
            second_factor.end_session_for_wrong_codes(request)
            return redirect(settings.LOGIN_URL)
        return response

    def get_urls(self):
        # Before the catch-all of the model admin; protected like its other pages.
        return [
            path(
                "preview.css",
                self.admin_site.admin_view(self.preview_stylesheet),
                name="accounts_portalsettings_preview_css",
            ),
            *super().get_urls(),
        ]

    def preview_stylesheet(self, request):
        """The saved colours for the preview panels as a file (no inline style under the CSP)."""
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        values = portal_values(request)
        css = (
            f".portal-preview-light{{{accent_rules(values.accent_light, values.text_light)}"
            f"--yellow:{values.highlight};}}"
            f".portal-preview-dark{{{accent_rules(values.accent_dark, values.text_dark)}"
            f"--yellow:{values.highlight};}}"
        )
        return HttpResponse(css, content_type="text/css; charset=utf-8")

    @admin.display(description=_("uploaded logos"))
    def logo_status(self, obj):
        server = environment_values()
        rows = []
        for variant, title in (("light", _("Light mode")), ("dark", _("Dark mode"))):
            kind = getattr(obj, f"logo_{variant}_type")
            digest = getattr(obj, f"logo_{variant}_sha256")
            if kind and digest:
                state = gettext("Uploaded (%(type)s, fingerprint %(digest)s).") % {
                    "type": kind.upper(),
                    "digest": digest[:12],
                }
            else:
                state = gettext("Not uploaded. Server configuration: %(value)s.") % {
                    "value": gettext("logo files set")
                    if getattr(server, f"logo_{variant}")
                    else gettext("not set")
                }
            rows.append((title, state))
        return format_html_join("", "<p>{}: {}</p>", rows)

    @admin.display(description=_("uploaded icon"))
    def favicon_status(self, obj):
        if obj.favicon_type and obj.favicon_sha256:
            state = gettext("Uploaded (%(type)s, fingerprint %(digest)s).") % {
                "type": obj.favicon_type.upper(),
                "digest": obj.favicon_sha256[:12],
            }
        else:
            state = gettext("Not uploaded. The built-in icon is used.")
        return format_html("<p>{}</p>", state)

    @admin.display(description=_("preview of the portal"))
    def preview(self, obj):
        server = environment_values()
        return render_to_string(
            "admin/accounts/portalsettings/_preview.html",
            {"values": portal_values(), "server": server},
        )

    @admin.display(description=_("values of the server"))
    def server_configuration(self, obj):
        def on_off(value):
            return gettext("on") if value else gettext("off")

        def listed(values):
            return ", ".join(values) if values else "-"

        rows = [
            (_("Environment"), settings.APP_ENV),
            (_("Debug mode"), on_off(settings.DEBUG)),
            (_("Base address of the portal (links in emails)"), settings.INSTANCE_BASE_URL),
            (_("Allowed host names"), listed(settings.ALLOWED_HOSTS)),
            (_("Trusted origins of forms"), listed(settings.CSRF_TRUSTED_ORIGINS)),
            (_("Email delivery"), settings.EMAIL_MODE),
            (_("Sender address of emails"), settings.DEFAULT_FROM_EMAIL),
            (
                _("Number of allowed recipients (development only)"),
                str(len(settings.EMAIL_ALLOWED_RECIPIENTS)),
            ),
            (
                _("Security log: days kept (usual, long)"),
                f"{settings.SECURITY_LOG_RETENTION_DAYS}, {settings.SECURITY_LOG_LONG_RETENTION_DAYS}",
            ),
            (
                _("Header of the reverse proxy for HTTPS"),
                gettext("set") if settings.SECURE_PROXY_SSL_HEADER else gettext("not set"),
            ),
            (_("Number of proxies in front of the portal"), str(settings.AXES_IPWARE_PROXY_COUNT)),
            (_("HSTS, seconds"), str(settings.SECURE_HSTS_SECONDS)),
        ]
        return render_to_string(
            "admin/accounts/portalsettings/_server_configuration.html", {"rows": rows}
        )

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if field is not None and db_field.name in self.address_examples:
            field.widget.attrs["placeholder"] = str(self.address_examples[db_field.name])
            field.help_text = "{} {}".format(
                field.help_text, gettext("It must start with https://. It opens in a new tab.")
            ).strip()
        if field is not None and db_field.name == "website_url":
            field.widget.attrs["placeholder"] = gettext("https://www.example.org")
        return field

    def response_change(self, request, obj):
        # One message for every way of saving, instead of the general sentence of Django.
        if getattr(request, "_portal_addresses_removed", False):
            message = _(
                "The addresses were removed. The footer shows the privacy notice and the legal "
                "notice without a link."
            )
        else:
            message = _("Saved. The changes are now visible on all pages.")
        self.message_user(request, message, messages.SUCCESS)
        return HttpResponseRedirect(self._change_url())

    @staticmethod
    def _preview(address):
        if not address:
            return "-"
        # The address passed the https check when it was saved; format_html escapes it anyway.
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>',
            address,
            gettext("Open page"),
        )

    @admin.display(description=_("preview of the privacy notice"))
    def privacy_preview(self, obj):
        return self._preview(obj.privacy_url)

    @admin.display(description=_("preview of the legal notice"))
    def imprint_preview(self, obj):
        return self._preview(obj.imprint_url)

    @staticmethod
    def _change_url():
        return reverse("admin:accounts_portalsettings_change", args=(1,))

    def get_object(self, request, object_id, from_field=None):
        if str(object_id) != "1":
            return None
        # Checked before load(), because load() creates the row when it does not exist yet.
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        # Without the two logo files: the page does not need them.
        return PortalSettings.load_without_logo_files()

    @staticmethod
    def _host(address):
        return (urlsplit(address).hostname or "-") if address else "-"

    @staticmethod
    def _quoted(value):
        """JSON quotes the text and escapes line breaks, so no value can add a line to the log."""
        return json.dumps(value or "")

    @staticmethod
    def _colour(value):
        return value if value and HEX_COLOUR.fullmatch(value) else ("-" if not value else "invalid")

    def _audit_lines(self, stored, obj, form):
        """One line per changed field; public values only, for logos never the content."""
        lines = []
        if stored.name != obj.name:
            lines.append(f"name changed, {self._quoted(stored.name)} -> {self._quoted(obj.name)}")
        for name in ("website_url", "privacy_url", "imprint_url"):
            if getattr(stored, name) != getattr(obj, name):
                lines.append(
                    f"{name} changed, host {self._host(getattr(stored, name))} -> "
                    f"{self._host(getattr(obj, name))}"
                )
        changed = [
            field for field in PortalSettings.COLOUR_FIELDS if getattr(stored, field) != getattr(obj, field)
        ]
        if changed and form.cleaned_data.get("reset_colours"):
            lines.append("colours reset to server configuration")
        else:
            lines += [
                f"{field} changed, {self._colour(getattr(stored, field))} -> "
                f"{self._colour(getattr(obj, field))}"
                for field in changed
            ]
        for variant in ("light", "dark"):
            old_digest = getattr(stored, f"logo_{variant}_sha256")
            new_digest = getattr(obj, f"logo_{variant}_sha256")
            if old_digest == new_digest:
                continue
            if new_digest:
                lines.append(
                    f"logo_{variant} replaced ({getattr(obj, f'logo_{variant}_type')}, "
                    f"{len(getattr(obj, f'logo_{variant}_data'))} bytes, sha256 {new_digest[:12]})"
                )
            else:
                lines.append(f"logo_{variant} removed")
        if stored.favicon_sha256 != obj.favicon_sha256:
            if obj.favicon_sha256:
                lines.append(
                    f"favicon replaced ({obj.favicon_type}, {len(obj.favicon_data)} bytes, "
                    f"sha256 {obj.favicon_sha256[:12]})"
                )
            else:
                lines.append("favicon removed")
        return lines

    def save_model(self, request, obj, form, change):
        # Every change is a security event (D-056, D-073); only public values are logged, the full
        # addresses are in the history of the administration.
        with transaction.atomic():
            stored = PortalSettings.objects.defer(*PortalSettings.LOGO_DATA_FIELDS).select_for_update().get(
                pk=obj.pk
            )
            changes = self._audit_lines(stored, obj, form)
            request._portal_addresses_removed = bool(
                (stored.privacy_url or stored.imprint_url)
                and not (obj.privacy_url or obj.imprint_url)
            )
            super().save_model(request, obj, form, change)
            if changes:
                security_log.record(
                    Event.PORTAL_SETTINGS_CHANGED, actor=request.user, detail="; ".join(changes)
                )

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        return HttpResponseRedirect(self._change_url())

    def history_view(self, request, object_id, extra_context=None):
        # The history names the acting account by its email address (see UserAdmin).
        if not request.user.is_superuser:
            raise PermissionDenied
        return super().history_view(request, object_id, extra_context)

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        # The group of the start page shows the module to whoever may see or change it (D-073).
        return self.has_view_or_change_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False
