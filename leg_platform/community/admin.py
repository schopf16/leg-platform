"""Django administration of the community app (D-086, D-091).

The grid operator row is for system administrators only. The other models are plain, read-only
tables of the database for the system administrator, who can hand out the ``view`` right of a table to a
role (Django model permissions; the management area itself uses none, D-086): list columns, search and
filters, no actions, no editing, no inline. Free text of staff (remarks, notes), the encrypted IBAN,
email addresses and names never appear, neither as a column nor on the page of a row.
"""

from django import forms
from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent
from leg_platform.community.models import (
    GridOperator,
    HistoryEntry,
    Leg,
    LegAssignment,
    Location,
    ManagementSettings,
    MeteringPoint,
    PersonAssignment,
    PersonRecord,
    ShareChange,
    TransformerCircuit,
)


class PlainTableAdmin(admin.ModelAdmin):
    """A read-only table: whoever holds the ``view`` right of the model sees the rows, nobody changes them here.

    Every column and every field of the page of a row is named explicitly (``fields`` equals
    ``readonly_fields``), so a column added to a model later is not shown by itself.
    """

    actions = None
    list_per_page = 50
    show_full_result_count = False

    def get_readonly_fields(self, request, obj=None):
        return self.fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class TransformerCircuitAdmin(PlainTableAdmin):
    list_display = ("grid_number", "name", "created_at", "updated_at")
    search_fields = ("grid_number", "name")
    fields = ("grid_number", "name", "created_at", "updated_at")


class LocationAdmin(PlainTableAdmin):
    list_display = (
        "street", "house_number", "postal_code", "town", "address_addition", "transformer_circuit",
        "is_archived", "needs_confirmation", "address_correction_open", "updated_at",
    )
    list_filter = ("is_archived", "needs_confirmation", "address_correction_open")
    list_select_related = ("transformer_circuit",)
    search_fields = ("street", "town", "postal_code")
    # No remark: free text of the staff.
    fields = (
        "street", "house_number", "postal_code", "town", "address_addition", "transformer_circuit",
        "is_archived", "needs_confirmation", "address_correction_open", "created_at", "updated_at",
    )


class MeteringPointAdmin(PlainTableAdmin):
    list_display = (
        "number", "direction", "location", "status_text", "pv_power_kwp", "battery_kwh", "wallbox_kw",
        "is_archived", "registered_with_operator_on", "confirmed_on", "start_date", "terminated_on",
    )
    list_filter = ("direction", "is_archived")
    list_select_related = ("location",)
    search_fields = ("number", "location__street", "location__town")
    fields = (
        "number", "direction", "location", "status_text", "pv_power_kwp", "battery_kwh", "wallbox_kw",
        "is_archived", "registered_with_operator_on", "confirmed_on", "start_date", "terminated_on",
        "created_at", "updated_at",
    )

    @admin.display(description=_("status"))
    def status_text(self, obj):
        # Derived from the dates and today's date, never stored.
        return obj.get_status_display()


class LegAdmin(PlainTableAdmin):
    list_display = ("name", "production_quota_percent", "quota_checked_on", "is_archived", "updated_at")
    list_filter = ("is_archived",)
    search_fields = ("name",)
    fields = ("name", "production_quota_percent", "quota_checked_on", "is_archived", "created_at", "updated_at")


class PersonAssignmentAdmin(PlainTableAdmin):
    # The account is shown as "Konto <customer number>" (``User.__str__``), never with name or email.
    list_display = ("metering_point", "person", "valid_from", "valid_to")
    list_select_related = ("metering_point", "person")
    search_fields = ("metering_point__number",)
    fields = ("metering_point", "person", "valid_from", "valid_to", "created_at", "updated_at")


class LegAssignmentAdmin(PlainTableAdmin):
    list_display = ("metering_point", "leg", "valid_from", "valid_to")
    list_select_related = ("metering_point", "leg")
    search_fields = ("metering_point__number", "leg__name")
    fields = ("metering_point", "leg", "valid_from", "valid_to", "created_at", "updated_at")


class PersonRecordAdmin(PlainTableAdmin):
    """The staff side of a person: the paper invoice wish only.

    Never the note (free text), the IBAN or the customer number at the grid operator: not as a column, not in the
    search, not on the page of a row (D-087, D-091; M-2 of the review of 2026-10-17: the number is shown on the
    detail page of the management area only)."""

    list_display = ("person", "paper_invoice", "updated_at")
    list_filter = ("paper_invoice",)
    list_select_related = ("person",)
    fields = ("person", "paper_invoice", "created_at", "updated_at")


class ShareChangeAdmin(PlainTableAdmin):
    list_display = ("record", "quantity", "valid_from", "created_at")
    list_select_related = ("record__person",)
    fields = ("record", "quantity", "valid_from", "actor_number", "created_at")

    @admin.display(description=_("performed by (internal number)"))
    def actor_number(self, obj):
        return obj.actor_id if obj.actor_id is not None else "-"


class HistoryEntryAdmin(PlainTableAdmin):
    """The history of the management objects: ids, dates and field names only, by its design (D-077)."""

    list_display = ("created_at", "subject_type", "subject_id", "event", "actor_number")
    list_filter = ("subject_type", "event")
    search_fields = ("event",)
    ordering = ("-created_at", "-pk")
    fields = ("created_at", "subject_type", "subject_id", "event", "actor_number", "detail")

    @admin.display(description=_("performed by (internal number)"))
    def actor_number(self, obj):
        return obj.actor_id if obj.actor_id is not None else "-"


class ManagementSettingsAdmin(PlainTableAdmin):
    list_display = (
        "country_code", "identifier", "warning_threshold_percent", "sender_street", "sender_house_number",
        "sender_postal_code", "sender_town", "updated_at",
    )
    fields = (
        "country_code", "identifier", "warning_threshold_percent", "sender_street", "sender_house_number",
        "sender_address_addition", "sender_postal_code", "sender_town", "updated_at",
    )


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
admin.site.register(TransformerCircuit, TransformerCircuitAdmin)
admin.site.register(Location, LocationAdmin)
admin.site.register(MeteringPoint, MeteringPointAdmin)
admin.site.register(Leg, LegAdmin)
admin.site.register(PersonAssignment, PersonAssignmentAdmin)
admin.site.register(LegAssignment, LegAssignmentAdmin)
admin.site.register(PersonRecord, PersonRecordAdmin)
admin.site.register(ShareChange, ShareChangeAdmin)
admin.site.register(HistoryEntry, HistoryEntryAdmin)
admin.site.register(ManagementSettings, ManagementSettingsAdmin)
