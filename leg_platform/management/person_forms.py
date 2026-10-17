"""Forms of the person pages (D-081): the note, the BKW customer number and the shares of the cooperative.

The only things that the staff edit about a person (``personen/UMSETZUNG.md``, section 1); the rules are in
``leg_platform.community.persons``, which checks them again.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from leg_platform.community import persons

CONTROL = {"class": "form-control", "autocomplete": "off"}


class NoteForm(forms.Form):
    note = forms.CharField(
        label=_("Note"), required=False, max_length=persons.NOTE_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 6}),
    )
    layout = [("single", ["note"])]
    optional = ()


class BkwNumberForm(forms.Form):
    bkw_customer_number = forms.CharField(
        label=_("BKW customer number"), required=False, max_length=persons.BKW_NUMBER_MAX_LENGTH,
        widget=forms.TextInput(attrs=CONTROL),
    )
    layout = [("single", ["bkw_customer_number"])]
    optional = ("bkw_customer_number",)

    def clean_bkw_customer_number(self):
        value = " ".join(self.cleaned_data["bkw_customer_number"].split())
        if not value.isprintable():
            raise forms.ValidationError(_("This value is not valid."), code="invalid")
        return value


class SharesForm(forms.Form):
    """The number of shares with the day they start and the day they end (empty: open). A form without a
    number of shares removes the shares ("Keine Anteile erfasst."); ending shares is entering the last day."""

    shares = forms.IntegerField(
        label=_("Number of shares"), required=False, min_value=1, max_value=persons.SHARES_MAX,
        widget=forms.NumberInput(attrs={**CONTROL, "inputmode": "numeric"}),
    )
    shares_from = forms.DateField(
        label=_("From"), required=False,
        widget=forms.DateInput(attrs={**CONTROL, "type": "date"}, format="%Y-%m-%d"),
    )
    shares_to = forms.DateField(
        label=_("To"), required=False,
        widget=forms.DateInput(attrs={**CONTROL, "type": "date"}, format="%Y-%m-%d"),
    )
    layout = [("single", ["shares"]), ("pair", ["shares_from", "shares_to"])]
    optional = ("shares_to",)
    hint = _("Without a number of shares the shares are removed.")

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        shares, start, end = cleaned.get("shares"), cleaned.get("shares_from"), cleaned.get("shares_to")
        if shares is None:
            # No shares: the period is dropped with them.
            cleaned["shares_from"] = cleaned["shares_to"] = None
        elif start is None:
            self.add_error("shares_from", forms.ValidationError(_("This field is required."), code="required"))
        elif end is not None and end < start:
            self.add_error("shares_to", _("The end must not be before the start."))
        return cleaned

    @staticmethod
    def initial_for(record):
        if record is None:
            return {}
        return {"shares": record.shares, "shares_from": record.shares_from, "shares_to": record.shares_to}
