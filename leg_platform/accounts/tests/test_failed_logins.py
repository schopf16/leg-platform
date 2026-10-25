"""The one entry "Failed logins" (owner decision): superusers see it with both sections, the old lists redirect there,
nobody can add or delete a row, staff with any right see nothing of it."""

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user


class FailedLoginsTests(TestCase):
    def setUp(self):
        self.superuser = create_active_user("failed.logins.superuser@example.org")
        self.superuser.set_privileges(is_staff=True, is_superuser=True)
        self.staff = create_active_user("failed.logins.staff@example.org")
        self.staff.set_privileges(is_staff=True, is_superuser=False)
        self.staff.user_permissions.add(Permission.objects.get(codename="request_directory_import"))

    def client_for(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client

    def test_a_superuser_sees_the_one_entry_and_both_sections(self):
        client = self.client_for(self.superuser)
        index = client.get(reverse("admin:index"))
        self.assertContains(index, reverse("admin:failed_logins"))
        page = client.get(reverse("admin:failed_logins"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="failed-logins-locked"')
        self.assertContains(page, 'id="failed-logins-log"')
        self.assertLess(page.content.index(b'id="failed-logins-locked"'), page.content.index(b'id="failed-logins-log"'))

    def test_staff_with_any_right_does_not_see_it(self):
        client = self.client_for(self.staff)
        self.assertNotContains(client.get(reverse("admin:index")), reverse("admin:failed_logins"))
        self.assertEqual(client.get(reverse("admin:failed_logins")).status_code, 403)

    def test_the_old_lists_redirect_to_the_new_page_for_superusers(self):
        client = self.client_for(self.superuser)
        for name in ("admin:axes_accessattempt_changelist", "admin:axes_accessfailurelog_changelist"):
            with self.subTest(name=name):
                response = client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse("admin:failed_logins"))

    def test_no_add_and_no_delete(self):
        client = self.client_for(self.superuser)
        self.assertEqual(client.get(reverse("admin:axes_accessattempt_add")).status_code, 403)
        self.assertEqual(client.get(reverse("admin:axes_accessfailurelog_add")).status_code, 403)
        self.assertEqual(client.get(reverse("admin:axes_accessattempt_delete", args=(1,))).status_code, 403)
        page = client.get(reverse("admin:failed_logins"))
        self.assertNotContains(page, reverse("admin:axes_accessattempt_add"))
        self.assertNotContains(page, 'name="action"')
