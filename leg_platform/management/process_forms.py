"""Forms of the process pages (applications, deregistrations, notices). A form only reads and checks input; the rules are
in ``leg_platform.community`` (``application_staff``, ``deregistrations``). Dialogs are laid out by ``layout`` as
``dialog_form.html`` expects (D-097)."""

import copy

from django import forms
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator, numbers
from leg_platform.community.models import (
    REJECTION_NOTE_MAX_LENGTH,
    RETURN_REASON_MAX_LENGTH,
    ManagementSettings,
    validate_postal_code,
)
from leg_platform.management.form_fields import CONTROL, address_suggestions, date_field
from leg_platform.management.metering_point_forms import DialogForm, MeteringPointForm
from leg_platform.management.metering_point_views import format_date

SELECT = {"class": "form-select"}


class OperatorStartedForm(DialogForm):
    started_on = date_field(grid_operator.text(_("Registration at the %(operator)s started on")), initial="today")
    layout = [("single", ["started_on"])]
    # One sentence only: the lead of the dialog already says that the customer can no longer withdraw (W5-04).
    hint = _("The date is not in the future.")


class OperatorConfirmationForm(DialogForm):
    confirmed_on = date_field(grid_operator.text(_("Confirmation by the %(operator)s received on")), initial="today")
    layout = [("single", ["confirmed_on"])]
    hint = _("The date is not in the future and not before the registration.")


class CompleteForm(DialogForm):
    """Complete an application: the start date and whether the addresses of new buildings are confirmed at once.

    ``defaults`` (``application_staff.completion_defaults``) fills the start date with the customer's wish, never before
    the earliest date, and names the earliest date. ``confirm_addresses`` is an explicit tick, off by default (what the
    service does without it: new buildings stay "not yet confirmed" and editable); it is drawn by
    ``processes/dialog_complete.html`` and exists only while a building of the application is not confirmed yet."""

    start_date = date_field(_("Start date"))
    confirm_addresses = forms.BooleanField(required=False)
    layout = [("single", ["start_date"])]

    def __init__(self, *args, defaults=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.unconfirmed = defaults.unconfirmed_addresses if defaults is not None else 0
        if not self.unconfirmed:
            # Nothing to confirm: the field is not offered (a posted value is ignored).
            del self.fields["confirm_addresses"]
        if defaults is not None:
            self.fields["start_date"].initial = defaults.suggested_start_date
            earliest = format_date(defaults.earliest_start_date)
            self.fields["start_date"].help_text = (
                _("Earliest on %(date)s (three months after the registration).") % {"date": earliest}
                if defaults.registered
                else _("Earliest on %(date)s (three months from today).") % {"date": earliest}
            )

    def clean(self):
        cleaned = super().clean()
        cleaned.setdefault("confirm_addresses", False)
        return cleaned


class ReturnForm(DialogForm):
    """Return an application for correction: the reason is required and shown to the customer (500 characters)."""

    reason = forms.CharField(
        label=_("Reason for the correction"), max_length=RETURN_REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4, "maxlength": RETURN_REASON_MAX_LENGTH}),
        help_text=_("What the customer has to correct. The customer reads this text in the portal."),
    )
    layout = [("single", ["reason"])]


class RejectForm(DialogForm):
    reason = forms.ChoiceField(
        label=_("Reason"),
        choices=[
            ("rejected", _("Not accepted by the LEG")),
            ("operator_rejected", grid_operator.text(_("Rejected by the %(operator)s"))),
        ],
        widget=forms.Select(attrs=SELECT),
    )
    note = forms.CharField(
        label=_("Note for the customer"), required=False, max_length=REJECTION_NOTE_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3, "maxlength": REJECTION_NOTE_MAX_LENGTH}),
        help_text=_("Shown to the customer once on the next visit, not in the email."),
    )
    layout = [("single", ["reason"]), ("single", ["note"])]
    optional = ("note",)
    hint = _(
        "The application and its metering points are deleted. The account, the bank connection and stored "
        "buildings stay. The customer receives an email."
    )


class ConfirmDeregistrationForm(DialogForm):
    """Confirm the termination date of a deregistration. The field is filled with ``suggested_date`` of
    ``deregistrations.confirmation_defaults`` (the customer's wish) and names the earliest date (W5-05)."""

    confirmed_date = date_field(_("Effective on"))
    layout = [("single", ["confirmed_date"])]
    hint = _("The last day of a month, not in the past.")

    def __init__(self, *args, defaults=None, **kwargs):
        super().__init__(*args, **kwargs)
        if defaults is not None:
            self.fields["confirmed_date"].initial = defaults.suggested_date
            self.fields["confirmed_date"].help_text = _(
                "Earliest on %(date)s (four months after the notice, to the end of a month)."
            ) % {"date": format_date(defaults.earliest_date)}


# ---- The staff's corrections of an application in flight (D-109) ----


class ApplicationBuildingForm(DialogForm):
    """Correct the address of a new building of an application in flight.

    Checked on the server like the customer's wizard (``community.application_corrections.correct_building``). ``expected``
    is the token of the row that the dialog showed (a hidden value: only a change in between is detected with it, nothing
    is authorised); ``confirm_unlisted`` is set by the dialog itself after the directory did not know the address, so that
    the next save keeps it as entered, without an EGID."""

    street = forms.CharField(label=_("Street"), max_length=150, widget=forms.TextInput(attrs=CONTROL))
    house_number = forms.CharField(label=_("House number"), max_length=30, widget=forms.TextInput(attrs=CONTROL))
    postal_code = forms.CharField(
        label=_("Postal code"), max_length=4,
        widget=forms.TextInput(attrs={**CONTROL, "inputmode": "numeric", "maxlength": "4"}),
    )
    town = forms.CharField(label=_("Town"), max_length=100, widget=forms.TextInput(attrs=CONTROL))
    egid = forms.CharField(
        label=_("EGID"), required=False, max_length=9,
        widget=forms.TextInput(attrs={**CONTROL, "inputmode": "numeric", "maxlength": "9"}),
        help_text=_(
            "Leave it empty: the address directory gives the EGID. Enter it only when several buildings have this address."
        ),
    )
    expected = forms.CharField(widget=forms.HiddenInput, max_length=64)
    confirm_unlisted = forms.CharField(required=False, widget=forms.HiddenInput, max_length=1)

    layout = [("street", ["street", "house_number"]), ("postal", ["postal_code", "town"]), ("single", ["egid"])]
    optional = ("egid",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The typing suggestions of the address directory, as in the wizard of the customer (D-111).
        address_suggestions(self, country="CH")
    def clean_postal_code(self):
        value = self.cleaned_data["postal_code"].strip()
        validate_postal_code(value)
        return value

    def clean_egid(self):
        value = self.cleaned_data["egid"].strip()
        if value and not (value.isascii() and value.isdigit()):
            raise ValidationError(_("Enter the EGID as a number."), code="egid_invalid")
        return value

    @property
    def allow_unlisted(self):
        return self.cleaned_data.get("confirm_unlisted") == "1"

    def address(self):
        return {name: self.cleaned_data[name] for name in ("street", "house_number", "postal_code", "town")}


class ApplicationPointForm(DialogForm):
    """Correct the number, the direction and the plant figures of a metering point of an application in flight.

    The fields are the definitions of ``MeteringPointForm`` (one definition each, D-094): the typed last 20 characters of
    the number (the prefix of the settings stands in front; white space is ignored and a pasted complete number loses the
    prefix, D-109), the direction and the three plant figures. The server checks everything again
    (``community.application_corrections.correct_metering_point``); ``expected`` as in ``ApplicationBuildingForm``."""

    number_digits = copy.deepcopy(MeteringPointForm.base_fields["number_digits"])
    direction = copy.deepcopy(MeteringPointForm.base_fields["direction"])
    pv_power_kwp = copy.deepcopy(MeteringPointForm.base_fields["pv_power_kwp"])
    battery_kwh = copy.deepcopy(MeteringPointForm.base_fields["battery_kwh"])
    wallbox_kw = copy.deepcopy(MeteringPointForm.base_fields["wallbox_kw"])
    expected = forms.CharField(widget=forms.HiddenInput, max_length=64)

    layout = [
        ("single", ["number_digits"]),
        ("single", ["direction"]),
        ("divider", _("Plant details")),
        ("triple", ["pv_power_kwp", "battery_kwh", "wallbox_kw"]),
    ]
    optional = ("pv_power_kwp", "battery_kwh", "wallbox_kw")
    hint = _("Plant details apply to a feed-in only.")

    def __init__(self, data=None, *args, **kwargs):
        if data is not None:
            data = MeteringPointForm.padded(data)
        super().__init__(data, *args, **kwargs)
        self.duplicate = None
        self.identifier_missing = False
        self.prefix_text = ManagementSettings.load().metering_point_prefix
        number = self.fields["number_digits"]
        number.prefix_text = self.prefix_text
        number.after_template = "management/metering_points/_number_after.html"
        number.widget.attrs["data-prefix"] = self.prefix_text
        number.widget.attrs["data-check-url"] = reverse("management:metering_points_check_number")

    def clean_number_digits(self):
        return numbers.normalise_entered_number(self.cleaned_data["number_digits"], self.prefix_text)

