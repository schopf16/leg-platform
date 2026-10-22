"""Forms of the LEG pages (D-080): create, edit quota, edit master data."""

from decimal import Decimal

from django import forms
from django.core.validators import MinValueValidator
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator, leg_services, numbers
from leg_platform.community.models import Direction, LegAssignment, MeteringPoint
from leg_platform.management.form_fields import DecimalInputField, date_field, remark_field

CONTROL = {"class": "form-control", "autocomplete": "off"}


class PercentField(DecimalInputField):
    """A percentage up to 999.99 with at most two decimals (D-089: no upper limit of 100); a decimal comma is
    accepted. The template draws ``unit`` after the field."""

    def __init__(self, **kwargs):
        kwargs.setdefault("unit", "%")
        kwargs.setdefault("max_digits", 5)
        kwargs.setdefault("error_messages", {"range": _("Enter a quota between 0 and %(max)s %%.")})
        super().__init__(**kwargs)


class QuotaFields(forms.Form):
    """The quota and its date, saved together: a quota needs a date, no quota drops the date, the date is not
    in the future (the page script defaults it to today; the server only requires it)."""

    quota = PercentField(
        label=_("Production quota"),
        validators=[MinValueValidator(Decimal(0))],
        attrs={"data-quota-input": "", "placeholder": _("e.g. 37.6")},
    )
    checked_on = forms.DateField(
        label=_("Checked on"), required=False,
        widget=forms.DateInput(attrs={**CONTROL, "type": "date", "data-quota-date": ""}, format="%Y-%m-%d"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.today = timezone.localdate()
        self.fields["quota"].widget.attrs["data-today"] = self.today.isoformat()
        self.fields["quota"].help_text = grid_operator.text(
            _("As shown in the %(operator)s LEG portal. It can also be above 100 %%.")
        )
        self.fields["checked_on"].help_text = _("Required when a quota is entered.")

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
    return forms.CharField(
        label=_("Name"), max_length=120,
        widget=forms.TextInput(attrs={**CONTROL, "placeholder": _("e.g. LEG Musterquartier")}),
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
    remark = remark_field()
    field_order = ("name", "quota", "checked_on", "remark")

    layout = [("single", ["name"]), ("quota", ["quota", "checked_on"]), ("single", ["remark"])]
    optional = ("quota", "remark")


class QuotaForm(QuotaFields):
    layout = [("quota", ["quota", "checked_on"])]
    optional = ("quota",)


class MasterDataForm(NameMixin, forms.Form):
    name = _name_field()
    remark = remark_field()
    layout = [("single", ["name"]), ("single", ["remark"])]
    optional = ("remark",)


class LegPointForm(forms.Form):
    """"Assign metering point" on the page of a LEG (D-095): one drop-down of the metering points that have no LEG (also
    none planned) and the day it applies from. A metering point of another LEG is moved with "Change LEG", which
    knows the related points. The choices are built from the database for every request; the service checks again
    under the locks."""

    metering_point = forms.ChoiceField(label=_("Metering point"), widget=forms.Select(attrs={"class": "form-select tnum"}))
    valid_from = date_field(_("Applies from"), initial="today")
    layout = [("single", ["metering_point"]), ("single", ["valid_from"])]
    optional = ()
    hint = _("From this day the metering point belongs to this LEG. This can change the discount tier of the LEG.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        has_leg = LegAssignment.objects.not_ended(today).filter(metering_point=OuterRef("pk"))
        points = (
            MeteringPoint.objects.filter(is_archived=False).exclude(Exists(has_leg))
            .select_related("location").order_by("number", "pk")
        )
        self.points = {str(point.pk): point for point in points}
        choices = []
        for key, point in self.points.items():
            parts = [numbers.format_metering_point_number(point.number) or "–", str(Direction(point.direction).label)]
            # The same words as in the location dialog: a point without a location says so (UX-011).
            parts.append(str(point.location) if point.location_id else gettext("no location"))
            choices.append((key, " · ".join(parts)))
        self.fields["metering_point"].choices = [("", gettext("No metering point chosen yet")), *choices]
        self.empty = not self.points

    def clean_metering_point(self):
        return self.points[self.cleaned_data["metering_point"]]
