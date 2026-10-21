"""Forms of the location pages (D-078, D-094): the address, the transformer circuit (chosen from a list, a new one
entered in the same form) and the remark. The metering point form is in ``metering_point_forms``."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator, numbers, transformer_circuits
from leg_platform.community.models import Location, validate_postal_code
from leg_platform.management.form_fields import CONTROL, address_suggestions, remark_field

NEW_CIRCUIT = "__new__"  # the value of the last option of the circuit list: "+ Create new transformer circuit"


class CircuitFields(forms.Form):
    """The transformer circuit of a location: a drop-down of the existing circuits whose last option creates a new one
    in the same form (D-095). The two fields of the new circuit are always in the page; the page script shows them
    only for that option, and the server decides from what is posted: a chosen circuit with a number typed is refused,
    a number without a choice creates a circuit. ``cleaned_data["transformer_circuit"]`` is the circuit or None,
    ``new_circuit`` the pair ``(grid number, name)`` or None; the service creates the circuit and saves it with the
    location in one transaction."""

    transformer_circuit = forms.ChoiceField(
        label=_("Transformer circuit"), required=False, widget=forms.Select(attrs={"class": "form-select tnum"})
    )
    new_circuit_number = forms.CharField(
        required=False, max_length=numbers.TRANSFORMER_CIRCUIT_MAX_LENGTH,
        widget=forms.TextInput(attrs={**CONTROL, "class": "form-control tnum"}),
    )
    new_circuit_name = forms.CharField(
        label=_("Name"), required=False, max_length=120, widget=forms.TextInput(attrs=CONTROL)
    )

    circuit_layout = [("single", ["transformer_circuit"]), ("circuit-new", ["new_circuit_number", "new_circuit_name"])]
    circuit_optional = ("transformer_circuit", "new_circuit_name")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.new_circuit = None
        self.circuits = {str(circuit.pk): circuit for circuit in transformer_circuits.choices()}
        self.fields["transformer_circuit"].choices = [
            ("", _("No transformer circuit")),
            *((key, transformer_circuits.label(circuit)) for key, circuit in self.circuits.items()),
            (NEW_CIRCUIT, _("+ Create new transformer circuit…")),
        ]
        number = self.fields["new_circuit_number"]
        number.label = grid_operator.text(_("%(operator)s number"))
        number.help_text = _("TRA followed by digits, for example TRA21359.")

    def clean(self):
        cleaned = super().clean()
        choice = cleaned.get("transformer_circuit", "")
        number = numbers.normalise_transformer_circuit_number(cleaned.get("new_circuit_number"))
        name = (cleaned.get("new_circuit_name") or "").strip()
        if "transformer_circuit" in self.errors or "new_circuit_number" in self.errors:
            return cleaned
        if choice and choice != NEW_CIRCUIT:
            cleaned["transformer_circuit"] = self.circuits[choice]
            if number or name:
                self.add_error(
                    "new_circuit_number",
                    _("Choose an existing transformer circuit or enter a new one, not both."),
                )
            return cleaned
        cleaned["transformer_circuit"] = None
        if choice == NEW_CIRCUIT or number or name:
            try:
                if not number:
                    raise ValidationError(
                        _("Enter the %(operator)s number."), code="required", params={"operator": grid_operator.name()}
                    )
                numbers.validate_transformer_circuit_number(number)
            except ValidationError as error:
                self.add_error("new_circuit_number", error)
                return cleaned
            if transformer_circuits.find(number) is not None:
                self.add_error(
                    "new_circuit_number", _("This transformer circuit already exists. Choose it from the list.")
                )
                return cleaned
            self.new_circuit = (number, name)
        return cleaned

    def circuit_errors(self, error):
        """Show the messages of a service ``ValidationError`` for the new circuit at its fields."""
        names = {"grid_number": "new_circuit_number", "name": "new_circuit_name"}
        for key, messages in error.message_dict.items():
            self.add_error(names.get(key), messages)


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


class LocationCreateForm(CircuitFields, AddressForm):
    """"Create location": the address, the transformer circuit (existing or new, D-095) and the remark."""

    remark = remark_field()

    layout = [*AddressForm.layout, *CircuitFields.circuit_layout, ("single", ["remark"])]
    optional = ("address_addition", *CircuitFields.circuit_optional, "remark")

    def clean(self):
        cleaned = super().clean()
        if self.duplicate is not None:
            raise ValidationError(_("This address already exists."), code="duplicate_address")
        return cleaned


class LocationCircuitForm(CircuitFields):
    """"Change transformer circuit" at a location: the same fields as in the form "Create location"."""

    layout = list(CircuitFields.circuit_layout)
    optional = CircuitFields.circuit_optional

    def __init__(self, *args, location=None, **kwargs):
        initial = kwargs.setdefault("initial", {})
        if location is not None and location.transformer_circuit_id:
            initial.setdefault("transformer_circuit", str(location.transformer_circuit_id))
        super().__init__(*args, **kwargs)
        self.location = location


class AddressEditForm(AddressForm):
    """Change the address; ``confirm_merge`` and ``merge_target`` (id of the location shown) carry the merge
    confirmation, which the server compares again under the lock."""

    confirm_merge = forms.BooleanField(required=False, widget=forms.HiddenInput)
    merge_target = forms.CharField(required=False, max_length=12, widget=forms.HiddenInput)

    def target_id(self):
        value = self.cleaned_data.get("merge_target", "")
        return int(value) if value.isascii() and value.isdigit() else None


class RemarkForm(forms.Form):
    remark = remark_field()
    layout = [("single", ["remark"])]
    optional = ("remark",)


class CircuitForm(forms.Form):
    """Change a transformer circuit (grid number and free name); the new-circuit fields of the location forms use the
    same rules."""

    grid_number = forms.CharField(
        max_length=numbers.TRANSFORMER_CIRCUIT_MAX_LENGTH,
        widget=forms.TextInput(attrs={**CONTROL, "class": "form-control tnum"}),
    )
    name = forms.CharField(label=_("Name"), max_length=120, required=False, widget=forms.TextInput(attrs=CONTROL))
    layout = [("single", ["grid_number"]), ("single", ["name"])]
    optional = ("name",)

    def __init__(self, *args, circuit=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.circuit = circuit
        self.existing = None
        self.fields["grid_number"].label = grid_operator.text(_("%(operator)s number"))  # D-086
        self.fields["grid_number"].help_text = _("TRA followed by digits, for example TRA21359.")

    def clean_grid_number(self):
        value = numbers.normalise_transformer_circuit_number(self.cleaned_data["grid_number"])
        numbers.validate_transformer_circuit_number(value)
        found = transformer_circuits.find(value)
        if found is not None and (self.circuit is None or found.pk != self.circuit.pk):
            self.existing = found
        return value
