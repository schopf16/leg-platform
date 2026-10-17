"""Forms of the LEG pages (D-080): create, edit quota, edit master data."""

from decimal import Decimal

from django import forms
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.community import leg_services
from leg_platform.community.models import REMARK_MAX_LENGTH

CONTROL = {"class": "form-control", "autocomplete": "off"}


class PercentField(forms.DecimalField):
    """A percentage with at most two decimals; a decimal comma is accepted. The template draws ``unit`` after the field."""

    unit = "%"

    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        return super().to_python(value)


class QuotaFields(forms.Form):
    """The quota and its date, saved together: a quota needs a date, no quota drops the date, the date is not
    in the future (the page script defaults it to today; the server only requires it)."""

    quota = PercentField(
        label=_("Production quota"), required=False, max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal(0)), MaxValueValidator(Decimal(100))],
        widget=forms.TextInput(
            attrs={**CONTROL, "inputmode": "decimal", "data-quota-input": "", "placeholder": _("e.g. 37.6")}
        ),
    )
    checked_on = forms.DateField(
        label=_("Checked on"), required=False,
        widget=forms.DateInput(attrs={**CONTROL, "type": "date", "data-quota-date": ""}, format="%Y-%m-%d"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.today = timezone.localdate()
        self.fields["quota"].widget.attrs["data-today"] = self.today.isoformat()

    def clean(self):
        cleaned = super().clean()
        if "quota" in self.errors or "checked_on" in self.errors:
            return cleaned
        quota, checked_on = cleaned.get("quota"), cleaned.get("checked_on")
        if quota is None:
            cleaned["checked_on"] = None
        elif checked_on is None:
            self.add_error("checked_on", forms.ValidationError(_("This field is required."), code="required"))
        elif checked_on > self.today:
            self.add_error("checked_on", _("The date must not be in the future."))
        return cleaned


def _name_field():
    return forms.CharField(label=_("Name"), max_length=120, widget=forms.TextInput(attrs=CONTROL))


def _remark_field():
    return forms.CharField(
        label=_("Remark"), required=False, max_length=REMARK_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )


class NameMixin:
    """Rejects a name that is already in use."""

    def __init__(self, *args, leg=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.leg = leg

    def clean_name(self):
        name = leg_services.normalise_name(self.cleaned_data["name"])
        if not name:
            raise forms.ValidationError(_("This field is required."), code="required")
        if leg_services.name_in_use(name, exclude_pk=self.leg.pk if self.leg else None):
            raise forms.ValidationError(_("This name is already in use."), code="duplicate_name")
        return name


class LegCreateForm(NameMixin, QuotaFields):
    name = _name_field()
    remark = _remark_field()
    field_order = ("name", "quota", "checked_on", "remark")

    layout = [("single", ["name"]), ("quota", ["quota", "checked_on"]), ("single", ["remark"])]
    optional = ("quota",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs["placeholder"] = _("e.g. LEG Musterquartier")


class QuotaForm(QuotaFields):
    layout = [("quota", ["quota", "checked_on"])]
    optional = ("quota",)


class MasterDataForm(NameMixin, forms.Form):
    name = _name_field()
    remark = _remark_field()
    layout = [("single", ["name"]), ("single", ["remark"])]
    optional = ()
