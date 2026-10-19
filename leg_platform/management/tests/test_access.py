"""Access to the management area (D-077, D-086): every URL denies the anonymous visitor, a person without
staff status, a blocked account and a session without the second factor; every other staff account may use
every page (there is no permission of the area). The URL configuration is walked, not a hand-picked list,
so that a later route without the access check is noticed (the equivalent of the walk of the
administration URLs, ``accounts/tests/test_admin_exposure.py``)."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, tag
from django.urls import URLResolver, get_resolver, resolve, reverse
from django.urls.resolvers import RoutePattern

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community.tests.helpers import staff_user
from leg_platform.management import access, navigation
from leg_platform.management.templatetags import management_ui

User = get_user_model()
CONVERTER = re.compile(r"<(?:\w+:)?\w+>")
LOGIN = reverse("accounts:login")


def management_routes():
    """``[(path, callback)]`` of every route under ``management/``, converters replaced by 1."""
    found = []

    def walk(patterns, prefix):
        for entry in patterns:
            if not isinstance(entry.pattern, RoutePattern):
                raise AssertionError(f"A route that is not a plain route: {prefix}{entry.pattern}")
            text = CONVERTER.sub("1", prefix + str(entry.pattern))
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, text)
            else:
                found.append(("/" + text, entry.callback))

    for entry in get_resolver().url_patterns:
        if isinstance(entry, URLResolver) and str(entry.pattern) == "management/":
            walk(entry.url_patterns, "management/")
    return found


ROUTES = management_routes()
PATHS = [path for path, _callback in ROUTES]


class FakeUser:
    """An account as the access check and the menu see it (no database)."""

    def __init__(self, staff=True, active=True, authenticated=True, verified=True, perms=()):
        self.is_staff, self.is_active, self.is_authenticated = staff, active, authenticated
        self.verified, self.perms = verified, set(perms)

    def is_verified(self):
        return self.verified

    def has_perm(self, name):
        return name in self.perms


class RouteWalkTests(SimpleTestCase):
    def test_the_walk_finds_the_routes_of_the_area(self):
        self.assertIn("/management/", PATHS)
        self.assertIn("/management/settings/", PATHS)
        for path in PATHS:
            self.assertEqual(resolve(path).view_name.split(":")[0], "management")

    def test_every_route_has_the_access_check(self):
        # The rule for developers: a new management view uses ManagementAccessMixin or
        # @management_view, or this test fails.
        unprotected = [path for path, callback in ROUTES if not access.is_protected(callback)]
        self.assertEqual(unprotected, [], "Management routes without the access check")

    def test_a_view_without_the_check_is_recognised(self):
        self.assertFalse(access.is_protected(lambda request: HttpResponse("open")))

        @access.management_view
        def protected(request):
            return HttpResponse("ok")

        self.assertTrue(access.is_protected(protected))

    def test_the_url_names_follow_the_menu_keys(self):
        for entry in navigation.MENU:
            self.assertEqual(resolve(reverse(entry.url_name)).view_name, entry.url_name)


class CheckAccessTests(SimpleTestCase):
    def request(self, user, method="get"):
        request = getattr(RequestFactory(), method)("/management/x/")
        request.user = user
        return request

    def test_the_anonymous_visitor_goes_to_the_login_with_the_way_back_only_for_a_page(self):
        get = access.check_access(self.request(AnonymousUser()))
        self.assertEqual(get.status_code, 302)
        self.assertEqual(get["Location"], f"{LOGIN}?next=/management/x/")
        post = access.check_access(self.request(AnonymousUser(), "post"))
        self.assertEqual(post["Location"], LOGIN)

    def test_an_account_that_is_not_an_active_staff_account_gets_a_404(self):
        for user in (FakeUser(staff=False), FakeUser(active=False)):
            with self.assertRaises(Http404):
                access.check_access(self.request(user))

    def test_a_missing_second_factor_is_refused_and_every_other_staff_account_passes(self):
        with self.assertRaises(PermissionDenied):
            access.check_access(self.request(FakeUser(verified=False)))
        # No permission is held or asked for: a staff account with the second factor passes.
        self.assertIsNone(access.check_access(self.request(FakeUser())))
        self.assertIsNone(access.check_access(self.request(FakeUser(), "post")))


@tag("mariadb")
class AccessTests(TestCase):
    # The page of a person asks the address directory for the correction of the address (D-081); the walk
    # opens it as the accounts would.
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.plain = create_active_user("plain.person@example.org")
        cls.staff = staff_user("staff.person@example.org")  # no group, no permission
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def test_anonymous_visitors_are_sent_to_the_login_on_every_route(self):
        client = Client()
        for path in PATHS:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertRedirects(response, f"{LOGIN}?next={path}", fetch_redirect_response=False)
                post = client.post(path)
                self.assertRedirects(post, LOGIN, fetch_redirect_response=False)

    def test_an_account_without_staff_status_is_told_nothing_on_every_route(self):
        client = self.login(self.plain)
        for path in PATHS:
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path)
                    # 404, like a page that does not exist: the area is not announced.
                    self.assertEqual(response.status_code, 404)
                    self.assertNotContains(response, "Einstellungen", status_code=404)

    def test_a_staff_account_without_a_completed_second_factor_can_only_set_it_up(self):
        client = Client()
        client.force_login(self.staff)
        for path in PATHS:
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path)
                    self.assertRedirects(response, reverse("accounts:two_factor_setup"), fetch_redirect_response=False)

    def test_a_staff_account_that_was_blocked_after_the_login_is_refused_on_every_route(self):
        user = staff_user("late.blocked@example.org")
        client = self.login(user)
        self.assertEqual(client.get(reverse("management:overview")).status_code, 200)
        user.set_status(User.Status.BLOCKED)
        for path in PATHS:
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path)
                    # The session no longer yields the account (a visitor: the login) or the access check
                    # answers 404; never a page.
                    self.assertIn(response.status_code, (302, 404))
                    if response.status_code == 302:
                        self.assertTrue(response["Location"].startswith(LOGIN), response["Location"])

    def test_every_staff_account_is_not_refused_on_any_route(self):
        # One staff account without a group and without a permission, and the administrator: the same.
        for user in (self.staff, self.administrator):
            client = self.login(user)
            for path in PATHS:
                with self.subTest(user=user.email, path=path):
                    # GET only; the samples have the id 1, so a page may be missing (404) and a page that
                    # only takes a POST answers 405. What must not happen: a refusal or a login redirect.
                    response = client.get(path)
                    self.assertIn(response.status_code, (200, 302, 404, 405))
                    if response.status_code == 302:
                        self.assertFalse(response["Location"].startswith(LOGIN))

    def test_the_overview_and_the_settings_open_for_a_staff_account_and_a_post_needs_the_token(self):
        client = self.login(self.staff)
        self.assertEqual(client.get(reverse("management:overview")).status_code, 200)
        self.assertEqual(client.get(reverse("management:settings")).status_code, 200)
        # No token: the CSRF refusal (the only 403 a staff account meets on the settings page).
        csrf_client = self.login(self.staff, enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(reverse("management:settings"), {}).status_code, 403)

    def test_a_response_of_the_area_is_not_cached(self):
        response = self.login(self.staff).get(reverse("management:overview"))
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_refusals_and_redirects_of_the_area_are_not_cached_either(self):
        # L-3: the 404 page, the redirect to the login and the CSRF refusal.
        overview = reverse("management:overview")
        csrf_client = self.login(self.staff, enforce_csrf_checks=True)
        responses = {
            "login redirect": Client().get(overview),
            "404 for a non-staff account": self.login(self.plain).get(overview),
            "404 of an unknown page": self.login(self.staff).get("/management/does-not-exist/"),
            "CSRF refusal": csrf_client.post(reverse("management:settings"), {}),
        }
        for name, response in responses.items():
            with self.subTest(name):
                self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(responses["CSRF refusal"].status_code, 403)

    def menu_parts(self, user):
        """The menu of the frame and the drawer on the home page for the account."""
        html = self.login(user).get(reverse("home")).content.decode()
        menu = html[html.index('<nav class="frame-menu'): html.index("</nav>", html.index('<nav class="frame-menu'))]
        drawer = html[html.index('class="frame-drawer-nav"'):]
        return menu, drawer[: drawer.index("</nav>")]

    def test_the_menu_of_a_staff_account_has_every_entry_and_a_plain_account_has_none(self):
        links = [reverse(entry.url_name) for entry in navigation.MENU]
        # overview, processes, persons, locations, LEGs, metering points, settings, contracts
        self.assertEqual(len(links), 8)
        for user in (self.staff, self.administrator):
            for part in self.menu_parts(user):
                with self.subTest(user=user.email):
                    for link in links:
                        self.assertIn(f'href="{link}"', part)
                    self.assertIn('<p class="frame-group">Verwaltung</p>', part)
        for part in self.menu_parts(self.plain):
            for link in links:
                self.assertNotIn(f'href="{link}"', part)
            self.assertNotIn('<p class="frame-group">Verwaltung</p>', part)

    def test_the_active_entry_is_marked_in_the_menu(self):
        html = self.login(self.staff).get(reverse("management:settings")).content.decode()
        menu = html[html.index('<nav class="frame-menu'):]
        menu = menu[: menu.index("</nav>")]
        self.assertRegex(menu, r'<a class="frame-item is-active" href="/management/settings/" aria-current="page">')
        self.assertRegex(menu, r'<a class="frame-item" href="/management/">')
        # The other pages of the frame do not mark it.
        home = self.login(self.staff).get(reverse("home")).content.decode()
        self.assertNotIn('href="/management/settings/" aria-current', home)

    def test_the_system_administration_link_has_its_own_name_and_is_for_accounts_that_can_use_it(self):
        # D-102: a superuser sees it; staff without any right of the administration do not (see
        # ``test_review_corrections.SystemAdministrationMenuTests`` for the full set of accounts).
        html = self.login(self.administrator).get(reverse("home")).content.decode()
        self.assertIn("Systemverwaltung", html)
        self.assertNotIn("Systemverwaltung", self.login(self.staff).get(reverse("home")).content.decode())


class MenuRuleTests(SimpleTestCase):
    def keys(self, user, view_name=""):
        return [entry["key"] for entry in navigation.menu_entries(user, view_name)]

    def test_an_active_staff_account_sees_every_entry_in_the_order_of_the_menu(self):
        self.assertEqual(
            self.keys(FakeUser()),
            ["overview", "processes", "persons", "locations", "legs", "metering_points", "settings", "contracts"],
        )
        self.assertEqual(self.keys(FakeUser()), [entry.key for entry in navigation.MENU])

    def test_nobody_but_an_active_staff_user_sees_an_entry(self):
        for user in (FakeUser(staff=False), FakeUser(active=False), FakeUser(authenticated=False), AnonymousUser()):
            self.assertEqual(self.keys(user), [])

    def test_the_link_to_the_mail_texts_of_the_administration_needs_its_own_right(self):
        self.assertNotIn("email_texts", self.keys(FakeUser()))
        keys = self.keys(FakeUser(perms=["accounts.view_emailtext"]))
        # Owner design: "Contracts" follows "Mail texts"; without the right it follows "Settings".
        self.assertEqual(keys[-2:], ["email_texts", "contracts"])
        self.assertEqual([key for key in keys if key != "email_texts"], [entry.key for entry in navigation.MENU])

    def test_the_area_of_an_url_name_is_active(self):
        entry = navigation.MenuEntry("metering_points", "x", "i-x")
        for name, active in (
            ("management:metering_points", True), ("management:metering_points_detail", True),
            ("management:metering_pointsx", False), ("management:legs", False), ("", False),
            ("other:metering_points", False),
        ):
            self.assertEqual(navigation.is_active_entry(entry, name), active, name)

    def test_the_menu_tag_needs_a_request_with_a_user(self):
        self.assertEqual(management_ui.management_menu({}), [])
        self.assertEqual(management_ui.management_menu({"request": object()}), [])

    def test_every_entry_names_a_symbol_of_the_frame(self):
        sprite = (Path(settings.BASE_DIR) / "leg_platform" / "templates" / "_frame_icons.html").read_text(encoding="utf-8")
        for entry in navigation.MENU:
            self.assertIn(f'id="{entry.icon}"', sprite)
