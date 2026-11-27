"""Forms and texts of the transformer circuits (D-142): ``CircuitForm`` creates and renames a circuit (one form for
both, D-097); ``resolve_choice`` and ``suggestions`` serve the circuit field of the location forms
(``location_forms.CircuitChoiceFields``) and its suggestion list (``circuit_views.CircuitSearchView``)."""

import re

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator, numbers, transformer_circuits
from leg_platform.community.models import TransformerCircuit
from leg_platform.management.form_fields import CONTROL, ManagementForm, make_fact

MAX_SUGGESTIONS = 10
MAX_WORDS = 5
LEADING_NUMBER = re.compile(r"(TRA[0-9]+)\b")


def automatic_text(number=""):
    """The choice "Automatic (according to BKW)" of a location, with the derived number when it is known."""
    base = gettext("Automatic (according to %(operator)s)") % {"operator": grid_operator.name()}
    return f"{base}: {number}" if number else base


def source_text(source):
    """The source of a circuit as a word: the operator's name, "manual" or "unknown"."""
    if source == transformer_circuits.SOURCE_OPERATOR:
        return grid_operator.name()
    if source == transformer_circuits.SOURCE_MANUAL:
        return gettext("manual")
    return gettext("unknown")


def derived_number(location):
    """The number the automatic choice gives a location: its stored circuit while it is not chosen by hand, else the
    station of its building ("" when none is known)."""
    if location is None:
        return ""
    if not location.circuit_manual:
        return location.transformer_circuit.grid_number if location.transformer_circuit_id else ""
    return transformer_circuits.station_number(location.egid) or ""


def current_text(location):
    """What the circuit field of a location shows at the start: the hand choice, else the automatic choice."""
    if location is not None and location.circuit_manual and location.transformer_circuit_id:
        return transformer_circuits.label(location.transformer_circuit)
    return automatic_text(derived_number(location))


def _is_automatic(text):
    return not text or text.casefold().startswith(automatic_text().casefold())


UNKNOWN_MESSAGE = _("This transformer circuit does not exist. Create it in the menu “Transformer circuits”.")


def resolve_choice(text):
    """``(circuit, station)`` of the typed or chosen text: ``(None, None)`` for the automatic choice (an empty field or
    the text "Automatic ..."), a stored circuit, or the grid number of a station of the address directory that is not
    stored yet. The number at the start of the text decides (the suggestions start with it); a text without a number
    must be the name of exactly one circuit. Raises ``ValidationError`` otherwise."""
    text = (text or "").strip()
    if _is_automatic(text):
        return None, None
    match = LEADING_NUMBER.match(text.upper())
    if match is None:
        named = list(TransformerCircuit.objects.filter(name__iexact=text)[:2])
        if len(named) == 1:
            return named[0], None
        raise ValidationError(UNKNOWN_MESSAGE, code="unknown_circuit")
    number = match.group(1)
    found = transformer_circuits.find(number)
    if found is not None:
        return found, None
    if number in (transformer_circuits.directory_numbers() or ()):
        return None, number
    raise ValidationError(UNKNOWN_MESSAGE, code="unknown_circuit")


def suggestions(text, location=None):
    """At most ``MAX_SUGGESTIONS`` texts for the circuit field: the automatic choice first, then every stored circuit and
    every station of the address directory ("TRA21359 · Name · source"), those whose number starts with the typed text
    first. Every word must occur in the text. An empty text (or the automatic text itself) lists from the start."""
    text = (text or "").strip()[:100]
    words = [] if _is_automatic(text) else text.casefold().split()[:MAX_WORDS]
    stations = transformer_circuits.directory_numbers()
    stored = {circuit.grid_number: circuit for circuit in TransformerCircuit.objects.all()}
    entries = []
    for number in sorted(set(stored) | (stations or set()), key=transformer_circuits.number_key):
        shown = transformer_circuits.label(stored[number]) if number in stored else number
        source = transformer_circuits.source(number, stations)
        entries.append((number, f"{shown}{transformer_circuits.SEPARATOR}{source_text(source)}"))
    matching = [(number, entry) for number, entry in entries if all(word in entry.casefold() for word in words)]
    if words:
        start = words[0].upper()
        matching.sort(key=lambda pair: not pair[0].startswith(start))  # stable: the number order stays inside
    automatic = automatic_text(derived_number(location))
    results = [automatic] if all(word in automatic.casefold() for word in words) else []
    return results + [entry for _number, entry in matching][: MAX_SUGGESTIONS - len(results)]


class CircuitForm(ManagementForm):
    """Create a transformer circuit (number and optional name) or rename one. ``number_locked``: the number of a circuit
    whose source is the operator (or unknown) is a locked row, only the name changes (D-142)."""

    grid_number = forms.CharField(
        max_length=numbers.TRANSFORMER_CIRCUIT_MAX_LENGTH,
        widget=forms.TextInput(attrs={**CONTROL, "class": "form-control tnum"}),
    )
    name = forms.CharField(label=_("Name"), max_length=120, required=False, widget=forms.TextInput(attrs=CONTROL))
    layout = [("single", ["grid_number"]), ("single", ["name"])]
    optional = ("name",)

    def __init__(self, *args, circuit=None, number_locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.circuit = circuit
        self.existing = None
        self.fields["grid_number"].label = grid_operator.text(_("%(operator)s number"))  # D-086
        self.fields["grid_number"].help_text = _("TRA followed by digits, for example TRA21359.")
        if circuit is not None and number_locked:
            make_fact(self, "grid_number", circuit.grid_number, initial=circuit.grid_number)

    def clean_grid_number(self):
        value = numbers.normalise_transformer_circuit_number(self.cleaned_data["grid_number"])
        numbers.validate_transformer_circuit_number(value)
        found = transformer_circuits.find(value)
        if found is not None and (self.circuit is None or found.pk != self.circuit.pk):
            self.existing = found
        return value
