"""Forms of the person pages (D-081, D-087): note, grid operator customer number, shares (D-086), creating a
person and the customer data dialogs. The rules are in ``community.persons``, which checks them again; the
fields, validators and name/address rules are those of the account model (``persons.group_errors``), the postal
code is one field written to the Swiss or foreign code by the country (D-036), the IBAN is checked by
``community.iban``.
"""

import copy

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_countries import countries
from leg_platform.accounts.forms import clean_postal_code, postal_code_field
from leg_platform.accounts.models import EMAIL_MAX_LENGTH, SWITZERLAND, User, validate_account_email
from leg_platform.accounts.normalization import normalize_email_address
from leg_platform.community import grid_operator, persons
from leg_platform.community import iban as iban_module

CONTROL = {"class": "form-control", "autocomplete": "off"}
SELECT = {"class": "form-select", "autocomplete": "off"}


class NoteForm(forms.Form):
    note = forms.CharField(
        label=_("Note"), required=False, max_length=persons.NOTE_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 6}),
    )
    layout = [("single", ["note"])]
    optional = ()


class PaymentForm(forms.Form):
    """The payment card dialog (D-087). IBAN and paper invoice are customer data and are left out
    (``customer_data`` false) when the actor may not change the account (D-012)."""

    iban = forms.CharField(
        label=_("IBAN"), required=False, max_length=iban_module.IBAN_MAX_LENGTH * 2,
        widget=forms.TextInput(attrs={**CONTROL, "class": "form-control tnum", "autocapitalize": "characters", "spellcheck": "false"}),
    )
    paper_invoice = forms.TypedChoiceField(
        label=_("Paper invoice"), choices=[("0", _("No")), ("1", _("Yes"))], coerce=lambda value: value == "1",
        widget=forms.Select(attrs=SELECT),
    )
    operator_customer_number = forms.CharField(
        required=False, max_length=persons.OPERATOR_NUMBER_MAX_LENGTH, widget=forms.TextInput(attrs=CONTROL),
    )
    optional = ("iban", "operator_customer_number")

    def __init__(self, *args, customer_data=True, **kwargs):
        super().__init__(*args, **kwargs)
        # The label names the grid operator of the instance (D-086).
        self.fields["operator_customer_number"].label = grid_operator.text(_("%(operator)s customer number"))
        self.customer_data = customer_data
        if not customer_data:
            del self.fields["iban"]
            del self.fields["paper_invoice"]
        self.layout = [("single", [name]) for name in self.fields]

    def clean_iban(self):
        try:
            return iban_module.validate(self.cleaned_data["iban"])
        except ValidationError as error:
            raise forms.ValidationError(error.messages, code="invalid_iban") from None

    def clean_operator_customer_number(self):
        value = " ".join(self.cleaned_data["operator_customer_number"].split())
        if not value.isprintable():
            raise forms.ValidationError(_("This value is not valid."), code="invalid")
        return value


class PersonDataForm(forms.Form):
    """Registration/profile fields entered by staff: one form to create a person and one per card. A subclass names
    its ``field_names`` and the ``groups`` of ``persons.GROUP_FIELDS`` whose rules apply. The model rules run
    through ``persons.group_errors`` on a copy of the account (same code as the service); ``values()`` feeds the
    service."""

    email = forms.EmailField(
        label=_("Email"), max_length=EMAIL_MAX_LENGTH,
        widget=forms.EmailInput(attrs={**CONTROL, "inputmode": "email", "autocapitalize": "none", "spellcheck": "false"}),
    )
    salutation = forms.ChoiceField(
        label=_("Salutation"), required=False, choices=[("", "–"), *User.Salutation.choices],
        widget=forms.Select(attrs=SELECT),
    )
    organisation_name = forms.CharField(
        label=_("Company"), required=False, max_length=60, widget=forms.TextInput(attrs=CONTROL)
    )
    first_name = forms.CharField(
        label=_("First name"), required=False, max_length=30, widget=forms.TextInput(attrs=CONTROL)
    )
    last_name = forms.CharField(
        label=_("Last name"), required=False, max_length=30, widget=forms.TextInput(attrs=CONTROL)
    )
    address_line_1 = forms.CharField(
        label=_("Second person"), required=False, max_length=150, widget=forms.TextInput(attrs=CONTROL)
    )
    phone_number = User._meta.get_field("phone_number").formfield(
        label=_("Phone"), required=False,
        widget=forms.TextInput(attrs={**CONTROL, "inputmode": "tel"}),
    )
    secondary_email = forms.EmailField(
        label=_("Second email"), required=False, max_length=EMAIL_MAX_LENGTH,
        widget=forms.EmailInput(attrs={**CONTROL, "inputmode": "email", "autocapitalize": "none", "spellcheck": "false"}),
    )
    street = forms.CharField(label=_("Street"), required=False, max_length=150, widget=forms.TextInput(attrs=CONTROL))
    house_number = forms.CharField(
        label=_("House number"), required=False, max_length=30, widget=forms.TextInput(attrs=CONTROL)
    )
    post_office_box_number = User._meta.get_field("post_office_box_number").formfield(
        label=_("Post office box"), required=False, widget=forms.TextInput(attrs={**CONTROL, "inputmode": "numeric"}),
    )
    postal_code = postal_code_field()
    town = forms.CharField(label=_("Town"), required=False, max_length=40, widget=forms.TextInput(attrs=CONTROL))
    country = forms.ChoiceField(
        label=_("Country"), initial=SWITZERLAND, choices=lambda: list(countries), widget=forms.Select(attrs=SELECT)
    )

    field_names = ()
    groups = ()
    layout = []
    optional = ()

    def __init__(self, data=None, *, person=None, **kwargs):
        # A copy of the account for the model rules; never saved.
        self.instance = copy.copy(person) if person is not None else User()
        initial = kwargs.pop("initial", None) or {}
        if person is not None:
            initial = {**self.initial_of(person), **initial}
        super().__init__(data, initial=initial, **kwargs)
        for name in list(self.fields):
            if name not in self.field_names:
                del self.fields[name]
        if "postal_code" in self.fields:
            self.fields["postal_code"].label = _("Postal code")
            self.fields["postal_code"].widget = forms.TextInput(attrs={**CONTROL, "maxlength": "15", "autocomplete": "off"})
        if "street" in self.fields:
            # Address directory suggestions under the street field, as in the registration and the profile.
            self.fields["street"].after_template = "addresses/widget.html"
            if "town" in self.fields:
                self.fields["town"].after_template = "addresses/widget_notice.html"
        if "post_office_box_number" in self.fields:
            self.fields["post_office_box_number"].help_text = _("Only if you have no street address.")

    @staticmethod
    def initial_of(person):
        return {
            "salutation": person.salutation,
            "organisation_name": person.organisation_name,
            "first_name": person.first_name,
            "last_name": person.last_name,
            "address_line_1": person.address_line_1,
            "phone_number": person.phone_number_display,
            "secondary_email": person.secondary_email,
            "street": person.street,
            "house_number": person.house_number,
            "post_office_box_number": person.post_office_box_number,
            "postal_code": person.postal_code_display,
            "town": person.town,
            "country": person.country.code if person.country else SWITZERLAND,
        }

    @staticmethod
    def _clean_address(value):
        try:
            validate_account_email(value)
        except ValueError:
            raise forms.ValidationError(_("Enter a valid email address."), code="invalid") from None
        return normalize_email_address(value)

    def clean_email(self):
        return self._clean_address(self.cleaned_data["email"])

    def clean_secondary_email(self):
        value = self.cleaned_data.get("secondary_email")
        return self._clean_address(value) if value else ""

    def clean(self):
        cleaned = super().clean()
        fields = {name for group in self.groups for name in persons.GROUP_FIELDS[group]}
        for name in fields:
            if name in cleaned and name not in ("swiss_postal_code", "foreign_postal_code"):
                setattr(self.instance, name, cleaned[name])
        if "postal_code" in self.fields:
            clean_postal_code(self, cleaned)
        for group in self.groups:
            for name, messages in persons.group_errors(self.instance, group).items():
                if name in ("swiss_postal_code", "foreign_postal_code"):
                    name = "postal_code"
                if name in self.errors:
                    continue
                self.add_error(name if name in self.fields else None, messages)
        return cleaned

    def values(self):
        """The cleaned values of the group fields (the postal code as the Swiss or foreign one)."""
        return {
            name: getattr(self.instance, name)
            for group in self.groups
            for name in persons.GROUP_FIELDS[group]
            if name != "post_office_box_number" or name in self.fields
        }


class PersonCreateForm(PersonDataForm):
    """"Person anlegen": the registration data (D-087), confirmed by the person at registration."""

    field_names = (
        "email", "salutation", "organisation_name", "first_name", "last_name", "address_line_1", "street",
        "house_number", "postal_code", "town", "country", "phone_number",
    )
    groups = (persons.GROUP_DETAILS, persons.GROUP_ADDRESS, persons.GROUP_CONTACT)
    layout = [
        ("single", ["email"]),
        ("single", ["salutation"]),
        ("single", ["organisation_name"]),
        ("pair", ["first_name", "last_name"]),
        ("single", ["address_line_1"]),
        ("street", ["street", "house_number"]),
        ("postal", ["postal_code", "town"]),
        ("single", ["country"]),
        ("single", ["phone_number"]),
    ]
    optional = ("salutation", "organisation_name", "first_name", "last_name", "address_line_1", "phone_number")

    def clean(self):
        cleaned = super().clean()
        # The registration asks for a street.
        if not self.cleaned_data.get("street") and "street" not in self.errors:
            self.add_error("street", _("Enter a street or a post office box."))
        return cleaned


class DetailsForm(PersonDataForm):
    """The card "Angaben"."""

    field_names = ("salutation", "organisation_name", "first_name", "last_name", "address_line_1")
    groups = (persons.GROUP_DETAILS,)
    layout = [
        ("single", ["salutation"]),
        ("single", ["organisation_name"]),
        ("pair", ["first_name", "last_name"]),
        ("single", ["address_line_1"]),
    ]
    optional = ("salutation", "organisation_name", "first_name", "last_name", "address_line_1")


class ContactForm(PersonDataForm):
    """The card "Kontakt": phone and, for an active account, the second email. The login address cannot be changed (D-012)."""

    field_names = ("phone_number", "secondary_email")
    groups = (persons.GROUP_CONTACT,)
    layout = [("single", ["phone_number"]), ("single", ["secondary_email"])]
    optional = ("phone_number", "secondary_email")

    def __init__(self, *args, person=None, **kwargs):
        super().__init__(*args, person=person, **kwargs)
        # A new second address link works for an active account only (``persons.change_second_email``).
        if person is not None and person.status != User.Status.ACTIVE:
            del self.fields["secondary_email"]
            self.layout = [("single", ["phone_number"])]

    def clean(self):
        cleaned = super().clean()
        second = cleaned.get("secondary_email")
        if second and second == (self.instance.email or "").lower() and "secondary_email" not in self.errors:
            self.add_error("secondary_email", _("The second address must differ from the address for login."))
        return cleaned


class BillingAddressForm(PersonDataForm):
    """The card "Rechnungsadresse"."""

    field_names = ("street", "house_number", "post_office_box_number", "postal_code", "town", "country")
    groups = (persons.GROUP_ADDRESS,)
    layout = [
        ("street", ["street", "house_number"]),
        ("single", ["post_office_box_number"]),
        ("postal", ["postal_code", "town"]),
        ("single", ["country"]),
    ]
    optional = ("post_office_box_number",)


class SharesForm(forms.Form):
    """One change of the shares: the number (may be 0) that holds from a free day, also in the past (D-086)."""

    quantity = forms.IntegerField(
        label=_("Number of shares"), min_value=0, max_value=persons.SHARES_MAX,
        widget=forms.NumberInput(attrs={**CONTROL, "inputmode": "numeric"}),
    )
    valid_from = forms.DateField(
        label=_("Applies from"), initial=timezone.localdate,
        widget=forms.DateInput(attrs={**CONTROL, "type": "date"}, format="%Y-%m-%d"),
    )
    layout = [("single", ["quantity"]), ("single", ["valid_from"])]
    optional = ()
