"""Administration of the email texts: list in process order, preview, test email, versions."""

import logging

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.http import Http404, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.formats import date_format
from django.utils.html import format_html, format_html_join
from django.utils.timezone import localtime
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import email_texts, emails, greeting
from leg_platform.accounts.models import EmailText, EmailTextVersion

logger = logging.getLogger(__name__)

# Versions of an email text that are kept per email (D-039).
MAX_EMAIL_TEXT_VERSIONS = 100

# The email texts in the order of the process of a person. Every key is named once (a test checks
# it); a key that is not named is listed last.
_Key = EmailText.Key
EMAIL_TEXT_PROCESSES = (
    (
        _("Registration"),
        (_Key.REGISTRATION_VERIFICATION, _Key.REGISTRATION_EXISTING_ACCOUNT, _Key.INVITATION),
    ),
    (_("Login and password"), (_Key.PASSWORD_RESET, _Key.PASSWORD_CHANGED)),
    (
        _("Email addresses"),
        (
            _Key.EMAIL_CHANGE_VERIFICATION,
            _Key.EMAIL_CHANGE_EXISTING_ACCOUNT,
            _Key.EMAIL_CHANGE_NOTICE,
            _Key.EMAIL_CHANGED,
            _Key.SECONDARY_EMAIL_VERIFICATION,
            _Key.SECONDARY_EMAIL_CHANGED,
        ),
    ),
    (
        _("Two-factor authentication (2FA)"),
        (
            _Key.TWO_FACTOR_ENABLED,
            _Key.TWO_FACTOR_DISABLED,
            _Key.RECOVERY_CODES_RENEWED,
            _Key.RECOVERY_CODE_USED,
            _Key.CODE_ATTEMPTS_WARNING,
            _Key.TWO_FACTOR_RESET_LINK,
            _Key.TWO_FACTOR_RESET,
        ),
    ),
    (
        _("Account"),
        (
            _Key.ACCOUNT_DELETED,
            _Key.SESSION_ENDED_WRONG_CODES,
            _Key.ALL_SESSIONS_ENDED,
            _Key.PERSISTENT_SESSION_STARTED,
        ),
    ),
)


class EmailTextForm(forms.ModelForm):
    reset_to_default = forms.BooleanField(
        required=False,
        label=_("Use the default text again"),
        help_text=_("Discards the subject and the text written here."),
    )

    class Meta:
        model = EmailText
        fields = ("subject", "body")
        widgets = {"body": forms.Textarea(attrs={"rows": 20, "cols": 80})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_subject, self.default_body = email_texts.default_texts(self.instance.key)
        # The form shows the text that is in use, so that it can be edited from there.
        if not self.instance.subject:
            self.initial["subject"] = self.default_subject
        if not self.instance.body:
            self.initial["body"] = self.default_body

    def clean(self):
        data = super().clean()
        if data.get("reset_to_default"):
            data["subject"] = data["body"] = ""
            return data
        key = self.instance.key
        for field, default, validate in (
            ("subject", self.default_subject, email_texts.validate_subject),
            ("body", self.default_body, email_texts.validate_body),
        ):
            if field not in data:
                continue
            text = data[field].replace("\r\n", "\n").replace("\r", "\n").strip()
            # The unchanged default stays "not customised", so later improvements of it reach
            # this instance.
            data[field] = "" if text == default else text
            try:
                validate(key, data[field])
            except forms.ValidationError as error:
                self.add_error(field, error)
        return data


class EmailTextAdmin(admin.ModelAdmin):
    form = EmailTextForm
    list_display = ("title", "process", "text_status", "last_changed")
    list_display_links = ("title",)
    fields = ("title", "subject", "body", "reset_to_default", "placeholder_help")
    readonly_fields = ("title", "placeholder_help")
    change_list_template = "admin/accounts/emailtext/change_list.html"

    def get_queryset(self, request):
        order = [key for _name, keys in EMAIL_TEXT_PROCESSES for key in keys]
        position = Case(
            *[When(key=key, then=Value(index)) for index, key in enumerate(order)],
            default=Value(len(order)),
            output_field=IntegerField(),
        )
        # Not ``super().get_queryset()``: that one orders by ``get_ordering`` before the position
        # exists.
        queryset = self.model._default_manager.get_queryset().annotate(process_position=position)
        return queryset.order_by(*self.get_ordering(request))

    def get_ordering(self, request):
        return ("process_position", "key")

    @admin.display(description=_("email"))
    def title(self, obj):
        return str(obj)

    @admin.display(description=_("process"))
    def process(self, obj):
        for name, keys in EMAIL_TEXT_PROCESSES:
            if obj.key in keys:
                return name
        return "-"

    @staticmethod
    def _customised(obj):
        return bool(obj.subject or obj.body)

    @admin.display(description=_("text"))
    def text_status(self, obj):
        return _("Customised") if self._customised(obj) else _("Default text")

    @admin.display(description=_("last changed"))
    def last_changed(self, obj):
        if not self._customised(obj):
            return "-"
        return date_format(localtime(obj.updated_at), "DATETIME_FORMAT")

    @admin.display(description=_("Placeholders"))
    def placeholder_help(self, obj):
        required = {placeholder.name for placeholder in email_texts.required_placeholders(obj.key)}
        rows = format_html_join(
            "",
            "<tr><td><code>{{{}}}</code></td><td>{}</td><td>{}</td></tr>",
            (
                (
                    placeholder.name,
                    placeholder.description,
                    _("yes") if placeholder.name in required else "",
                )
                for placeholder in email_texts.placeholders_of(obj.key)
            ),
        )
        return format_html(
            "<p>{}</p><p>{}</p><table><thead><tr><th>{}</th><th>{}</th><th>{}</th></tr></thead>"
            "<tbody>{}</tbody></table><p>{}</p>",
            _("Write a placeholder in curly brackets. It is replaced when the email is sent."),
            _(
                "Web and email addresses cannot be typed; use the placeholders for addresses. "
                "The subject can only use the name of the portal."
            ),
            _("Placeholder"),
            _("Meaning"),
            _("Required"),
            rows,
            _("An empty subject or text, or the default text itself, means the default text."),
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # Preview and test email use the text as typed, validated like a save, and store nothing.

    @staticmethod
    def _is_test_action(request):
        return "_preview" in request.POST or "_send_test" in request.POST

    def save_model(self, request, obj, form, change):
        if self._is_test_action(request):
            return
        with transaction.atomic():
            previous = (
                EmailText.objects.select_for_update()
                .filter(pk=obj.pk)
                .values_list("subject", "body")
                .first()
            )
            super().save_model(request, obj, form, change)
            self._record_version(request, obj, previous)

    @staticmethod
    def _record_version(request, obj, previous):
        if previous != (obj.subject, obj.body):
            EmailTextVersion.objects.create(
                email_text=obj, subject=obj.subject, body=obj.body, changed_by=request.user
            )
            # The oldest versions go, so that the table and the page stay small.
            old = list(obj.versions.values_list("pk", flat=True)[MAX_EMAIL_TEXT_VERSIONS:])
            EmailTextVersion.objects.filter(pk__in=old).delete()

    def log_change(self, request, obj, message):
        if self._is_test_action(request):
            return None
        if request.POST.get("reset_to_default") and not (obj.subject or obj.body):
            message = gettext("Default text restored.")
        return super().log_change(request, obj, message)

    def response_change(self, request, obj):
        if not self._is_test_action(request):
            return super().response_change(request, obj)
        if "_send_test" in request.POST:
            self._send_test(request, obj)
        subject, body = emails.preview(obj.key, obj.subject, obj.body)
        posted = [
            (name, request.POST[name])
            for name in ("subject", "body", "reset_to_default")
            if name in request.POST
        ]
        context = {
            **self.admin_site.each_context(request),
            "title": gettext("Preview: %(name)s") % {"name": obj},
            "opts": self.opts,
            "original": obj,
            "subject": subject,
            "body": body,
            "posted": posted,
            "test_address": request.user.email,
            "change_url": reverse("admin:accounts_emailtext_change", args=(obj.pk,)),
        }
        return TemplateResponse(request, "admin/accounts/emailtext/preview.html", context)

    def _send_test(self, request, obj):
        # Only to the address of the person who asks, never to a typed address.
        try:
            # The real greeting rule, applied to the administrator's own account (D-068).
            emails.send_test(
                request.user.email,
                obj.key,
                obj.subject,
                obj.body,
                greeting_name=greeting.name_for(request.user),
            )
        except Exception as error:  # noqa: BLE001 - any delivery error; only its class is logged
            logger.error("A test email could not be sent: %s.", type(error).__name__)
            self.message_user(request, _("The test email could not be sent."), messages.ERROR)
        else:
            self.message_user(
                request,
                gettext("A test email was sent to %(address)s.") % {"address": request.user.email},
                messages.SUCCESS,
            )

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        # Before the default patterns: their last pattern would accept these paths.
        return [
            path(
                "<path:object_id>/versions/",
                self.admin_site.admin_view(self.versions_view),
                name="%s_%s_versions" % info,
            ),
            path(
                "<path:object_id>/versions/<int:version_id>/restore/",
                self.admin_site.admin_view(self.restore_view),
                name="%s_%s_restore" % info,
            ),
            *super().get_urls(),
        ]

    def versions_view(self, request, object_id):
        if not self.has_view_permission(request):
            raise PermissionDenied
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            raise Http404
        context = {
            **self.admin_site.each_context(request),
            "title": gettext("Versions: %(name)s") % {"name": obj},
            "opts": self.opts,
            "original": obj,
            "versions": [
                (version, (version.subject, version.body) == (obj.subject, obj.body))
                for version in obj.versions.all()
            ],
            "default_is_current": not (obj.subject or obj.body),
            "can_restore": self.has_change_permission(request, obj),
            "change_url": reverse("admin:accounts_emailtext_change", args=(obj.pk,)),
        }
        return TemplateResponse(request, "admin/accounts/emailtext/versions.html", context)

    def restore_view(self, request, object_id, version_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied
        versions_url = reverse("admin:accounts_emailtext_versions", args=(object_id,))
        with transaction.atomic():
            obj = self.get_object(request, unquote(object_id))
            if obj is None:
                raise Http404
            obj = EmailText.objects.select_for_update().get(pk=obj.pk)
            subject = body = ""
            if version_id:
                version = obj.versions.filter(pk=version_id).first()
                if version is None:
                    raise Http404
                subject, body = version.subject, version.body
            try:
                email_texts.validate_subject(obj.key, subject)
                email_texts.validate_body(obj.key, body)
            except ValidationError:
                self.message_user(
                    request,
                    _("This version cannot be restored because it is no longer valid."),
                    messages.ERROR,
                )
                return HttpResponseRedirect(versions_url)
            previous = (obj.subject, obj.body)
            obj.subject, obj.body = subject, body
            obj.save()
            self._record_version(request, obj, previous)
            # Not self.log_change(): that one logs nothing when a preview field was posted.
            super().log_change(
                request,
                obj,
                gettext("Restored an earlier version.")
                if version_id
                else gettext("Default text restored."),
            )
        self.message_user(request, _("The version was restored."), messages.SUCCESS)
        return HttpResponseRedirect(versions_url)

    def changelist_view(self, request, extra_context=None):
        if self.has_view_or_change_permission(request):
            EmailText.objects.ensure_all()
        return super().changelist_view(request, extra_context)

    def history_view(self, request, object_id, extra_context=None):
        # The history names the acting account by its email address (see UserAdmin).
        if not request.user.is_superuser:
            raise PermissionDenied
        return super().history_view(request, object_id, extra_context)
