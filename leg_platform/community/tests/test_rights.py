"""Rights of the management data (D-086, D-091): the management area itself uses none; the plain tables of the
administration have a "view" right each, offered to a role, and nothing else (no add, change or delete)."""

from django.contrib.auth.models import Permission
from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.permission_names import assignable_permissions_filter, rights_help
from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community.tests.helpers import staff_user

TABLES = (
    "historyentry", "leg", "legassignment", "location", "managementsettings", "meteringpoint",
    "personassignment", "personrecord", "sharechange", "transformercircuit",
)


@tag("mariadb")
class RightsOfTheManagementDataTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)

    def test_the_only_rights_of_the_community_are_the_view_rights_of_the_tables(self):
        codenames = set(
            Permission.objects.filter(content_type__app_label="community").values_list("codename", flat=True)
        )
        self.assertEqual(codenames, {f"view_{name}" for name in TABLES})

    def test_exactly_these_rights_are_offered_to_a_role(self):
        offered = set(
            Permission.objects.filter(assignable_permissions_filter(), content_type__app_label="community")
            .values_list("codename", flat=True)
        )
        self.assertEqual(offered, {f"view_{name}" for name in TABLES})

    def test_nothing_of_the_grid_operator_or_the_list_preferences_is_offered(self):
        everything = Permission.objects.filter(assignable_permissions_filter())
        self.assertFalse(everything.filter(content_type__model__in=("gridoperator", "listpreference")).exists())
        self.assertFalse(everything.filter(content_type__app_label="community").exclude(codename__startswith="view_").exists())

    def test_the_help_names_the_rights_of_the_accounts_the_portal_and_the_tables(self):
        rights = {right for _task, right in rights_help()}

        for expected in (
            "Konto: ansehen", "Konto: ändern", "Mailtext: ansehen", "Mailtext: ändern",
            "Portal-Einstellungen: ansehen", "Portal-Einstellungen: ändern",
            "Standort: ansehen", "Messpunkt: ansehen", "LEG: ansehen", "Trafokreis: ansehen",
        ):
            self.assertIn(expected, rights)
        # No right to change or delete anything of the management data.
        for right in rights:
            self.assertNotIn("Standort: ändern", right)
            self.assertNotIn("Messpunkt: löschen", right)

    def test_the_page_of_a_role_offers_the_view_rights_of_the_tables_and_nothing_to_change_them(self):
        client = VerifiedClient()
        client.force_login(self.administrator)

        page = client.get(reverse("admin:auth_group_add")).content.decode()

        for text in ("Konto: ansehen", "Mailtext: ändern", "Portal-Einstellungen: ändern", "Standort: ansehen",
                     "Messpunkt: ansehen", "LEG: ansehen", "Trafokreis: ansehen"):
            self.assertIn(text, page)
        for text in ("Standort: ändern", "Standort: löschen", "Messpunkt: löschen", "Person: Adresse korrigieren",
                     "Einstellungen der Verwaltung: ändern", "Trafokreis: ändern", "LEG: hinzufügen"):
            self.assertNotIn(text, page)
