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
    PasskeyIdentity,
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
    passkey_devices_confirmed = forms.BooleanField(
        required=False,
        label=_("The person has confirmed to me that their passkeys are set up on two devices."),
    )

    class Meta:
        model = User
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        self.actor = kwargs.pop("actor", None)
        super().__init__(*args, **kwargs)
        if not (
            self.actor and self.actor.is_active and self.actor.is_superuser
            and self.actor.pk != self.instance.pk
            and self.instance.authentication_method == User.AuthenticationMethod.PASSKEY
            and (
                not (self.instance.is_staff or self.instance.is_superuser)
                or not PasskeyIdentity.objects.filter(
                    user=self.instance, two_devices_confirmed=True
                ).exists()
            )
        ):
            self.fields.pop("passkey_devices_confirmed", None)
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
            if self.instance.authentication_method == User.AuthenticationMethod.PASSKEY:
                field = "is_staff" if not self.instance.is_staff else "is_superuser"
                if self.instance.passkeys.filter(active=True).count() < 2:
                    self.add_error(field, _("Add a second active passkey before granting these rights."))
                elif not (self.instance.is_staff or self.instance.is_superuser) or not PasskeyIdentity.objects.filter(
                    user=self.instance, two_devices_confirmed=True
                ).exists():
                    if "passkey_devices_confirmed" not in self.fields:
                        self.add_error(field, _("The two-device confirmation is required before granting these rights."))
                    elif not cleaned_data.get("passkey_devices_confirmed"):
                        self.add_error(
                            "passkey_devices_confirmed",
                            _("The two-device confirmation is required before granting these rights."),
                        )
                return
            if not second_factor.has_second_factor(self.instance):
                field = "is_staff" if not self.instance.is_staff else "is_superuser"
                self.add_error(field, SECOND_FACTOR_REQUIRED_MESSAGE)


class AccountLifecycleForm(forms.Form):
    """Require an explicit POST confirmation for account lifecycle actions."""

    confirm = forms.BooleanField(label=_("I confirm this account change."))


class ResetSecondFactorForm(forms.Form):
    """Asks a system administrator to confirm the identity check for a privileged account."""

    identity_verified = forms.BooleanField(
        label=_("I compared the name on the official ID document received by email with this account.")
    )

    def __init__(self, *args, privileged, **kwargs):
        super().__init__(*args, **kwargs)


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
            fields = self.privilege_fields
            if (
                obj.authentication_method == User.AuthenticationMethod.PASSKEY
                and (
                    not (obj.is_staff or obj.is_superuser)
                    or not PasskeyIdentity.objects.filter(
                        user=obj, two_devices_confirmed=True
                    ).exists()
                )
            ):
                fields = (*fields, "passkey_devices_confirmed")
            return (*fieldsets, (None, {"fields": fields}))
        return fieldsets

    def get_form(self, request, obj=None, **kwargs):
        base_form = super().get_form(request, obj, **kwargs)

        class ActorBoundUserAdminForm(base_form):
            def __init__(self, *args, **form_kwargs):
                form_kwargs["actor"] = request.user
                super().__init__(*args, **form_kwargs)

        return ActorBoundUserAdminForm

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
            "end_sessions": (
                gettext("End all sessions"),
                gettext(
                    "Every session of the account on every device ends at its next request, including "
                    "sessions with \"Stay signed in\". The account can log in again (with its second "
                    "factor, if it has one). Use this when a device or a session may have got into "
                    "the wrong hands."
                ),
            ),
        }
        if operation not in operations:
            raise Http404
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            raise Http404
        if operation == "end_sessions" and obj.pk == request.user.pk:
            # Like the reset of the second factor (D-041): not for the own account, so that an administrator does not
            # cut the own session by accident; a new password ends the own sessions as well (D-100).
            raise PermissionDenied

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
                        "end_sessions": account.status != User.Status.DEACTIVATED,
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
                    elif operation == "end_sessions":
                        account.end_all_sessions()
                        event = Event.ACCOUNT_SESSIONS_ENDED_BY_ADMIN
                        message = gettext("Ended all sessions of the account.")
                    else:
                        result = account.delete_or_deactivate()
                        if result == "deactivated":
                            from leg_platform.community import customer_messages

                            customer_messages.purge(account)
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
        """Start unified recovery after the official-ID name check (D-106).

        Active staff and system administrators may request the same 24-hour confirmation link
        for any eligible account, including their own and the last system administrator.
        Requesting a link removes no method and grants no unrelated account-editing rights.
        """
        if not request.user.is_active or not (request.user.is_staff or request.user.is_superuser):
            raise PermissionDenied
        # The reset exception deliberately does not widen get_queryset or account editing.
        try:
            obj = User.objects.filter(pk=unquote(object_id)).first()
        except (ValueError, TypeError):
            raise Http404
        if obj is None:
            raise Http404
        privileged = obj.is_staff or obj.is_superuser
        recovery_eligible = obj.is_active
        change_url = reverse("admin:accounts_user_change", args=(obj.pk,))
        form = ResetSecondFactorForm(
            request.POST if request.method == "POST" else None, privileged=privileged
        )
        if request.method == "POST" and recovery_eligible and form.is_valid():
            try:
                second_factor_reset.request_link(request.user, obj)
            except second_factor_reset.ResetRefused:
                self.message_user(request, _("The change could not be made."), messages.ERROR)
            else:
                note = gettext("The sign-in recovery link was requested.")
                self.log_change(request, obj, note)
                self.message_user(request, note, messages.SUCCESS)
            return HttpResponseRedirect(change_url)
        context = {
            **self.admin_site.each_context(request),
            "title": gettext("Recover sign-in method"),
            "opts": self.opts,
            "original": obj,
            "form": form,
            "privileged": privileged,
            "recovery_eligible": recovery_eligible,
            "change_url": change_url,
        }
        return TemplateResponse(request, "admin/accounts/user/reset_second_factor.html", context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        """Minimal recovery-only settings when ordinary view/edit access is not granted."""
        if not request.user.is_active or not (request.user.is_staff or request.user.is_superuser):
            raise PermissionDenied
        obj = self.get_object(request, object_id)
        if obj is not None and self.has_view_or_change_permission(request, obj):
            return super().change_view(request, object_id, form_url, extra_context)
        try:
            target = User.objects.filter(pk=unquote(object_id)).first()
        except (ValueError, TypeError):
            raise Http404
        if target is None:
            raise Http404
        if request.method != "GET":
            raise PermissionDenied
        return TemplateResponse(request, "admin/accounts/user/recovery_settings.html", {
            **self.admin_site.each_context(request), "title": _("Recover sign-in method"),
            "target_number": target.customer_number_display, "target_name": target.display_name,
            "reset_url": reverse("admin:accounts_user_reset_second_factor", args=[target.pk]),
        })

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
            if (
                privileges is not None and privileges_granted(stored, *privileges)
                and stored.authentication_method == User.AuthenticationMethod.PASSKEY
            ):
                if (
                    not request.user.is_active or not request.user.is_superuser
                    or request.user.pk == stored.pk
                    or stored.passkeys.filter(active=True).count() < 2
                ):
                    raise PermissionDenied
                identity = PasskeyIdentity.objects.select_for_update().get(user=stored)
                if not (stored.is_staff or stored.is_superuser) or not identity.two_devices_confirmed:
                    if not form.cleaned_data.get("passkey_devices_confirmed"):
                        raise PermissionDenied
                    identity.two_devices_confirmed = True
                    identity.save(update_fields=["two_devices_confirmed"])
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
