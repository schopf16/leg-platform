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
from django.utils.formats import date_format
from django.utils.html import format_html, format_html_join
from django.utils.timezone import localtime
from django.utils.translation import gettext, override
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables
from axes.models import AccessAttempt, AccessFailureLog

from leg_platform.accounts import (
    email_texts,
    emails,
    rate_limits,
    second_factor,
    second_factor_reset,
    security_log,
)
from leg_platform.accounts.forms import clean_postal_code, postal_code_field
from leg_platform.accounts.models import (
    AccountIntegrityError,
    CERTIFICATE_EXPIRED,
    CERTIFICATE_EXPIRING,
    CUSTOMER_NUMBER_SPACE,
    GRAPH_CERTIFICATE_WARNING_DAYS,
    AuthenticatorDevice,
    EmailText,
    EmailTextVersion,
    GraphConfiguration,
    IssuedCustomerNumber,
    SecurityEvent,
    User,
    customer_numbers_nearly_exhausted,
)
from leg_platform.environment import EMAIL_MODE_ALLOWLIST, EMAIL_MODE_CONSOLE
from leg_platform.mail import GraphEmailBackend, GraphEmailError

logger = logging.getLogger(__name__)

Event = SecurityEvent.Event

# Versions of an email text that are kept per email (D-039).
MAX_EMAIL_TEXT_VERSIONS = 100

# Plain, instance-neutral titles instead of Django's defaults, which name Django (D-036).
# Instance-specific names follow with the instance configuration (README, "Current Status").
admin.site.site_header = _("LEG Platform")
admin.site.site_title = _("LEG Platform")
admin.site.index_title = _("Administration")


# Rights in the administration are granted only to an account that already uses two-factor
# authentication; otherwise whoever knows its password could set up their own authenticator
# app at the next login (docs/SECURITY.md, section 4; D-042).
SECOND_FACTOR_REQUIRED_MESSAGE = _(
    "Only accounts that have set up two-factor authentication can receive these rights. The "
    "person must first set up two-factor authentication in their profile."
)


def privileges_granted(account, is_staff, is_superuser):
    """Whether the new values give the account access or rights it does not have yet."""
    return (is_staff and not account.is_staff) or (is_superuser and not account.is_superuser)


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
            return
        if cleaned_data["is_staff"] and not self.instance.is_staff:
            if self.instance.status != User.Status.ACTIVE:
                self.add_error(
                    "is_staff", _("Only active accounts can receive access to the administration.")
                )
                return
        if privileges_granted(self.instance, cleaned_data["is_staff"], cleaned_data["is_superuser"]):
            if not second_factor.has_second_factor(self.instance):
                field = "is_staff" if not self.instance.is_staff else "is_superuser"
                self.add_error(field, SECOND_FACTOR_REQUIRED_MESSAGE)


class AccountLifecycleForm(forms.Form):
    """Require an explicit POST confirmation for account lifecycle actions."""

    confirm = forms.BooleanField(label=_("I confirm this account change."))


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
        "retention_obligation",
        "is_staff",
        "is_superuser",
    )
    list_filter = ("status", "retention_obligation", "is_staff", "is_superuser")
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
            path(
                "<path:object_id>/lifecycle/<str:operation>/",
                self.admin_site.admin_view(self.lifecycle_view),
                name="%s_%s_lifecycle" % info,
            ),
            *super().get_urls(),
        ]

    def lifecycle_view(self, request, object_id, operation):
        """Manage status or deletion only as a system administrator (D-016)."""
        if not request.user.is_superuser:
            raise PermissionDenied
        operations = {
            "block": (
                gettext("Block account"),
                gettext("The account will not be able to log in or reset its password."),
            ),
            "unblock": (
                gettext("Unblock account"),
                gettext("The account will be able to log in and reset its password again."),
            ),
            "delete": (
                gettext("Delete account"),
                gettext(
                    "The account is deleted if no retention obligation applies. Otherwise it is "
                    "kept as \"Deleted (retention obligation)\" and its email address becomes "
                    "available for a new registration."
                ),
            ),
            "reactivate": (
                gettext("Restore account"),
                gettext(
                    "The account will be restored if no active, temporarily locked, or blocked "
                    "account uses its email address. Any pending registration at that address "
                    "will be removed."
                ),
            ),
        }
        if operation not in operations:
            raise Http404
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            raise Http404

        title, description = operations[operation]
        form = AccountLifecycleForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            result = None
            try:
                with transaction.atomic():
                    account = User._base_manager.select_for_update().get(pk=obj.pk)
                    expected = {
                        "block": account.status == User.Status.ACTIVE,
                        "unblock": account.status == User.Status.BLOCKED,
                        "delete": account.status != User.Status.DEACTIVATED,
                        "reactivate": account.status == User.Status.DEACTIVATED,
                    }
                    if not expected[operation]:
                        raise AccountIntegrityError(
                            "The account state no longer permits this action."
                        )
                    if operation == "block":
                        account.set_status(User.Status.BLOCKED)
                        event = Event.ACCOUNT_BLOCKED
                        message = gettext("Blocked the account.")
                    elif operation == "unblock":
                        account.set_status(User.Status.ACTIVE)
                        event = Event.ACCOUNT_UNBLOCKED
                        message = gettext("Unblocked the account.")
                    elif operation == "reactivate":
                        account.reactivate()
                        event = Event.ACCOUNT_REACTIVATED
                        message = gettext("Restored the account.")
                    else:
                        result = account.delete_or_deactivate()
                        if result == "deactivated":
                            event = Event.ACCOUNT_DEACTIVATED
                            message = gettext(
                                "Deleted the account; it is kept under the retention obligation."
                            )
                        else:
                            event = Event.ACCOUNT_DELETED
                            message = gettext("Deleted the account.")
                    security_log.record(event, account=account, actor=request.user)
                    if result == "deleted":
                        self._log_neutral(request, [account], DELETION, message)
                    else:
                        self._log_neutral(request, [account], CHANGE, message)
            except AccountIntegrityError:
                self.message_user(
                    request,
                    _("The account change could not be made. It may be the last system administrator or its email address may be in use."),
                    messages.ERROR,
                )
                return HttpResponseRedirect(
                    reverse("admin:accounts_user_change", args=(obj.pk,))
                )

            self.message_user(request, message, messages.SUCCESS)
            return HttpResponseRedirect(
                reverse("admin:accounts_user_changelist")
                if result == "deleted"
                else reverse("admin:accounts_user_change", args=(obj.pk,))
            )

        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "opts": self.opts,
            "original": obj,
            "form": form,
            "description": description,
            "change_url": reverse("admin:accounts_user_change", args=(obj.pk,)),
        }
        return TemplateResponse(request, "admin/accounts/user/lifecycle_action.html", context)

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
            if (
                privileges is not None
                and privileges_granted(stored, *privileges)
                and not second_factor.has_second_factor(stored)
            ):
                # The form checked it; checked again on the locked row, because the person
                # could have turned the second factor off in the meantime (that also locks it).
                raise PermissionDenied
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

    # No actions, not even the site-wide "delete selected" (Django documentation, "Admin
    # actions": "actions = None" disables them for the model; works on Django 6.0 and 6.1).
    actions = None
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


class ReadOnlyAxesAdmin(admin.ModelAdmin):
    """Expose pseudonymous Axes lockout records only to system administrators."""

    actions = None
    list_display = ("attempt_time", "username", "path_info", "failures_since_start")
    list_filter = ("attempt_time", "path_info")
    ordering = ("-attempt_time",)
    list_per_page = 100
    show_full_result_count = False
    # Do not expose GET/POST data or IP addresses, even read-only. Authentication values and
    # client identifiers have no operational need on this administration page.
    fields = readonly_fields = (
        "username",
        "path_info",
        "failures_since_start",
        "user_agent",
        "http_accept",
        "attempt_time",
    )
    search_fields = ("username", "path_info")

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


class ReadOnlyAxesFailureAdmin(ReadOnlyAxesAdmin):
    list_display = ("attempt_time", "username", "path_info", "locked_out")
    fields = readonly_fields = (
        "username",
        "path_info",
        "locked_out",
        "user_agent",
        "http_accept",
        "attempt_time",
    )


# Microsoft Graph email delivery (D-006): one row (id 1), system administrators only. The private
# key is accepted as an upload, stored encrypted and never shown again.
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
        # Checked here, so that a wrong upload is shown as a form error before anything is saved.
        GraphConfiguration.check_certificate(private_key_pem, certificate_pem)
        return private_key_pem, certificate_pem


class GraphConfigurationAdmin(admin.ModelAdmin):
    """The Microsoft Graph settings of the email delivery (D-006).

    Only system administrators see and change it. No row can be added or deleted. Only the public
    certificate, its thumbprints and its expiry are displayed.
    """

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
        return reverse(
            "admin:accounts_graphconfiguration_change", args=(GRAPH_CONFIGURATION_PK,)
        )

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

    # Test email (D-006). It goes through GraphEmailBackend, never through the email framework,
    # and only to the address of the person who asks: in allowlist mode only to an allowed address.

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
        allowed = {recipient.lower() for recipient in settings.EMAIL_ALLOWED_RECIPIENTS}
        if settings.EMAIL_MODE == EMAIL_MODE_ALLOWLIST and address.lower() not in allowed:
            return _(
                "In this mode, email goes only to the allowed recipients. Your address is not "
                "one of them, so no test email was sent."
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
        return TemplateResponse(
            request, "admin/accounts/graphconfiguration/send_test.html", context
        )

    def _send_test(self, request, configuration):
        address = request.user.email
        # Recorded for every attempt, also for a refused one (D-018).
        security_log.record(Event.GRAPH_TEST_EMAIL_REQUESTED, actor=request.user)
        # D-048: test sends use the same per-address budget as other outgoing mail. The stored
        # account email is canonical; the rate-limit helper serialises the counter transactionally.
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


admin.site.unregister(Group)
admin.site.register(Group, RestrictedGroupAdmin)
admin.site.register(User, UserAdmin)
admin.site.register(EmailText, EmailTextAdmin)
admin.site.register(SecurityEvent, SecurityEventAdmin)
admin.site.register(GraphConfiguration, GraphConfigurationAdmin)
admin.site.register(AccessAttempt, ReadOnlyAxesAdmin)
admin.site.register(AccessFailureLog, ReadOnlyAxesFailureAdmin)