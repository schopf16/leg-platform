"""The admin groups as collapsible blocks in the spec's order (admin spec section 2)."""

from django.test import TestCase
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user


class AdminGroupTests(TestCase):
    def setUp(self):
        self.superuser = create_active_user("admin.groups.superuser@example.org")
        self.superuser.set_privileges(is_staff=True, is_superuser=True)
        self.client_obj = VerifiedClient()
        self.client_obj.force_login(self.superuser)

    def test_the_groups_are_collapsible_blocks(self):
        response = self.client_obj.get(reverse("admin:index"))
        html = response.content.decode("utf-8")
        self.assertIn('<details class="admin-group app-system" open>', html)
        self.assertIn('<details class="admin-group app-users" open>', html)

    def test_the_system_entries_are_in_the_spec_order(self):
        html = self.client_obj.get(reverse("admin:index")).content.decode("utf-8")
        urls = [
            reverse("admin:addresses_importrequest_changelist"),
            reverse("admin:addresses_housekeepingstatus_changelist"),
            reverse("admin:logmenu"),
            reverse("admin:logs"),
            reverse("admin:failed_logins"),
        ]
        positions = [html.index(f'href="{url}"') for url in urls]
        self.assertEqual(positions, sorted(positions))
