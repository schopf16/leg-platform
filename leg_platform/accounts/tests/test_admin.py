"""Admin authorization tests (AGENTS.md, section 6; docs/SECURITY.md, section 4)."""

import re
from unittest import mock

from axes.models import AccessAttempt, AccessFailureLog
from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.admin.utils import flatten_fieldsets
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import translation

from leg_platform.accounts.admin import SECOND_FACTOR_REQUIRED_MESSAGE
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    RecoveryCodeDevice,
    SecurityEvent,
    User,
)
from leg_platform.accounts.tests.helpers import VerifiedClient

PASSWORD = "copper kettle hums beside quiet orchard"


def german_message():
    with translation.override("de-ch"):
        return str(SECOND_FACTOR_REQUIRED_MESSAGE)


# The fields of the account form that every change in these tests sends unchanged.
PROFILE_DATA = {
    "first_name": "Anna",
    "last_name": "Example",
    "town": "Bern",
    "street": "Examplestrasse",
    "house_number": "12a",
    "postal_code": "3000",
    "country": "CH",
}


def create_active_user(email, customer_number=True):
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
    if customer_number:
        user.assign_customer_number()
    return user


@tag("mariadb")
class AdminAuthorizationTests(TestCase):
    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("admin@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)
        cls.staff_user = create_active_user("staff@example.org")
        cls.staff_user.set_privileges(is_staff=True, is_superuser=False)
        permission = Permission.objects.get(
            content_type__app_label="accounts", codename="change_user"
        )
        cls.staff_user.user_permissions.add(permission)

    def test_staff_user_cannot_open_group_admin_urls(self):
        self.client.force_login(self.staff_user)
        for url in (reverse("admin:auth_group_changelist"), reverse("admin:auth_group_add")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_axes_lockout_records_are_system_administrator_only_and_read_only(self):
        model_request = RequestFactory().get("/admin/")
        for model in (AccessAttempt, AccessFailureLog):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                model_request.user = self.staff_user
                self.assertFalse(model_admin.has_view_permission(model_request))
                model_request.user = self.administrator
                self.assertTrue(model_admin.has_view_permission(model_request))
                self.assertFalse(model_admin.has_add_permission(model_request))
                self.assertFalse(model_admin.has_change_permission(model_request))
                self.assertFalse(model_admin.has_delete_permission(model_request))
                self.assertIsNone(model_admin.actions)

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
        AuthenticatorDevice.objects.create_device(account, confirmed=True)
        self.client.force_login(self.administrator)
        response = self.client.post(
            reverse("admin:accounts_user_change", args=(account.pk,)),
            {**PROFILE_DATA, "is_staff": "on"},
        )
        self.assertEqual(response.status_code, 302)
        account.refresh_from_db()
        self.assertTrue(account.is_staff)
        self.assertFalse(account.is_superuser)

    def test_an_account_without_a_second_factor_cannot_receive_rights(self):
        # Otherwise whoever knows the password could set up their own authenticator app at the
        # next login (D-042). An unconfirmed app or recovery codes alone do not count.
        account = create_active_user("member@example.org")
        AuthenticatorDevice.objects.create_device(account, confirmed=False)
        RecoveryCodeDevice.objects.create(user=account, name="recovery codes", confirmed=True)
        self.client.force_login(self.administrator)
        url = reverse("admin:accounts_user_change", args=(account.pk,))
        for privileges in ({"is_staff": "on"}, {"is_staff": "on", "is_superuser": "on"}):
            with self.subTest(privileges=privileges):
                response = self.client.post(url, {**PROFILE_DATA, **privileges})

                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response, "Nur Konten mit eingerichteter Zwei-Faktor-Anmeldung (2FA)"
                )
                self.assertEqual(
                    response.context["adminform"].form.errors["is_staff"], [german_message()]
                )
                account.refresh_from_db()
                self.assertFalse(account.is_staff or account.is_superuser)
        self.assertFalse(SecurityEvent.objects.filter(account_id=account.pk).exists())

    def test_a_staff_user_without_a_second_factor_cannot_become_a_system_administrator(self):
        # E.g. a staff user whose second factor was reset, or promoted before the rule.
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.staff_user).exists())
        self.client.force_login(self.administrator)

        response = self.client.post(
            reverse("admin:accounts_user_change", args=(self.staff_user.pk,)),
            {**PROFILE_DATA, "is_staff": "on", "is_superuser": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["adminform"].form.errors["is_superuser"], [german_message()]
        )
        self.staff_user.refresh_from_db()
        self.assertFalse(self.staff_user.is_superuser)

    def test_rights_can_still_be_removed_from_an_account_without_a_second_factor(self):
        self.client.force_login(self.administrator)

        response = self.client.post(
            reverse("admin:accounts_user_change", args=(self.staff_user.pk,)), PROFILE_DATA
        )

        self.assertEqual(response.status_code, 302)
        self.staff_user.refresh_from_db()
        self.assertFalse(self.staff_user.is_staff)

    def test_a_second_factor_removed_after_the_form_check_refuses_the_promotion(self):
        account = create_active_user("member@example.org")
        device = AuthenticatorDevice.objects.create_device(account, confirmed=True)
        model_admin = admin.site._registry[User]
        request = RequestFactory().post(reverse("admin:accounts_user_change", args=(account.pk,)))
        request.user = self.administrator
        form = model_admin.get_form(request, account)(
            {**PROFILE_DATA, "is_staff": "on"}, instance=account
        )
        self.assertTrue(form.is_valid(), form.errors)
        device.delete()

        with self.assertRaises(PermissionDenied):
            model_admin.save_model(request, form.instance, form, change=True)

        account.refresh_from_db()
        self.assertFalse(account.is_staff)

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
                "postal_code": "3000",
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
                "postal_code": "3000",
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
                "postal_code": "3000",
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

    def test_the_administration_does_not_depend_on_the_environment_name(self):
        # The temporary local-only gate is gone (D-052): what protects the administration
        # (login, second factor, permissions) is the same in every environment; the walk of
        # every route is in test_admin_exposure.py.
        from django.test import Client, override_settings

        for app_env in ("local", "stage", "production"):
            with self.subTest(app_env=app_env), override_settings(APP_ENV=app_env):
                anonymous = Client().get("/admin/")
                self.assertEqual(anonymous.status_code, 302)
                self.assertTrue(anonymous["Location"].startswith(reverse("admin:login")))
                unverified = Client()
                unverified.force_login(self.staff_user)
                self.assertRedirects(
                    unverified.get("/admin/"),
                    reverse("accounts:two_factor_setup"),
                    fetch_redirect_response=False,
                )
        # Only after the loop: the verified login creates the staff user's second factor.
        for app_env in ("local", "stage", "production"):
            with self.subTest(app_env=app_env, verified=True), override_settings(APP_ENV=app_env):
                self.client.force_login(self.staff_user)
                self.assertEqual(self.client.get("/admin/").status_code, 200)

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


@tag("mariadb")
class AdminPresentationTests(TestCase):
    """Titles, list columns, search and messages of the account admin (D-036)."""

    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("admin@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)
        cls.member = create_active_user("member@example.org")
        cls.pending = User.objects.create_user("pending@example.org")

    def test_site_titles_do_not_name_django_and_are_the_name_of_the_instance(self):
        with translation.override("en"):
            self.assertEqual(str(admin.site.index_title), "Administration")
        with translation.override("de-ch"):
            self.assertEqual(str(admin.site.index_title), "Verwaltung")
            self.client.force_login(self.administrator)
            with override_settings(INSTANCE_NAME="LEG-Portal"):
                content = self.client.get(reverse("admin:index")).content.decode()
        self.assertNotIn("Django", content)
        self.assertIn("Verwaltung", content)
        # One name for the portal and its administration (WL-23).
        self.assertIn("<title>Verwaltung | LEG-Portal</title>", content)
        self.assertIn("LEG-Portal", content)
        self.assertNotIn("LEG-Plattform", content)

    def test_the_start_page_has_three_groups_and_the_hint(self):
        self.client.force_login(self.administrator)

        with translation.override("de-ch"):
            content = self.client.get(reverse("admin:index")).content.decode()

        groups = re.findall(r'<a href="[^"]*" class="section"[^>]*>([^<]+)</a>', content)
        self.assertEqual(groups, ["Benutzer", "Inhalte", "System"])
        self.assertIn(
            "Name, Logo, Farben und Adressen des Portals sowie die Texte der E-Mails pflegen Sie unter Inhalte.",
            content,
        )
        for name in ("Mailtexte", "Portal-Einstellungen", "Gesperrte und fehlgeschlagene Anmeldungen", "Sicherheitsprotokoll"):
            self.assertIn(name, content)
        # No product name of a package as a heading.
        self.assertNotIn(">Axes<", content)

    def test_the_groups_follow_the_rights_of_the_account(self):
        staff = create_active_user("staff@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        self.client.force_login(staff)

        with translation.override("de-ch"):
            content = self.client.get(reverse("admin:index")).content.decode()

        # A staff user without rights sees no group at all, and never the system group.
        self.assertNotIn("Sicherheitsprotokoll", content)
        self.assertNotIn("Gesperrte und fehlgeschlagene Anmeldungen", content)
        self.assertNotIn(">System<", content)

    def test_the_hint_is_shown_only_to_an_account_that_sees_the_group_content(self):
        from django.contrib.auth.models import Permission

        def hint_for(codename):
            staff = create_active_user(f"{codename}@example.org")
            staff.set_privileges(is_staff=True, is_superuser=False)
            staff.user_permissions.set(Permission.objects.filter(codename=codename))
            client = VerifiedClient()
            client.force_login(staff)
            with translation.override("de-ch"):
                return client.get(reverse("admin:index")).content.decode()

        only_users = hint_for("view_user")
        with_content = hint_for("change_emailtext")

        self.assertIn("Konten", only_users)
        self.assertNotIn("pflegen Sie unter Inhalte", only_users)
        self.assertIn("pflegen Sie unter Inhalte", with_content)

    def test_the_page_of_one_app_keeps_the_list_of_that_app(self):
        self.client.force_login(self.administrator)

        response = self.client.get(reverse("admin:app_list", args=["accounts"]))

        self.assertEqual(response.status_code, 200)

    def test_admin_pages_need_no_inline_code(self):
        # The pages must work under the Content Security Policy: no inline scripts or styles,
        # style attributes or event handler attributes (docs/SECURITY.md, section 6).
        self.client.force_login(self.administrator)
        for url in (reverse("admin:index"), reverse("admin:accounts_user_changelist")):
            with self.subTest(url=url):
                response = self.client.get(url)
                html = response.content.decode()
                self.assertEqual(response.status_code, 200)
                self.assertIn("default-src 'self'", response["Content-Security-Policy"])
                for script in re.findall(r"<script\b[^>]*>", html):
                    self.assertIn("src=", script)
                self.assertNotRegex(html, r"<style\b")
                self.assertNotRegex(html, r"\sstyle\s*=")
                self.assertNotRegex(html, r"\son[a-z]+\s*=")
                self.assertNotIn("javascript:", html)

    def test_user_list_shows_the_customer_number_instead_of_the_id(self):
        model_admin = admin.site._registry[User]
        self.assertNotIn("id", model_admin.list_display)
        self.assertEqual(model_admin.list_display[0], "customer_number_column")
        self.client.force_login(self.administrator)
        with translation.override("de-ch"):
            response = self.client.get(reverse("admin:accounts_user_changelist"))
        content = response.content.decode()
        self.assertIn(self.member.customer_number_display, content)
        self.assertIn("Kundennummer", content)
        self.assertNotIn('class="column-id', content)

    def test_the_user_list_warns_system_administrators_when_customer_numbers_run_out(self):
        url = reverse("admin:accounts_user_changelist")
        self.client.force_login(self.administrator)
        with translation.override("de-ch"):
            quiet = self.client.get(url).content.decode()
            with mock.patch(
                "leg_platform.accounts.admin.customer_numbers_nearly_exhausted", return_value=True
            ):
                warned = self.client.get(url).content.decode()

        self.assertNotIn("Die Kundennummern gehen aus", quiet)
        self.assertIn("Die Kundennummern gehen aus", warned)
        self.assertIn("von 1000000 sind vergeben", warned)

    def test_the_customer_number_warning_is_not_shown_to_staff_users(self):
        staff_user = create_active_user("staff@example.org")
        staff_user.set_privileges(is_staff=True, is_superuser=False)
        staff_user.user_permissions.add(
            Permission.objects.get(content_type__app_label="accounts", codename="view_user")
        )
        self.client.force_login(staff_user)
        with translation.override("de-ch"):
            with mock.patch(
                "leg_platform.accounts.admin.customer_numbers_nearly_exhausted", return_value=True
            ):
                content = self.client.get(reverse("admin:accounts_user_changelist")).content.decode()

        self.assertNotIn("Die Kundennummern gehen aus", content)

    def test_user_list_sorts_by_customer_number(self):
        self.client.force_login(self.administrator)
        # "o" is the index of the column in list_display (no action column: no bulk actions).
        for order, descending in (("0", False), ("-0", True)):
            with self.subTest(order=order):
                response = self.client.get(
                    reverse("admin:accounts_user_changelist"), {"o": order}
                )
                self.assertEqual(response.status_code, 200)
                numbers = [
                    user.customer_number_id
                    for user in response.context["cl"].result_list
                    if user.customer_number_id
                ]
                self.assertEqual(len(numbers), 2)
                self.assertEqual(numbers, sorted(numbers, reverse=descending))

    def test_search_by_customer_number(self):
        self.client.force_login(self.administrator)
        url = reverse("admin:accounts_user_changelist")
        for query in (self.member.customer_number_id, self.member.customer_number_display):
            with self.subTest(query=query):
                response = self.client.get(url, {"q": query})
                self.assertEqual(response.status_code, 200)
                self.assertIn(self.member, response.context["cl"].result_list)

    def test_staff_search_by_customer_number_does_not_find_staff_accounts(self):
        staff_user = create_active_user("staff@example.org")
        staff_user.set_privileges(is_staff=True, is_superuser=False)
        staff_user.user_permissions.add(
            Permission.objects.get(content_type__app_label="accounts", codename="view_user")
        )
        self.client.force_login(staff_user)
        response = self.client.get(
            reverse("admin:accounts_user_changelist"),
            {"q": self.administrator.customer_number_id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["cl"].result_list), [])

    def test_change_form_shows_the_customer_number(self):
        self.client.force_login(self.administrator)
        with translation.override("de-ch"):
            response = self.client.get(
                reverse("admin:accounts_user_change", args=(self.member.pk,))
            )
        content = response.content.decode()
        self.assertIn(self.member.customer_number_display, content)
        self.assertIn(f"Konto {self.member.customer_number_display}", content)
        for hidden in ("Titel", "Namenszusatz", "Wohnungsnummer", "Ortschaft", "Postfachtext",
                       "Adresszeile", "Schweizer Postleitzahl", "Ausländische Postleitzahl",
                       "Normalisiert", "Django"):
            self.assertNotIn(hidden, content)

    def test_privilege_messages_use_the_administration_wording(self):
        self.client.force_login(self.administrator)
        data = {
            "first_name": "Anna",
            "last_name": "Example",
            "town": "Bern",
            "street": "Examplestrasse",
            "postal_code": "3000",
            "country": "CH",
        }
        with translation.override("de-ch"):
            response = self.client.post(
                reverse("admin:accounts_user_change", args=(self.member.pk,)),
                {**data, "is_superuser": "on"},
            )
            self.assertContains(
                response, "Ein Systemadministrator braucht auch Zugang zur Verwaltung."
            )
            response = self.client.post(
                reverse("admin:accounts_user_change", args=(self.pending.pk,)),
                {**data, "is_staff": "on"},
            )
            self.assertContains(
                response, "Nur aktive Konten können Zugang zur Verwaltung erhalten."
            )
        self.member.refresh_from_db()
        self.pending.refresh_from_db()
        self.assertFalse(self.member.is_staff or self.member.is_superuser)
        self.assertFalse(self.pending.is_staff)

    def test_profile_messages_in_the_change_form(self):
        self.client.force_login(self.administrator)
        with translation.override("de-ch"):
            response = self.client.post(
                reverse("admin:accounts_user_change", args=(self.member.pk,)),
                {"first_name": "Anna", "last_name": "Example", "town": "Bern",
                 "street": "", "postal_code": "300", "country": "CH"},
            )
        self.assertContains(response, "Geben Sie eine Strasse oder ein Postfach ein.")
        self.assertContains(
            response, "Geben Sie eine Postleitzahl mit 4 Ziffern von 1000 bis 9999 ein."
        )

    def test_duplicate_group_name_message_is_plain(self):
        Group.objects.create(name="Example group")
        self.client.force_login(self.administrator)
        with translation.override("de-ch"):
            response = self.client.post(reverse("admin:auth_group_add"), {"name": "Example group"})
        self.assertContains(response, "Gruppe: Dieser Wert für «Name» wird bereits verwendet.")


HIDDEN_FIELDS = {
    "title",
    "organisation_name_add_on_1",
    "organisation_name_add_on_2",
    "address_line_2",
    "dwelling_number",
    "post_office_box_text",
    "locality",
    "swiss_postal_code",
    "foreign_postal_code",
}


@tag("mariadb")
class AdminPostalCodeTests(TestCase):
    """One postal code form field instead of the two eCH-0010 fields (D-036)."""

    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("admin@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)
        cls.member = create_active_user("member@example.org")
        cls.url = reverse("admin:accounts_user_change", args=(cls.member.pk,))

    def setUp(self):
        self.client.force_login(self.administrator)

    def post(self, country, postal_code):
        return self.client.post(
            self.url,
            {
                "first_name": "Anna",
                "last_name": "Example",
                "town": "Bern",
                "street": "Examplestrasse",
                "country": country,
                "postal_code": postal_code,
            },
        )

    def stored(self):
        self.member.refresh_from_db()
        return self.member.swiss_postal_code, self.member.foreign_postal_code

    def test_hidden_fields_are_not_in_the_form(self):
        model_admin = admin.site._registry[User]
        request = RequestFactory().get("/admin/accounts/user/")
        request.user = self.administrator
        form = model_admin.get_form(request, self.member)
        self.assertEqual(HIDDEN_FIELDS & set(form.base_fields), set())
        self.assertIn("postal_code", form.base_fields)
        shown = set(flatten_fieldsets(model_admin.get_fieldsets(request, self.member)))
        self.assertEqual(HIDDEN_FIELDS & shown, set())

    def test_saving_the_form_keeps_hidden_values(self):
        hidden_values = {
            "title": "Dr.",
            "organisation_name_add_on_1": "Add-on 1",
            "organisation_name_add_on_2": "Add-on 2",
            "address_line_2": "Second line",
            "dwelling_number": "5",
            "post_office_box_text": "Postfach",
            "locality": "Example locality",
        }
        for name, value in hidden_values.items():
            setattr(self.member, name, value)
        self.member.save()
        self.assertEqual(self.post("CH", "3063").status_code, 302)
        self.member.refresh_from_db()
        for name, value in hidden_values.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(self.member, name), value)
        self.assertEqual(self.member.swiss_postal_code, 3063)

    def test_initial_value_comes_from_the_stored_postal_code(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["adminform"].form["postal_code"].value(), "3000")
        self.post("DE", "10115")
        response = self.client.get(self.url)
        self.assertEqual(response.context["adminform"].form["postal_code"].value(), "10115")

    def test_switzerland_writes_the_swiss_postal_code_and_clears_the_foreign_one(self):
        self.assertEqual(self.post("DE", "10115").status_code, 302)
        self.assertEqual(self.stored(), (None, "10115"))
        self.assertEqual(self.post("CH", " 3063 ").status_code, 302)
        self.assertEqual(self.stored(), (3063, ""))

    def test_other_countries_write_the_foreign_postal_code(self):
        for country, value in (("DE", "10115"), ("LI", "9490"), ("GB", "SW1A 1AA")):
            with self.subTest(country=country):
                self.assertEqual(self.post(country, value).status_code, 302)
                self.assertEqual(self.stored(), (None, value))

    def test_invalid_postal_codes_are_reported_on_the_form_field(self):
        cases = (
            ("CH", "", "Geben Sie eine Postleitzahl mit 4 Ziffern von 1000 bis 9999 ein."),
            ("CH", "999", "Geben Sie eine Postleitzahl mit 4 Ziffern von 1000 bis 9999 ein."),
            ("CH", "10115", "Geben Sie eine Postleitzahl mit 4 Ziffern von 1000 bis 9999 ein."),
            ("CH", "30a0", "Geben Sie eine Postleitzahl mit 4 Ziffern von 1000 bis 9999 ein."),
            ("DE", "", "Bitte füllen Sie dieses Feld aus."),
            ("LI", "  ", "Bitte füllen Sie dieses Feld aus."),
        )
        for country, value, message in cases:
            with self.subTest(country=country, value=value):
                with translation.override("de-ch"):
                    response = self.post(country, value)
                self.assertEqual(response.status_code, 200)
                form = response.context["adminform"].form
                self.assertEqual(form.errors["postal_code"], [message])
                self.assertEqual(set(form.errors), {"postal_code"})
                self.assertEqual(self.stored(), (3000, ""))

    def test_view_only_staff_user_sees_the_postal_code(self):
        staff_user = create_active_user("staff@example.org")
        staff_user.set_privileges(is_staff=True, is_superuser=False)
        staff_user.user_permissions.add(
            Permission.objects.get(content_type__app_label="accounts", codename="view_user")
        )
        self.client.force_login(staff_user)
        with translation.override("de-ch"):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Postleitzahl")
        self.assertContains(response, "3000")
        response = self.client.post(self.url, {"country": "DE", "postal_code": "10115"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stored(), (3000, ""))



@tag("mariadb")
class AdminLogEntryTests(TestCase):
    """Admin log entries of accounts store a neutral text with the internal number (D-018, D-036)."""

    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("admin@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)
        cls.member = create_active_user("member@example.org")

    def setUp(self):
        self.client.force_login(self.administrator)
        self.model_admin = admin.site._registry[User]
        self.request = RequestFactory().post("/admin/accounts/user/")
        self.request.user = self.administrator

    def assert_neutral(self, entry, account):
        self.assertEqual(entry.object_repr, f"Konto mit interner Nummer {account.pk}")
        self.assertEqual(str(entry.object_id), str(account.pk))
        for personal in (
            account.customer_number_id,
            account.customer_number_display,
            account.email,
            account.first_name,
            account.last_name,
        ):
            self.assertNotIn(personal, entry.object_repr)

    def test_change_through_the_admin_stores_a_neutral_entry(self):
        response = self.client.post(
            reverse("admin:accounts_user_change", args=(self.member.pk,)),
            {
                "first_name": "Berta",
                "last_name": "Example",
                "town": "Bern",
                "street": "Examplestrasse",
                "house_number": "12a",
                "country": "CH",
                "postal_code": "3000",
            },
        )
        self.assertEqual(response.status_code, 302)
        entry = LogEntry.objects.get()
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertEqual(entry.user, self.administrator)
        self.member.refresh_from_db()
        self.assert_neutral(entry, self.member)
        # The change message names the changed fields, never their values.
        self.assertEqual(entry.change_message, '[{"changed": {"fields": ["First name"]}}]')

    def test_every_logging_hook_stores_a_neutral_entry(self):
        entry = self.model_admin.log_addition(self.request, self.member, [{"added": {}}])
        self.assertEqual(entry.action_flag, ADDITION)
        self.assert_neutral(entry, self.member)
        entry = self.model_admin.log_change(self.request, self.member, "")
        self.assertEqual(entry.action_flag, CHANGE)
        self.assert_neutral(entry, self.member)
        entries = self.model_admin.log_deletions(
            self.request, User.objects.filter(pk__in=[self.member.pk, self.administrator.pk])
        )
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(entry.action_flag, DELETION)
        accounts = {self.member.pk: self.member, self.administrator.pk: self.administrator}
        for entry in LogEntry.objects.filter(action_flag=DELETION):
            self.assert_neutral(entry, accounts[int(entry.object_id)])

    def test_stored_text_does_not_depend_on_the_viewer_language(self):
        with translation.override("en"):
            entry = self.model_admin.log_change(self.request, self.member, "")
        self.assert_neutral(entry, self.member)

    def test_recent_actions_and_history_show_the_neutral_text(self):
        self.model_admin.log_change(self.request, self.member, "")
        with translation.override("de-ch"):
            index = self.client.get(reverse("admin:index")).content.decode()
            history = self.client.get(
                reverse("admin:accounts_user_history", args=(self.member.pk,))
            ).content.decode()
        self.assertIn(f"Konto mit interner Nummer {self.member.pk}", index)
        self.assertNotIn(self.member.customer_number_display, index)
        # The history page names the actor, read from the account at display time.
        self.assertIn(self.administrator.email, history)

    def test_change_form_shows_the_internal_number(self):
        with translation.override("de-ch"):
            response = self.client.get(
                reverse("admin:accounts_user_change", args=(self.member.pk,))
            )
        content = response.content.decode()
        self.assertIn("Interne Nummer:", content)
        self.assertNotIn(">ID:<", content)
        self.assertIn(f'<div class="readonly">{self.member.pk}</div>', content)


@tag("mariadb")
class AdminProtectedFieldTests(TestCase):
    """Fields that are hidden or read-only in the admin cannot be changed by POST (D-036)."""

    client_class = VerifiedClient

    def test_hidden_and_read_only_fields_ignore_posted_values(self):
        administrator = create_active_user("admin@example.org")
        administrator.set_privileges(is_staff=True, is_superuser=True)
        member = create_active_user("member@example.org")
        before = User.objects.values().get(pk=member.pk)
        self.client.force_login(administrator)
        response = self.client.post(
            reverse("admin:accounts_user_change", args=(member.pk,)),
            {
                "first_name": "Anna",
                "last_name": "Example",
                "town": "Bern",
                "street": "Examplestrasse",
                "house_number": "12a",
                "country": "CH",
                "postal_code": "3000",
                "title": "Dr.",
                "locality": "Elsewhere",
                "swiss_postal_code": "9999",
                "foreign_postal_code": "X",
                "customer_number": "000001",
                "retention_obligation": "on",
                "email": "attacker@example.org",
                "normalized_email": "attacker@example.org",
                "status": "blocked",
                "is_active": "",
                "id": "999",
            },
        )
        self.assertEqual(response.status_code, 302)
        after = User.objects.values().get(pk=member.pk)
        self.assertEqual(after, before)


@tag("mariadb")
class AdminHistoryAccessTests(TestCase):
    """The history of accounts is shown only to system administrators (N-1, D-036)."""

    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("admin@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)
        cls.member = create_active_user("member@example.org")
        cls.history_url = reverse("admin:accounts_user_history", args=(cls.member.pk,))
        cls.change_url = reverse("admin:accounts_user_change", args=(cls.member.pk,))

    def create_staff_user(self, *codenames):
        staff_user = create_active_user("staff@example.org")
        staff_user.set_privileges(is_staff=True, is_superuser=False)
        for app_label, codename in codenames:
            staff_user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        return staff_user

    def test_staff_users_cannot_open_the_account_history(self):
        staff_user = self.create_staff_user()
        self.client.force_login(staff_user)
        missing = reverse("admin:accounts_user_history", args=(999999,))
        for codename in ("view_user", "change_user"):
            with self.subTest(codename=codename):
                staff_user.user_permissions.set(
                    [Permission.objects.get(content_type__app_label="accounts", codename=codename)]
                )
                self.assertEqual(self.client.get(self.history_url).status_code, 403)
                self.assertEqual(self.client.get(missing).status_code, 403)

    def test_staff_users_see_no_history_link(self):
        self.client.force_login(self.create_staff_user(("accounts", "change_user")))
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "historylink")
        self.assertNotContains(response, self.history_url)

    def test_system_administrator_sees_the_history_with_swiss_german_labels(self):
        self.client.force_login(self.administrator)
        response = self.client.post(
            self.change_url,
            {
                "first_name": "Berta",
                "last_name": "Muster",
                "town": "Bern",
                "street": "Examplestrasse",
                "house_number": "12a",
                "country": "CH",
                "postal_code": "3063",
            },
        )
        self.assertEqual(response.status_code, 302)
        with translation.override("de-ch"):
            self.assertContains(self.client.get(self.change_url), "historylink")
            response = self.client.get(self.history_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vorname, Nachname und Postleitzahl geändert.")
        self.assertContains(response, self.administrator.email)

    def test_group_history_is_refused_to_staff_users(self):
        group = Group.objects.create(name="Example group")
        staff_user = self.create_staff_user(("auth", "view_group"), ("auth", "change_group"))
        self.client.force_login(staff_user)
        response = self.client.get(reverse("admin:auth_group_history", args=(group.pk,)))
        self.assertEqual(response.status_code, 403)

    def test_recent_actions_of_staff_users_show_no_other_actor(self):
        model_admin = admin.site._registry[User]
        request = RequestFactory().post("/")
        request.user = self.administrator
        model_admin.log_change(request, self.member, "")
        self.client.force_login(self.create_staff_user(("accounts", "view_user")))
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.administrator.email)
        self.assertNotContains(response, f"Konto mit interner Nummer {self.member.pk}")
