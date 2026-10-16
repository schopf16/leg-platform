"""Exposure of the administration without the temporary local-only gate (D-052).

The administration is protected by the central second-factor check, the forced password
change, the portal login with its abuse protection and the permission checks of each model
admin. These tests walk every URL of the administration instead of a hand-picked list, so that
a later URL added without ``admin_view()`` is noticed (security review of 2026-10-14, C-1, C-2).
"""

import re

from django.test import Client, TestCase, tag
from django.urls import Resolver404, URLResolver, get_resolver, resolve, reverse
from django.urls.resolvers import RoutePattern

from leg_platform.accounts.models import User
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user

# Not routes with converters: the administration's app index patterns are regular expressions.
EXTRA_PATHS = ("/admin/accounts/", "/admin/auth/", "/admin/axes/", "/admin/addresses/")
# Reachable by a verified staff account without any permission: the empty index, the translation
# catalogue and the static "password changed" page; none shows data of the application.
OPEN_TO_VERIFIED_STAFF = {
    ("/admin/", "get"),
    ("/admin/", "post"),
    ("/admin/jsi18n/", "get"),
    ("/admin/password_change/done/", "get"),
}
PUBLIC_ROUTES = ("/admin/login/", "/admin/logout/")
CONVERTER = re.compile(r"<(?:\w+:)?\w+>")


def administration_paths():
    """Return every route of the administration with sample values for its converters."""
    paths = set(EXTRA_PATHS)

    def walk(patterns, prefix):
        for entry in patterns:
            if not isinstance(entry.pattern, RoutePattern):
                continue
            text = CONVERTER.sub("1", prefix + str(entry.pattern))
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, text)
            else:
                paths.add("/" + text)

    for entry in get_resolver().url_patterns:
        if isinstance(entry, URLResolver) and str(entry.pattern) == "admin/":
            walk(entry.url_patterns, "admin/")
    resolvable = set()
    for path in paths:
        try:
            resolve(path)
        except Resolver404:
            continue
        resolvable.add(path)
    return sorted(resolvable)


@tag("mariadb")
class AdministrationExposureTests(TestCase):
    # The walk also opens the pages of the address directory administration, which read it.
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.paths = administration_paths()
        cls.plain = create_active_user("plain.person@example.org")
        cls.staff = create_active_user("staff.person@example.org")
        cls.staff.set_privileges(is_staff=True, is_superuser=False)

    def test_the_walk_covers_the_administration(self):
        # Guards against a walk that silently finds nothing.
        self.assertGreater(len(self.paths), 25)
        for expected in (
            "/admin/",
            "/admin/jsi18n/",
            "/admin/accounts/user/",
            "/admin/accounts/user/1/lifecycle/1/",
            "/admin/accounts/graphconfiguration/send-test/",
            "/admin/accounts/securityevent/",
            "/admin/addresses/importrequest/",
        ):
            self.assertIn(expected, self.paths)

    def protected_paths(self):
        """The routes behind the administration's permission checks.

        The administration's own login and logout routes are public by design: the login only
        redirects to the portal login (``PortalAdminSite.login``) and logout shows a page.
        """
        return [path for path in self.paths if path not in PUBLIC_ROUTES]

    def test_the_public_routes_show_no_administration_data(self):
        for path in PUBLIC_ROUTES:
            for client in (Client(), VerifiedClient()):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertIn(response.status_code, {302, 405})

    def assert_every_path_redirects_to(self, client, location_for):
        for path in self.protected_paths():
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(
                        response["Location"].startswith(location_for),
                        response["Location"],
                    )

    def test_anonymous_visitors_are_sent_to_the_login_on_every_route(self):
        self.assert_every_path_redirects_to(Client(), reverse("admin:login"))

    def test_an_authenticated_account_without_staff_status_is_sent_to_the_login(self):
        client = VerifiedClient()
        client.force_login(self.plain)

        self.assert_every_path_redirects_to(client, reverse("admin:login"))

    def test_a_staff_account_without_a_completed_second_factor_can_only_set_it_up(self):
        client = Client()
        client.force_login(self.staff)

        self.assert_every_path_redirects_to(client, reverse("accounts:two_factor_setup"))

    def test_a_staff_account_without_permissions_gets_no_data_from_any_route(self):
        client = VerifiedClient()
        client.force_login(self.staff)

        for path in self.protected_paths():
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path)
                    if (path, method) in OPEN_TO_VERIFIED_STAFF:
                        self.assertEqual(response.status_code, 200)
                    else:
                        self.assertNotEqual(response.status_code, 200)
        index = client.get("/admin/").content.decode()
        for application in ("/admin/accounts/", "/admin/auth/", "/admin/axes/", "/admin/addresses/"):
            self.assertNotIn(application, index)

    def test_no_account_state_changes_through_a_get_request(self):
        administrator = create_active_user("administrator.person@example.org")
        administrator.set_privileges(is_staff=True, is_superuser=True)
        before = list(
            User.objects.order_by("pk").values_list("pk", "status", "is_staff", "is_superuser")
        )
        client = VerifiedClient()
        client.force_login(administrator)

        for path in self.paths:
            client.get(path)

        self.assertEqual(
            before,
            list(User.objects.order_by("pk").values_list("pk", "status", "is_staff", "is_superuser")),
        )
