import json
import logging
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.http import Http404, HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.loader import render_to_string
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
    greeting,
    rate_limits,
    second_factor,
    second_factor_reset,
    security_log,
)
from leg_platform.accounts.forms import clean_postal_code, postal_code_field
from leg_platform.accounts.permission_names import (
    PermissionLabelsMixin,
    assignable_permissions_filter,
    rights_help,
)
from leg_platform.accounts.portal_settings import environment_values, portal_values
from leg_platform.accounts.portal_settings_form import PortalSettingsForm
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
    PortalSettings,
    SecurityEvent,
    User,
    customer_numbers_nearly_exhausted,
)
from leg_platform.branding import HEX_COLOUR
from leg_platform.environment import EMAIL_MODE_ALLOWLIST, EMAIL_MODE_CONSOLE
from leg_platform.mail import GraphEmailBackend, GraphEmailError
from leg_platform.presentation import accent_rules

logger = logging.getLogger(__name__)

Event = SecurityEvent.Event

# Versions of an email text that are kept per email (D-039).
MAX_EMAIL_TEXT_VERSIONS = 100

# The email texts in the order of the process of a person (design package, acceptance, P1-2). Every
# key is named once (a test checks it); a key that is not named is listed last.
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
    (_("Account"), (_Key.ACCOUNT_DELETED, _Key.SESSION_ENDED_WRONG_CODES, _Key.ALL_SESSIONS_ENDED)),
)

# Plain, instance-neutral titles instead of Django's defaults, which name Django (D-036). The
# header and the title of the site are the name of the instance (``PortalAdminSite.each_context``).
admin.site.index_title = _("Administration")

# The names of the pages of the lockout protection are plain words instead of the product name
# (WL-23); the models are those of django-axes, only their names are set here.
AccessAttempt._meta.verbose_name = _("blocked or failed login")
AccessAttempt._meta.verbose_name_plural = _("blocked and failed logins")
AccessFailureLog._meta.verbose_name = _("failed login in the log")
AccessFailureLog._meta.verbose_name_plural = _("log of failed logins")


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


class UserAdmin(PermissionLabelsMixin, admin.ModelAdmin):
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
        "hidden_tasks_display",
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
        (None, {"fields": ("hidden_tasks_display",)}),
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

    @admin.display(description=_("hidden tasks"))
    def hidden_tasks_display(self, obj):
        # Read only, as a help for support: it explains "I do not see the tile" (D-057).
        from leg_platform.accounts import tasks

        keys = sorted(obj.hidden_tasks.values_list("task_key", flat=True))
        if not keys:
            return "-"
        known = tasks.TASKS_BY_KEY
        return ", ".join(str(known[key].title) if key in known else key for key in keys)

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


class RestrictedGroupAdmin(PermissionLabelsMixin, DjangoGroupAdmin):
    """Groups are made by the system administrators from the rights of the list; the portal ships
    none (D-012). The pages carry the help "Which rights do I need for what?"."""

    change_form_template = "admin/auth/group/change_form.html"
    change_list_template = "admin/auth/group/change_list.html"

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        field = form.base_fields.get("permissions")
        if field is not None:
            # Only the documented rights; a right that the group already holds stays in the
            # list, so that saving never removes it silently.
            condition = assignable_permissions_filter()
            if obj is not None and obj.pk:
                condition |= Q(group=obj)
            field.queryset = (
                Permission.objects.filter(condition).select_related("content_type").distinct()
            )
        return form

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {"rights_help": rights_help(), **(extra_context or {})})

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        return super().changeform_view(
            request, object_id, form_url, {"rights_help": rights_help(), **(extra_context or {})}
        )
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
    list_display = ("title", "process", "text_status", "last_changed")
    list_display_links = ("title",)
    fields = ("title", "subject", "body", "reset_to_default", "placeholder_help")
    readonly_fields = ("title", "placeholder_help")
    change_list_template = "admin/accounts/emailtext/change_list.html"

    def get_queryset(self, request):
        # In the order of the process of a person: registration, login, addresses, 2FA, account.
        order = [key for _name, keys in EMAIL_TEXT_PROCESSES for key in keys]
        position = Case(
            *[When(key=key, then=Value(index)) for index, key in enumerate(order)],
            default=Value(len(order)),
            output_field=IntegerField(),
        )
        # Not ``super().get_queryset()``: that one orders by the names of ``get_ordering`` before
        # the position exists.
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
        # Plain words, not an icon: a default text is the normal state, not an error.
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
        if request.POST.get("reset_to_default") and not (obj.subject or obj.body):
            # The history says what happened in plain words (design package, final spot check, 7).
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
            # The real rule of the greeting, applied to the administrator's own account (D-068).
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
                gettext("Restored an earlier version.") if version_id else gettext("Default text restored."),
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
        "attempt_time",
    )
    search_fields = ("username", "path_info")
    change_list_template = "admin/axes_change_list.html"

    def changelist_view(self, request, extra_context=None):
        from leg_platform.accounts.login_protection import LOGIN_COOLOFF

        minutes = int(LOGIN_COOLOFF.total_seconds() // 60)
        return super().changelist_view(request, {"lockout_minutes": minutes, **(extra_context or {})})

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


class PortalSettingsAdmin(admin.ModelAdmin):
    """The settings of the portal: name, website, addresses of the legal notices, logos and colours,
    and, for system administrators, the values of the server configuration, read only (D-073).

    One row, no add and no delete. The rights to see and to change it are ordinary rights that
    a role gets at runtime (D-012); superusers pass automatically. Every change is a security event
    and is in the history of the administration. The logos are uploads that are checked before
    anything is stored (``portal_settings_form``, ``logo_images``).
    """

    form = PortalSettingsForm
    change_form_template = "admin/accounts/portalsettings/change_form.html"
    readonly_fields = (
        "privacy_preview",
        "imprint_preview",
        "logo_status",
        "preview",
        "server_configuration",
    )
    # The own preview link below opens a new tab; the standard "Currently" link of the widget is
    # not needed. The width of the fields is set in ``static/css/admin-portal.css``.
    formfield_overrides = {models.URLField: {"widget": forms.URLInput()}}
    # The example and the hint of each address (design package, acceptance, P2-5).
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
                _("Colours"),
                {"fields": (*PortalSettings.COLOUR_FIELDS, *(("reset_colours",) if can_change else ()))},
            ),
            (_("Preview"), {"fields": ("preview",)}),
        ]
        if request.user.is_superuser:
            # Only the system administrators see the server configuration: least disclosure
            # (D-073). It is read only and holds no secret.
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
        return fieldsets

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # The acting account of a rejected upload (the security event) and the request for the
        # check of the recent login; the class is built for this request only.
        return type(form.__name__, (form,), {"actor": request.user, "request": request})

    def change_view(self, request, object_id, form_url="", extra_context=None):
        # Saving needs a recent login (``PortalSettingsForm``); say so at the top of the page
        # before the person types and uploads, with a logout action next to it (the template
        # ``change_form.html``). The check at the save stays authoritative and unchanged.
        if (
            request.method == "GET"
            and self.has_change_permission(request)
            and not second_factor.recently_authenticated(request)
        ):
            extra_context = {
                **(extra_context or {}),
                "portal_login_stale": True,
                "portal_login_minutes": int(second_factor.RECENT_AUTHENTICATION_WINDOW.total_seconds() // 60),
            }
        return super().change_view(request, object_id, form_url, extra_context)

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
        """The saved colours for the preview panels of the form, as a file (no inline style is
        possible under the Content Security Policy). The script ``portal-preview.js`` changes the
        variables while the administrator types."""
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
        # One message for every way of saving ("Save" and "Save and continue editing"), instead
        # of the general sentence of Django (design package, final spot check, 6).
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
        # Without the two files of the logos: the page does not need them.
        return PortalSettings.load_without_logo_files()

    @staticmethod
    def _host(address):
        return (urlsplit(address).hostname or "-") if address else "-"

    @staticmethod
    def _quoted(value):
        """A text of the portal settings for the log line: JSON quotes it and escapes line breaks
        and control characters, so that no value can add a line to the log."""
        return json.dumps(value or "")

    @staticmethod
    def _colour(value):
        return value if value and HEX_COLOUR.fullmatch(value) else ("-" if not value else "invalid")

    def _audit_lines(self, stored, obj, form):
        """One line for every changed field. Public values only; for the logos the type, the size
        and the start of the digest, never the content."""
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
        return lines

    def save_model(self, request, obj, form, change):
        # Every change is a security event (D-056, D-073): the values are shown to every visitor,
        # and the addresses are shown as links. Only public values are logged: the name, the
        # host names of addresses, colours, and the type, size and digest start of a logo (never
        # its content); the full addresses are in the history of the administration.
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
        # The history names the acting account by its email address: system administrators only,
        # as for the email texts and the accounts (D-039, D-073).
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


admin.site.unregister(Group)
admin.site.register(Group, RestrictedGroupAdmin)
admin.site.register(User, UserAdmin)
admin.site.register(EmailText, EmailTextAdmin)
admin.site.register(SecurityEvent, SecurityEventAdmin)
admin.site.register(GraphConfiguration, GraphConfigurationAdmin)
admin.site.register(PortalSettings, PortalSettingsAdmin)
admin.site.register(AccessAttempt, ReadOnlyAxesAdmin)
admin.site.register(AccessFailureLog, ReadOnlyAxesFailureAdmin)