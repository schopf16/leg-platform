"""Admin authorization tests (AGENTS.md, section 6; docs/SECURITY.md, section 4)."""

from django.contrib import admin
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import User

PASSWORD = "copper kettle hums beside quiet orchard"


def create_active_user(email):
    user = User.objects.create_user(
        email,
        first_name="Anna",
        last_name="Example",
        street="Examplestrasse",
        house_number="12a",
        town="Bern",
        swiss_postal_code=3000,
    )
    user.set_password(PASSWORD)
    with user._account_change("status"):
        user.status = User.Status.ACTIVE
        user.save()
    return user


@tag("mariadb")
class AdminAuthorizationTests(TestCase):
    def setUp(self):
        self.administrator = create_active_user("admin@example.org")
        self.administrator.set_privileges(is_staff=True, is_superuser=True)
        self.staff_user = create_active_user("staff@example.org")
        self.staff_user.set_privileges(is_staff=True, is_superuser=False)
        permission = Permission.objects.get(
            content_type__app_label="accounts", codename="change_user"
        )
        self.staff_user.user_permissions.add(permission)

    def test_staff_user_cannot_open_group_admin_urls(self):
        self.client.force_login(self.staff_user)
        for url in (reverse("admin:auth_group_changelist"), reverse("admin:auth_group_add")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_staff_user_cannot_change_staff_or_administrator_by_direct_url(self):
        self.client.force_login(self.staff_user)
        for account in (self.staff_user, self.administrator):
            with self.subTest(account=account.pk):
                url = reverse("admin:accounts_user_change", args=(account.pk,))
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse("admin:index"))
                response = self.client.post(
                    url,
                    {"is_staff": "on", "is_superuser": "on", "groups": ["1"]},
                )
                self.assertEqual(response.status_code, 302)
                account.refresh_from_db()
                if account.pk == self.staff_user.pk:
                    self.assertFalse(account.is_superuser)
                else:
                    self.assertTrue(account.is_superuser)

    def test_privilege_fields_are_not_in_staff_or_own_admin_forms(self):
        model_admin = admin.site._registry[User]
        request = RequestFactory().get("/admin/accounts/user/")
        request.user = self.staff_user
        form = model_admin.get_form(request, self.staff_user)
        self.assertTrue(set(model_admin.privilege_fields).isdisjoint(form.base_fields))
        request.user = self.administrator
        form = model_admin.get_form(request, self.administrator)
        self.assertTrue(set(model_admin.privilege_fields).isdisjoint(form.base_fields))

    def test_system_administrator_can_manage_other_accounts_and_groups(self):
        model_admin = admin.site._registry[User]
        request = RequestFactory().get("/admin/accounts/user/")
        request.user = self.administrator
        form = model_admin.get_form(request, self.staff_user)
        self.assertTrue(set(model_admin.privilege_fields).issubset(form.base_fields))
        self.client.force_login(self.administrator)
        self.assertEqual(self.client.get(reverse("admin:auth_group_changelist")).status_code, 200)

    def test_system_administrator_can_promote_another_active_account(self):
        account = create_active_user("member@example.org")
        self.client.force_login(self.administrator)
        response = self.client.post(
            reverse("admin:accounts_user_change", args=(account.pk,)),
            {
                "first_name": "Anna",
                "last_name": "Example",
                "town": "Bern",
                "street": "Examplestrasse",
                "house_number": "12a",
                "swiss_postal_code": "3000",
                "country": "CH",
                "is_staff": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        account.refresh_from_db()
        self.assertTrue(account.is_staff)
        self.assertFalse(account.is_superuser)

    def test_system_administrator_can_assign_groups_and_direct_permissions(self):
        account = create_active_user("member@example.org")
        group = Group.objects.create(name="Admin assignment test group")
        permission = Permission.objects.get(
            content_type__app_label="accounts", codename="view_user"
        )
        self.client.force_login(self.administrator)
        response = self.client.post(
            reverse("admin:accounts_user_change", args=(account.pk,)),
            {
                "first_name": "Anna",
                "last_name": "Example",
                "town": "Bern",
                "street": "Examplestrasse",
                "house_number": "12a",
                "swiss_postal_code": "3000",
                "country": "CH",
                "groups": [str(group.pk)],
                "user_permissions": [str(permission.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        account.refresh_from_db()
        self.assertQuerySetEqual(account.groups.all(), [group])
        self.assertQuerySetEqual(account.user_permissions.all(), [permission])

    def test_system_administrator_cannot_assign_access_to_their_own_account_by_post(self):
        group = Group.objects.create(name="Admin assignment test group")
        permission = Permission.objects.get(
            content_type__app_label="accounts", codename="view_user"
        )
        self.client.force_login(self.administrator)
        response = self.client.post(
            reverse("admin:accounts_user_change", args=(self.administrator.pk,)),
            {
                "first_name": "Anna",
                "last_name": "Example",
                "town": "Bern",
                "street": "Examplestrasse",
                "house_number": "12a",
                "swiss_postal_code": "3000",
                "country": "CH",
                "groups": [str(group.pk)],
                "user_permissions": [str(permission.pk)],
                "is_staff": "on",
                "is_superuser": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.administrator.refresh_from_db()
        self.assertTrue(self.administrator.is_staff)
        self.assertTrue(self.administrator.is_superuser)
        self.assertFalse(self.administrator.groups.exists())
        self.assertFalse(self.administrator.user_permissions.exists())

    def test_staff_user_cannot_assign_groups_permissions_or_privileges_by_post(self):
        account = create_active_user("member@example.org")
        group = Group.objects.create(name="Admin assignment test group")
        permission = Permission.objects.get(
            content_type__app_label="accounts", codename="view_user"
        )
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("admin:accounts_user_change", args=(account.pk,)),
            {
                "first_name": "Anna",
                "last_name": "Example",
                "town": "Bern",
                "street": "Examplestrasse",
                "house_number": "12a",
                "swiss_postal_code": "3000",
                "country": "CH",
                "groups": [str(group.pk)],
                "user_permissions": [str(permission.pk)],
                "is_staff": "on",
                "is_superuser": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        account.refresh_from_db()
        self.assertFalse(account.is_staff)
        self.assertFalse(account.is_superuser)
        self.assertFalse(account.groups.exists())
        self.assertFalse(account.user_permissions.exists())

    def test_admin_is_unavailable_outside_local_development(self):
        from django.test import override_settings

        with override_settings(APP_ENV="production"):
            response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 404)

    def test_staff_cannot_save_account_promoted_after_permission_check(self):
        account = create_active_user("member@example.org")
        stale_account = User.objects.get(pk=account.pk)
        model_admin = admin.site._registry[User]
        request = RequestFactory().post(
            reverse("admin:accounts_user_change", args=(account.pk,))
        )
        request.user = self.staff_user
        form_class = model_admin.get_form(request, stale_account)
        form = form_class(instance=stale_account)

        self.assertTrue(model_admin.has_change_permission(request, stale_account))
        self.assertIn(stale_account, model_admin.get_queryset(request))
        stale_account.first_name = "Changed"
        account.set_privileges(is_staff=True, is_superuser=False)

        with self.assertRaises(PermissionDenied):
            model_admin.save_model(request, stale_account, form, change=True)

        account.refresh_from_db()
        self.assertTrue(account.is_staff)
        self.assertEqual(account.first_name, "Anna")