"""The grid operator of the instance (D-086): the name in the texts of the management area with the neutral word as
long as none is entered, the model with its one row, the message of the circuit number, and the Django administration
(system administrators only, one row, no add and no delete)."""

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, tag
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext_lazy

from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community import grid_operator, numbers
from leg_platform.community.models import GridOperator
from leg_platform.community.tests.helpers import staff_user

OPERATOR = "Netzbetreiber Beispiel"
CHANGE_URL = reverse("admin:community_gridoperator_change", args=(1,))


def enter_name(name):
    row = GridOperator.load()
    row.name = name
    row.save()
    return row


@tag("mariadb")
class GridOperatorNameTests(TestCase):
    def test_the_neutral_word_stands_in_until_a_name_is_entered(self):
        with translation.override("de-ch"):
            self.assertFalse(GridOperator.objects.exists())
            self.assertEqual(grid_operator.name(), "Netzbetreiber")  # no row at all
            enter_name("")
            self.assertEqual(grid_operator.name(), "Netzbetreiber")  # a row without a name
            enter_name(OPERATOR)
            self.assertEqual(grid_operator.name(), OPERATOR)

    def test_a_lazy_text_reads_the_name_when_it_is_used(self):
        text = grid_operator.text(gettext_lazy("%(operator)s number"))  # created before any name exists

        with translation.override("de-ch"):
            self.assertEqual(str(text), "Netzbetreiber-Nummer")
            enter_name(OPERATOR)
            self.assertEqual(str(text), f"{OPERATOR}-Nummer")
            enter_name("Anderer Netzbetreiber")
            self.assertEqual(str(text), "Anderer Netzbetreiber-Nummer")

    def test_the_row_is_loaded_or_created_once_with_id_1_and_never_a_second_one(self):
        row = GridOperator.load()
        self.assertEqual((row.pk, row.name), (1, ""))
        self.assertEqual(GridOperator.load().pk, 1)
        self.assertEqual(GridOperator.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GridOperator.objects.create(id=2)
        self.assertEqual(GridOperator.objects.count(), 1)

    def test_the_name_is_stored_with_white_space_collapsed(self):
        enter_name("  Netz   betreiber \t Beispiel ")

        self.assertEqual(GridOperator.objects.get(pk=1).name, "Netz betreiber Beispiel")

    def test_the_message_of_a_wrong_circuit_number_names_the_operator_and_the_format(self):
        enter_name(OPERATOR)

        with translation.override("de-ch"), self.assertRaises(ValidationError) as caught:
            numbers.validate_transformer_circuit_number("XYZ")

        self.assertEqual(
            caught.exception.messages, [f"Geben Sie die {OPERATOR}-Nummer ein: TRA und Ziffern."]
        )


@tag("mariadb")
class GridOperatorAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)
        cls.staff = staff_user("staff.person@example.org")

    def client_of(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client

    def test_a_system_administrator_opens_the_form_and_changes_the_name(self):
        client = self.client_of(self.administrator)

        self.assertEqual(client.get(CHANGE_URL).status_code, 200)
        self.assertEqual(GridOperator.objects.get(pk=1).name, "")
        response = client.post(CHANGE_URL, {"name": f"  {OPERATOR} ", "_save": "Speichern"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(GridOperator.objects.get(pk=1).name, OPERATOR)
        self.assertContains(client.get(CHANGE_URL), OPERATOR)
        # L-8: the change is a security event for the long period (actor only, no value); a save without a change is none.
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.GRID_OPERATOR_CHANGED)
        self.assertEqual((event.actor_id, event.account_id, event.long_retention), (self.administrator.pk, None, True))
        client.post(CHANGE_URL, {"name": OPERATOR, "_save": "Speichern"})
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.GRID_OPERATOR_CHANGED).count(), 1)

    def test_the_list_leads_straight_to_the_form_of_the_one_row(self):
        response = self.client_of(self.administrator).get(reverse("admin:community_gridoperator_changelist"))

        self.assertRedirects(response, CHANGE_URL, fetch_redirect_response=False)
        self.assertTrue(GridOperator.objects.filter(pk=1).exists())

    def test_there_is_no_add_no_delete_and_no_other_row_not_even_for_a_system_administrator(self):
        client = self.client_of(self.administrator)
        GridOperator.load()

        self.assertEqual(client.get(reverse("admin:community_gridoperator_add")).status_code, 403)
        self.assertEqual(client.post(reverse("admin:community_gridoperator_add"), {"name": "X"}).status_code, 403)
        delete_url = reverse("admin:community_gridoperator_delete", args=(1,))
        self.assertEqual(client.get(delete_url).status_code, 403)
        self.assertEqual(client.post(delete_url, {"post": "yes"}).status_code, 403)
        self.assertEqual(
            client.post(reverse("admin:community_gridoperator_change", args=(2,)), {"name": "X"}).status_code, 302
        )
        self.assertEqual(list(GridOperator.objects.values_list("pk", "name")), [(1, "")])

    def test_a_staff_account_that_is_no_system_administrator_gets_nothing_and_changes_nothing(self):
        client = self.client_of(self.staff)
        GridOperator.load()
        urls = (
            CHANGE_URL,
            reverse("admin:community_gridoperator_changelist"),
            reverse("admin:community_gridoperator_add"),
            reverse("admin:community_gridoperator_delete", args=(1,)),
        )

        for url in urls:
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    response = getattr(client, method)(url, {"name": "Changed", "post": "yes"} if method == "post" else {})
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(list(GridOperator.objects.values_list("pk", "name")), [(1, "")])

    def test_the_start_page_lists_the_model_in_the_group_content_for_a_system_administrator_only(self):
        request = RequestFactory().get(reverse("admin:index"))
        request.user = self.administrator
        groups = {group["app_label"]: group for group in admin.site.get_app_list(request)}
        self.assertIn("GridOperator", [model["object_name"] for model in groups["content"]["models"]])
        page = self.client_of(self.administrator).get(reverse("admin:index")).content.decode()
        self.assertIn(reverse("admin:community_gridoperator_changelist"), page)

        request.user = self.staff
        self.assertEqual(
            [model["object_name"] for group in admin.site.get_app_list(request) for model in group["models"]], []
        )
        page = self.client_of(self.staff).get(reverse("admin:index")).content.decode()
        self.assertNotIn("/admin/community/", page)
