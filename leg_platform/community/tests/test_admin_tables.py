"""The plain, read-only face of the tables of the management data in the Django administration (D-091).

Who may see it: the "view" right of the table, handed out by a system administrator (never by a group
name in code). A staff account with that right changes nothing there. Free text of the staff, the IBAN and the
email address of a person never appear. System administrators edit the same tables raw: ``test_admin_raw_tables``."""


from django.contrib import admin
from django.contrib.auth.models import Group, Permission
from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community.models import (
    GridOperator,
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
from leg_platform.community.tests.helpers import (
    create_table_rows,
    staff_user,
)

MODELS = (
    HistoryEntry, Leg, LegAssignment, Location, ManagementSettings, MeteringPoint, PersonAssignment,
    PersonRecord, ShareChange, TransformerCircuit,
)
SECRET_NOTE = "SECRET-NOTE-TEXT"
SECRET_REMARK = "SECRET-REMARK-TEXT"
SECRET_EMAIL = "secret.customer@example.org"
SECRET_IBAN = "CH9300762011623852957"
FORBIDDEN_COLUMNS = {
    "iban", "iban_encrypted", "staff_note", "remark", "email", "address_digest", "first_name", "last_name",
    "suggested_street", "suggested_town", "suggestion_dismissed_for", "password", "operator_customer_number",
}


def url(model, kind="changelist", *args):
    return reverse(f"admin:community_{model._meta.model_name}_{kind}", args=args)


@tag("mariadb")
class PlainTablesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)
        cls.customer = create_active_user(SECRET_EMAIL, first_name="Secretia", last_name="Customerson")
        create_table_rows(cls, cls.customer, cls.administrator, note=SECRET_NOTE, remark=SECRET_REMARK, iban=SECRET_IBAN)

    def client_of(self, person):
        client = VerifiedClient()
        client.force_login(person)
        return client

    def staff_with(self, email, *codenames):
        """A staff account that holds exactly these rights, through a group a test makes itself."""
        person = staff_user(email)
        group = Group.objects.create(name=f"Test group for {email}")
        group.permissions.set(Permission.objects.filter(codename__in=codenames))
        person.groups.add(group)
        return self.client_of(person)

    def test_every_table_is_registered_with_a_plain_face_that_is_read_only(self):
        staff = staff_user("plain.face@example.org")
        request = type("Request", (), {"user": staff})()
        for model in MODELS:
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertIsNone(model_admin.actions)
                self.assertEqual(list(model_admin.inlines), [])
                self.assertEqual(set(model_admin.get_readonly_fields(request)), set(model_admin.staff_fields))
                self.assertEqual(list(model_admin.get_fields(request)), list(model_admin.staff_fields))
                self.assertEqual(list(model_admin.get_list_display(request)), list(model_admin.staff_list_display))
                for method in ("has_add_permission", "has_change_permission", "has_delete_permission"):
                    self.assertFalse(getattr(model_admin, method)(request))
                    self.assertFalse(getattr(model_admin, method)(None))
                self.assertTrue(model_admin.staff_list_display)
                self.assertLessEqual(model_admin.list_per_page, 100)
        self.assertIn(GridOperator, admin.site._registry)

    def test_no_column_or_field_of_the_plain_face_can_show_free_text_the_iban_or_personal_data(self):
        for model in MODELS:
            model_admin = admin.site._registry[model]
            with self.subTest(model=model.__name__):
                shown = {*model_admin.staff_list_display, *model_admin.staff_fields, *model_admin.search_fields}
                shown = {name.lstrip("=^@").split("__")[-1] for name in shown}
                self.assertFalse(shown & FORBIDDEN_COLUMNS, shown & FORBIDDEN_COLUMNS)
                # The search reaches no personal column of the account either.
                for field in model_admin.search_fields:
                    self.assertNotIn("person__", field.replace("person__customer_number", ""))

    def test_a_system_administrator_sees_every_table_and_every_row(self):
        client = self.client_of(self.administrator)
        for model in MODELS:
            with self.subTest(model=model.__name__):
                self.assertEqual(client.get(url(model)).status_code, 200)
                row = self.objects[model]
                self.assertEqual(client.get(url(model, "change", row.pk)).status_code, 200)

    def test_nothing_secret_is_in_any_page_of_any_table_for_a_staff_account_with_the_view_rights(self):
        client = self.staff_with("every.view@example.org", *[f"view_{model._meta.model_name}" for model in MODELS])
        for model in MODELS:
            for page in (url(model), url(model, "change", self.objects[model].pk)):
                response = client.get(page)
                html = response.content.decode()
                with self.subTest(page=page):
                    self.assertEqual(response.status_code, 200)
                    for secret in (SECRET_NOTE, SECRET_REMARK, SECRET_EMAIL, SECRET_IBAN, "Secretia", "Customerson", "OP-1"):
                        self.assertNotIn(secret, html)
                    self.assertNotIn(self.record.iban_encrypted[:20], html)

    def test_the_lists_show_the_data_they_are_for(self):
        client = self.staff_with("every.view.two@example.org", *[f"view_{model._meta.model_name}" for model in MODELS])
        self.assertContains(client.get(url(TransformerCircuit)), "TRA4711")
        self.assertContains(client.get(url(Location)), "Examplestrasse")
        self.assertContains(client.get(url(MeteringPoint)), self.point.number)
        self.assertContains(client.get(url(Leg)), "LEG Table")
        # The number at the grid operator is shown on the detail page of the management area only (M-2).
        self.assertNotContains(client.get(url(PersonRecord)), "OP-1")
        self.assertEqual(admin.site._registry[PersonRecord].search_fields, ())  # nothing to search it with either
        # A person is the customer number ("Konto ..."), not the name or the email.
        self.assertContains(client.get(url(PersonAssignment)), "Konto")
        self.assertContains(client.get(url(HistoryEntry)), "location")
        self.assertContains(client.get(url(ManagementSettings)), "CH")

    def test_a_staff_account_without_a_right_sees_nothing(self):
        client = self.client_of(staff_user("no.rights@example.org"))
        for model in MODELS:
            row = self.objects[model]
            for page in (url(model), url(model, "change", row.pk), url(model, "history", row.pk)):
                with self.subTest(page=page):
                    self.assertEqual(client.get(page).status_code, 403)
        index = client.get(reverse("admin:index")).content.decode()
        self.assertNotIn("/admin/community/", index)

    def test_the_view_right_of_one_table_opens_that_table_only_and_changes_nothing(self):
        client = self.staff_with("viewer.person@example.org", "view_location")
        self.assertEqual(client.get(url(Location)).status_code, 200)
        self.assertEqual(client.get(url(Location, "change", self.location.pk)).status_code, 200)
        for model in MODELS:
            if model is Location:
                continue
            with self.subTest(model=model.__name__):
                self.assertEqual(client.get(url(model)).status_code, 403)
        self.assertEqual(client.get(url(Location, "add")).status_code, 403)
        self.assertEqual(client.get(url(Location, "delete", self.location.pk)).status_code, 403)
        self.assertEqual(client.post(url(Location, "change", self.location.pk), {"street": "Hacked"}).status_code, 403)
        self.assertEqual(client.post(url(Location, "delete", self.location.pk), {"post": "yes"}).status_code, 403)
        self.assertEqual(Location.objects.get(pk=self.location.pk).street, "Examplestrasse")
        index = client.get(reverse("admin:index")).content.decode()
        self.assertIn(url(Location), index)
        self.assertNotIn(url(MeteringPoint), index)

    def test_a_staff_account_with_every_view_right_still_cannot_change_a_thing(self):
        client = self.staff_with("viewer.two@example.org", *[f"view_{model._meta.model_name}" for model in MODELS])
        for model in MODELS:
            row = self.objects[model]
            with self.subTest(model=model.__name__):
                self.assertEqual(client.get(url(model)).status_code, 200)
                self.assertEqual(client.post(url(model, "change", row.pk), {}).status_code, 403)
                self.assertEqual(client.post(url(model, "delete", row.pk), {"post": "yes"}).status_code, 403)
                self.assertEqual(client.post(url(model, "add"), {}).status_code, 403)
        for model in MODELS:
            self.assertTrue(model.objects.filter(pk=self.objects[model].pk).exists())

    def test_the_grid_operator_stays_for_system_administrators_only(self):
        client = self.staff_with("viewer.three@example.org", *[f"view_{model._meta.model_name}" for model in MODELS])
        self.assertEqual(client.get(reverse("admin:community_gridoperator_changelist")).status_code, 403)

    def test_the_start_page_groups_the_tables_under_management_data(self):
        client = self.staff_with("viewer.four@example.org", "view_meteringpoint", "view_leg")
        index = client.get(reverse("admin:index")).content.decode()
        self.assertIn("Verwaltungsdaten", index)
        self.assertIn(url(MeteringPoint), index)
        self.assertIn(url(Leg), index)
        self.assertNotIn(url(Location), index)

    def test_the_labels_are_swiss_german(self):
        client = self.client_of(self.administrator)
        index = client.get(reverse("admin:index")).content.decode()
        for text in ("Standorte", "Messpunkte", "LEGs", "Trafokreise", "Verlauf"):
            self.assertIn(text, index)
        page = client.get(url(MeteringPoint)).content.decode()
        for text in ("Messpunkt-Nummer", "Richtung", "Status"):
            self.assertIn(text, page)
