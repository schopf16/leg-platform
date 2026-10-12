from django import forms
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models import User


class UserAdminForm(forms.ModelForm):
    class Meta:
        model = User
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        if "is_staff" not in cleaned_data or "is_superuser" not in cleaned_data:
            return cleaned_data
        if cleaned_data["is_superuser"] and not cleaned_data["is_staff"]:
            self.add_error("is_staff", _("A system administrator must also be a staff user."))
        elif cleaned_data["is_staff"] and not self.instance.is_staff:
            if self.instance.status != User.Status.ACTIVE:
                self.add_error("is_staff", _("Only active accounts can receive staff privileges."))
        return cleaned_data


class UserAdmin(admin.ModelAdmin):
    form = UserAdminForm
    list_display = (
        "id",
        "email",
        "first_name",
        "last_name",
        "status",
        "is_staff",
        "is_superuser",
    )
    list_filter = ("status", "is_staff", "is_superuser")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("id",)
    readonly_fields = (
        "id",
        "email",
        "customer_number",
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
                    "id",
                    "email",
                    "status",
                    "is_active",
                    "customer_number",
                    "retention_obligation",
                )
            },
        ),
        (
            None,
            {
                "fields": (
                    "salutation",
                    "title",
                    "first_name",
                    "last_name",
                    "organisation_name",
                    "organisation_name_add_on_1",
                    "organisation_name_add_on_2",
                )
            },
        ),
        (
            None,
            {
                "fields": (
                    "address_line_1",
                    "address_line_2",
                    "street",
                    "house_number",
                    "dwelling_number",
                    "post_office_box_number",
                    "post_office_box_text",
                    "locality",
                    "town",
                    "swiss_postal_code",
                    "foreign_postal_code",
                    "country",
                )
            },
        ),
        (None, {"fields": ("phone_number",)}),
    )

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