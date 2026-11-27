"""Forms of the location pages (D-078, D-094): the address, the transformer circuit (D-142) and the remark. The metering
point form is in ``metering_point_forms``, the forms of the circuits themselves in ``circuit_forms``."""

from django import forms
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.community.models import Location, validate_postal_code
from leg_platform.management import circuit_forms
from leg_platform.management.form_fields import CONTROL, ManagementForm, address_suggestions, remark_field
from leg_platform.management.metering_point_forms import suggest_attributes


class CircuitChoiceFields(ManagementForm):
    """The transformer circuit of a location (D-142): one searchable field (``combobox.js``, suggestions from
    ``management:circuits_search``) whose first choice is "Automatic (according to BKW)" (the circuit follows the
    building, ``transformer_circuits.derive``), then every stored circuit and every station of the address directory.
    Without the script the typed text decides (``circuit_forms.resolve_choice``). ``cleaned_data["transformer_circuit"]``
    is a stored circuit or None, ``circuit_station`` the number of a station that is not stored yet (the service creates
    its circuit in the same transaction). A new circuit is created only in the menu "Transformer circuits"."""

    transformer_circuit = forms.CharField(
        label=_("Transformer circuit"), required=False, max_length=200,
        widget=forms.TextInput(attrs={**CONTROL, "class": "form-control tnum"}),
    )

    circuit_layout = [("single", ["transformer_circuit"])]
    optional = ()

    def __init__(self, *args, location=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.circuit_station = None
        self.initial.setdefault("transformer_circuit", circuit_forms.current_text(location))
        url = reverse("management:circuits_search")
        if location is not None:
            url += f"?location={location.pk}"
        field = self.fields["transformer_circuit"]
        field.widget.attrs.update(suggest_attributes(url))
        field.widget.attrs.update({
            "data-suggest-minimum": "0",
            "data-detail-label": gettext("Source:"),
            "data-message-none": gettext("No transformer circuit found."),
        })
        field.help_text = _("Search by number or name. A new transformer circuit is created in the menu “Transformer circuits”.")

    def clean_transformer_circuit(self):
        circuit, self.circuit_station = circuit_forms.resolve_choice(self.cleaned_data["transformer_circuit"])
        return circuit


class AddressForm(ManagementForm):
    """Street, house number, address addition, postal code and town of a location."""

    street = forms.CharField(label=_("Street"), max_length=150, widget=forms.TextInput(attrs=CONTROL))
    house_number = forms.CharField(label=_("House number"), max_length=30, widget=forms.TextInput(attrs=CONTROL))
    address_addition = forms.CharField(
        label=_("Address addition"), max_length=150, required=False,
        widget=forms.TextInput(attrs={**CONTROL, "placeholder": _("e.g. rear building")}),
    )
    postal_code = forms.CharField(
        label=_("Postal code"), max_length=4, widget=forms.TextInput(attrs={**CONTROL, "inputmode": "numeric", "maxlength": "4"})
    )
    town = forms.CharField(label=_("Town"), max_length=100, widget=forms.TextInput(attrs=CONTROL))

    layout = [
        ("street", ["street", "house_number"]),
        ("single", ["address_addition"]),
        ("postal", ["postal_code", "town"]),
    ]
    optional = ("address_addition",)

    def __init__(self, *args, location=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.location = location  # being changed: its own address is no duplicate
        self.duplicate = None
        # The typing suggestions of the address directory; a location lies in Switzerland (D-111).
        address_suggestions(self, country="CH")

    def clean_postal_code(self):
        value = self.cleaned_data["postal_code"].strip()
        validate_postal_code(value)
        return value

    def clean(self):
        cleaned = super().clean()
        if all(name in cleaned for name in ("street", "house_number", "postal_code", "town")):
            found = Location.find_by_address(
                cleaned["street"], cleaned["house_number"], cleaned["postal_code"], cleaned["town"]
            )
            if found is not None and (self.location is None or found.pk != self.location.pk):
                self.duplicate = found
        return cleaned

    def address(self):
        return {name: self.cleaned_data[name] for name in ("street", "house_number", "postal_code", "town")}


class LocationCreateForm(CircuitChoiceFields, AddressForm):
    """"Create location": the address, the transformer circuit (D-142) and the remark."""

    remark = remark_field()

    layout = [*AddressForm.layout, *CircuitChoiceFields.circuit_layout, ("single", ["remark"])]
    optional = ("address_addition", "remark")

    def clean(self):
        cleaned = super().clean()
        if self.duplicate is not None:
            raise ValidationError(_("This address already exists."), code="duplicate_address")
        return cleaned


class LocationCircuitForm(CircuitChoiceFields):
    """"Choose transformer circuit" at a location: the same field as in the form "Create location"."""

    layout = list(CircuitChoiceFields.circuit_layout)

    def __init__(self, *args, location=None, **kwargs):
        super().__init__(*args, location=location, **kwargs)
        self.location = location


class AddressEditForm(AddressForm):
    """Change the address; ``confirm_merge`` and ``merge_target`` (id of the location shown) carry the merge
    confirmation, which the server compares again under the lock."""

    confirm_merge = forms.BooleanField(required=False, widget=forms.HiddenInput)
    merge_target = forms.CharField(required=False, max_length=12, widget=forms.HiddenInput)

    def target_id(self):
        value = self.cleaned_data.get("merge_target", "")
        return int(value) if value.isascii() and value.isdigit() else None


class RemarkForm(ManagementForm):
    remark = remark_field()
    layout = [("single", ["remark"])]
    optional = ("remark",)
