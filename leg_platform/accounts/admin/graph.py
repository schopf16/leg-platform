"""Administration of the Microsoft Graph email delivery (D-006): one row, system administrators only."""

import logging

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.timezone import localtime
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from leg_platform.accounts import emails, rate_limits, security_log
from leg_platform.accounts.models import (
    CERTIFICATE_EXPIRED,
    CERTIFICATE_EXPIRING,
    GRAPH_CERTIFICATE_WARNING_DAYS,
    GraphConfiguration,
    SecurityEvent,
)
from leg_platform.environment import EMAIL_MODE_CONSOLE
from leg_platform.mail import GraphEmailBackend, GraphEmailError

logger = logging.getLogger(__name__)

Event = SecurityEvent.Event

GRAPH_CONFIGURATION_PK = 1
GRAPH_UPLOAD_MAX_BYTES = 16 * 1024
GRAPH_SETTING_FIELDS = ("tenant_id", "client_id", "sender_address")


class GraphConfigurationForm(forms.ModelForm):
    certificate_file = forms.FileField(
        required=False,
        label=_("Certificate file (PEM)"),
        help_text=_("The public certificate. Upload it together with the private key."),
    )
    private_key_file = forms.FileField(
        required=False,
        label=_("Private key file (PEM)"),
        help_text=_(
            "Stored encrypted and never shown again. Upload it together with the certificate."
        ),
    )

    class Meta:
        model = GraphConfiguration
        fields = GRAPH_SETTING_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # (private key PEM, certificate PEM) of a new upload; set in clean(), saved in save_model().
        self.new_certificate = None

    def clean(self):
        cleaned_data = super().clean()
        certificate_file = cleaned_data.get("certificate_file")
        private_key_file = cleaned_data.get("private_key_file")
        if bool(certificate_file) != bool(private_key_file):
            raise ValidationError(_("Upload the certificate and the private key together."))
        if certificate_file:
            self.new_certificate = self._read_upload(certificate_file, private_key_file)
        return cleaned_data

    @sensitive_variables("private_key_pem")
    def _read_upload(self, certificate_file, private_key_file):
        if max(certificate_file.size, private_key_file.size) > GRAPH_UPLOAD_MAX_BYTES:
            raise ValidationError(_("The file is too large."))
        certificate_pem = certificate_file.read()
        private_key_pem = private_key_file.read()
        # Checked here, so a wrong upload is a form error before anything is saved.
        GraphConfiguration.check_certificate(private_key_pem, certificate_pem)
        return private_key_pem, certificate_pem


class GraphConfigurationAdmin(admin.ModelAdmin):
    """No row can be added or deleted; only the public certificate data is displayed."""

    form = GraphConfigurationForm
    fieldsets = (
        (None, {"fields": (*GRAPH_SETTING_FIELDS, "is_ready")}),
        (
            _("Certificate"),
            {
                "fields": (
                    "certificate_subject",
                    "certificate_sha1",
                    "certificate_sha256",
                    "certificate_expiry",
                    "certificate_file",
                    "private_key_file",
                )
            },
        ),
    )
    readonly_fields = (
        "is_ready",
        "certificate_subject",
        "certificate_sha1",
        "certificate_sha256",
        "certificate_expiry",
    )

    @admin.display(boolean=True, description=_("ready to send"))
    def is_ready(self, obj):
        return obj.is_configured

    @staticmethod
    def _certificate_details(obj):
        return obj.certificate_details() if obj.certificate_pem else None

    @admin.display(description=_("certificate subject"))
    def certificate_subject(self, obj):
        details = self._certificate_details(obj)
        return details["subject"] if details else _("No certificate stored.")

    @admin.display(description=_("SHA-1 thumbprint"))
    def certificate_sha1(self, obj):
        details = self._certificate_details(obj)
        return details["sha1"] if details else "-"

    @admin.display(description=_("SHA-256 thumbprint"))
    def certificate_sha256(self, obj):
        details = self._certificate_details(obj)
        return details["sha256"] if details else "-"

    @admin.display(description=_("expires on"))
    def certificate_expiry(self, obj):
        if obj.certificate_expires_on is None:
            return "-"
        expires = date_format(localtime(obj.certificate_expires_on), "DATETIME_FORMAT")
        status = obj.certificate_status()
        if status == CERTIFICATE_EXPIRED:
            note = gettext("The certificate has expired. Upload a new certificate.")
        elif status == CERTIFICATE_EXPIRING:
            note = gettext(
                "The certificate expires within %(days)d days. Upload a new certificate before "
                "it expires."
            ) % {"days": GRAPH_CERTIFICATE_WARNING_DAYS}
        else:
            return expires
        return format_html("{} <strong>{}</strong>", expires, note)

    @staticmethod
    def _change_url():
        return reverse("admin:accounts_graphconfiguration_change", args=(GRAPH_CONFIGURATION_PK,))

    def get_object(self, request, object_id, from_field=None):
        if str(object_id) != str(GRAPH_CONFIGURATION_PK):
            return None
        # Checked before load(), because load() creates the row when it does not exist yet.
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        return GraphConfiguration.load()

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        return HttpResponseRedirect(self._change_url())

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            stored = GraphConfiguration.objects.select_for_update().get(pk=obj.pk)
            settings_changed = any(
                getattr(stored, name) != getattr(obj, name) for name in GRAPH_SETTING_FIELDS
            )
            if form.new_certificate is None:
                # Only the settings are written; the certificate fields keep their stored values.
                obj.save(update_fields=(*GRAPH_SETTING_FIELDS, "updated_at"))
            else:
                # The upload was checked in the form; set_certificate() stores it encrypted.
                obj.set_certificate(*form.new_certificate)
                security_log.record(Event.GRAPH_CERTIFICATE_UPLOADED, actor=request.user)
            if settings_changed:
                security_log.record(Event.GRAPH_CONFIGURATION_CHANGED, actor=request.user)

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False

    # Test email: through GraphEmailBackend, never the email framework, and only to the address
    # of the person who asks.

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        # Before the default patterns: their last pattern would accept this path.
        return [
            path(
                "send-test/",
                self.admin_site.admin_view(self.send_test_view),
                name="%s_%s_send_test" % info,
            ),
            *super().get_urls(),
        ]

    @staticmethod
    def _test_refusal(configuration, address):
        """Return the reason why no test email may be sent to the address, or None."""
        if not configuration.is_configured:
            return _(
                "The Microsoft Graph configuration is incomplete. Save the settings and upload "
                "a certificate first."
            )
        if settings.EMAIL_MODE == EMAIL_MODE_CONSOLE:
            # No Graph request is made in console mode, so a test would not test the delivery.
            return _(
                "In console mode, no test email is sent, so the Microsoft Graph delivery is "
                "not tested."
            )
        return None

    def send_test_view(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied
        configuration = GraphConfiguration.load()
        if request.method == "POST":
            self._send_test(request, configuration)
            return HttpResponseRedirect(self._change_url())
        context = {
            **self.admin_site.each_context(request),
            "title": gettext("Send test email"),
            "opts": self.opts,
            "original": configuration,
            "address": request.user.email,
            "refusal": self._test_refusal(configuration, request.user.email),
            "change_url": self._change_url(),
        }
        return TemplateResponse(request, "admin/accounts/graphconfiguration/send_test.html", context)

    def _send_test(self, request, configuration):
        address = request.user.email
        # Recorded for every attempt, also for a refused one (D-018).
        security_log.record(Event.GRAPH_TEST_EMAIL_REQUESTED, actor=request.user)
        # Test sends use the same per-address budget as other outgoing mail (D-048).
        if not rate_limits.consume(
            rate_limits.SCOPE_MAIL_ADDRESS,
            address,
            rate_limits.MAIL_ADDRESS_LIMITS,
        ):
            self.message_user(
                request,
                _("The test email limit has been reached. Try again later."),
                messages.ERROR,
            )
            return
        refusal = self._test_refusal(configuration, address)
        if refusal is not None:
            self.message_user(request, refusal, messages.ERROR)
            return
        message = emails.graph_test_message(address, configuration.sender_address)
        try:
            sent = GraphEmailBackend().send_messages([message])
        except GraphEmailError as error:
            # The text of GraphEmailError names no secret, address or token.
            logger.error("A Microsoft Graph test email could not be sent: %s", error)
            sent = 0
        except Exception as error:  # noqa: BLE001 - any other failure; only its class is logged
            logger.error("A Microsoft Graph test email could not be sent: %s.", type(error).__name__)
            sent = 0
        if sent == 1:
            self.message_user(
                request,
                gettext("A test email was sent to %(address)s.") % {"address": address},
                messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                _("The test email could not be sent. The reason is in the server log."),
                messages.ERROR,
            )
