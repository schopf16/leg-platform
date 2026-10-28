"""Django administration of the community app (D-086, D-091).

The grid operator row is for system administrators only. The other models are raw tables
(``raw_tables.RawTableAdmin``): a system administrator edits, adds and deletes rows without checks (owner decision
2026-10-19, D-091); a staff account that holds the ``view`` right of a table (handed out by a system administrator;
the management area itself uses none, D-086) sees the plain, read-only table: list columns, search and filters, no
actions, no editing, no inline. For that face, free text of staff (remarks, notes), the encrypted IBAN, email
addresses and names never appear, neither as a column nor on the page of a row: the explicit ``staff_list_display``
and ``staff_fields`` of each table are the only layout it ever gets.
"""

from django import forms
from django.contrib import admin
from django.db import models
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent
from leg_platform.addresses.admin_widgets import AddressSuggestionsMixin
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
from leg_platform.community.raw_tables import RawTableAdmin


class PlainTableAdmin(RawTableAdmin):
    """A table of the management data: plain and read-only for the ``view`` right, raw for system administrators.

    The read-only face names every column and every field of the page of a row explicitly
    (``staff_list_display``, ``staff_fields``; the fields are all read-only), so a column added to a model later is
    not shown to it by itself. Add, change and delete are for system administrators only (``RawTableAdmin``).
    """


class TransformerCircuitAdmin(PlainTableAdmin):
    staff_list_display = ("grid_number", "name", "created_at", "updated_at")
    search_fields = ("grid_number", "name")
    staff_fields = ("grid_number", "name", "created_at", "updated_at")


class LocationAdmin(AddressSuggestionsMixin, PlainTableAdmin):
    # The address suggestions work in the change form of a row, not in the editable list (a location lies in Switzerland).
    address_country = "CH"
    staff_list_display = (
        "street", "house_number", "postal_code", "town", "address_addition", "transformer_circuit",
        "is_archived", "needs_confirmation", "address_correction_open", "updated_at",
    )
    list_filter = ("is_archived", "needs_confirmation", "address_correction_open")
    list_select_related = ("transformer_circuit",)
    search_fields = ("street", "town", "postal_code")
    # No remark: free text of the staff.
    staff_fields = (
        "street", "house_number", "postal_code", "town", "address_addition", "transformer_circuit",
        "is_archived", "needs_confirmation", "address_correction_open", "created_at", "updated_at",
    )


class MeteringPointAdmin(PlainTableAdmin):
    staff_list_display = (
        "number", "direction", "location", "status_text", "pv_power_kwp", "battery_kwh", "wallbox_kw",
        "is_archived", "registered_with_operator_on", "confirmed_on", "start_date", "terminated_on",
    )
    list_filter = ("direction", "is_archived")
    list_select_related = ("location",)
    search_fields = ("number", "location__street", "location__town")
    derived_columns = ("status_text",)
    staff_fields = (
        "number", "direction", "location", "status_text", "pv_power_kwp", "battery_kwh", "wallbox_kw",
        "is_archived", "registered_with_operator_on", "confirmed_on", "start_date", "terminated_on",
        "created_at", "updated_at",
    )

    @admin.display(description=_("status"))
    def status_text(self, obj):
        # Derived from the dates and today's date, never stored.
        return obj.get_status_display()


class LegAdmin(PlainTableAdmin):
    staff_list_display = ("name", "production_quota_percent", "quota_checked_on", "is_archived", "updated_at")
    list_filter = ("is_archived",)
    search_fields = ("name",)
    staff_fields = ("name", "production_quota_percent", "quota_checked_on", "is_archived", "created_at", "updated_at")


class PersonAssignmentAdmin(PlainTableAdmin):
    # The account is shown as "Konto <customer number>" (``User.__str__``), never with name or email.
    staff_list_display = ("metering_point", "person", "valid_from", "valid_to")
    list_select_related = ("metering_point", "person")
    search_fields = ("metering_point__number",)
    staff_fields = ("metering_point", "person", "valid_from", "valid_to", "created_at", "updated_at")


class LegAssignmentAdmin(PlainTableAdmin):
    staff_list_display = ("metering_point", "leg", "valid_from", "valid_to")
    list_select_related = ("metering_point", "leg")
    search_fields = ("metering_point__number", "leg__name")
    staff_fields = ("metering_point", "leg", "valid_from", "valid_to", "created_at", "updated_at")


class PersonRecordAdmin(PlainTableAdmin):
    """The staff side of a person: the paper invoice wish only.

    Never the note (free text), the IBAN or the customer number at the grid operator: not as a column, not in the
    search, not on the page of a row (D-087, D-091; M-2 of the review of 2026-10-17: the number is shown on the
    detail page of the management area only)."""

    staff_list_display = ("person", "paper_invoice", "updated_at")
    list_filter = ("paper_invoice",)
    list_select_related = ("person",)
    staff_fields = ("person", "paper_invoice", "created_at", "updated_at")


class ShareChangeAdmin(PlainTableAdmin):
    staff_list_display = ("record", "quantity", "valid_from", "created_at")
    list_select_related = ("record__person",)
    staff_fields = ("record", "quantity", "valid_from", "actor_number", "created_at")

    @admin.display(description=_("performed by (internal number)"))
    def actor_number(self, obj):
        return obj.actor_id if obj.actor_id is not None else "-"


class HistoryEntryAdmin(PlainTableAdmin):
    """The history of the management objects: ids, dates and field names only, by its design (D-077)."""

    staff_list_display = ("created_at", "subject_type", "subject_id", "event", "actor_number")
    list_filter = ("subject_type", "event")
    search_fields = ("event",)
    ordering = ("-created_at", "-pk")
    staff_fields = ("created_at", "subject_type", "subject_id", "event", "actor_number", "detail")

    def save_row(self, obj, change):
        # ``HistoryEntry.save`` refuses a change (the application never changes an entry); the raw table does not.
        if change:
            models.Model.save(obj)
        else:
            obj.save()

    @admin.display(description=_("performed by (internal number)"))
    def actor_number(self, obj):
        return obj.actor_id if obj.actor_id is not None else "-"


class ManagementSettingsAdmin(AddressSuggestionsMixin, PlainTableAdmin):
    address_prefix = "sender_"
    address_country = "CH"
    staff_list_display = (
        "country_code", "identifier", "warning_threshold_percent", "sender_street", "sender_house_number",
        "sender_postal_code", "sender_town", "updated_at",
    )
    staff_fields = (
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
