"""Forms of the customer's pages. Rules are checked by the services of ``leg_platform.community`` (the numbers, the
address, the IBAN, the ticks); these forms only read, trim and shape the input and give the fields their texts. The
server has the last word: a script only helps (progressive enhancement).
"""

from decimal import Decimal, InvalidOperation

from django import forms
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.forms import PortalFormMixin, ReauthenticationForm
from leg_platform.community import deregistrations, grid_operator, numbers
from leg_platform.community.application_common import add_months, month_end
from leg_platform.community.models import (
    BUILDING_DESCRIPTION_MAX_LENGTH,
    BUILDING_NAME_MAX_LENGTH,
    METERING_POINT_NAME_MAX_LENGTH,
)
from leg_platform.customer import contact, texts

YES, NO = "yes", "no"
ASAP, DATE = "asap", "date"
EARLIEST = "earliest"
DESIRED_DATE_CHOICES = 12  # how many month ends the list of desired dates offers


class CustomerForm(PortalFormMixin, forms.Form):
    """The control classes of Tabler; a radio list gets the class of a check box, not of a text field."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.RadioSelect):
                field.widget.attrs["class"] = "form-check-input"


class BuildingForm(CustomerForm):
    """Step 1: the address of the building (and the customer's own name for it)."""

    name = forms.CharField(
        label=_("Name of the building"), required=False, max_length=BUILDING_NAME_MAX_LENGTH,
        widget=forms.TextInput(attrs={"placeholder": _("e.g. House or block A")}),
    )
    street = forms.CharField(label=_("Street"), max_length=150)
    house_number = forms.CharField(
        label=_("House number"), max_length=30, widget=forms.TextInput(attrs={"autocomplete": "address-line2"})
    )
    postal_code = forms.CharField(
        label=_("Postal code"), max_length=4, widget=forms.TextInput(attrs={"inputmode": "numeric"})
    )
    town = forms.CharField(label=_("Town"), max_length=100)
    # The choice among several buildings of one address (a radio list) and the two ways to go on after suggestions;
    # the service checks the EGID against what the address directory offers for the typed address.
    egid = forms.CharField(required=False, max_length=12, widget=forms.HiddenInput)
    suggestion = forms.CharField(required=False, max_length=3, widget=forms.HiddenInput)
    confirm_typed = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def typed(self):
        data = self.cleaned_data
        return data["street"], data["house_number"], data["postal_code"], data["town"]


class DecimalField(forms.CharField):
    """A figure typed as text: digits with a point or a comma; empty is allowed (``None``)."""

    def clean(self, value):
        text = super().clean(value)
        if not text:
            return None
        try:
            number = Decimal(text.replace(",", ".").replace("’", "").replace(" ", ""))
        except InvalidOperation:
            raise forms.ValidationError(_("Enter a number."), code="invalid") from None
        if not number.is_finite() or number < 0:
            raise forms.ValidationError(_("Enter a number of at least 0."), code="invalid")
        return number


class MeteringPointsForm(CustomerForm):
    """Step 2: the draw, optionally the feed-in with its figures.

    ``draw_needed`` is False where the account has a draw at the building already; ``feed_in_offered`` is False where it
    has a feed-in there; ``feed_in_required`` is True where the draw exists and only a feed-in can be added."""

    draw_number = forms.CharField(
        label=grid_operator.text(
            _("Which metering point number does %(operator)s use to measure your electricity consumption?")
        ),
        required=False, max_length=250,
        help_text=_(
            "On your electricity bill, with the details of your supply point, also called metering point ID (not the shorter meter number). It starts with %(prefix)s, which is already filled in. Add the remaining %(length)s characters."
        ),
    )
    draw_name = forms.CharField(
        label=_("Name of the metering point"), required=False, max_length=METERING_POINT_NAME_MAX_LENGTH,
        widget=forms.TextInput(attrs={"placeholder": _("e.g. House")}),
    )
    feed_in = forms.ChoiceField(
        label=_("Do you feed in solar power?"), required=False, choices=((NO, _("No")), (YES, _("Yes"))),
        widget=forms.RadioSelect,
    )
    feed_in_number = forms.CharField(
        label=grid_operator.text(
            _("Which metering point number does %(operator)s use to measure your solar power feed-in?")
        ),
        required=False, max_length=250,
        help_text=_(
            "In the documents of your grid operator, with the details of your feed-in point, also called metering point ID (not the shorter meter number). It starts with %(prefix)s, which is already filled in. Add the remaining %(length)s characters."
        ),
    )
    feed_in_name = forms.CharField(
        label=_("Name of the metering point"), required=False, max_length=METERING_POINT_NAME_MAX_LENGTH,
        widget=forms.TextInput(attrs={"placeholder": _("e.g. PV system")}),
    )
    pv_power_kwp = DecimalField(
        label=_("PV power in kWp"), required=False, max_length=20, widget=forms.TextInput(attrs={"inputmode": "decimal"})
    )
    battery_kwh = DecimalField(
        label=_("Battery storage in kWh"), required=False, max_length=20,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    wallbox_kw = DecimalField(
        label=_("Wallbox in kW"), required=False, max_length=20, widget=forms.TextInput(attrs={"inputmode": "decimal"})
    )

    def __init__(self, *args, draw_needed=True, feed_in_offered=True, feed_in_required=False, prefix_text="", **kwargs):
        super().__init__(*args, **kwargs)
        self.draw_needed, self.feed_in_offered, self.feed_in_required = draw_needed, feed_in_offered, feed_in_required
        for name in ("draw_number", "feed_in_number"):
            self.fields[name].help_text = str(self.fields[name].help_text) % {"prefix": prefix_text, "length": numbers.SUFFIX_LENGTH}
        for name in ("draw_number", "feed_in_number"):
            self.fields[name].widget.attrs.update({"autocapitalize": "characters", "spellcheck": "false"})
        self.fields["draw_number"].summary_label = _("Metering point number")
        self.fields["feed_in_number"].summary_label = _("Feed-in metering point number")
        for name in ("draw_number", "draw_name", "feed_in_number", "feed_in_name"):
            self.fields[name].widget.attrs["autocomplete"] = "off"

    def _complete_number(self, name):
        """The typed part is never padded for the customer: fewer than ``SUFFIX_LENGTH`` characters is an error here
        (a padded number would be a number the customer did not type)."""
        value = self.cleaned_data.get(name) or ""
        if 0 < len(numbers.compact_number(value)) < numbers.SUFFIX_LENGTH:
            raise forms.ValidationError(
                _("Please enter all %(length)s characters."), code="too_short", params={"length": numbers.SUFFIX_LENGTH}
            )
        return value

    def clean_draw_number(self):
        return self._complete_number("draw_number") if self.draw_needed else self.cleaned_data.get("draw_number")

    def clean_feed_in_number(self):
        # A hidden feed-in block (answer "No") is not checked.
        if not self.wants_feed_in:
            return self.cleaned_data.get("feed_in_number")
        return self._complete_number("feed_in_number")

    @property
    def wants_feed_in(self):
        if not self.feed_in_offered:
            return False
        return self.feed_in_required or self.cleaned_data.get("feed_in") == YES

    def clean(self):
        data = super().clean()
        if self.draw_needed and not data.get("draw_number") and "draw_number" not in self.errors:
            self.add_error("draw_number", forms.ValidationError(_("Enter the last digits of the number."), code="empty"))
        if self.wants_feed_in and not data.get("feed_in_number") and "feed_in_number" not in self.errors:
            self.add_error(
                "feed_in_number", forms.ValidationError(_("Enter the last digits of the number."), code="empty")
            )
        return data


class BankForm(CustomerForm):
    """Step 3: the IBAN (full validation is the service's)."""

    iban = forms.CharField(
        label=_("IBAN"), max_length=60,
        help_text=_(
            "We only need your IBAN to pay you the compensation for the electricity you feed in. "
            "We transfer the credit to this account. You find the IBAN on your account or payment statement "
            "and on the back of your bank card."
        ),
        widget=forms.TextInput(attrs={
            "placeholder": "CH93 0076 2011 6238 5295 7", "autocomplete": "off", "autocapitalize": "characters",
            "spellcheck": "false", "class": "iban-input",
        }),
    )


class ContractForm(CustomerForm):
    """Step 4 and the Information page: the version shown and the tick (required; the service checks)."""

    version = forms.IntegerField(widget=forms.HiddenInput)
    agrees_contract = forms.BooleanField(required=False)


class StartForm(CustomerForm):
    """Step 5: as soon as possible or a desired date (the service checks the earliest day)."""

    start = forms.ChoiceField(
        label=_("When should it start?"), choices=((ASAP, _("As soon as possible")), (DATE, _("Desired date"))),
        widget=forms.RadioSelect,
    )
    desired_date = forms.DateField(
        label=_("Desired date"), required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    def clean(self):
        data = super().clean()
        if data.get("start") == DATE and not data.get("desired_date") and "desired_date" not in self.errors:
            self.add_error("desired_date", forms.ValidationError(_("Choose a date."), code="required"))
        if data.get("start") == ASAP:
            data["desired_date"] = None
        return data


class BuildingDetailsForm(CustomerForm):
    """The customer's own name and description of a building (the pencil on the page Buildings)."""

    name = forms.CharField(
        label=_("Name of the building"), required=False, max_length=BUILDING_NAME_MAX_LENGTH,
        widget=forms.TextInput(attrs={"placeholder": _("e.g. House or block A")}),
    )
    description = forms.CharField(label=_("Description"), required=False, max_length=BUILDING_DESCRIPTION_MAX_LENGTH)


class PointNameForm(CustomerForm):
    """The customer's own name of a metering point."""

    name = forms.CharField(
        label=_("Name of the metering point"), required=False, max_length=METERING_POINT_NAME_MAX_LENGTH,
        widget=forms.TextInput(attrs={"placeholder": _("e.g. House")}),
    )


class FeedInPointForm(PointNameForm):
    """The name and the plant figures of a feed-in point (the fields and the checks of the wizard form)."""

    pv_power_kwp = DecimalField(
        label=_("PV power in kWp"), required=False, max_length=20, widget=forms.TextInput(attrs={"inputmode": "decimal"})
    )
    battery_kwh = DecimalField(
        label=_("Battery storage in kWh"), required=False, max_length=20,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    wallbox_kw = DecimalField(
        label=_("Wallbox in kW"), required=False, max_length=20, widget=forms.TextInput(attrs={"inputmode": "decimal"})
    )


class DeregistrationForm(ReauthenticationForm):
    """The deregistration (ABMELDUNG.md): the metering points, the date and **one proof** (D-062: the code of the second
    factor if one is set up, else the password; wrong entries are counted per session like everywhere).

    The rules are the service's (``community.deregistrations``); this form checks what it can before the proof is used up
    (the code is checked only when nothing else is wrong): a point of the account's own list, the date (never in the
    past, a month end, not before the notice period; the date itself is never shown). A feed-in without an IBAN does not
    block it."""

    metering_points = forms.MultipleChoiceField(
        label=_("Which metering points should be deregistered?"), widget=forms.CheckboxSelectMultiple, required=False,
        error_messages={"invalid_choice": texts.NOT_DEREGISTRABLE},
    )
    # Registrations in progress that the customer withdraws instead (not chosen at the start); only the ids that the view
    # offers are valid, and the service checks the account and the state again.
    withdraw_applications = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple, error_messages={"invalid_choice": texts.NOT_DEREGISTRABLE},
    )
    termination = forms.ChoiceField(
        label=_("When should the deregistration take effect?"), initial=EARLIEST, widget=forms.RadioSelect,
        required=False, choices=((EARLIEST, _("Earliest possible")), (DATE, _("Desired date"))),
    )
    # A list of the realistic dates (the month ends from the earliest on); the posted value is still parsed as a date and
    # checked by ``check_changes`` and the service, so a date that is not offered is rejected.
    desired_date = forms.DateField(label=_("Desired date (last day of a month)"), required=False, widget=forms.Select)
    field_order = ("metering_points", "withdraw_applications", "termination", "desired_date", "current_password", "otp_token")

    def __init__(self, *args, points, withdrawable=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.points = {str(point.pk): point for point in points}
        self.fields["metering_points"].choices = [(key, key) for key in self.points]
        self.fields["withdraw_applications"].choices = [(str(row["pk"]), str(row["pk"])) for row in withdrawable]
        self.fields["termination"].widget.attrs["class"] = "form-check-input"
        first = deregistrations.earliest_termination_date()
        dates = [month_end(add_months(first, number)) for number in range(DESIRED_DATE_CHOICES)]
        self.fields["desired_date"].widget.choices = [(day.isoformat(), date_format(day, "d.m.Y")) for day in dates]
        self.fields["desired_date"].initial = first
        for name in ("current_password", "otp_token"):
            if name in self.fields:
                # The script keeps the button inactive until the proof is typed; no field takes the focus on arrival.
                attrs = self.fields[name].widget.attrs
                attrs.pop("autofocus", None)
                attrs["data-proof"] = "1"

    def clean(self):
        data = self.cleaned_data
        if "metering_points" in data and not data["metering_points"] and not data.get("withdraw_applications"):
            if "withdraw_applications" not in self.errors:
                self.add_error(
                    "metering_points" if self.points else None,
                    forms.ValidationError(_("Choose at least one metering point."), code="required"),
                )
        # The date matters whenever points are deregistered (the notice period is the same for every point).
        if data.get("metering_points"):
            if not data.get("termination"):
                self.add_error("termination", forms.ValidationError(_("This field is required."), code="required"))
            if data.get("termination") == DATE and not data.get("desired_date") and "desired_date" not in self.errors:
                self.add_error("desired_date", forms.ValidationError(_("Choose a date."), code="required"))
        if data.get("termination") == EARLIEST or not data.get("metering_points"):
            data["desired_date"] = None
        return super().clean()

    def chosen_points(self, cleaned_data=None):
        data = self.cleaned_data if cleaned_data is None else cleaned_data
        return [self.points[key] for key in data.get("metering_points", []) if key in self.points]

    def check_changes(self, cleaned_data):
        """Before the code is used up: the date. A missing IBAN of a feed-in does not block (owner decision 2026-10-29)."""
        desired = cleaned_data.get("desired_date")
        if cleaned_data.get("termination") == DATE and desired is not None:
            if desired < timezone.localdate():
                self.add_error("desired_date", texts.DATE_PAST)
            elif desired != month_end(desired):
                self.add_error("desired_date", texts.DATE_NOT_MONTH_END)
            elif desired < deregistrations.earliest_termination_date():
                self.add_error("desired_date", texts.DATE_TOO_EARLY)


# LastPass, 1Password and Bitwarden leave a field alone that carries their ignore attribute (D-115).
PASSWORD_MANAGER_IGNORE = {"data-lpignore": "true", "data-1p-ignore": "true", "data-bwignore": "true"}


class ContactForm(CustomerForm):
    """The contact form "Contact the LEG" (D-115): the message, the answer by mail or by phone, a copy. The mail address
    is prefilled from the account and changed only for this message; the phone number is checked and saved to the profile
    when it is sent."""

    message = forms.CharField(
        label=_("Your message"), max_length=4000, widget=forms.Textarea(attrs={"rows": 6}),
        help_text=_("Please do not enter passwords or access data."),
    )
    reply_by = forms.ChoiceField(
        label=_("How should we answer you?"),
        choices=((contact.REPLY_EMAIL, _("By email")), (contact.REPLY_PHONE, _("By phone"))),
        widget=forms.RadioSelect, initial=contact.REPLY_EMAIL,
    )
    # Password managers would fill a saved login into these fields over the offered address: they are told to ignore them.
    email = forms.EmailField(
        label=_("Email address"), max_length=254, required=False,
        widget=forms.EmailInput(attrs={"autocomplete": "off", **PASSWORD_MANAGER_IGNORE}),
    )
    phone = forms.CharField(
        label=_("Telephone number"), max_length=30, required=False,
        widget=forms.TextInput(attrs={"type": "tel", "placeholder": "+41 79 123 45 67", "autocomplete": "off", **PASSWORD_MANAGER_IGNORE}),
    )
    copy = forms.BooleanField(label=_("I would like a copy of this message by email."), required=False)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("reply_by") == contact.REPLY_EMAIL and not cleaned.get("email"):
            self.add_error("email", _("Please enter an email address."))
        if cleaned.get("reply_by") == contact.REPLY_PHONE:
            try:
                cleaned["phone"] = contact.swiss_phone(cleaned.get("phone") or "")
            except ValueError:
                self.add_error(
                    "phone",
                    _("Please enter a valid telephone number, for example +41 79 123 45 67 or 079 123 45 67."),
                )
        return cleaned
