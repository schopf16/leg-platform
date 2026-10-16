"""The rights of the management area in the administration of roles (D-012, D-077): a system
administrator can assign every right the pages check, with a readable name, and the help says what
they are for. Nothing is created or assigned by the migrations."""

from django.contrib.auth.models import Permission
from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.permission_names import (
    assignable_permissions_filter,
    permission_label,
    rights_help,
)
from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community.tests.helpers import staff_user

COMMUNITY_CODENAMES = {
    "view_location", "add_location", "change_location", "delete_location",
    "view_meteringpoint", "add_meteringpoint", "change_meteringpoint", "delete_meteringpoint",
    "view_leg", "add_leg", "change_leg", "delete_leg",
    "view_transformercircuit", "add_transformercircuit", "change_transformercircuit",
    "view_managementsettings", "change_managementsettings",
    "view_personrecord", "change_personrecord", "correct_person_address",
}


@tag("mariadb")
class RightsOfTheManagementAreaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)

    def offered(self):
        return Permission.objects.filter(assignable_permissions_filter(), content_type__app_label="community")

    def test_a_role_can_hold_every_right_of_the_area_and_nothing_else_of_it(self):
        self.assertEqual(set(self.offered().values_list("codename", flat=True)), COMMUNITY_CODENAMES)
        # The rights of the area that exist in the database are exactly these.
        existing = Permission.objects.filter(content_type__app_label="community")
        self.assertEqual(set(existing.values_list("codename", flat=True)), COMMUNITY_CODENAMES)

    def test_the_rights_have_readable_names(self):
        labels = {
            permission.codename: permission_label(permission)
            for permission in self.offered().select_related("content_type")
        }
        self.assertEqual(labels["view_location"], "Standort: ansehen")
        self.assertEqual(labels["change_meteringpoint"], "Messpunkt: ändern")
        self.assertEqual(labels["add_transformercircuit"], "Trafokreis: hinzufügen")
        self.assertEqual(labels["change_managementsettings"], "Einstellungen der Verwaltung: ändern")
        self.assertEqual(labels["correct_person_address"], "Person: Adresse korrigieren")
        self.assertEqual(labels["view_personrecord"], "Person: ansehen")

    def test_the_help_names_the_rights_of_the_area(self):
        rows = {str(task): rights for task, rights in rights_help()}
        self.assertEqual(rows["Die Adresse einer Person korrigieren"], "Person: Adresse korrigieren")
        self.assertIn("Standort: ansehen", rows["Personen, Standorte, LEGs und Messpunkte ansehen"])
        self.assertEqual(
            rows["Die Einstellungen der Verwaltung ändern"], "Einstellungen der Verwaltung: ändern"
        )

    def test_the_page_of_a_role_offers_them_to_a_system_administrator(self):
        client = VerifiedClient()
        client.force_login(self.administrator)

        page = client.get(reverse("admin:auth_group_add")).content.decode()

        for text in ("Standort: ansehen", "Messpunkt: löschen", "Person: Adresse korrigieren",
                     "Einstellungen der Verwaltung: ändern", "Trafokreis: ändern"):
            self.assertIn(text, page)
        # Rights of other kinds that no page checks are still not offered.
        self.assertNotIn("Verlaufseintrag", page)
        self.assertNotIn("Personenzuordnung", page)
