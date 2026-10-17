"""Forms of the management area."""

from django import forms
from django.utils.translation import gettext_lazy as _

from leg_platform.community.models import ManagementSettings
from leg_platform.community.settings_service import EDITABLE_FIELDS

ADDRESS_FIELDS = ("sender_street", "sender_house_number", "sender_postal_code", "sender_town")


class ManagementSettingsForm(forms.ModelForm):
    """Settings page form. The address addition is optional; country and identifier are required and upper-cased."""

    class Meta:
        model = ManagementSettings
        fields = EDITABLE_FIELDS
        labels = {"sender_postal_code": _("Postal code")}
        widgets = {
            "sender_street": forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
            "sender_house_number": forms.TextInput(
                attrs={"class": "form-control mgmt-field-short", "autocomplete": "off"}
            ),
            "sender_address_addition": forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
            "sender_postal_code": forms.TextInput(
                attrs={
                    "class": "form-control mgmt-field-short tnum",
                    "autocomplete": "off",
                    "inputmode": "numeric",
                    "maxlength": "4",
                }
            ),
            "sender_town": forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
            "country_code": forms.TextInput(
                attrs={"class": "form-control mgmt-field-short tnum", "autocomplete": "off", "maxlength": "2"}
            ),
            "identifier": forms.TextInput(
                attrs={"class": "form-control tnum", "autocomplete": "off", "maxlength": "11"}
            ),
            "warning_threshold_percent": forms.NumberInput(
                attrs={"class": "form-control mgmt-field-short tnum", "min": "1", "max": "100"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in EDITABLE_FIELDS:
            self.fields[name].required = name != "sender_address_addition"

    def clean_sender_postal_code(self):
        from leg_platform.community.models import validate_postal_code

        value = self.cleaned_data["sender_postal_code"].strip()
        validate_postal_code(value)
        return value

    def clean_country_code(self):
        return self.cleaned_data["country_code"].strip().upper()

    def clean_identifier(self):
        return self.cleaned_data["identifier"].strip().upper()

    def sender_fields_are_valid(self):
        """Whether the four fields read by the directory check have no error."""
        return not any(name in self.errors for name in ADDRESS_FIELDS)

    def settings_values(self):
        """The cleaned values of all editable fields (after ``is_valid``)."""
        return {name: self.cleaned_data[name] for name in EDITABLE_FIELDS}
