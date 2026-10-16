"""Access to the management area (D-077): every URL denies the anonymous visitor, a person without
staff status, a staff user without the permission and a session without the second factor; the
menu offers only what the account may use. The URL configuration is walked, not a hand-picked list,
so that a later route without the access check is noticed (the equivalent of the walk of the
administration URLs, ``accounts/tests/test_admin_exposure.py``)."""

import re

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import Http404, HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, tag
from django.urls import URLResolver, get_resolver, resolve, reverse
from django.urls.resolvers import RoutePattern

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community.tests.helpers import staff_user
from leg_platform.management import access, navigation
from leg_platform.management.templatetags import management_ui

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

        @access.management_view("community.view_leg")
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

    def test_a_view_without_permissions_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            access.check_access(self.request(AnonymousUser()))

        class Open(access.ManagementAccessMixin):
            pass

        with self.assertRaises(ImproperlyConfigured):
            access.check_access(self.request(AnonymousUser()), Open().get_required_permissions(), Open().get_any_permissions())

    def test_the_anonymous_visitor_goes_to_the_login_with_the_way_back_only_for_a_page(self):
        get = access.check_access(self.request(AnonymousUser()), ("community.view_leg",))
        self.assertEqual(get.status_code, 302)
        self.assertEqual(get["Location"], f"{LOGIN}?next=/management/x/")
        post = access.check_access(self.request(AnonymousUser(), "post"), ("community.view_leg",))
        self.assertEqual(post["Location"], LOGIN)


@tag("mariadb")
class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plain = create_active_user("plain.person@example.org")
        cls.staff_none = staff_user("staff.none@example.org")
        cls.staff_view = staff_user("staff.view@example.org", "view_managementsettings")
        cls.staff_change = staff_user("staff.change@example.org", "change_managementsettings")
        cls.staff_location = staff_user("staff.location@example.org", "view_location")
        cls.staff_all = staff_user("staff.all@example.org")
        cls.staff_all.user_permissions.set(Permission.objects.filter(content_type__app_label="community"))
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)

    def login(self, user):
        client = VerifiedClient()
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

    def test_a_staff_account_without_the_permission_gets_the_page_without_access_on_every_route(self):
        client = self.login(self.staff_none)
        for path in PATHS:
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path)
                    self.assertContains(response, "Hier haben Sie keinen Zugriff.", status_code=403)

    def test_a_staff_account_without_a_completed_second_factor_can_only_set_it_up(self):
        client = Client()
        client.force_login(self.staff_all)
        for path in PATHS:
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path)
                    self.assertRedirects(response, reverse("accounts:two_factor_setup"), fetch_redirect_response=False)

    def test_an_account_with_all_rights_and_the_administrator_reach_every_route(self):
        for user in (self.staff_all, self.administrator):
            client = self.login(user)
            for path in PATHS:
                with self.subTest(user=user.email, path=path):
                    # GET only: the pages that change data are tested with their own data.
                    self.assertEqual(client.get(path).status_code, 200)

    def test_the_right_to_view_opens_a_page_and_the_right_to_change_saves_it(self):
        settings_url = reverse("management:settings")
        viewer = self.login(self.staff_view)
        self.assertEqual(viewer.get(settings_url).status_code, 200)
        self.assertEqual(viewer.post(settings_url, {}).status_code, 403)

        changer = self.login(self.staff_change)
        # Changing without viewing: the page is not shown, saving is allowed (the form is
        # checked, so a wrong post is an error page, not a refusal).
        self.assertEqual(changer.get(settings_url).status_code, 403)
        self.assertNotEqual(changer.post(settings_url, {}).status_code, 403)

    def test_the_overview_needs_one_right_of_an_area(self):
        overview = reverse("management:overview")
        self.assertEqual(self.login(self.staff_location).get(overview).status_code, 200)
        self.assertEqual(self.login(self.staff_change).get(overview).status_code, 403)
        self.assertEqual(self.login(self.staff_none).get(overview).status_code, 403)

    def test_a_response_of_the_area_is_not_cached(self):
        response = self.login(self.staff_all).get(reverse("management:overview"))
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_menu_offers_only_what_the_account_may_use(self):
        home = reverse("home")
        expectations = (
            (self.plain, (), ("/management/", "/management/settings/")),
            (self.staff_none, (), ("/management/", "/management/settings/")),
            (self.staff_view, ("/management/", "/management/settings/"), ()),
            (self.staff_location, ("/management/",), ("/management/settings/",)),
            (self.administrator, ("/management/", "/management/settings/"), ()),
        )
        for user, present, absent in expectations:
            with self.subTest(user=user.email):
                html = self.login(user).get(home).content.decode()
                menu = html[html.index('<nav class="frame-menu'): html.index("</nav>", html.index('<nav class="frame-menu'))]
                drawer = html[html.index('class="frame-drawer-nav"'):]
                for part in (menu, drawer[: drawer.index("</nav>")]):
                    for link in present:
                        self.assertIn(f'href="{link}"', part)
                    for link in absent:
                        self.assertNotIn(f'href="{link}"', part)
                    if present:
                        self.assertIn('<p class="frame-group">Verwaltung</p>', part)
                    else:
                        self.assertNotIn('<p class="frame-group">Verwaltung</p>', part)

    def test_the_active_entry_is_marked_in_the_menu(self):
        html = self.login(self.staff_all).get(reverse("management:settings")).content.decode()
        menu = html[html.index('<nav class="frame-menu'):]
        menu = menu[: menu.index("</nav>")]
        self.assertRegex(menu, r'<a class="frame-item is-active" href="/management/settings/" aria-current="page">')
        self.assertRegex(menu, r'<a class="frame-item" href="/management/">')
        # The other pages of the frame do not mark it.
        home = self.login(self.staff_all).get(reverse("home")).content.decode()
        self.assertNotIn('href="/management/settings/" aria-current', home)

    def test_the_system_administration_link_has_its_own_name(self):
        html = self.login(self.staff_none).get(reverse("home")).content.decode()
        self.assertIn("Systemverwaltung", html)


class MenuRuleTests(SimpleTestCase):
    class User:
        def __init__(self, perms=(), staff=True, active=True, authenticated=True):
            self.perms, self.is_staff, self.is_active, self.is_authenticated = set(perms), staff, active, authenticated

        def has_perm(self, name):
            return name in self.perms

        def has_perms(self, names):
            return all(self.has_perm(n) for n in names)

    def keys(self, user, view_name=""):
        return [entry["key"] for entry in navigation.menu_entries(user, view_name)]

    def test_entries_follow_the_permissions_of_their_pages(self):
        self.assertEqual(self.keys(self.User()), [])
        self.assertEqual(self.keys(self.User(["community.view_managementsettings"])), ["overview", "settings"])
        self.assertEqual(self.keys(self.User(["community.view_leg"])), ["overview"])
        self.assertEqual(self.keys(self.User(["community.change_managementsettings"])), [])

    def test_nobody_but_an_active_staff_user_sees_an_entry(self):
        everything = navigation.AREA_VIEW_PERMISSIONS
        for user in (
            self.User(everything, staff=False), self.User(everything, active=False),
            self.User(everything, authenticated=False),
        ):
            self.assertEqual(self.keys(user), [])

    def test_the_area_of_an_url_name_is_active(self):
        entry = navigation.MenuEntry("metering_points", "x", "i-x", ("a.b",))
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
        from pathlib import Path

        sprite = (Path(settings.BASE_DIR) / "leg_platform" / "templates" / "_frame_icons.html").read_text(encoding="utf-8")
        for entry in navigation.MENU:
            self.assertIn(f'id="{entry.icon}"', sprite)

    def test_an_account_that_is_not_staff_gets_a_404(self):
        request = RequestFactory().get("/management/")
        request.user = self.User(["a.b"], staff=False)
        with self.assertRaises(Http404):
            access.check_access(request, ("a.b",))
        request.user = self.User(["a.b"], active=False)
        with self.assertRaises(Http404):
            access.check_access(request, ("a.b",))
    def test_a_permission_denied_is_raised_for_a_staff_user_without_the_permission(self):
        request = RequestFactory().get("/management/")
        request.user = self.User([], staff=True)
        request.user.is_verified = lambda: True
        with self.assertRaises(PermissionDenied):
            access.check_access(request, ("a.b",))
        request.user = self.User(["a.b"], staff=True)
        request.user.is_verified = lambda: True
        self.assertIsNone(access.check_access(request, ("a.b",)))
        # No completed second factor: refused even with the permission.
        request.user.is_verified = lambda: False
        with self.assertRaises(PermissionDenied):
            access.check_access(request, ("a.b",))
