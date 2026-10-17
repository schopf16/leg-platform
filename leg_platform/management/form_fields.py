"""Building blocks shared by the forms of the management area (D-094).

* ``DecimalInputField``: a decimal number typed as text (digits and one decimal point or comma, at most two decimals,
  a unit after the field); the page script ``field-check.js`` checks it while typing with the same messages.
* ``CheckedInput``: the text widget that carries the data attributes of the script (``data-check``). The messages
  are translated when the field is drawn (a field object lives as long as the process, a language does not).
* ``SegmentedChoiceField``, ``remark_field``, ``date_field``, ``select_field``: one definition each, so that the same
  kind of field looks the same in every form.
* ``make_fact``: turns a field into a read-only fact row (a value the context gives: D-094, rule on locked fields).
  The field stays in the form and its place in ``layout``, but it is ``disabled``: Django then ignores whatever the
  browser posts and takes the initial value, so a forged POST cannot change it.

The server is always the authority; the script only makes the answer faster.
"""

import re
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.community import numbers
from leg_platform.community.models import REMARK_MAX_LENGTH

CONTROL = {"class": "form-control", "autocomplete": "off"}

REMARK_ROWS = 4

# Text that may be a decimal number: digits and at most one decimal point or comma (checked for a digit below).
DECIMAL_TEXT = re.compile(r"[0-9]*[.,]?[0-9]*")


class CheckedInput(forms.TextInput):
    """A text input checked while typing by ``field-check.js`` (``data-check``): ``kind`` is ``digits`` (the typed
    last digits of a metering point number) or ``decimal``."""

    def __init__(self, kind, attrs=None, **options):
        super().__init__(attrs)
        self.kind = kind
        self.options = options

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["attrs"].update(self.check_attributes())
        return context

    def check_attributes(self):
        if self.kind == "digits":
            limit = self.options["limit"]
            return {
                "data-check": "digits",
                "data-limit": limit,
                "data-message-digits": str(numbers.NOT_DIGITS_MESSAGE),
                # The script puts the count in place of the marker.
                "data-message-many": str(numbers.TOO_MANY_DIGITS_MESSAGE) % {"limit": limit, "count": "%(count)s"},
                "data-message-empty": str(numbers.NO_DIGITS_MESSAGE),
                "data-complete-label": str(_("Complete number:")),
            }
        return {
            "data-check": "decimal",
            "data-places": self.options["places"],
            "data-max": self.options["maximum"],
            "data-message-invalid": str(self.options["messages"]["invalid"]),
            "data-message-places": str(self.options["messages"]["places"]) % {"places": self.options["places"]},
            "data-message-range": str(self.options["messages"]["range"]) % {"max": self.options["maximum"]},
        }


class DecimalInputField(forms.DecimalField):
    """A non-negative decimal number typed as text, with the unit after the field (``unit``).

    Accepts digits and one decimal point or comma ("12,5" is 12.5), no sign, no letters, at most ``decimal_places``
    decimals and at most ``max_digits`` digits in all (the column of the model: 8 and 2, so up to 999999.99).
    Empty is None unless required."""

    default_error_messages = {
        "invalid": _("Enter a number, for example 12.5 (digits and one decimal point or comma)."),
        "places": _("At most %(places)s decimal places."),
        "range": _("The value must be between 0 and %(max)s."),
    }

    def __init__(self, *, unit="", max_digits=8, decimal_places=2, error_messages=None, **kwargs):
        kwargs.setdefault("required", False)
        self.unit = unit
        self.places = decimal_places
        # 999999.99 for 8 digits and 2 decimals.
        step = Decimal(1).scaleb(-decimal_places)
        self.maximum = Decimal(10) ** (max_digits - decimal_places) - step
        messages = {**self.default_error_messages, **(error_messages or {})}
        kwargs["widget"] = CheckedInput(
            "decimal",
            attrs={**CONTROL, "class": "form-control tnum", "inputmode": "decimal", **kwargs.pop("attrs", {})},
            places=decimal_places, maximum=self.maximum, messages=messages,
        )
        super().__init__(error_messages=error_messages, **kwargs)

    def to_python(self, value):
        text = value.strip() if isinstance(value, str) else value
        if text in self.empty_values:
            return None
        text = str(text)
        if DECIMAL_TEXT.fullmatch(text) is None or not any(character.isdigit() for character in text):
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        text = text.replace(",", ".")
        if len(text.partition(".")[2]) > self.places:
            raise ValidationError(self.error_messages["places"], code="places", params={"places": self.places})
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValidationError(self.error_messages["invalid"], code="invalid") from None
        if number > self.maximum:
            raise ValidationError(self.error_messages["range"], code="range", params={"max": self.maximum})
        return number


class SegmentedChoiceField(forms.ChoiceField):
    """A choice of a few options drawn as a segmented control (the direction of a metering point)."""

    segmented = True


class NotedSelect(forms.Select):
    """A drop-down whose options can carry a note (``notes``: option value -> text, set per form instance). The page
    script ``option-note.js`` shows the note of the chosen option in a polite live line under the field
    (``data-option-note`` names it), e.g. the consequence of a move (UX-005). The server stays the authority: a note
    is a hint and never a rule."""

    def __init__(self, attrs=None, choices=()):
        super().__init__(attrs, choices)
        self.notes = {}

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        if self.notes:
            widget_attrs = context["widget"]["attrs"]
            widget_attrs["data-option-note"] = f"{widget_attrs.get('id', 'id_' + name)}_note"
        return context

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        note = self.notes.get(str(getattr(value, "value", value)))
        if note:
            option["attrs"]["data-note"] = note
        return option


def select_field(label, queryset, empty_label, *, required=False):
    """A drop-down of existing objects with a real empty option that says what it means (D-094)."""
    return forms.ModelChoiceField(
        label=label, queryset=queryset, required=required, empty_label=empty_label,
        widget=NotedSelect(attrs={"class": "form-select"}),
    )


def remark_field(label=None):
    """The free-text remark: the same size and the same marking everywhere (list it in ``optional``)."""
    return forms.CharField(
        label=label or _("Remark"), required=False, max_length=REMARK_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": REMARK_ROWS}),
    )


def date_field(label, *, required=True, initial=None):
    """A browser date field; ``initial="today"`` defaults to today."""
    return forms.DateField(
        label=label, required=required,
        initial=timezone.localdate if initial == "today" else initial,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control tnum"}, format="%Y-%m-%d"),
    )


def make_fact(form, name, text, *, link=None, initial=None):
    """Show a field of the form as a read-only row ``text`` (``link`` = ``(url, label)``) and keep its value away from
    the browser: the field is disabled, so the posted value is ignored and ``initial`` (the value the context gave)
    is used. It keeps its place in ``form.layout``."""
    field = form.fields[name]
    field.disabled = True
    field.required = False
    field.fact = text
    field.fact_link = link
    if initial is not None:
        form.initial[name] = initial


__all__ = [
    "CONTROL", "CheckedInput", "DecimalInputField", "NotedSelect", "REMARK_ROWS", "SegmentedChoiceField", "date_field",
    "make_fact", "remark_field", "select_field",
]
