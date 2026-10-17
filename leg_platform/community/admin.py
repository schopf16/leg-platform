"""The Django administration of the community app: the grid operator of the instance (D-086).

Only the grid operator is registered; the other community models have their own pages in the management area
(D-077) and stay out of the administration. The operator is one row (the current operator of the instance, no add
and no delete; the list page leads straight to the form), maintained by system administrators only, like the Microsoft
Graph settings: the name is shown to every staff account in the texts of the management area, and the owner plans a
catalogue of operators here later.
"""

from django import forms
from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

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
        # Checked before load(), because load() creates the row when it does not exist yet.
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


admin.site.register(GridOperator, GridOperatorAdmin)
