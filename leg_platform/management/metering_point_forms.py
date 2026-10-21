"""Forms of the metering point pages. A form only reads and checks input; the rules are in
``leg_platform.community``. Dialogs are laid out by ``layout`` (``(row class, [field names])``; ``("divider", text)``
draws a rule with a label), ``optional`` (fields marked "(optional)") and ``hint`` (one note), as ``dialog_form.html``
expects. One form per subject, in every place it is used (D-094): ``MeteringPointForm`` creates and changes a metering
point; a context only pre-fills and locks a field."""

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.community import assignments, leg_switch, numbers, person_search
from leg_platform.community.metering_points import is_month_end
from leg_platform.community.models import (
    Direction,
    Leg,
    Location,
    ManagementSettings,
    MeteringPoint,
)
from leg_platform.management.form_fields import (
    CheckedInput,
    DecimalInputField,
    NotedSelect,
    SegmentedChoiceField,
    date_field,
    make_fact,
    remark_field,
    select_field,
)

NUMBER_ATTRS = {"class": "form-control tnum", "autocomplete": "off", "autocapitalize": "characters", "spellcheck": "false"}


def location_queryset(*include):
    """The locations that can be chosen: not archived (and the ones in ``include``, which keep their place)."""
    return Location.objects.filter(Q(is_archived=False) | Q(pk__in=[pk for pk in include if pk]))


def circuit_text(circuit):
    """The transformer circuit for a sentence: its number, or "no transformer circuit"."""
    return circuit.grid_number if circuit is not None else gettext("no transformer circuit")


def move_note(old, new):
    """The consequence of a move that changes the transformer circuit (UX-005)."""
    return gettext(
        "The transformer circuit changes: %(old)s → %(new)s. This can change the discount tier of the LEG."
    ) % {"old": old, "new": new}


class MeteringPointForm(forms.Form):
    """Create or change a metering point (the one form of this subject).

    Fields in order: location, number, direction, then "Optional": LEG, PV power, battery, wallbox, remark. The number
    is the last digits only: country and identifier come from the settings and stand in front, shorter entries are
    filled with zeros on the left (D-093). Contexts (D-094): ``location`` (the page of a location) fills in the
    location and locks it; ``point`` (the page of a metering point) is the change mode: number, direction and LEG are
    shown as facts (the LEG has its own dialog with a start date), location, plant data and remark can be changed. A
    locked or fact field is disabled: the server takes its value from the page's object, never from the POST."""

    location = select_field(_("Location"), Location.objects.none(), _("No location chosen yet"))
    number_digits = forms.CharField(
        label=_("Metering point number"),
        error_messages={"required": numbers.NO_DIGITS_MESSAGE},
        help_text=_(
            "The last 20 characters: capital letters A–Z, digits 0–9 and the hyphen (small letters become capitals). "
            "Shorter entries are filled with zeros on the left (23 becomes …0023)."
        ),
        widget=CheckedInput("digits", attrs=NUMBER_ATTRS, limit=numbers.SUFFIX_LENGTH),
    )
    direction = SegmentedChoiceField(
        label=_("Direction"), choices=Direction.choices, initial=Direction.CONSUMPTION, widget=forms.RadioSelect,
    )
    leg = select_field(_("LEG"), Leg.objects.none(), _("No LEG chosen yet"))
    pv_power_kwp = DecimalInputField(label=_("PV power"), unit="kWp")
    battery_kwh = DecimalInputField(label=_("Battery"), unit="kWh")
    wallbox_kw = DecimalInputField(label=_("Wallbox"), unit="kW")
    remark = remark_field()

    layout = [
        ("single", ["location"]),
        ("single", ["number_digits"]),
        ("single", ["direction"]),
        ("divider", _("Optional")),
        ("single", ["leg"]),
        ("triple", ["pv_power_kwp", "battery_kwh", "wallbox_kw"]),
        ("single", ["remark"]),
    ]
    optional = ("location", "leg", "pv_power_kwp", "battery_kwh", "wallbox_kw", "remark")
    hint = ""

    def __init__(self, data=None, *, location=None, point=None, **kwargs):
        if data is not None:
            data = self.padded(data)
        initial = kwargs.pop("initial", None) or {}
        if point is not None:
            initial = {
                "location": point.location_id, "pv_power_kwp": point.pv_power_kwp, "battery_kwh": point.battery_kwh,
                "wallbox_kw": point.wallbox_kw, "remark": point.remark, **initial,
            }
        super().__init__(data, initial=initial, **kwargs)
        self.point = point
        # A form that comes back from "Location missing?" has the new location chosen: the cursor goes to the next
        # field (UX-010).
        self.focus_skips = ("location",) if point is None and location is None and initial.get("location") else ()
        self.number = None
        self.duplicate = None
        self.identifier_missing = False
        settings_row = ManagementSettings.load()
        self.prefix_text = settings_row.metering_point_prefix
        number = self.fields["number_digits"]
        number.prefix_text = self.prefix_text
        number.after_template = "management/metering_points/_number_after.html"
        number.widget.attrs["data-prefix"] = self.prefix_text
        number.widget.attrs["data-check-url"] = reverse("management:metering_points_check_number")
        self.fields["leg"].queryset = Leg.objects.filter(is_archived=False).order_by("name")
        keep = [point.location_id] if point is not None else []
        if location is not None:
            keep.append(location.pk)
        self.fields["location"].queryset = location_queryset(*keep)
        self.fields["location"].after_template = "management/metering_points/_location_after.html"
        if location is not None:
            make_fact(self, "location", str(location), initial=location.pk)
        if point is not None:
            self.change_mode(point)

    def move_notes(self, point):
        """The consequence of choosing another location, per option: the transformer circuit changes (UX-005). A hint
        for the person; the service decides."""
        old = circuit_text(point.location.transformer_circuit if point.location_id else None)
        notes = {}
        for other in self.fields["location"].queryset.select_related("transformer_circuit"):
            new = circuit_text(other.transformer_circuit)
            if other.pk != point.location_id and new != old:
                notes[str(other.pk)] = move_note(old, new)
        if point.location_id and old != circuit_text(None):
            notes[""] = gettext(
                "Without a location the metering point has no transformer circuit any more (now: %(old)s). "
                "This can change the discount tier of the LEG."
            ) % {"old": old}
        return notes

    @staticmethod
    def padded(data):
        """The posted data with the typed number brought to its 20 characters (``numbers.normalise_entered_number``), so
        that a page shown again after an error holds the value that is checked and saved. An entry that is not valid
        stays as typed (the check says why)."""
        value = (data.get("number_digits") or "").strip()
        try:
            normalised = numbers.normalise_entered_number(value, ManagementSettings.load().metering_point_prefix)
        except ValidationError:
            return data
        if normalised == value:
            return data
        data = data.copy()
        data["number_digits"] = normalised
        return data

    def change_mode(self, point):
        """The form of an existing metering point: what cannot change here is a fact row."""
        self.hint = _(
            "The transformer circuit follows the location. If you change the location, the transformer circuit of the "
            "metering point changes with it. This can change the discount tier of the LEG."
        )
        self.fields["location"].widget.notes = self.move_notes(point)
        # The locked rows (number, direction, LEG) stand together above the divider; the order of the fields is the
        # one of the create form, only the divider moves (S-3).
        self.layout = [
            ("single", ["location"]),
            ("single", ["number_digits"]),
            ("single", ["direction"]),
            ("single", ["leg"]),
            ("divider", _("Optional")),
            ("triple", ["pv_power_kwp", "battery_kwh", "wallbox_kw"]),
            ("single", ["remark"]),
        ]
        self.no_lead = True  # the locked rows say which point it is: no lead sentence that repeats them
        make_fact(self, "number_digits", numbers.format_metering_point_number(point.number) or "–")
        make_fact(self, "direction", str(Direction(point.direction).label), initial=point.direction)
        current = leg_switch.current_assignment(assignments.LEG, point)
        make_fact(
            self, "leg", current.leg.name if current else gettext("No LEG assigned"),
            link=(
                reverse("management:metering_points_leg", args=[point.pk]),
                gettext("Change LEG") if current else gettext("Assign LEG"),
            ),
        )

    def clean_number_digits(self):
        if self.fields["number_digits"].disabled:
            return ""
        settings_row = ManagementSettings.load()
        digits = numbers.normalise_entered_number(self.cleaned_data["number_digits"], settings_row.metering_point_prefix)
        try:
            self.number = numbers.compose_metering_point_number(settings_row.country_code, settings_row.identifier, digits)
        except ValidationError as error:
            if not settings_row.identifier:
                self.identifier_missing = True
                raise ValidationError(
                    _("Enter the country and the identifier in the settings first."), code="no_identifier"
                ) from error
            raise
        return digits

    def metering_point_details(self):
        data = self.cleaned_data
        return {
            "pv_power_kwp": data.get("pv_power_kwp"), "battery_kwh": data.get("battery_kwh"),
            "wallbox_kw": data.get("wallbox_kw"), "remark": data.get("remark", ""),
        }


class DialogForm(forms.Form):
    layout = []
    optional = ()
    hint = ""


class NotifiedForm(DialogForm):
    registered_on = date_field(_("Notified on"), initial="today")
    layout = [("single", ["registered_on"])]


class ConfirmationForm(DialogForm):
    confirmed_on = date_field(_("Confirmed on"), initial="today")
    start_date = date_field(_("Start date"), required=False)
    layout = [("pair", ["confirmed_on", "start_date"])]
    optional = ("start_date",)
    hint = _("Without a start date the registration stays on standby.")


class StartDateForm(DialogForm):
    start_date = date_field(_("Start date"))
    layout = [("single", ["start_date"])]


class TerminationForm(DialogForm):
    terminated_on = date_field(_("Terminated to"))
    layout = [("single", ["terminated_on"])]
    hint = _("The date is the end of a month.")

    def clean_terminated_on(self):
        value = self.cleaned_data["terminated_on"]
        if not is_month_end(value):
            raise ValidationError(_("Enter the last day of a month."), code="not_month_end")
        return value


class PersonAssignForm(DialogForm):
    """The person is found by name, customer number or e-mail address; the date is free. The field is a combobox:
    ``combobox.js`` shows the suggestions from ``suggest_url`` in a list under the field (D-094); without the script
    the typed text must name one person."""

    person = forms.CharField(
        label=_("Person"), max_length=person_search.MAX_TEXT_LENGTH,
        widget=forms.TextInput(attrs={
            "class": "form-control", "autocomplete": "off",
            "placeholder": _("Search name, customer number or e-mail address"),
        }),
    )
    valid_from = date_field(_("Applies from"), initial="today")
    layout = [("single", ["person"]), ("single", ["valid_from"])]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        attrs = self.fields["person"].widget.attrs
        attrs.update(suggest_attributes(reverse("management:metering_points_person_search")))
        attrs["data-detail-label"] = gettext("Customer no.")
        self.fields["person"].after_template = "management/persons/_assign_after.html"

    def clean_person(self):
        found = person_search.resolve(self.cleaned_data["person"])
        if found.person is None:
            raise ValidationError(
                NO_PERSON_MESSAGE if found.code == "none"
                else _("Several persons match. Type more of the name or the customer number."),
                code=found.code,
            )
        return found.person


def suggest_attributes(url):
    """The data attributes of a field that ``combobox.js`` turns into a combobox: the address of the suggestions and the
    texts it announces and shows (translated when the form is made, in the language of the request)."""
    return {
        "data-suggest-url": url,
        "data-message-one": gettext("1 suggestion available."),
        "data-message-many": gettext("%(count)s suggestions available."),
        "data-message-none": gettext("No person found. Only active persons with a customer number can be assigned."),
        "data-message-limited": gettext("Too many searches. Please wait a moment."),
    }


NO_PERSON_MESSAGE = _(
    "No person found. Only active persons with a customer number can be assigned. A person who has not yet "
    "registered cannot be assigned yet."
)


class AssignmentEndForm(DialogForm):
    valid_to = date_field(_("Ends on"), initial="today")
    # The assignment the dialog showed: only that one is ended (D-084).
    assignment = forms.CharField(widget=forms.HiddenInput, max_length=12)
    layout = [("single", ["valid_to"])]
    hint = _("Afterwards the metering point has no person. The person stays visible in the history.")


def leg_field(label):
    """The LEG drop-down of the assignment dialogs: nothing is chosen in advance (D-094)."""
    return select_field(label, Leg.objects.none(), _("No LEG chosen yet"), required=True)


class LegAssignForm(DialogForm):
    """A metering point without a LEG."""

    leg = leg_field(_("LEG"))
    valid_from = date_field(_("Applies from"), initial="today")
    layout = [("single", ["leg"]), ("single", ["valid_from"])]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["leg"].queryset = Leg.objects.filter(is_archived=False).order_by("name")


class LegSwitchForm(DialogForm):
    """The new LEG, the date and the related metering points to move along (none preselected)."""

    leg = leg_field(_("New LEG"))
    valid_from = date_field(_("Applies from"), initial="today")
    also = forms.ModelMultipleChoiceField(
        queryset=MeteringPoint.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )
    layout = [("single", ["leg"]), ("single", ["valid_from"])]

    def __init__(self, *args, metering_point, current_leg_id=None, today=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.metering_point = metering_point
        self.related = leg_switch.related_points(metering_point, today)
        self.fields["leg"].queryset = (
            Leg.objects.filter(is_archived=False).exclude(pk=current_leg_id).order_by("name")
        )
        # Submitted ids are checked against the related points, never trusted.
        self.fields["also"].queryset = MeteringPoint.objects.filter(pk__in=[point.pk for point in self.related])


class LocationPointForm(DialogForm):
    """"Assign existing metering point" at a location: one drop-down of the metering points that are not at this
    location, those without a location first (D-095). The choices are built from the database for every request, so a
    posted id that is not offered (archived, already at this location, unknown) is refused; the service checks again
    under the locks."""

    metering_point = forms.ChoiceField(label=_("Metering point"), widget=NotedSelect(attrs={"class": "form-select tnum"}))
    layout = [("single", ["metering_point"])]

    def __init__(self, *args, location, **kwargs):
        super().__init__(*args, **kwargs)
        self.location = location
        free, other = [], []
        notes = {}
        new = circuit_text(location.transformer_circuit)
        points = (
            MeteringPoint.objects.filter(is_archived=False).exclude(location=location)
            .select_related("location__transformer_circuit").order_by("number", "pk")
        )
        self.points = {}
        for point in points:
            self.points[str(point.pk)] = point
            direction = str(Direction(point.direction).label)
            shown = numbers.format_metering_point_number(point.number) or "–"
            if point.location_id is None:
                free.append((str(point.pk), f"{shown} · {direction} · {gettext('no location')}"))
            else:
                other.append((str(point.pk), f"{shown} · {direction} · {point.location}"))
                # Only a point that moves from another location can change its transformer circuit (UX-005).
                old = circuit_text(point.location.transformer_circuit)
                if old != new:
                    notes[str(point.pk)] = move_note(old, new)
        groups = []
        if free:
            groups.append((gettext("Without location"), free))
        if other:
            groups.append((gettext("At another location"), other))
        self.fields["metering_point"].choices = [("", gettext("No metering point chosen yet")), *groups]
        self.fields["metering_point"].widget.notes = notes
        self.fields["metering_point"].after_template = "management/_option_note.html"
        self.empty = not self.points

    def clean_metering_point(self):
        return self.points[self.cleaned_data["metering_point"]]


ASSIGNMENT_ERRORS = {
    "starts_not_after_previous": _("The date must be after the start of the current assignment."),
    "end_before_start": _("The end date must not be before the start of the assignment."),
    "already_ended": _("This assignment has already ended."),
    "not_deletable": _("This assignment cannot be deleted. End it instead."),
    "not_a_person": _("This account cannot be assigned."),
    "person_not_active": _("This account cannot be assigned."),
    "leg_archived": _("This LEG is archived."),
    "not_related": _("A selected metering point does not belong to this one."),
    "point_archived": _("This metering point is archived."),
    "point_missing": _("This metering point does not exist any more."),
    "already_has_leg": _("This metering point already has a LEG."),
}

STATUS_ERRORS = {
    "archived": _("This metering point is archived."),
    "missing": _("This metering point does not exist any more."),
    "wrong_status": _("This step is not possible in the current status of the metering point."),
    "date_in_future": _("The date must not be in the future."),
    "date_before_previous": _("The date must not be before the earlier step."),
    "not_month_end": _("Enter the last day of a month."),
}
