import json
import logging

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext, override
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import email_texts, emails, second_factor_reset, security_log
from leg_platform.accounts.forms import clean_postal_code, postal_code_field
from leg_platform.accounts.models import (
    CUSTOMER_NUMBER_SPACE,
    AuthenticatorDevice,
    EmailText,
    EmailTextVersion,
    IssuedCustomerNumber,
    SecurityEvent,
    User,
    customer_numbers_nearly_exhausted,
)

logger = logging.getLogger(__name__)

Event = SecurityEvent.Event

# Versions of an email text that are kept per email (D-039).
MAX_EMAIL_TEXT_VERSIONS = 100

# Plain, instance-neutral titles instead of Django's defaults, which name Django (D-036).
# Instance-specific names follow with the instance configuration (README, "Current Status").
admin.site.site_header = _("LEG Platform")
admin.site.site_title = _("LEG Platform")
admin.site.index_title = _("Administration")


class UserAdminForm(forms.ModelForm):
    # One postal code field instead of the two eCH-0010 fields (D-036), see forms.py.
    postal_code = postal_code_field()

    class Meta:
        model = User
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Also for bound forms: has_changed() compares the posted value with the initial one.
        self.initial.setdefault("postal_code", self.instance.postal_code_display)

    def clean(self):
        cleaned_data = super().clean()
        clean_postal_code(self, cleaned_data)
        self._clean_privileges(cleaned_data)
        return cleaned_data

    def _clean_privileges(self, cleaned_data):
        if "is_staff" not in cleaned_data or "is_superuser" not in cleaned_data:
            return
        if cleaned_data["is_superuser"] and not cleaned_data["is_staff"]:
            self.add_error(
                "is_staff", _("A system administrator also needs access to the administration.")
            )
        elif cleaned_data["is_staff"] and not self.instance.is_staff:
            if self.instance.status != User.Status.ACTIVE:
                self.add_error(
                    "is_staff", _("Only active accounts can receive access to the administration.")
                )


class ResetSecondFactorForm(forms.Form):
    """Asks a system administrator to confirm the identity check for a privileged account."""

    identity_verified = forms.BooleanField(
        label=_(
            "I have verified the identity of this person in person, or by calling back a phone "
            "number that I know independently of the account."
        )
    )

    def __init__(self, *args, privileged, **kwargs):
        super().__init__(*args, **kwargs)
        if not privileged:
            del self.fields["identity_verified"]


class UserAdmin(admin.ModelAdmin):
    form = UserAdminForm
    list_display = (
        "customer_number_column",
        "email",
        "first_name",
        "last_name",
        "status",
        "is_staff",
        "is_superuser",
    )
    list_filter = ("status", "is_staff", "is_superuser")
    # The customer number is searched in its stored form without the space; get_queryset()
    # restricts the searched accounts first.
    search_fields = ("email", "first_name", "last_name", "customer_number__number")
    ordering = ("id",)
    readonly_fields = (
        "internal_number",
        "email",
        "customer_number_column",
        "retention_obligation",
        "status",
        "is_active",
    )
    privilege_fields = ("is_staff", "is_superuser", "groups", "user_permissions")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "internal_number",
                    "email",
                    "status",
                    "is_active",
                    "customer_number_column",
                    "retention_obligation",
                )
            },
        ),
        (
            None,
            {
                "fields": (
                    "salutation",
                    "first_name",
                    "last_name",
                    "organisation_name",
                )
            },
        ),
        (
            None,
            {
                "fields": (
                    "address_line_1",
                    "street",
                    "house_number",
                    "post_office_box_number",
                    "postal_code",
                    "town",
                    "country",
                )
            },
        ),
        (None, {"fields": ("phone_number",)}),
    )

    # Not shown (D-036): the eCH-0010 fields title, organisation_name_add_on_1,
    # organisation_name_add_on_2, address_line_2, dwelling_number, post_office_box_text and
    # locality, and the two postal code fields, which the form's postal_code field replaces.

    @admin.display(description=_("internal number"))
    def internal_number(self, obj):
        # The number stored in admin log entries (LogEntry.object_id and object_repr), so that
        # a log entry can be mapped to the account while it exists (D-018, D-036).
        return obj.pk

    @admin.display(description=_("customer number"), ordering="customer_number")
    def customer_number_column(self, obj):
        # Read from the row (customer_number_id); no query per account.
        return obj.customer_number_display

    @admin.display(description=_("postal code"))
    def postal_code(self, obj):
        # Shown instead of the form field when the account is only viewed (read-only).
        return obj.postal_code_display

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not request.user.is_superuser:
            queryset = queryset.filter(is_staff=False, is_superuser=False)
        return queryset

    def changelist_view(self, request, extra_context=None):
        if request.user.is_superuser and request.method == "GET":
            issued = IssuedCustomerNumber.objects.count()
            if customer_numbers_nearly_exhausted(issued):
                messages.warning(
                    request,
                    gettext(
                        "Customer numbers are running out: %(issued)d of %(total)d have been "
                        "issued (%(percent)d%%). Once all are issued, no registration can be "
                        "completed."
                    )
                    % {
                        "issued": issued,
                        "total": CUSTOMER_NUMBER_SPACE,
                        "percent": issued * 100 // CUSTOMER_NUMBER_SPACE,
                    },
                )
        return super().changelist_view(request, extra_context)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if request.user.is_superuser and obj is not None and obj.pk != request.user.pk:
            return (*fieldsets, (None, {"fields": self.privilege_fields}))
        return fieldsets

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = super().get_readonly_fields(request, obj)
        if not request.user.is_superuser or obj is None or obj.pk == request.user.pk:
            return (*readonly_fields, *self.privilege_fields)
        return readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        if (
            obj is not None
            and not request.user.is_superuser
            and (obj.is_staff or obj.is_superuser)
        ):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def history_view(self, request, object_id, extra_context=None):
        # Only system administrators see the history of accounts: it names the acting account
        # by its email address (project owner, 2026-10-12; security review finding N-1). 403
        # as for the other system-administrator-only admin pages (RestrictedGroupAdmin); the
        # check comes first, so the response does not reveal whether the account exists.
        if not request.user.is_superuser:
            raise PermissionDenied
        return super().history_view(request, object_id, extra_context)

    # Reset of a lost second factor (D-004, D-041): only system administrators, never for their
    # own account. A user receives a link by email and confirms it; a staff user or system
    # administrator is reset at once after the administrator confirmed the identity check.

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        # Before the default patterns: their last pattern would accept this path.
        return [
            path(
                "<path:object_id>/reset-second-factor/",
                self.admin_site.admin_view(self.reset_second_factor_view),
                name="%s_%s_reset_second_factor" % info,
            ),
            *super().get_urls(),
        ]

    def reset_second_factor_view(self, request, object_id):
        if not request.user.is_superuser:
            raise PermissionDenied
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            raise Http404
        if obj.pk == request.user.pk:
            raise PermissionDenied
        privileged = obj.is_staff or obj.is_superuser
        has_factor = AuthenticatorDevice.objects.filter(user=obj, confirmed=True).exists()
        change_url = reverse("admin:accounts_user_change", args=(obj.pk,))
        form = ResetSecondFactorForm(
            request.POST if request.method == "POST" else None, privileged=privileged
        )
        if request.method == "POST" and has_factor and form.is_valid():
            try:
                if privileged:
                    second_factor_reset.reset_privileged(request.user, obj)
                else:
                    second_factor_reset.request_link(request.user, obj)
            except second_factor_reset.ResetRefused:
                self.message_user(request, _("The change could not be made."), messages.ERROR)
            else:
                note = (
                    gettext("Two-factor authentication was reset.")
                    if privileged
                    else gettext("The link for removing two-factor authentication was requested.")
                )
                self.log_change(request, obj, note)
                self.message_user(request, note, messages.SUCCESS)
            return HttpResponseRedirect(change_url)
        context = {
            **self.admin_site.each_context(request),
            "title": gettext("Reset two-factor authentication"),
            "opts": self.opts,
            "original": obj,
            "form": form,
            "privileged": privileged,
            "has_factor": has_factor,
            "change_url": change_url,
        }
        return TemplateResponse(request, "admin/accounts/user/reset_second_factor.html", context)

    # Admin log entries (LogEntry) keep object_repr permanently, also after the account is
    # deleted. For accounts it is a neutral text with the internal number, never the customer
    # number (it appears on invoices), the email address or the name (D-018, D-036). These
    # are all the logging hooks of ModelAdmin (Django 6.0 and 6.1); they replace
    # LogEntry.objects.log_actions(), which would store str(obj).

    def log_addition(self, request, obj, message):
        return self._log_neutral(request, [obj], ADDITION, message)[0]

    def log_change(self, request, obj, message):
        return self._log_neutral(request, [obj], CHANGE, message)[0]

    def log_deletions(self, request, queryset):
        return self._log_neutral(request, queryset, DELETION)

    @staticmethod
    def neutral_object_repr(obj):
        # Stored text, so in the default language of the instance, not the viewer's.
        with override(settings.LANGUAGE_CODE):
            return gettext("Account with internal number %(number)s") % {"number": obj.pk}

    def _log_neutral(self, request, objects, action_flag, message=""):
        if isinstance(message, list):
            message = json.dumps(message)
        content_type = ContentType.objects.get_for_model(User, for_concrete_model=False)
        entries = []
        for obj in objects:
            entry = LogEntry(
                user_id=request.user.pk,
                content_type_id=content_type.pk,
                object_id=obj.pk,
                object_repr=self.neutral_object_repr(obj)[:200],
                action_flag=action_flag,
                change_message=message,
            )
            entry.save()
            entries.append(entry)
        return entries

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            stored = None
            if change:
                stored = User._base_manager.select_for_update().get(pk=obj.pk)
                if not request.user.is_superuser and (stored.is_staff or stored.is_superuser):
                    raise PermissionDenied
            privileges = None
            if change:
                # The form instance can be stale; these values change only elsewhere (D-041).
                obj.password = stored.password
                obj.session_version = stored.session_version
                obj.password_change_required = stored.password_change_required
            if change and "is_staff" in form.fields:
                privileges = (obj.is_staff, obj.is_superuser)
                obj.is_staff = stored.is_staff
                obj.is_superuser = stored.is_superuser
            super().save_model(request, obj, form, change)
            if privileges is not None and privileges != (obj.is_staff, obj.is_superuser):
                obj.set_privileges(is_staff=privileges[0], is_superuser=privileges[1])
                obj.end_all_sessions()
                security_log.record(Event.PRIVILEGES_CHANGED, account=obj, actor=request.user)

    def save_related(self, request, form, formsets, change):
        if (
            request.user.is_superuser
            and form.instance.pk != request.user.pk
            and {"groups", "user_permissions"} & form.fields.keys()
        ):
            instance = form.instance
            before = self._access_relations(instance)
            instance.set_access_relations(
                groups=form.cleaned_data.get("groups", instance.groups.all()),
                user_permissions=form.cleaned_data.get(
                    "user_permissions", instance.user_permissions.all()
                ),
            )
            if before != self._access_relations(instance):
                instance.end_all_sessions()
                security_log.record(
                    Event.ACCESS_RELATIONS_CHANGED, account=instance, actor=request.user
                )
        for formset in formsets:
            self.save_formset(request, form, formset, change=change)

    @staticmethod
    def _access_relations(user):
        return (
            set(user.groups.values_list("pk", flat=True)),
            set(user.user_permissions.values_list("pk", flat=True)),
        )


class RestrictedGroupAdmin(DjangoGroupAdmin):
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        security_log.record(Event.ACCESS_RELATIONS_CHANGED, actor=request.user)

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        security_log.record(Event.ACCESS_RELATIONS_CHANGED, actor=request.user)

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset)
        security_log.record(Event.ACCESS_RELATIONS_CHANGED, actor=request.user)

    def has_module_permission(self, request):
        return request.user.is_superuser and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_delete_permission(request, obj)


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
            # The unchanged default stays "not customised", so that later improvements of it
            # reach this instance.
            data[field] = "" if text == default else text
            try:
                validate(key, data[field])
            except forms.ValidationError as error:
                self.add_error(field, error)
        return data


class EmailTextAdmin(admin.ModelAdmin):
    form = EmailTextForm
    list_display = ("title", "customised", "updated_at")
    list_display_links = ("title",)
    ordering = ("key",)
    fields = ("title", "subject", "body", "reset_to_default", "placeholder_help")
    readonly_fields = ("title", "placeholder_help")
    change_list_template = "admin/accounts/emailtext/change_list.html"

    @admin.display(description=_("email"), ordering="key")
    def title(self, obj):
        return str(obj)

    @admin.display(boolean=True, description=_("changed"))
    def customised(self, obj):
        return bool(obj.subject or obj.body)

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

    # Preview, test email and versions (D-039). The preview and the test email use the text as
    # it was typed, validated like a save, and store nothing.

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
            emails.send_test(request.user.email, obj.key, obj.subject, obj.body)
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
            super().log_change(request, obj, gettext("Restored an earlier version."))
        self.message_user(request, _("The version was restored."), messages.SUCCESS)
        return HttpResponseRedirect(versions_url)

    def changelist_view(self, request, extra_context=None):
        if self.has_view_or_change_permission(request):
            EmailText.objects.ensure_all()
        extra_context = {**(extra_context or {}), "overview": email_texts.overview()}
        return super().changelist_view(request, extra_context)

    def history_view(self, request, object_id, extra_context=None):
        # The history names the acting account by its email address (see UserAdmin).
        if not request.user.is_superuser:
            raise PermissionDenied
        return super().history_view(request, object_id, extra_context)


class SecurityEventAdmin(admin.ModelAdmin):
    """The security log: read only, for system administrators only (D-018, D-041).

    Nobody can add, change or delete an entry here; entries leave only through the retention.
    """

    list_display = ("occurred_at", "event", "account_number", "actor_number", "retention")
    list_filter = ("event", "long_retention")
    ordering = ("-occurred_at", "-id")
    list_per_page = 100
    show_full_result_count = False
    fields = readonly_fields = ("occurred_at", "event", "account_id", "actor_id", "long_retention")

    @admin.display(description=_("account (internal number)"), ordering="account_id")
    def account_number(self, obj):
        return obj.account_id if obj.account_id is not None else "-"

    @admin.display(description=_("performed by (internal number)"), ordering="actor_id")
    def actor_number(self, obj):
        return obj.actor_id if obj.actor_id is not None else "-"

    @admin.display(description=_("kept"))
    def retention(self, obj):
        days = (
            settings.SECURITY_LOG_LONG_RETENTION_DAYS
            if obj.long_retention
            else settings.SECURITY_LOG_RETENTION_DAYS
        )
        return gettext("%(days)d days") % {"days": days}

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}


admin.site.unregister(Group)
admin.site.register(Group, RestrictedGroupAdmin)
admin.site.register(User, UserAdmin)
admin.site.register(EmailText, EmailTextAdmin)
admin.site.register(SecurityEvent, SecurityEventAdmin)