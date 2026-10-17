"""Forms of the metering point pages. A form only reads and checks input; the rules are in
``leg_platform.community``. Dialogs are laid out by ``layout`` (``(row class, [field names])``),
``optional`` (fields marked "(optional)") and ``hint`` (one note), as ``dialog_form.html`` expects."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.community import leg_switch, person_search
from leg_platform.community.metering_points import is_month_end
from leg_platform.community.models import REMARK_MAX_LENGTH, Leg, MeteringPoint
from leg_platform.management.location_forms import MeteringPointForm

NUMBER_ATTRS = {"class": "form-control tnum", "autocomplete": "off", "inputmode": "numeric"}


def date_field(label, *, required=True, initial=None):
    """A browser date field; ``initial="today"`` defaults to today."""
    return forms.DateField(
        label=label, required=required,
        initial=timezone.localdate if initial == "today" else initial,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control tnum"}, format="%Y-%m-%d"),
    )


def decimal_field(label, unit):
    return forms.DecimalField(
        label=label, required=False, min_value=0, max_digits=8, decimal_places=2,
        widget=forms.TextInput(attrs={"class": "form-control tnum", "placeholder": unit, "inputmode": "decimal"}),
    )


class LocationChoiceField(forms.CharField):
    """Finds a location by the text of a suggestion or a part of the address; empty means none.
    Several matches are refused."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("label", _("Location"))
        kwargs.setdefault(
            "widget",
            forms.TextInput(attrs={
                "class": "form-control", "autocomplete": "off", "list": "location-options",
                "placeholder": _("Search street or metering point number"),
            }),
        )
        super().__init__(**kwargs)

    def clean(self, value):
        from leg_platform.community import location_list

        text = super().clean(value)
        if not text:
            return None
        found = location_list.resolve(text)
        if found.location is None:
            raise ValidationError(
                _("Choose a location from the list.") if found.code == "none"
                else _("Several locations match. Type more of the address."),
                code=found.code,
            )
        return found.location


class MeteringPointCreateForm(MeteringPointForm):
    """"Create metering point": number first, then direction; the rest is optional. No person."""

    number_digits = forms.CharField(
        label=_("Metering point number"), max_length=20, widget=forms.TextInput(attrs=NUMBER_ATTRS)
    )
    location = LocationChoiceField()
    pv_power_kwp = decimal_field(_("PV power"), "kWp")
    battery_kwh = decimal_field(_("Battery"), "kWh")
    wallbox_kw = decimal_field(_("Wallbox"), "kW")
    remark = forms.CharField(
        label=_("Remark"), required=False, max_length=REMARK_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )
    optional = ("location", "leg", "pv_power_kwp", "battery_kwh", "wallbox_kw", "remark")
    field_order = ("number_digits", "direction", "location", "leg", "pv_power_kwp", "battery_kwh", "wallbox_kw", "remark")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.duplicate = None
        self.fields["leg"].empty_label = "–"
        self.order_fields(self.field_order)

    def metering_point_details(self):
        data = self.cleaned_data
        return {
            "pv_power_kwp": data.get("pv_power_kwp"), "battery_kwh": data.get("battery_kwh"),
            "wallbox_kw": data.get("wallbox_kw"), "remark": data.get("remark", ""),
        }


class DialogForm(forms.Form):
    layout = []
    optional = ()
    hint = ""


class NotifiedForm(DialogForm):
    registered_on = date_field(_("Notified on"), initial="today")
    layout = [("single", ["registered_on"])]


class ConfirmationForm(DialogForm):
    confirmed_on = date_field(_("Confirmed on"), initial="today")
    start_date = date_field(_("Start date"), required=False)
    layout = [("pair", ["confirmed_on", "start_date"])]
    optional = ("start_date",)
    hint = _("Without a start date the registration stays on standby.")


class StartDateForm(DialogForm):
    start_date = date_field(_("Start date"))
    layout = [("single", ["start_date"])]


class TerminationForm(DialogForm):
    terminated_on = date_field(_("Terminated to"))
    layout = [("single", ["terminated_on"])]
    hint = _("The date is the end of a month.")

    def clean_terminated_on(self):
        value = self.cleaned_data["terminated_on"]
        if not is_month_end(value):
            raise ValidationError(_("Enter the last day of a month."), code="not_month_end")
        return value


class PersonAssignForm(DialogForm):
    """The person is found by name, customer number or e-mail address; the date is free."""

    person = forms.CharField(
        label=_("Person"),
        widget=forms.TextInput(attrs={
            "class": "form-control", "autocomplete": "off", "list": "person-options",
            "placeholder": _("Search name, customer number or e-mail address"),
        }),
    )
    valid_from = date_field(_("Applies from"), initial="today")
    layout = [("single", ["person"]), ("single", ["valid_from"])]

    def clean_person(self):
        found = person_search.resolve(self.cleaned_data["person"])
        if found.person is None:
            raise ValidationError(
                _("No person found.") if found.code == "none"
                else _("Several persons match. Type more of the name or the customer number."),
                code=found.code,
            )
        return found.person


class AssignmentEndForm(DialogForm):
    valid_to = date_field(_("Ends on"), initial="today")
    # The assignment the dialog showed: only that one is ended (D-084).
    assignment = forms.CharField(widget=forms.HiddenInput, max_length=12)
    layout = [("single", ["valid_to"])]
    hint = _("Afterwards the metering point has no person. The person stays visible in the history.")


class LegAssignForm(DialogForm):
    """A metering point without a LEG."""

    leg = forms.ModelChoiceField(
        label=_("LEG"), queryset=Leg.objects.none(), empty_label=None,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    valid_from = date_field(_("Applies from"), initial="today")
    layout = [("single", ["leg"]), ("single", ["valid_from"])]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["leg"].queryset = Leg.objects.filter(is_archived=False).order_by("name")


class LegSwitchForm(DialogForm):
    """The new LEG, the date and the related metering points to move along (none preselected)."""

    leg = forms.ModelChoiceField(
        label=_("New LEG"), queryset=Leg.objects.none(), empty_label=None,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    valid_from = date_field(_("Valid from"), initial="today")
    also = forms.ModelMultipleChoiceField(
        queryset=MeteringPoint.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )
    layout = [("single", ["leg"]), ("single", ["valid_from"])]

    def __init__(self, *args, metering_point, current_leg_id=None, today=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.metering_point = metering_point
        self.related = leg_switch.related_points(metering_point, today)
        self.fields["leg"].queryset = (
            Leg.objects.filter(is_archived=False).exclude(pk=current_leg_id).order_by("name")
        )
        # Submitted ids are checked against the related points, never trusted.
        self.fields["also"].queryset = MeteringPoint.objects.filter(pk__in=[point.pk for point in self.related])


ASSIGNMENT_ERRORS = {
    "starts_not_after_previous": _("The date must be after the start of the current assignment."),
    "end_before_start": _("The end date must not be before the start of the assignment."),
    "already_ended": _("This assignment has already ended."),
    "not_deletable": _("This assignment cannot be deleted. End it instead."),
    "not_a_person": _("This account cannot be assigned."),
    "person_not_active": _("This account cannot be assigned."),
    "leg_archived": _("This LEG is archived."),
    "not_related": _("A selected metering point does not belong to this one."),
}

STATUS_ERRORS = {
    "archived": _("This metering point is archived."),
    "wrong_status": _("This step is not possible in the current status of the metering point."),
    "date_in_future": _("The date must not be in the future."),
    "date_before_previous": _("The date must not be before the earlier step."),
    "not_month_end": _("Enter the last day of a month."),
}
