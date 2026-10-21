"""One form per subject, wherever it is used (D-094): the same kind of form looks the same in every place.

Every entry point of a subject is rendered and compared: the names and the order of the fields, their labels, hints,
"(optional)" marking and the way they are drawn must be identical. A context may only pre-fill and lock a field
(a read-only fact row, never trusted from the browser); it may not add, remove or reorder a field, nor change a label,
a hint or the button. Also the shared building blocks: one decimal field, one remark field, one date field, one
LEG drop-down.
"""

from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community import numbers
from leg_platform.community.models import ManagementSettings
from leg_platform.community.tests.helpers import (
    make_circuit,
    make_location,
    make_metering_point,
    staff_user,
)
from leg_platform.management import form_fields
from leg_platform.management.leg_forms import LegCreateForm, LegPointForm, MasterDataForm, PercentField, QuotaForm
from leg_platform.management.location_forms import (
    AddressEditForm,
    CircuitFields,
    LocationCircuitForm,
    LocationCreateForm,
    RemarkForm,
)
from leg_platform.management.metering_point_forms import (
    LegAssignForm,
    LegSwitchForm,
    MeteringPointForm,
    PersonAssignForm,
)
from leg_platform.management.person_forms import (
    BillingAddressForm,
    ContactForm,
    DetailsForm,
    NoteForm,
    PersonCreateForm,
)


def field_names(form):
    """The fields of a form in the order of its layout (a divider has no field)."""
    return [name for row, names in form.layout if row != "divider" for name in names]


def signature(form, names=None):
    """What a person sees of each field: name, label, hint, "(optional)", how it is drawn."""
    result = []
    for name in names or field_names(form):
        field = form.fields[name]
        result.append((
            name, str(field.label), str(field.help_text), name in form.optional, type(field.widget).__name__,
            getattr(field, "unit", ""),
        ))
    return result


@tag("mariadb")
class EntryPointsOfAMeteringPointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.identity@example.org")
        row = ManagementSettings.load()
        row.country_code, row.identifier = "CH", "1A2B3C4D5E6"
        row.save()
        cls.place = make_location("Musterweg", "12", "3063", "Ittigen", transformer_circuit=make_circuit("TRA1", "Eins"))
        cls.point = make_metering_point(1, location=cls.place)

    def form_of(self, name, *args, **query):
        client = VerifiedClient()
        client.force_login(self.staff)
        response = client.get(reverse(f"management:{name}", args=args), query)
        self.assertEqual(response.status_code, 200, name)
        return response.context["form"], response

    def test_the_overview_and_the_location_show_the_same_form_and_only_the_location_is_locked(self):
        overview, overview_page = self.form_of("metering_points_new")
        located, located_page = self.form_of("locations_metering_point_new", self.place.pk)
        self.assertEqual(type(overview), type(located))
        self.assertEqual(overview.layout, located.layout)
        self.assertEqual(field_names(overview), field_names(located))
        self.assertEqual(signature(overview), signature(located))
        self.assertEqual(overview.optional, located.optional)
        self.assertEqual(overview.hint, located.hint)
        # The locked field is the only one that differs: a disabled fact row, not an input.
        self.assertEqual([n for n, f in overview.fields.items() if f.disabled], [])
        self.assertEqual([n for n, f in located.fields.items() if f.disabled], ["location"])
        self.assertContains(located_page, "Musterweg 12, 3063 Ittigen")
        self.assertNotContains(located_page, 'name="location"')
        self.assertContains(overview_page, 'name="location"')
        # The button and the labels that a person reads are the same on both pages.
        for page in (overview_page, located_page):
            for text in ("Messpunkt anlegen</button>", "Messpunkt-Nummer", "Richtung", "Optional", "LEG", "PV-Leistung", "Batterie", "Wallbox", "Bemerkung"):
                self.assertContains(page, text)

    def test_the_form_to_change_shows_the_same_fields_and_fixes_number_direction_and_leg(self):
        created, _page = self.form_of("metering_points_new")
        changed, page = self.form_of("metering_points_edit", self.point.pk)
        self.assertEqual(type(created), type(changed))
        self.assertEqual(field_names(created), field_names(changed))
        fixed = [name for name, field in changed.fields.items() if field.disabled]
        self.assertEqual(fixed, ["number_digits", "direction", "leg"])
        # Everything else is the same field with the same label and hint.
        open_names = [name for name in field_names(created) if name not in fixed]
        self.assertEqual(signature(created, open_names), signature(changed, open_names))
        self.assertContains(page, numbers.format_metering_point_number(self.point.number))
        for name in ("number_digits", "direction", "leg"):
            self.assertNotContains(page, f'name="{name}"')


@tag("mariadb")
class EntryPointsOfOtherSubjectsTests(TestCase):
    """The subjects whose entry points are separate forms: they share the definitions of the fields."""

    def test_the_location_forms_share_the_address_and_the_circuit_fields(self):
        create, edit = LocationCreateForm(), AddressEditForm()
        address = ["street", "house_number", "address_addition", "postal_code", "town"]
        self.assertEqual(signature(create, address), signature(edit, address))
        self.assertEqual(field_names(edit)[:5], address)
        circuit = ["transformer_circuit", "new_circuit_number", "new_circuit_name"]
        self.assertEqual(signature(create, circuit), signature(LocationCircuitForm(), circuit))
        self.assertEqual(
            [row for row in LocationCreateForm.layout if row in CircuitFields.circuit_layout], CircuitFields.circuit_layout
        )
        self.assertEqual(signature(create, ["remark"]), signature(RemarkForm(), ["remark"]))

    def test_the_person_forms_share_the_fields_of_the_create_form_in_the_same_order(self):
        create = PersonCreateForm()
        cards = [DetailsForm(), BillingAddressForm(), ContactForm()]
        card_order = [name for card in cards for name in field_names(card)]
        common = [name for name in field_names(create) if name in card_order]
        # Same relative order as in the cards, and the same label, hint and marking for every shared field.
        self.assertEqual(common, [name for name in card_order if name in common])
        for card in cards:
            for name in field_names(card):
                if name in field_names(create):
                    self.assertEqual(signature(create, [name]), signature(card, [name]), name)
        self.assertEqual(signature(create, ["email"])[0][1], "E-Mail")

    def test_the_leg_forms_share_the_fields(self):
        create, quota, master = LegCreateForm(), QuotaForm(), MasterDataForm()
        self.assertEqual(signature(create, ["quota", "checked_on"]), signature(quota, ["quota", "checked_on"]))
        self.assertEqual(signature(create, ["name", "remark"]), signature(master, ["name", "remark"]))

    def test_the_assignments_share_the_date_and_the_leg_choice(self):
        labels = {
            str(form.base_fields["valid_from"].label)
            for form in (PersonAssignForm, LegAssignForm, LegSwitchForm, LegPointForm)
        }
        self.assertEqual(labels, {"Gilt ab"})
        for field in (LegAssignForm.base_fields["leg"], LegSwitchForm.base_fields["leg"]):
            # Nothing is chosen in advance and the empty option says what it means.
            self.assertTrue(field.required)
            self.assertEqual(str(field.empty_label), "Noch keine LEG gewählt")

    def test_every_remark_is_the_same_field_and_marked_optional(self):
        for form in (RemarkForm(), LegCreateForm(), MasterDataForm(), LocationCreateForm(), NoteForm()):
            name = "note" if isinstance(form, NoteForm) else "remark"
            self.assertIn(name, form.optional, type(form).__name__)
            self.assertEqual(form.fields[name].widget.attrs["rows"], form_fields.REMARK_ROWS, type(form).__name__)
        self.assertIn("remark", MeteringPointForm.optional)
        self.assertEqual(MeteringPointForm.base_fields["remark"].widget.attrs["rows"], form_fields.REMARK_ROWS)

    def test_every_number_with_a_unit_is_the_one_decimal_field(self):
        fields = [MeteringPointForm.base_fields[name] for name in ("pv_power_kwp", "battery_kwh", "wallbox_kw")]
        fields.append(QuotaForm.base_fields["quota"])
        for field in fields:
            self.assertIsInstance(field, form_fields.DecimalInputField)
            self.assertTrue(field.unit)
            self.assertEqual(field.widget.attrs["inputmode"], "decimal")
            self.assertEqual(field.widget.kind, "decimal")
        self.assertIsInstance(QuotaForm.base_fields["quota"], PercentField)
