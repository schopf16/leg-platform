"""Django administration of the community app: the single grid operator row, superusers only (D-086)."""

from django import forms
from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent
from leg_platform.community.models import GridOperator


class GridOperatorForm(forms.ModelForm):
    class Meta:
        model = GridOperator
        fields = ("name",)


class GridOperatorAdmin(admin.ModelAdmin):
    form = GridOperatorForm
    fields = ("name",)

    def get_object(self, request, object_id, from_field=None):
        if str(object_id) != "1" or not request.user.is_superuser:
            return None
        # Checked before load(), which creates the row.
        return GridOperator.load()

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            return super().changelist_view(request, extra_context)
        GridOperator.load()
        return HttpResponseRedirect(reverse("admin:community_gridoperator_change", args=(1,)))

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # The name appears in every management text and PDF: a change is a security event.
        if "name" in form.changed_data:
            security_log.record(SecurityEvent.Event.GRID_OPERATOR_CHANGED, actor=request.user)


admin.site.register(GridOperator, GridOperatorAdmin)
