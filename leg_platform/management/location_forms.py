"""Forms of the location pages (D-078): the address, the remark, the transformer circuit and the
first metering point of a location."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator, numbers, transformer_circuits
from leg_platform.community.models import (
    REMARK_MAX_LENGTH,
    Direction,
    Leg,
    Location,
    ManagementSettings,
    validate_postal_code,
)

CONTROL = {"class": "form-control", "autocomplete": "off"}
SHORT = {"class": "form-control", "autocomplete": "off"}


class CircuitChoiceField(forms.CharField):
    """A field in which a transformer circuit is chosen by its number: the text of a suggestion
    ("TRA21359 · Ittigen Nord") or just the number. Empty means none. The result is the circuit."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("label", _("Transformer circuit"))
        kwargs.setdefault(
            "widget",
            forms.TextInput(attrs={**CONTROL, "list": "circuit-options", "class": "form-control tnum"}),
        )
        super().__init__(**kwargs)

    def clean(self, value):
        value = super().clean(value)
        number = transformer_circuits.number_of_choice(value)
        if not number:
            return None
        circuit = transformer_circuits.find(number)
        if circuit is None:
            raise ValidationError(
                _("Choose a transformer circuit from the list or create a new one."), code="unknown_circuit"
            )
        return circuit


class AddressForm(forms.Form):
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
        # The location that is being changed (its own address is no duplicate).
        self.location = location
        self.duplicate = None

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


class LocationCreateForm(AddressForm):
    transformer_circuit = CircuitChoiceField()
    remark = forms.CharField(
        label=_("Remark"), required=False, max_length=REMARK_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )

    layout = [
        *AddressForm.layout,
        ("single", ["transformer_circuit"]),
        ("single", ["remark"]),
    ]
    optional = ("address_addition", "transformer_circuit")

    def clean(self):
        cleaned = super().clean()
        if self.duplicate is not None:
            # Saving is not possible while the address exists; the page shows the hint.
            raise ValidationError(_("This address already exists."), code="duplicate_address")
        return cleaned


class AddressEditForm(AddressForm):
    """Change the address of a location. The page asks for a confirmation when the new address is the
    address of another location (the merge); ``confirm_merge`` carries the answer and ``merge_target`` the id of
    the location that was shown (the server compares it again under the lock)."""

    confirm_merge = forms.BooleanField(required=False, widget=forms.HiddenInput)
    merge_target = forms.CharField(required=False, max_length=12, widget=forms.HiddenInput)

    def target_id(self):
        value = self.cleaned_data.get("merge_target", "")
        return int(value) if value.isascii() and value.isdigit() else None


class RemarkForm(forms.Form):
    remark = forms.CharField(
        label=_("Remark"), required=False, max_length=REMARK_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )
    layout = [("single", ["remark"])]
    optional = ()


class CircuitForm(forms.Form):
    """Create or change a transformer circuit: the grid number (TRA and digits) and a free name."""

    grid_number = forms.CharField(
        max_length=numbers.TRANSFORMER_CIRCUIT_MAX_LENGTH,
        widget=forms.TextInput(attrs={**CONTROL, "class": "form-control tnum"}),
    )
    name = forms.CharField(label=_("Name"), max_length=120, required=False, widget=forms.TextInput(attrs=CONTROL))
    layout = [("single", ["grid_number"]), ("single", ["name"])]
    optional = ()

    def __init__(self, *args, circuit=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.circuit = circuit
        self.existing = None
        # The label names the grid operator of the instance (D-086).
        self.fields["grid_number"].label = grid_operator.text(_("%(operator)s number"))

    def clean_grid_number(self):
        value = numbers.normalise_transformer_circuit_number(self.cleaned_data["grid_number"])
        numbers.validate_transformer_circuit_number(value)
        found = transformer_circuits.find(value)
        if found is not None and (self.circuit is None or found.pk != self.circuit.pk):
            self.existing = found
        return value


class MeteringPointForm(forms.Form):
    """A metering point at a location: the direction is required; the number (only its last digits
    are typed, the country and the identifier come from the settings) and the LEG are optional."""

    direction = forms.ChoiceField(
        label=_("Direction"), choices=Direction.choices, initial=Direction.CONSUMPTION,
        widget=forms.RadioSelect,
    )
    number_digits = forms.CharField(
        label=_("Metering point number"), required=False, max_length=numbers.SUFFIX_LENGTH,
        widget=forms.TextInput(attrs={"class": "form-control tnum", "autocomplete": "off", "inputmode": "numeric"}),
    )
    leg = forms.ModelChoiceField(
        label=_("LEG"), queryset=Leg.objects.none(), required=False, empty_label=_("No LEG chosen yet"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    optional = ("number_digits", "leg")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prefix_text = ManagementSettings.load().metering_point_prefix
        self.number = None
        self.fields["leg"].queryset = Leg.objects.filter(is_archived=False).order_by("name")

    def clean_number_digits(self):
        digits = self.cleaned_data["number_digits"].strip()
        if not digits:
            return ""
        settings_row = ManagementSettings.load()
        try:
            self.number = numbers.compose_metering_point_number(
                settings_row.country_code, settings_row.identifier, digits
            )
        except ValidationError as error:
            if not settings_row.identifier:
                raise ValidationError(
                    _("Enter the country and the identifier in the settings first."), code="no_identifier"
                ) from error
            raise
        return digits
