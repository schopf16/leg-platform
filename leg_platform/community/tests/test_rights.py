"""No rights for the management area (D-086): every staff account may use it, so the community models create no
permission, none is offered to a role in the administration, and the help of the rights names none."""

from django.contrib.auth.models import Permission
from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.permission_names import assignable_permissions_filter, rights_help
from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community.tests.helpers import staff_user


@tag("mariadb")
class NoRightsForTheManagementAreaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)

    def test_no_right_of_the_community_exists_or_is_offered_to_a_role(self):
        self.assertFalse(Permission.objects.filter(content_type__app_label="community").exists())
        self.assertFalse(
            Permission.objects.filter(assignable_permissions_filter(), content_type__app_label="community").exists()
        )

    def test_the_help_names_only_rights_of_the_accounts_and_the_portal(self):
        rights = {right for _task, right in rights_help()}

        self.assertEqual(
            rights,
            {
                "Konto: ansehen", "Konto: ändern", "Mailtext: ansehen", "Mailtext: ändern",
                "Portal-Einstellungen: ansehen", "Portal-Einstellungen: ändern",
            },
        )

    def test_the_page_of_a_role_offers_the_rights_of_the_accounts_and_the_portal_only(self):
        client = VerifiedClient()
        client.force_login(self.administrator)

        page = client.get(reverse("admin:auth_group_add")).content.decode()

        for text in ("Konto: ansehen", "Mailtext: ändern", "Portal-Einstellungen: ändern"):
            self.assertIn(text, page)
        for text in ("Standort: ansehen", "Messpunkt: löschen", "Person: Adresse korrigieren",
                     "Einstellungen der Verwaltung: ändern", "Trafokreis: ändern", "LEG: ansehen"):
            self.assertNotIn(text, page)
