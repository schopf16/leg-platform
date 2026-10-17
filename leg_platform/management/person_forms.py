"""Forms of the person pages (D-081): the note, the customer number at the grid operator and the change of the
shares of the cooperative (D-086).

The only things that the staff edit about a person (``personen/UMSETZUNG.md``, section 1); the rules are in
``leg_platform.community.persons``, which checks them again.
"""

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator, persons

CONTROL = {"class": "form-control", "autocomplete": "off"}


class NoteForm(forms.Form):
    note = forms.CharField(
        label=_("Note"), required=False, max_length=persons.NOTE_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 6}),
    )
    layout = [("single", ["note"])]
    optional = ()


class OperatorCustomerNumberForm(forms.Form):
    operator_customer_number = forms.CharField(
        required=False, max_length=persons.OPERATOR_NUMBER_MAX_LENGTH, widget=forms.TextInput(attrs=CONTROL),
    )
    layout = [("single", ["operator_customer_number"])]
    optional = ("operator_customer_number",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The label names the grid operator of the instance (D-086).
        self.fields["operator_customer_number"].label = grid_operator.text(_("%(operator)s customer number"))

    def clean_operator_customer_number(self):
        value = " ".join(self.cleaned_data["operator_customer_number"].split())
        if not value.isprintable():
            raise forms.ValidationError(_("This value is not valid."), code="invalid")
        return value


class SharesForm(forms.Form):
    """One change of the shares: the number that holds from a day on (D-086). The number may be 0 (no shares
    any more); the day is free, also in the past."""

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
