"""The raw tables of the management data for system administrators (D-091, owner decision 2026-10-19).

A system administrator edits every column in the list, adds a row with the new row of the list and deletes one row
at a time, without any rule check; every write is in Django's ``LogEntry`` and in one security event per request that
names the table, the row ids and the field names, never a value. Everybody else keeps the read-only tables."""

from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.models import Group, Permission
from django.test import TestCase, tag
from django.urls import reverse
from django.utils.html import escape

from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community.models import (
    HistoryEntry,
    Leg,
    LegAssignment,
    Location,
    ManagementSettings,
    MeteringPoint,
    PersonAssignment,
    PersonRecord,
    ShareChange,
    TransformerCircuit,
)
from leg_platform.community.raw_tables import DATABASE_REFUSED, NEVER_EDITABLE, RawTableAdmin
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)

MODELS = (
    HistoryEntry, Leg, LegAssignment, Location, ManagementSettings, MeteringPoint, PersonAssignment,
    PersonRecord, ShareChange, TransformerCircuit,
)
SECRET_NOTE = "SECRET-NOTE-TEXT"
SECRET_REMARK = "SECRET-REMARK-TEXT"
SECRET_IBAN = "CH9300762011623852957"
NEW_VALUE = "ValueNeverToBeLogged"


def url(model, kind="changelist", *args):
    return reverse(f"admin:community_{model._meta.model_name}_{kind}", args=args)


def row_data(form):
    """The POST data a browser sends for the inputs of one form (checked boxes only; dates as ISO text)."""
    data = {}
    for bound in form:
        value = bound.value()
        if isinstance(bound.field, forms.BooleanField):
            if value:
                data[bound.html_name] = "on"
        elif value is None:
            data[bound.html_name] = ""
        elif isinstance(value, (date,)):
            data[bound.html_name] = value.isoformat()
        else:
            data[bound.html_name] = str(value)
    return data


@tag("mariadb")
class RawTablesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)
        cls.customer = create_active_user("customer.person@example.org")
        cls.other_customer = create_active_user("other.customer@example.org")
        cls.circuit = make_circuit("TRA4711", "Circuit name")
        cls.spare_circuit = make_circuit("TRA4712")
        cls.location = make_location(transformer_circuit=cls.circuit, remark=SECRET_REMARK)
        cls.point = make_metering_point(5, location=cls.location, remark=SECRET_REMARK)
        cls.leg = make_leg("LEG Table", remark=SECRET_REMARK)
        cls.leg_assignment = assign_leg(cls.point, cls.leg)
        cls.person_assignment = assign_person(cls.point, cls.customer)
        cls.record = PersonRecord(person=cls.customer, staff_note=SECRET_NOTE, operator_customer_number="OP-1")
        cls.record.iban = SECRET_IBAN
        cls.record.save()
        cls.shares = ShareChange.objects.create(
            record=cls.record, quantity=4, valid_from=date(2026, 1, 1), actor=cls.administrator
        )
        cls.history = HistoryEntry.objects.create(
            subject_type="location", subject_id=cls.location.pk, event="created", actor=cls.administrator,
            detail={"fields": ["street"]},
        )
        cls.settings_row = ManagementSettings.load()
        cls.objects = {
            HistoryEntry: cls.history, Leg: cls.leg, LegAssignment: cls.leg_assignment, Location: cls.location,
            ManagementSettings: cls.settings_row, MeteringPoint: cls.point,
            PersonAssignment: cls.person_assignment, PersonRecord: cls.record, ShareChange: cls.shares,
            TransformerCircuit: cls.circuit,
        }

    def setUp(self):
        self.client = VerifiedClient()
        self.client.force_login(self.administrator)

    def staff_client(self, email, *codenames):
        person = staff_user(email)
        group = Group.objects.create(name=f"Test group for {email}")
        group.permissions.set(Permission.objects.filter(codename__in=codenames))
        person.groups.add(group)
        client = VerifiedClient()
        client.force_login(person)
        return client

    # Helpers that read the page like a browser does.

    def list_post(self, model, row, changes, address=None):
        """Save the list of a table with ``changes`` on the row ``row`` (the other rows are posted unchanged)."""
        address = address or url(model)
        formset = self.client.get(address).context["cl"].formset
        data = {}
        for form in formset.forms:
            data.update(row_data(form))
            if form.instance.pk == row.pk:
                index = form.prefix
                data.update({f"{index}-{name}": value for name, value in changes.items()})
        data.update({
            f"{formset.prefix}-TOTAL_FORMS": str(len(formset.forms)),
            f"{formset.prefix}-INITIAL_FORMS": str(len(formset.forms)),
            f"{formset.prefix}-MIN_NUM_FORMS": "0",
            f"{formset.prefix}-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        })
        return self.client.post(address, data)

    def new_row_post(self, model, **values):
        """Post the new row of a table: the defaults of the empty row, then ``values``."""
        page = self.client.get(url(model))
        new_row = page.context["raw_new_row"]
        data = row_data(new_row["form"])
        data.update({f"new-{name}": value for name, value in values.items()})
        data["next"] = new_row["next"]
        return self.client.post(new_row["url"], data)

    # The faces.

    def test_every_table_has_the_raw_face_for_a_system_administrator_and_the_plain_one_for_everybody_else(self):
        staff = staff_user("face.staff@example.org")
        for model in MODELS:
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertIsInstance(model_admin, RawTableAdmin)
                self.assertIsNone(model_admin.actions)
                self.assertEqual(list(model_admin.inlines), [])
                self.assertLessEqual(model_admin.list_per_page, 100)
                request = type("Request", (), {"user": staff})()
                self.assertEqual(set(model_admin.get_readonly_fields(request)), set(model_admin.staff_fields))
                self.assertEqual(list(model_admin.get_list_display(request)), list(model_admin.staff_list_display))
                for method in ("has_add_permission", "has_change_permission", "has_delete_permission"):
                    self.assertFalse(getattr(model_admin, method)(request), method)
                    self.assertFalse(getattr(model_admin, method)(None), method)
                request.user = self.administrator
                for method in ("has_add_permission", "has_change_permission", "has_delete_permission"):
                    self.assertTrue(getattr(model_admin, method)(request), method)

    def test_an_inactive_administrator_may_do_nothing(self):
        request = type("Request", (), {"user": type("Inactive", (), {"is_active": False, "is_superuser": True})()})()
        for model in MODELS:
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_change_permission(request))
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))

    def test_the_list_of_an_administrator_has_an_input_for_every_column_that_may_be_edited(self):
        for model in MODELS:
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                page = self.client.get(url(model))
                self.assertEqual(page.status_code, 200)
                form = page.context["cl"].formset.forms[0]
                new_form = page.context["raw_new_row"]["form"]
                columns = {f.name for f in model._meta.concrete_fields if not f.primary_key}
                stamps = {f.name for f in model._meta.concrete_fields if getattr(f, "auto_now", False)
                          or getattr(f, "auto_now_add", False)}
                expected = columns - stamps - NEVER_EDITABLE
                self.assertEqual(set(form.fields) - {"id"}, expected)
                self.assertEqual(set(new_form.fields), expected)
                # The first column (the primary key) is the link to the page of the row; no input is a link.
                self.assertEqual(model_admin.list_display_links, (model._meta.pk.name,))
                self.assertFalse(set(model_admin.list_editable) & set(model_admin.list_display_links))
                self.assertContains(page, url(model, "change", self.objects[model].pk))
                self.assertContains(page, 'class="raw-new-row"')
                self.assertContains(page, 'name="_save"')

    def test_the_ciphertext_of_the_iban_and_the_address_digest_are_never_an_input(self):
        page = self.client.get(url(PersonRecord))
        self.assertNotIn("iban_encrypted", page.content.decode())
        self.assertNotContains(page, self.record.iban_encrypted[:20])
        detail = self.client.get(url(PersonRecord, "change", self.record.pk))
        self.assertNotContains(detail, self.record.iban_encrypted[:20])
        self.assertNotIn("address_digest", self.client.get(url(Location)).content.decode())
        for model in (PersonRecord, Location):
            admin_of = admin.site._registry[model]
            self.assertFalse(set(NEVER_EDITABLE) & set(admin_of.list_display))
            self.assertFalse(set(NEVER_EDITABLE) & set(admin_of.fields))

    def test_the_administrator_sees_and_edits_the_free_text_the_plain_tables_hide(self):
        self.assertContains(self.client.get(url(Location)), SECRET_REMARK)
        self.assertContains(self.client.get(url(MeteringPoint)), SECRET_REMARK)
        self.assertContains(self.client.get(url(Leg)), SECRET_REMARK)
        self.assertContains(self.client.get(url(PersonRecord)), SECRET_NOTE)
        self.assertContains(self.client.get(url(PersonRecord)), "OP-1")
        self.assertContains(self.client.get(url(Location, "change", self.location.pk)), SECRET_REMARK)

    # The list is saved like a spreadsheet.

    def test_the_list_is_saved_with_one_button_and_every_column_changes(self):
        response = self.list_post(Location, self.location, {
            "street": "Changedstrasse", "remark": NEW_VALUE, "is_archived": "on", "suggested_town": "Suggestia",
            "address_correction_open": "on",
        })

        self.assertEqual(response.status_code, 302)
        row = Location.objects.get(pk=self.location.pk)
        self.assertEqual((row.street, row.remark, row.is_archived), ("Changedstrasse", NEW_VALUE, True))
        # Also a column the application keeps (``editable=False``) is an input.
        self.assertEqual((row.suggested_town, row.address_correction_open), ("Suggestia", True))
        # The save of the model keeps the digest in step with the address.
        self.assertEqual(Location.find_by_address("Changedstrasse", "1", "3000", "Bern").pk, row.pk)

    def test_every_change_of_the_list_is_in_the_admin_log_and_one_security_event(self):
        with self.assertLogs("leg_platform.security", level="WARNING") as logs, self.captureOnCommitCallbacks(
            execute=True
        ):
            response = self.list_post(Leg, self.leg, {"name": NEW_VALUE, "remark": f"{NEW_VALUE}-remark"})

        self.assertEqual(response.status_code, 302)
        entry = LogEntry.objects.get(action_flag=CHANGE, object_id=str(self.leg.pk))
        self.assertEqual(entry.user_id, self.administrator.pk)
        self.assertNotIn(NEW_VALUE, entry.get_change_message())
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED)
        self.assertEqual((event.actor_id, event.account_id, event.long_retention), (self.administrator.pk, None, True))
        line = logs.output[0]
        self.assertIn("community.leg", line)
        self.assertIn(f"rows {self.leg.pk}", line)
        self.assertIn("name", line)
        self.assertIn("remark", line)
        self.assertNotIn(NEW_VALUE, line)

    def test_a_save_of_the_list_without_a_change_writes_nothing(self):
        response = self.list_post(Leg, self.leg, {})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).exists())
        self.assertFalse(LogEntry.objects.filter(action_flag=CHANGE).exists())

    def test_one_request_is_one_security_event_for_all_its_rows(self):
        other = make_leg("LEG Other")
        formset = self.client.get(url(Leg)).context["cl"].formset
        data = {}
        for form in formset.forms:
            data.update(row_data(form))
            data[f"{form.prefix}-remark"] = "edited"
        data.update({
            "form-TOTAL_FORMS": str(len(formset.forms)), "form-INITIAL_FORMS": str(len(formset.forms)),
            "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000", "_save": "Save",
        })

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(url(Leg), data).status_code, 302)

        self.assertEqual(LogEntry.objects.filter(action_flag=CHANGE).count(), 2)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).count(), 1)
        self.assertEqual(Leg.objects.get(pk=other.pk).remark, "edited")

    def test_a_wrong_type_in_the_list_is_a_form_error_not_a_server_error(self):
        response = self.list_post(MeteringPoint, self.point, {"pv_power_kwp": "not a number"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "errorlist")
        self.assertIsNone(MeteringPoint.objects.get(pk=self.point.pk).pv_power_kwp)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).exists())

    def test_a_missing_related_row_in_the_list_is_a_form_error(self):
        response = self.list_post(MeteringPoint, self.point, {"location": "99999999"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "errorlist")
        self.assertEqual(MeteringPoint.objects.get(pk=self.point.pk).location_id, self.location.pk)

    def test_an_entry_of_the_history_can_be_changed_here_although_the_application_never_does(self):
        response = self.list_post(HistoryEntry, self.history, {"event": "corrected", "detail": '{"fields": []}'})

        self.assertEqual(response.status_code, 302)
        row = HistoryEntry.objects.get(pk=self.history.pk)
        self.assertEqual((row.event, row.detail), ("corrected", {"fields": []}))

    def test_a_relation_is_changed_through_its_number(self):
        # A round trip of the list keeps the encrypted IBAN: it is not an input and so never posted.
        self.assertEqual(self.list_post(PersonRecord, self.record, {}).status_code, 302)
        self.assertEqual(PersonRecord.objects.get(pk=self.record.pk).iban, SECRET_IBAN)
        response = self.list_post(
            PersonAssignment, self.person_assignment, {"person": str(self.other_customer.pk)}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(PersonAssignment.objects.get(pk=self.person_assignment.pk).person_id, self.other_customer.pk)

    def test_the_page_of_the_mask_is_not_a_way_to_add_a_row(self):
        before = Leg.objects.count()

        self.assertRedirects(self.client.get(url(Leg, "add")), url(Leg), fetch_redirect_response=False)
        self.assertRedirects(self.client.post(url(Leg, "add"), {"name": "LEG Mask"}), url(Leg), fetch_redirect_response=False)
        self.assertEqual(Leg.objects.count(), before)
        # Nor is there an "Add" link on the start page or the list.
        self.assertNotContains(self.client.get(reverse("admin:index")), url(Leg, "add"))
        self.assertNotContains(self.client.get(url(Leg)), url(Leg, "add"))

    def test_a_save_of_the_list_that_the_database_refuses_is_a_message_not_a_server_error(self):
        # The save of the model writes the number in capital letters: "tra4712" collides with the row "TRA4712" in
        # the database although the form of the list (an exact comparison) did not see it.
        response = self.list_post(TransformerCircuit, self.circuit, {"grid_number": "tra4712"})

        self.assertRedirects(response, url(TransformerCircuit), fetch_redirect_response=False)
        self.assertEqual(TransformerCircuit.objects.get(pk=self.circuit.pk).grid_number, "TRA4711")
        self.assertContains(self.client.get(url(TransformerCircuit)), escape(str(DATABASE_REFUSED)))
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).exists())
        self.assertFalse(LogEntry.objects.exists())

    def test_the_page_of_a_row_is_editable_and_audited_as_well(self):
        page = self.client.get(url(Leg, "change", self.leg.pk))
        data = row_data(page.context["adminform"].form)
        data.update({"name": "Renamed on the page", "_save": "Save"})

        with self.assertLogs("leg_platform.security", level="WARNING"), self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(url(Leg, "change", self.leg.pk), data).status_code, 302)

        self.assertEqual(Leg.objects.get(pk=self.leg.pk).name, "Renamed on the page")
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).count(), 1)

    # The new row.

    def new_row_values(self):
        return {
            TransformerCircuit: {"grid_number": "TRA9001", "name": "New circuit"},
            Location: {"street": "Newstrasse", "house_number": "7", "postal_code": "3011", "town": "Bern"},
            MeteringPoint: {"direction": "feed_in", "pv_power_kwp": "9.50", "location": str(self.location.pk)},
            Leg: {"name": "LEG New", "production_quota_percent": "204.10", "quota_checked_on": "2026-10-01"},
            PersonAssignment: {
                "metering_point": str(self.point.pk), "person": str(self.other_customer.pk), "valid_from": "2027-01-01",
            },
            LegAssignment: {"metering_point": str(self.point.pk), "leg": str(self.leg.pk), "valid_from": "2027-01-01"},
            PersonRecord: {"person": str(self.other_customer.pk), "staff_note": SECRET_NOTE},
            ShareChange: {"record": str(self.record.pk), "quantity": "2", "valid_from": "2027-01-01"},
            HistoryEntry: {"subject_type": "leg", "subject_id": "1", "event": "raw"},
        }

    def test_the_new_row_adds_a_row_to_every_table_and_returns_to_the_list(self):
        for model, values in self.new_row_values().items():
            with self.subTest(model=model.__name__), self.captureOnCommitCallbacks(execute=True):
                before = model.objects.count()
                response = self.new_row_post(model, **values)

                self.assertRedirects(response, url(model), fetch_redirect_response=False)
                self.assertEqual(model.objects.count(), before + 1)
                row = model.objects.order_by("-pk").first()
                self.assertTrue(LogEntry.objects.filter(
                    action_flag=ADDITION, object_id=str(row.pk),
                    content_type__model=model._meta.model_name,
                ).exists())
                self.assertContains(self.client.get(url(model)), "(ID %s)" % row.pk)
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).count(),
            len(self.new_row_values()),
        )

    def test_the_new_row_applies_the_save_of_the_model_and_no_service_rule(self):
        self.new_row_post(TransformerCircuit, grid_number=" tra9002 ", name="x")
        self.new_row_post(TransformerCircuit, grid_number="no format here", name="x")
        self.new_row_post(Leg, name="LEG Rule free", production_quota_percent="204.10", quota_checked_on="2026-10-01")
        self.new_row_post(Location, street="Rawstrasse", house_number="1", postal_code="ABCD", town="Bern")

        # The save of the model still normalises; the format rules of the application are not applied.
        self.assertTrue(TransformerCircuit.objects.filter(grid_number="TRA9002").exists())
        self.assertTrue(TransformerCircuit.objects.filter(grid_number="NO FORMAT HERE").exists())
        # 204.1 % is above the legal figure and above 100: nothing here checks a rule of the management area.
        self.assertEqual(Leg.objects.get(name="LEG Rule free").production_quota_percent, Decimal("204.10"))
        self.assertEqual(Location.objects.get(street="Rawstrasse").postal_code, "ABCD")
        # No history entry is written by the application.
        self.assertEqual(HistoryEntry.objects.count(), 1)

    def test_what_the_database_needs_is_still_checked_as_a_form_error(self):
        for model, values in (
            (Location, {"street": "x" * 151, "house_number": "1", "postal_code": "3000", "town": "Bern"}),
            (Location, {"street": "Streetonly", "house_number": "1", "postal_code": "30001", "town": "Bern"}),
            (Leg, {"name": "LEG Wide", "production_quota_percent": "1000.00", "quota_checked_on": "2026-10-01"}),
            (ShareChange, {"record": str(self.record.pk), "quantity": "-1", "valid_from": "2027-01-01"}),
        ):
            with self.subTest(model=model.__name__, values=values):
                before = model.objects.count()
                response = self.new_row_post(model, **values)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "errorlist")
                self.assertEqual(model.objects.count(), before)

    def test_a_wrong_type_in_the_new_row_is_shown_in_the_row_not_as_a_server_error(self):
        before = Leg.objects.count()
        response = self.new_row_post(
            Leg, name="LEG Broken", production_quota_percent="abc", quota_checked_on="not a date"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Leg.objects.count(), before)
        self.assertContains(response, "errorlist")
        self.assertContains(response, 'class="raw-new-row"')
        self.assertContains(response, 'value="LEG Broken"')
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).exists())

    def test_a_missing_required_value_and_a_missing_related_row_are_form_errors(self):
        for model, values in (
            (Leg, {"name": ""}),
            (MeteringPoint, {"direction": "consumption", "location": "99999999"}),
            (ShareChange, {"record": "99999999", "quantity": "1", "valid_from": "2027-01-01"}),
        ):
            with self.subTest(model=model.__name__):
                before = model.objects.count()
                response = self.new_row_post(model, **values)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "errorlist")
                self.assertEqual(model.objects.count(), before)

    def test_a_row_the_database_refuses_is_a_clear_message_not_a_server_error(self):
        # The settings table holds one row (id 1): a second one is refused by the database, not by a form check.
        response = self.new_row_post(ManagementSettings)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, escape(str(DATABASE_REFUSED)))
        self.assertEqual(ManagementSettings.objects.count(), 1)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).exists())
        # The page still works afterwards (the transaction was rolled back cleanly).
        self.assertEqual(self.client.get(url(ManagementSettings)).status_code, 200)

    def test_a_duplicate_is_refused_with_a_form_error(self):
        response = self.new_row_post(TransformerCircuit, grid_number="TRA4711")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "errorlist")
        self.assertEqual(TransformerCircuit.objects.filter(grid_number="TRA4711").count(), 1)

    def test_the_settings_row_can_be_made_again_when_the_table_is_empty(self):
        ManagementSettings.objects.all().delete()
        response = self.new_row_post(ManagementSettings, identifier="")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ManagementSettings.objects.count(), 1)

    def test_an_empty_list_still_shows_the_new_row(self):
        HistoryEntry.objects.all().delete()
        page = self.client.get(url(HistoryEntry))

        self.assertContains(page, 'class="raw-new-row"')

    def test_the_list_state_is_kept_and_a_foreign_address_is_not_followed(self):
        page = self.client.get(url(Leg), {"o": "1"})
        new_row = page.context["raw_new_row"]
        data = row_data(new_row["form"])
        data.update({"new-name": "LEG State"})

        data["next"] = url(Leg) + "?o=1"
        self.assertRedirects(self.client.post(new_row["url"], data), url(Leg) + "?o=1", fetch_redirect_response=False)
        data.update({"new-name": "LEG State two", "next": "https://evil.example.org/"})
        self.assertRedirects(self.client.post(new_row["url"], data), url(Leg), fetch_redirect_response=False)
        data.update({"new-name": "LEG State three", "next": url(Location)})
        self.assertRedirects(self.client.post(new_row["url"], data), url(Leg), fetch_redirect_response=False)

    def test_the_address_of_the_new_row_shows_the_list_for_a_get(self):
        address = reverse("admin:community_leg_add_row")

        self.assertRedirects(self.client.get(address), url(Leg), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(address, {"o": "1"}), url(Leg) + "?o=1", fetch_redirect_response=False)

    def test_the_save_of_the_list_after_an_invalid_new_row_still_works(self):
        # The list of that page is posted to the address of the new row (the page was drawn there).
        formset = self.client.get(url(Leg)).context["cl"].formset
        data = {}
        for form in formset.forms:
            data.update(row_data(form))
            data[f"{form.prefix}-remark"] = "saved after the error"
        data.update({
            "form-TOTAL_FORMS": str(len(formset.forms)), "form-INITIAL_FORMS": str(len(formset.forms)),
            "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000", "_save": "Save",
        })

        response = self.client.post(reverse("admin:community_leg_add_row"), data)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Leg.objects.get(pk=self.leg.pk).remark, "saved after the error")

    # Delete.

    def test_a_single_row_is_deleted_after_the_confirmation_and_audited(self):
        spare = self.spare_circuit
        address = url(TransformerCircuit, "delete", spare.pk)

        self.assertEqual(self.client.get(address).status_code, 200)
        self.assertTrue(TransformerCircuit.objects.filter(pk=spare.pk).exists())
        with self.assertLogs("leg_platform.security", level="WARNING") as logs, self.captureOnCommitCallbacks(
            execute=True
        ):
            response = self.client.post(address, {"post": "yes"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(TransformerCircuit.objects.filter(pk=spare.pk).exists())
        self.assertTrue(LogEntry.objects.filter(action_flag=DELETION, object_id=str(spare.pk)).exists())
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).count(), 1)
        self.assertIn(f"rows {spare.pk}", logs.output[0])
        self.assertNotIn("TRA4712", logs.output[0])

    def test_a_row_that_is_still_in_use_cannot_be_deleted_and_the_page_says_so(self):
        address = url(Location, "delete", self.location.pk)

        page = self.client.get(address)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="post"')
        response = self.client.post(address, {"post": "yes"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Location.objects.filter(pk=self.location.pk).exists())
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).exists())

    def test_there_is_no_bulk_delete(self):
        for model in MODELS:
            with self.subTest(model=model.__name__):
                page = self.client.get(url(model))
                self.assertNotContains(page, 'name="action"')
                self.assertNotContains(page, 'name="_selected_action"')
                self.assertIsNone(admin.site._registry[model].actions)
        response = self.client.post(url(Leg), {"action": "delete_selected", "_selected_action": [self.leg.pk]})
        self.assertTrue(Leg.objects.filter(pk=self.leg.pk).exists())
        self.assertNotEqual(response.status_code, 500)

    # Everybody else: the read-only face with the leak rule.

    def test_a_staff_account_with_the_view_right_still_sees_the_plain_read_only_table(self):
        client = self.staff_client(
            "viewer.person@example.org", *[f"view_{model._meta.model_name}" for model in MODELS]
        )
        for model in MODELS:
            row = self.objects[model]
            for page in (url(model), url(model, "change", row.pk)):
                with self.subTest(page=page):
                    response = client.get(page)
                    html = response.content.decode()
                    self.assertEqual(response.status_code, 200)
                    for secret in (SECRET_NOTE, SECRET_REMARK, "OP-1", SECRET_IBAN, self.record.iban_encrypted[:20]):
                        self.assertNotIn(secret, html)
                    self.assertNotIn("raw-new-row", html)
                    self.assertNotIn('name="_save"', html)
                    self.assertNotIn("form-0-", html)
                    self.assertNotIn("raw-new-row-form", html)

    def test_a_staff_account_with_the_view_right_cannot_write_in_any_way(self):
        client = self.staff_client(
            "viewer.two@example.org", *[f"view_{model._meta.model_name}" for model in MODELS]
        )
        for model in MODELS:
            row = self.objects[model]
            with self.subTest(model=model.__name__):
                self.assertEqual(client.post(url(model), {"_save": "Save", "form-TOTAL_FORMS": "0"}).status_code, 403)
                self.assertEqual(client.get(url(model, "add_row")).status_code, 403)
                self.assertEqual(client.post(url(model, "add_row"), {"new-name": "x"}).status_code, 403)
                self.assertEqual(client.get(url(model, "add")).status_code, 403)
                self.assertEqual(client.post(url(model, "add"), {}).status_code, 403)
                self.assertEqual(client.post(url(model, "change", row.pk), {"_save": "x"}).status_code, 403)
                self.assertEqual(client.get(url(model, "delete", row.pk)).status_code, 403)
                self.assertEqual(client.post(url(model, "delete", row.pk), {"post": "yes"}).status_code, 403)
        for model in MODELS:
            self.assertTrue(model.objects.filter(pk=self.objects[model].pk).exists())
        self.assertEqual(Leg.objects.count(), 1)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.MANAGEMENT_DATA_EDITED).exists())
        self.assertFalse(LogEntry.objects.exists())

    def test_the_view_columns_of_the_plain_face_are_the_explicit_leak_safe_ones(self):
        forbidden = {
            "iban", "iban_encrypted", "staff_note", "remark", "email", "address_digest", "first_name", "last_name",
            "suggested_street", "suggested_town", "suggestion_dismissed_for", "password", "operator_customer_number",
        }
        for model in MODELS:
            model_admin = admin.site._registry[model]
            shown = {*model_admin.staff_list_display, *model_admin.staff_fields, *model_admin.search_fields}
            shown = {name.lstrip("=^@").split("__")[-1] for name in shown}
            with self.subTest(model=model.__name__):
                self.assertFalse(shown & forbidden, shown & forbidden)

    def test_a_view_holder_cannot_probe_hidden_columns_through_the_address(self):
        client = self.staff_client(
            "prober.person@example.org", "view_personrecord", "view_location", "view_meteringpoint", "view_leg",
        )
        for model, query in (
            (PersonRecord, "staff_note__startswith=SECRET"),
            (PersonRecord, "operator_customer_number__startswith=OP"),
            (PersonRecord, "note__startswith=SECRET"),
            (Location, "remark__contains=SECRET"),
            (Location, "remarks__contains=SECRET"),
            (MeteringPoint, "remark__startswith=SECRET"),
            (MeteringPoint, "location__remark__contains=SECRET"),
            (Leg, "remark__contains=SECRET"),
            (Location, "address_digest__startswith=a"),
        ):
            with self.subTest(model=model.__name__, query=query):
                response = client.get(f"{url(model)}?{query}")
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(SECRET_REMARK, response.content.decode())
        # A lookup of a filter of the list still works, and so does the search and the sorting.
        self.assertEqual(client.get(f"{url(PersonRecord)}?paper_invoice__exact=0").status_code, 200)
        self.assertEqual(client.get(f"{url(Location)}?is_archived__exact=0&o=1").status_code, 200)
        self.assertEqual(client.get(f"{url(Leg)}?q=LEG").status_code, 200)

    def test_an_administrator_still_filters_by_any_column(self):
        self.assertEqual(self.client.get(f"{url(Location)}?remark__contains=SECRET").status_code, 200)

    def test_a_write_without_the_audit_context_is_refused_before_it_happens(self):
        model_admin = admin.site._registry[Leg]
        request = type("Request", (), {"user": self.administrator})()
        form = type("Form", (), {"changed_data": []})()

        with self.assertRaises(RuntimeError):
            model_admin.save_model(request, Leg(name="LEG Unaudited"), form, change=False)
        with self.assertRaises(RuntimeError):
            model_admin.delete_model(request, self.leg)

        self.assertFalse(Leg.objects.filter(name="LEG Unaudited").exists())
        self.assertTrue(Leg.objects.filter(pk=self.leg.pk).exists())

    def test_the_audit_of_a_delete_counts_the_rows_that_went_with_it_and_never_a_value(self):
        with self.assertLogs("leg_platform.security", level="WARNING") as logs, self.captureOnCommitCallbacks(
            execute=True
        ):
            response = self.client.post(url(PersonRecord, "delete", self.record.pk), {"post": "yes"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ShareChange.objects.filter(pk=self.shares.pk).exists())
        self.assertIn("cascade community.sharechange=1", logs.output[0])
        self.assertNotIn("community.personrecord=", logs.output[0])
        self.assertNotIn(SECRET_NOTE, logs.output[0])

    def test_any_database_error_is_a_message_never_a_server_error(self):
        from unittest import mock

        from django.db import OperationalError

        failure = OperationalError(2013, "Lost connection near 'ValueNeverToBeLogged'")
        with mock.patch.object(RawTableAdmin, "save_row", side_effect=failure), self.assertLogs(
            "leg_platform.community.raw_tables", level="WARNING"
        ) as logs:
            listed = self.list_post(Leg, self.leg, {"remark": "changed"})
            added = self.new_row_post(Leg, name="LEG Lost")

        self.assertRedirects(listed, url(Leg), fetch_redirect_response=False)
        self.assertEqual(added.status_code, 200)
        self.assertContains(added, escape(str(DATABASE_REFUSED)))
        self.assertTrue(all("OperationalError" in line and NEW_VALUE not in line for line in logs.output))
        self.assertFalse(any("Lost connection" in line for line in logs.output))
        self.assertFalse(Leg.objects.filter(name="LEG Lost").exists())

    def test_one_save_of_a_list_stays_below_the_limit_of_form_fields(self):
        from django.conf import settings
        from django.test import RequestFactory

        limit = settings.DATA_UPLOAD_MAX_NUMBER_FIELDS
        self.assertIsNotNone(limit)
        request = RequestFactory().get("/")
        request.user = self.administrator
        for model in MODELS:
            model_admin = admin.site._registry[model]
            with self.subTest(model=model.__name__):
                inputs_per_row = len(model_admin.get_changelist_form(request).base_fields) + 1  # and the hidden id
                # The management form, the token and the button come on top.
                total = inputs_per_row * model_admin.list_per_page + 10
                self.assertLess(total, limit, (inputs_per_row, model_admin.list_per_page))

    def test_a_staff_account_without_a_right_and_anonymous_visitors_reach_nothing(self):
        staff = VerifiedClient()
        staff.force_login(staff_user("no.rights@example.org"))
        plain = VerifiedClient()
        plain.force_login(create_active_user("plain.person@example.org"))
        for model in MODELS:
            for address in (url(model, "add_row"), url(model)):
                with self.subTest(address=address):
                    self.assertEqual(staff.post(address, {}).status_code, 403)
                    for client in (plain, self.client_class()):
                        response = client.post(address, {})
                        self.assertEqual(response.status_code, 302)
                        self.assertIn(reverse("admin:login"), response["Location"])
