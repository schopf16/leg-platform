import json

from django import forms
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.translation import gettext, override
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models import (
    SWISS_POSTAL_CODE_MESSAGE,
    SWITZERLAND,
    User,
    parse_swiss_postal_code,
)

# Plain, instance-neutral titles instead of Django's defaults, which name Django (D-036).
# Instance-specific names follow with the instance configuration (README, "Current Status").
admin.site.site_header = _("LEG Platform")
admin.site.site_title = _("LEG Platform")
admin.site.index_title = _("Administration")


class UserAdminForm(forms.ModelForm):
    # One postal code field instead of the two eCH-0010 fields (D-036): it is written into the
    # Swiss postal code for Switzerland and into the foreign postal code for every other
    # country (Liechtenstein included); the other one is cleared.
    postal_code = forms.CharField(label=_("postal code"), max_length=15, required=False)

    class Meta:
        model = User
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Also for bound forms: has_changed() compares the posted value with the initial one.
        self.initial.setdefault("postal_code", self.instance.postal_code_display)

    def clean(self):
        cleaned_data = super().clean()
        self._clean_postal_code(cleaned_data)
        self._clean_privileges(cleaned_data)
        return cleaned_data

    def _clean_postal_code(self, cleaned_data):
        if "country" not in cleaned_data or "postal_code" not in cleaned_data:
            return
        country = cleaned_data["country"]
        value = cleaned_data["postal_code"]
        if country == SWITZERLAND:
            swiss_postal_code = parse_swiss_postal_code(value)
            if swiss_postal_code is None:
                self.add_error("postal_code", SWISS_POSTAL_CODE_MESSAGE)
                return
            self.instance.swiss_postal_code = swiss_postal_code
            self.instance.foreign_postal_code = ""
        elif country:
            if not value:
                self.add_error("postal_code", _("This field is required."))
                return
            self.instance.swiss_postal_code = None
            self.instance.foreign_postal_code = value

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
            if change and "is_staff" in form.fields:
                privileges = (obj.is_staff, obj.is_superuser)
                obj.is_staff = stored.is_staff
                obj.is_superuser = stored.is_superuser
            super().save_model(request, obj, form, change)
            if privileges is not None and privileges != (obj.is_staff, obj.is_superuser):
                obj.set_privileges(is_staff=privileges[0], is_superuser=privileges[1])

    def save_related(self, request, form, formsets, change):
        if (
            request.user.is_superuser
            and form.instance.pk != request.user.pk
            and {"groups", "user_permissions"} & form.fields.keys()
        ):
            form.instance.set_access_relations(
                groups=form.cleaned_data.get("groups", form.instance.groups.all()),
                user_permissions=form.cleaned_data.get(
                    "user_permissions", form.instance.user_permissions.all()
                ),
            )
        for formset in formsets:
            self.save_formset(request, form, formset, change=change)


class RestrictedGroupAdmin(DjangoGroupAdmin):
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


admin.site.unregister(Group)
admin.site.register(Group, RestrictedGroupAdmin)
admin.site.register(User, UserAdmin)