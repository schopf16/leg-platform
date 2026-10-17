"""Administration of groups: made by system administrators from the documented rights (D-012)."""

from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.models import Permission
from django.db.models import Q

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.permission_names import (
    PermissionLabelsMixin,
    assignable_permissions_filter,
    rights_help,
)

Event = SecurityEvent.Event


class RestrictedGroupAdmin(PermissionLabelsMixin, DjangoGroupAdmin):
    """System administrators only; the pages carry the help "Which rights do I need for what?"."""

    change_form_template = "admin/auth/group/change_form.html"
    change_list_template = "admin/auth/group/change_list.html"

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        field = form.base_fields.get("permissions")
        if field is not None:
            # A right the group already holds stays in the list, so saving never removes it silently.
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
