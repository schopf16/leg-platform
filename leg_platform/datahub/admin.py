"""Administration of the Swisseldex Datahub access (D-139): one row, system administrators only.

The password is write-only. Every save that changes something and every connection test is a security event with
field names or a fixed result code only: never a value, a user name, a password or a text of the server.
"""

import logging
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.decorators import method_decorator
from django.utils.timezone import localtime
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from leg_platform.accounts import security_log
from leg_platform.accounts.encryption import DecryptionError
from leg_platform.accounts.models import SecurityEvent

from . import connection
from .models import CONTROL, PASSWORD_MAX_LENGTH, DatahubConfiguration

logger = logging.getLogger(__name__)

Event = SecurityEvent.Event
Outcome = DatahubConfiguration.TestOutcome

CONFIGURATION_PK = 1
SETTING_FIELDS = (
    "host", "port", "username", "directory", "own_name", "own_eic", "grid_operator_eic", "collection_enabled",
)
# The stored password goes only to the server it was entered for: a change of these fields needs it again.
SERVER_FIELDS = ("host", "port")
# Fields the browser must not fill in from its saved logins.
NO_AUTOCOMPLETE_FIELDS = ("username", "directory")
# The Datahub refuses repeated logins within 60 seconds (manual, chapter 4.4); a test is refused before that.
TEST_INTERVAL = timedelta(seconds=60)
TOO_SOON = "too_soon"


class DatahubConfigurationForm(forms.ModelForm):
    new_password = forms.CharField(
        label=_("FTPES password"),
        required=False,
        strip=False,
        max_length=PASSWORD_MAX_LENGTH,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text=_("Stored encrypted and never shown again. Leave it empty to keep the stored password."),
    )
    clear_password = forms.BooleanField(
        label=_("Remove the stored password"),
        required=False,
    )

    class Meta:
        model = DatahubConfiguration
        fields = SETTING_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in NO_AUTOCOMPLETE_FIELDS:
            self.fields[name].widget.attrs["autocomplete"] = "off"

    def clean_new_password(self):
        password = self.cleaned_data.get("new_password", "")
        if CONTROL.search(password):
            raise ValidationError(_("The value must not contain line breaks or control characters."))
        return password

    def _clean_eic(self, name):
        return (self.cleaned_data.get(name) or "").strip().upper()

    def clean_own_eic(self):
        return self._clean_eic("own_eic")

    def clean_grid_operator_eic(self):
        return self._clean_eic("grid_operator_eic")

    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get("new_password")
        clear = cleaned_data.get("clear_password")
        if new_password and clear:
            raise ValidationError(_("Enter a new password or remove the stored one, not both."))
        if not new_password and not clear and server_changed(self._stored(), cleaned_data):
            self.add_error(
                "new_password", _("Enter the password again when you change the server or the port.")
            )
        if cleaned_data.get("collection_enabled"):
            if not settings.DATAHUB_COLLECTION_ALLOWED:
                self.add_error(
                    "collection_enabled",
                    _(
                        "The collection is locked on this server by the server configuration. Only the one "
                        "server that collects the messages may switch it on."
                    ),
                )
            has_password = bool(new_password) or (self.instance.has_password and not clear)
            if not (cleaned_data.get("username") and has_password and cleaned_data.get("own_eic")):
                self.add_error(
                    "collection_enabled",
                    _("Enter the user name, the password and the own EIC before switching the collection on."),
                )
        return cleaned_data

    def _stored(self):
        """The row as stored in the database: the form assigns the entered values to the instance after clean()."""
        if self.instance.pk is None:
            return None
        return DatahubConfiguration.objects.filter(pk=self.instance.pk).first()


def server_changed(stored, values):
    """True when a stored password would go to another server than the one it was entered for (M-1, D-139)."""
    if stored is None or not stored.has_password:
        return False
    return any(name in values and values[name] != getattr(stored, name) for name in SERVER_FIELDS)


@admin.register(DatahubConfiguration)
class DatahubConfigurationAdmin(admin.ModelAdmin):
    form = DatahubConfigurationForm
    fieldsets = (
        (
            _("FTPES access"),
            {
                "description": _(
                    "The platform collects the messages from the server of the Datahub (FTP with explicit TLS). "
                    "The access data come from the Datahub."
                ),
                "fields": ("host", "port", "username", "password_state", "new_password", "clear_password",
                           "directory"),
            },
        ),
        (
            _("Market partners"),
            {"fields": ("own_name", "own_eic", "grid_operator_eic")},
        ),
        (
            _("Collection"),
            {"fields": ("collection_state", "collection_enabled", "last_test")},
        ),
    )
    readonly_fields = ("password_state", "collection_state", "last_test")

    @admin.display(description=_("stored password"))
    def password_state(self, obj):
        return _("stored") if obj.has_password else _("none stored")

    @admin.display(description=_("collection on this server"))
    def collection_state(self, obj):
        if not settings.DATAHUB_COLLECTION_ALLOWED:
            note = gettext(
                "Locked by the server configuration: this server never collects. Only the one server that "
                "collects may allow it (DATAHUB_COLLECTION_ALLOWED)."
            )
            if obj.collection_enabled:
                return format_html(
                    "<strong>{}</strong> {}",
                    note,
                    gettext("The switch below is on (for example from a copied database); it has no effect here."),
                )
            return format_html("<strong>{}</strong>", note)
        if obj.collection_active:
            return gettext("Active.")
        return gettext("Allowed on this server; the switch is off.")

    @admin.display(description=_("last connection test"))
    def last_test(self, obj):
        if obj.last_test_at is None:
            return gettext("No test yet.")
        when = date_format(localtime(obj.last_test_at), "DATETIME_FORMAT")
        if not obj.last_test_outcome:
            return gettext("%(time)s: running") % {"time": when}
        label = obj.get_last_test_outcome_display()
        if obj.last_test_outcome == Outcome.SUCCEEDED and obj.last_test_file_count is not None:
            count = obj.last_test_file_count
            return ngettext(
                "%(time)s: %(result)s, %(count)d file", "%(time)s: %(result)s, %(count)d files", count
            ) % {"time": when, "result": label, "count": count}
        return gettext("%(time)s: %(result)s") % {"time": when, "result": label}

    @staticmethod
    def _change_url():
        return reverse("admin:datahub_datahubconfiguration_change", args=(CONFIGURATION_PK,))

    def get_object(self, request, object_id, from_field=None):
        if str(object_id) != str(CONFIGURATION_PK):
            return None
        # Checked before load(), because load() creates the row when it does not exist yet.
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        return DatahubConfiguration.load()

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        return HttpResponseRedirect(self._change_url())

    @method_decorator(sensitive_post_parameters("new_password"))
    def change_view(self, request, object_id, form_url="", extra_context=None):
        return super().change_view(request, object_id, form_url, extra_context)

    @sensitive_variables("password")
    def save_model(self, request, obj, form, change):
        password = form.cleaned_data.get("new_password") or ""
        clear = form.cleaned_data.get("clear_password")
        with transaction.atomic():
            stored = DatahubConfiguration.objects.select_for_update().get(pk=obj.pk)
            changed = [name for name in SETTING_FIELDS if getattr(stored, name) != getattr(obj, name)]
            update_fields = [*SETTING_FIELDS, "updated_at"]
            # The form refused this already; checked again under the lock against a concurrent save. The stored
            # password never stays with another server.
            if not password and server_changed(stored, {name: getattr(obj, name) for name in SERVER_FIELDS}):
                clear = True
            if password or (clear and stored.has_password):
                obj.set_password(password)
                changed.append("password")
                update_fields.append("encrypted_password")
            if not changed:
                return
            obj.save(update_fields=update_fields)
            # Field names only, never a value (docs/SECURITY.md, section 10).
            security_log.record(
                Event.DATAHUB_CONFIGURATION_CHANGED, actor=request.user, detail="fields: " + ", ".join(changed)
            )

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

    # The connection test: POST only, system administrators only, in the web process with a hard deadline (D-139).

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        # Before the default patterns: their last pattern would accept this path.
        return [
            path(
                "connection-test/",
                self.admin_site.admin_view(self.connection_test_view),
                name="%s_%s_connection_test" % info,
            ),
            *super().get_urls(),
        ]

    def connection_test_view(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied
        configuration = DatahubConfiguration.load()
        if request.method == "POST":
            self._run_test(request)
            return HttpResponseRedirect(self._change_url())
        context = {
            **self.admin_site.each_context(request),
            "title": gettext("Test connection"),
            "opts": self.opts,
            "original": configuration,
            "complete": configuration.is_complete,
            "change_url": self._change_url(),
        }
        return TemplateResponse(request, "admin/datahub/datahubconfiguration/connection_test.html", context)

    @sensitive_variables("password")
    def _run_test(self, request):
        now = timezone.now()
        with transaction.atomic():
            row = DatahubConfiguration.objects.select_for_update().get(pk=CONFIGURATION_PK)
            if row.last_test_at is not None and now - row.last_test_at < TEST_INTERVAL:
                security_log.record(Event.DATAHUB_CONNECTION_TESTED, actor=request.user, detail="result " + TOO_SOON)
                self.message_user(
                    request,
                    _("The Datahub refuses a second login within 60 seconds. Wait a minute and test again."),
                    messages.ERROR,
                )
                return
            password = None
            if not row.is_complete:
                outcome = Outcome.INCOMPLETE
            else:
                try:
                    password = row.password()
                    outcome = None
                except DecryptionError:
                    outcome = Outcome.PASSWORD_UNREADABLE
            row.last_test_at = now
            row.last_test_outcome = outcome or ""
            row.last_test_file_count = None
            row.save(update_fields=["last_test_at", "last_test_outcome", "last_test_file_count"])
            if outcome is not None:
                security_log.record(Event.DATAHUB_CONNECTION_TESTED, actor=request.user, detail="result " + outcome)
                self.message_user(request, self._outcome_text(outcome), messages.ERROR)
                return
        # Outside the transaction: no row lock is held during the network test.
        result = connection.check_connection(row, password)
        with transaction.atomic():
            DatahubConfiguration.objects.filter(pk=CONFIGURATION_PK).update(
                last_test_outcome=result.outcome, last_test_file_count=result.file_count
            )
            detail = "result " + result.outcome
            if result.file_count is not None:
                detail += f", files {result.file_count}"
            security_log.record(Event.DATAHUB_CONNECTION_TESTED, actor=request.user, detail=detail)
        if result.outcome == Outcome.SUCCEEDED:
            self.message_user(
                request,
                gettext("Connection succeeded. Files in the directory: %(count)d.") % {"count": result.file_count},
                messages.SUCCESS,
            )
            return
        # The application log gets the fixed code only: a reply of the server may repeat what it was sent.
        logger.warning("The Datahub connection test failed: %s.", result.outcome)
        text = self._outcome_text(result.outcome)
        if result.server_text:
            text = format_html("{} {}", text, gettext("Answer: %(text)s") % {"text": result.server_text})
        self.message_user(request, text, messages.ERROR)

    @staticmethod
    def _outcome_text(outcome):
        return {
            Outcome.INCOMPLETE: _("The test needs the server, the port, the user name and the password."),
            Outcome.PASSWORD_UNREADABLE: _(
                "The stored password cannot be read (the secret key of the server changed). Enter it again."
            ),
            Outcome.CONNECTION_FAILED: _("The server cannot be reached. Check the server name, the port and "
                                         "whether this server may connect to it."),
            Outcome.TLS_FAILED: _("The encrypted connection failed: the certificate or TLS was not accepted."),
            Outcome.LOGIN_FAILED: _("The login was refused. Check the user name and the password."),
            Outcome.DIRECTORY_FAILED: _("The directory was not found on the server."),
            Outcome.LISTING_FAILED: _("The directory could not be listed."),
            Outcome.TIMED_OUT: _("The server did not answer in time."),
        }.get(outcome, _("The connection test failed. The reason is not known."))
