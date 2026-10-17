"""Administration of accounts: privileges, lifecycle actions, second-factor reset, neutral log."""

import json

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.admin.utils import unquote
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.translation import gettext, override
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import second_factor, second_factor_reset, security_log
from leg_platform.accounts.forms import clean_postal_code, postal_code_field
from leg_platform.accounts.models import (
    CUSTOMER_NUMBER_SPACE,
    AccountIntegrityError,
    AuthenticatorDevice,
    IssuedCustomerNumber,
    SecurityEvent,
    User,
    customer_numbers_nearly_exhausted,
)
from leg_platform.accounts.permission_names import PermissionLabelsMixin

Event = SecurityEvent.Event

# Rights are granted only to accounts that already use two-factor authentication (D-042).
SECOND_FACTOR_REQUIRED_MESSAGE = _(
    "Only accounts that have set up two-factor authentication can receive these rights. The "
    "person must first set up two-factor authentication in their profile."
)


def privileges_granted(account, is_staff, is_superuser):
    """Whether the new values give the account access or rights it does not have yet."""
    return (is_staff and not account.is_staff) or (is_superuser and not account.is_superuser)


class UserAdminForm(forms.ModelForm):
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
        (None, {"fields": ("salutation", "first_name", "last_name", "organisation_name")}),
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

    @admin.display(description=_("internal number"))
    def internal_number(self, obj):
        # The number stored in the admin log entries (neutral_object_repr).
        return obj.pk

    @admin.display(description=_("customer number"), ordering="customer_number")
    def customer_number_column(self, obj):
        return obj.customer_number_display

    @admin.display(description=_("hidden tasks"))
    def hidden_tasks_display(self, obj):
        from leg_platform.accounts import tasks

        keys = sorted(obj.hidden_tasks.values_list("task_key", flat=True))
        if not keys:
            return "-"
        known = tasks.TASKS_BY_KEY
        return ", ".join(str(known[key].title) if key in known else key for key in keys)

    @admin.display(description=_("postal code"))
    def postal_code(self, obj):
        # Shown instead of the form field when the account is only viewed.
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
        # The history names the acting account by its email address: system administrators only.
        # The check comes first, so the response does not reveal whether the account exists.
        if not request.user.is_superuser:
            raise PermissionDenied
        return super().history_view(request, object_id, extra_context)

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        # Before the default patterns: their last pattern would accept these paths.
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
                    self._log_neutral(
                        request,
                        [account],
                        DELETION if result == "deleted" else CHANGE,
                        message,
                    )
            except AccountIntegrityError:
                self.message_user(
                    request,
                    _(
                        "The account change could not be made. It may be the last system "
                        "administrator or its email address may be in use."
                    ),
                    messages.ERROR,
                )
                return HttpResponseRedirect(reverse("admin:accounts_user_change", args=(obj.pk,)))

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
        """Only system administrators, never for their own account (D-004, D-041).

        A user gets a link by email; a staff user or system administrator is reset at once after
        the administrator confirmed the identity check.
        """
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

    # LogEntry keeps object_repr permanently, also after the account is deleted: for accounts it
    # is a neutral text with the internal number, never the customer number, email or name
    # (D-018). These are all the ModelAdmin logging hooks (Django 6.0, 6.1).

    def log_addition(self, request, obj, message):
        return self._log_neutral(request, [obj], ADDITION, message)[0]

    def log_change(self, request, obj, message):
        return self._log_neutral(request, [obj], CHANGE, message)[0]

    def log_deletions(self, request, queryset):
        return self._log_neutral(request, queryset, DELETION)

    @staticmethod
    def neutral_object_repr(obj):
        # Stored text: the default language of the instance, not the viewer's.
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
                # Checked again on the locked row: the person could have turned the second factor
                # off in the meantime.
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
