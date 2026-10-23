"""Access to the customer's pages (D-103): the URL configuration is walked, not a hand-picked list, so a later route
without the check is noticed. Anonymous visitors, accounts without a customer number, blocked accounts and accounts of other
customers get nothing; staff accounts with a customer number take part like everybody else (D-113); ids in addresses are
resolved through the logged-in account (a foreign id is a 404, like a missing one);
no response is cached; the request body of a POST is limited."""

import re

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import Http404, HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings, tag
from django.urls import URLResolver, get_resolver, resolve, reverse
from django.urls.resolvers import RoutePattern

from leg_platform.accounts.models import User
from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community import applications, contracts
from leg_platform.community.models import Application
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.customer import access
from leg_platform.customer.tests.helpers import (
    LOGIN,
    configure_numbers,
    customer,
    draft_with_building,
    draft_with_points,
    login,
    publish_contract,
)

CONVERTER = re.compile(r"<(?:\w+:)?\w+>")


def customer_routes():
    """``[(path, callback)]`` of every route under ``participation/``, converters replaced by 1."""
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
        if isinstance(entry, URLResolver) and str(entry.pattern) == "participation/":
            walk(entry.url_patterns, "participation/")
    return found


ROUTES = customer_routes()
PATHS = [path for path, _callback in ROUTES]


class FakeUser:
    """An account as the access check sees it (no database)."""

    class Status:
        ACTIVE = "active"

    def __init__(self, staff=False, active=True, authenticated=True, number=1, status="active", superuser=False,
                 verified=True):
        self.is_staff, self.is_active, self.is_authenticated = staff, active, authenticated
        self.is_superuser = superuser
        self.customer_number_id, self.status = number, status
        self.is_verified = lambda: verified  # django-otp: the second factor of this session


class RouteWalkTests(SimpleTestCase):
    def test_the_walk_finds_the_routes_of_the_area(self):
        self.assertIn("/participation/buildings/", PATHS)
        self.assertIn("/participation/application/1/building/", PATHS)
        self.assertIn("/participation/contract/download/", PATHS)
        for path in PATHS:
            self.assertEqual(resolve(path).view_name.split(":")[0], "customer")

    def test_every_route_has_the_access_check(self):
        unprotected = [path for path, callback in ROUTES if not access.is_protected(callback)]
        self.assertEqual(unprotected, [], "Customer routes without the access check")

    def test_a_view_without_the_check_is_recognised(self):
        self.assertFalse(access.is_protected(lambda request: HttpResponse("open")))

    def test_the_placeholder_of_the_deregistration_exists_under_the_agreed_name(self):
        self.assertEqual(resolve(reverse("customer:deregistration_start")).view_name, "customer:deregistration_start")

    def test_no_route_is_csrf_exempt_and_none_takes_put_patch_or_delete(self):
        for path, callback in ROUTES:
            with self.subTest(path=path):
                self.assertFalse(getattr(callback, "csrf_exempt", False))
                view_class = callback.view_class
                for method in ("put", "patch", "delete"):
                    self.assertFalse(hasattr(view_class, method))


class CheckAccessTests(SimpleTestCase):
    def request(self, user, method="get"):
        request = getattr(RequestFactory(), method)("/participation/x/")
        request.user = user
        return request

    def test_the_anonymous_visitor_goes_to_the_login_with_the_way_back_only_for_a_page(self):
        get = access.check_access(self.request(AnonymousUser()))
        self.assertEqual((get.status_code, get["Location"]), (302, f"{LOGIN}?next=/participation/x/"))
        post = access.check_access(self.request(AnonymousUser(), "post"))
        self.assertEqual(post["Location"], LOGIN)

    def test_only_an_active_customer_with_a_number_passes_whatever_the_privileges(self):
        # D-113: staff and system administrators take part like everybody else.
        for user in (FakeUser(), FakeUser(staff=True), FakeUser(superuser=True), FakeUser(staff=True, superuser=True)):
            with self.subTest(user=vars(user)):
                self.assertIsNone(access.check_access(self.request(user)))
        for user in (
            FakeUser(active=False), FakeUser(number=None), FakeUser(status="blocked"), FakeUser(status="deactivated"),
            FakeUser(staff=True, active=False), FakeUser(staff=True, number=None), FakeUser(staff=True, status="blocked"),
            FakeUser(staff=True, superuser=True, number=None), FakeUser(superuser=True, status="blocked"),
        ):
            with self.subTest(user=vars(user)), self.assertRaises(Http404):
                access.check_access(self.request(user))

    def test_an_account_that_must_use_a_second_factor_and_has_not_completed_it_is_refused(self):
        # The second line of the second factor (the middleware is the first): staff and system administrators, D-113.
        for user in (
            FakeUser(staff=True, verified=False), FakeUser(superuser=True, verified=False),
            FakeUser(staff=True, superuser=True, verified=False),
        ):
            with self.subTest(user=vars(user)), self.assertRaises(PermissionDenied):
                access.check_access(self.request(user))
            with self.subTest(user=vars(user), method="post"), self.assertRaises(PermissionDenied):
                access.check_access(self.request(user, "post"))
        # A customer needs none; the number and the status are asked first (a 404 does not announce the second factor).
        self.assertIsNone(access.check_access(self.request(FakeUser(verified=False))))
        with self.assertRaises(Http404):
            access.check_access(self.request(FakeUser(staff=True, number=None, verified=False)))

    def test_the_tiles_of_the_start_page_use_the_same_predicate(self):
        from leg_platform.accounts import tasks

        for user, expected in (
            (FakeUser(), True), (FakeUser(staff=True), True), (FakeUser(superuser=True), True),
            (FakeUser(staff=True, superuser=True), True),
            (FakeUser(number=None), False), (FakeUser(status="blocked"), False), (FakeUser(active=False), False),
            (FakeUser(staff=True, number=None), False), (FakeUser(staff=True, status="blocked"), False),
            (FakeUser(authenticated=False), False),
        ):
            with self.subTest(user=vars(user)):
                self.assertEqual(tasks._customer(user), expected)
                self.assertEqual(access.is_customer(user), expected)


@tag("mariadb")
class AccessTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.access@example.org")
        cls.other = customer("other.access@example.org")
        cls.staff = staff_user("staff.access@example.org")
        cls.administrator = staff_user("administrator.access@example.org", superuser=True)
        cls.blocked = customer("blocked.access@example.org")
        cls.blocked.set_status(type(cls.blocked).Status.BLOCKED)

    def test_anonymous_visitors_are_sent_to_the_login_on_every_route(self):
        client = Client()
        for path in PATHS:
            with self.subTest(path=path, method="get"):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(LOGIN), response["Location"])
            with self.subTest(path=path, method="post"):
                response = client.post(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], LOGIN)

    def test_a_staff_account_and_an_administrator_with_a_number_are_treated_like_a_customer_on_every_route(self):
        # D-113: the same answers as an ordinary customer for the same address (a 404 only where an id does not exist).
        reference = login(self.mine)
        expected = {path: reference.get(path).status_code for path in PATHS}
        self.assertIn(200, expected.values())
        for user in (self.staff, self.administrator):
            self.assertIsNotNone(user.customer_number_id)
            client = login(user)
            for path in PATHS:
                with self.subTest(user=user.pk, path=path, method="get"):
                    self.assertEqual(client.get(path).status_code, expected[path])

    def test_a_staff_account_and_an_administrator_with_a_number_can_use_the_pages_and_start_an_application(self):
        publish_contract()
        configure_numbers()
        for user in (self.staff, self.administrator):
            client = login(user)
            for name in ("customer:buildings", "customer:contract", "customer:deregistration_start"):
                with self.subTest(user=user.pk, name=name):
                    self.assertEqual(client.get(reverse(name)).status_code, 200)
            with self.subTest(user=user.pk, name="application_start"):
                self.assertEqual(client.post(reverse("customer:application_start")).status_code, 302)
                application = Application.objects.get(person=user)
                self.assertEqual(
                    client.get(reverse("customer:application_building", args=[application.pk])).status_code, 200
                )

    def test_a_staff_account_cannot_read_the_application_of_a_customer(self):
        configure_numbers()
        application, _building = draft_with_points(self.other)
        for user in (self.staff, self.administrator):
            with self.subTest(user=user.pk):
                self.assertEqual(
                    login(user).get(reverse("customer:application_building", args=[application.pk])).status_code, 404
                )

    def test_a_customer_gets_a_404_under_the_management_area_and_never_sees_the_staff_entry(self):
        client = login(self.mine)
        self.assertEqual(client.get("/management/").status_code, 404)
        self.assertEqual(client.get("/management/persons/").status_code, 404)
        html = client.get(reverse("home")).content.decode()
        self.assertNotIn("/management/", html)
        self.assertNotIn("Mitarbeitenden-Bereich", html)
    def test_a_blocked_account_with_an_old_session_gets_nothing(self):
        # A blocked account cannot log in; a forced session is refused by the authentication as well.
        client = Client()
        client.force_login(self.blocked)
        response = client.get(reverse("customer:buildings"))
        self.assertIn(response.status_code, (302, 404))
        self.assertNotIn(b"Geb\xc3\xa4ude", response.content)

    def test_a_customer_gets_the_pages_and_every_response_is_not_stored(self):
        publish_contract()
        configure_numbers()
        client = login(self.mine)
        application, building = draft_with_building(self.mine)
        for name, args in (
            ("customer:buildings", []), ("customer:application_start", []), ("customer:contract", []),
            ("customer:deregistration_start", []),
            ("customer:application_building", [application.pk]),
            ("customer:application_metering_points", [application.pk]),
            ("customer:contract_download", []), ("customer:contract_read", []),
        ):
            with self.subTest(name=name):
                response = client.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 200, name)
                cache = response["Cache-Control"]
                for directive in ("no-cache", "no-store", "private"):
                    self.assertIn(directive, cache)

    def test_the_refusals_are_not_stored_either(self):
        response = Client().get(reverse("customer:buildings"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_id_of_another_account_is_a_404_like_a_missing_one(self):
        publish_contract()
        configure_numbers()
        application, building = draft_with_points(self.other, feed_in=True)
        mine = login(self.mine)
        missing = 987654
        for name in (
            "application", "application_building", "application_metering_points", "application_bank_connection",
            "application_contract", "application_start_date", "application_cancel", "application_submitted",
            "application_withdraw", "application_change",
        ):
            view_class = resolve(reverse(f"customer:{name}", args=[1])).func.view_class
            for method in ("get", "post"):
                if not hasattr(view_class, method):
                    continue
                with self.subTest(name=name, method=method):
                    foreign = getattr(mine, method)(reverse(f"customer:{name}", args=[application.pk]))
                    absent = getattr(mine, method)(reverse(f"customer:{name}", args=[missing]))
                    self.assertEqual((foreign.status_code, absent.status_code), (404, 404))
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())

    def test_the_building_and_the_point_of_another_account_are_a_404(self):
        location = make_location("Otherstrasse", "5", "3000", "Bern")
        point = make_metering_point(3, location=location)
        assign_person(point, self.other)
        mine = login(self.mine)
        for path in (
            reverse("customer:building_details", args=[location.pk]),
            reverse("customer:metering_point_name", args=[point.pk]),
        ):
            with self.subTest(path=path):
                self.assertEqual(mine.get(path).status_code, 404)
                self.assertEqual(mine.post(path, {"name": "Taken"}).status_code, 404)
        point.refresh_from_db()
        self.assertEqual(point.name, "")
        self.assertEqual(login(self.other).get(reverse("customer:building_details", args=[location.pk])).status_code, 200)

    def test_a_foreign_or_unknown_building_in_the_start_address_is_a_404(self):
        location = make_location("Otherstrasse", "6", "3000", "Bern")
        point = make_metering_point(4, location=location)
        assign_person(point, self.other)
        mine = login(self.mine)
        start = reverse("customer:application_start")
        for value in (location.pk, 987654, "x", "-1"):
            with self.subTest(value=value):
                self.assertEqual(mine.get(start, {"building": value}).status_code, 404)
                self.assertEqual(mine.post(start, {"building": value}).status_code, 404)
        self.assertFalse(Application.objects.filter(person=self.mine).exists())

    def test_a_post_needs_the_csrf_token(self):
        publish_contract()
        configure_numbers()
        application, _building = draft_with_building(self.mine)
        client = VerifiedClient(enforce_csrf_checks=True)
        client.force_login(self.mine)
        for path in (
            reverse("customer:application_start"),
            reverse("customer:application_cancel", args=[application.pk]),
            reverse("customer:application_metering_points", args=[application.pk]),
        ):
            with self.subTest(path=path):
                self.assertEqual(client.post(path, {}).status_code, 403)
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())

    def test_the_request_body_of_a_post_is_limited(self):
        client = login(self.mine)
        response = client.post(
            reverse("customer:application_start"), data="x=" + "a" * 20000, content_type="application/x-www-form-urlencoded"
        )
        self.assertEqual(response.status_code, 413)

    def test_a_404_and_a_csrf_refusal_under_the_prefix_are_not_stored_either(self):
        # The pages review: answers the view decorators never see (no route, a missing token, a body that is too large)
        # carry no-store like the pages themselves; so do the answers to an anonymous visitor.
        guarded = VerifiedClient(enforce_csrf_checks=True)
        guarded.force_login(self.mine)
        answers = {
            "no route, logged in": login(self.mine).get("/participation/zzz/"),
            "no route, anonymous": Client().get("/participation/zzz/"),
            "no route without the slash": Client().get("/participation"),
            "csrf": guarded.post(reverse("customer:application_start"), {}),
            "staff": login(self.staff).get(reverse("customer:buildings")),
            "too large": login(self.mine).post(
                reverse("customer:application_start"), data="x=" + "a" * 20000,
                content_type="application/x-www-form-urlencoded",
            ),
        }
        for name, response in answers.items():
            with self.subTest(name=name):
                self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            [answers[name].status_code for name in ("no route, logged in", "no route, anonymous", "csrf", "staff", "too large")],
            [404, 404, 403, 200, 413],  # a staff account with a number takes part (D-113)
        )

    def test_a_get_never_changes_anything(self):
        publish_contract()
        configure_numbers()
        application, _building = draft_with_points(self.mine)
        client = login(self.mine)
        before = (Application.objects.count(), contracts.has_accepted_current(self.mine))
        for path in PATHS:
            client.get(path.replace("/1/", f"/{application.pk}/") if "/application/1/" in path else path)
        self.assertEqual((Application.objects.count(), contracts.has_accepted_current(self.mine)), before)
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())
        applications.own_application(self.mine, application.pk)


def privilege_snapshot(user):
    """Everything about an account that a customer page must never change."""
    stored = User.objects.get(pk=user.pk)
    return (
        stored.is_staff, stored.is_superuser, stored.customer_number_id, stored.status, stored.retention_obligation,
        stored.email, stored.is_active, sorted(stored.groups.values_list("pk", flat=True)),
        sorted(stored.user_permissions.values_list("pk", flat=True)),
    )


FORGED = {
    "is_staff": "on", "is_superuser": "on", "groups": "1", "user_permissions": "1", "customer_number": "999999",
    "status": "blocked", "retention_obligation": "", "email": "forged@example.org", "is_active": "",
    "password": "x", "session_version": "99",
}


@tag("mariadb")
class StaffAccountSecondFactorTests(TestCase):
    """D-113 review M-1: a staff account or administrator with a customer number and no completed second factor."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.sf@example.org")
        cls.administrator = staff_user("administrator.sf@example.org", superuser=True)

    def test_the_middleware_sends_the_account_to_the_setup_on_every_route_and_the_start_page(self):
        for user in (self.staff, self.administrator):
            client = Client()
            client.force_login(user)  # no verification of the second factor in this session
            for path in (*PATHS, reverse("home")):
                for method in ("get", "post"):
                    with self.subTest(user=user.pk, path=path, method=method):
                        response = getattr(client, method)(path)
                        self.assertRedirects(response, reverse("accounts:login_method"), fetch_redirect_response=False)

    def test_the_pages_refuse_the_account_themselves_if_the_middleware_were_missing(self):
        without_middleware = [m for m in settings.MIDDLEWARE if not m.endswith("SecondFactorMiddleware")]
        self.assertNotEqual(without_middleware, list(settings.MIDDLEWARE))
        with override_settings(MIDDLEWARE=without_middleware):
            for user in (self.staff, self.administrator):
                client = Client()
                client.force_login(user)
                for path in PATHS:
                    for method in ("get", "post"):
                        with self.subTest(user=user.pk, path=path, method=method):
                            response = getattr(client, method)(path)
                            self.assertEqual(response.status_code, 403)
                            self.assertIn("no-store", response["Cache-Control"])
                            self.assertNotIn("Ittigen", response.content.decode())

    def test_a_verified_session_passes_and_a_customer_needs_no_second_factor(self):
        for user in (self.staff, self.administrator):
            self.assertEqual(login(user).get(reverse("customer:buildings")).status_code, 200)
        plain = customer("plain.sf@example.org")
        client = Client()
        client.force_login(plain)
        self.assertEqual(client.get(reverse("customer:buildings")).status_code, 200)


@tag("mariadb")
class StaffAccountStateTests(TestCase):
    """D-113 review N-3: states of a staff account at database level, on the customer and the management pages."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.state@example.org")

    def candidates(self):
        return (*PATHS, reverse("management:overview"), reverse("management:persons"), reverse("management:settings"))

    def test_a_staff_account_without_a_customer_number_gets_the_404_on_every_customer_page_but_keeps_the_staff_area(self):
        user = staff_user("staff.nonumber@example.org")
        client = login(user)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE accounts_user SET customer_number = NULL WHERE id = %s", [user.pk])
        self.assertIsNone(User.objects.get(pk=user.pk).customer_number_id)
        for path in PATHS:
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(getattr(client, method)(path).status_code, 404)
        # The staff area asks for staff status only (D-086); the start page shows no participation.
        self.assertEqual(client.get(reverse("management:overview")).status_code, 200)
        html = client.get(reverse("home")).content.decode()
        self.assertNotIn("/participation/", html)
        self.assertNotIn("task-register_building-title", html)

    def test_a_blocked_or_deactivated_staff_account_with_an_old_session_gets_no_page(self):
        for state in ("blocked", "deactivated"):
            user = staff_user(f"staff.{state}@example.org")
            client = login(user)
            self.assertEqual(client.get(reverse("customer:buildings")).status_code, 200)
            self.assertEqual(client.get(reverse("management:overview")).status_code, 200)
            if state == "blocked":
                user.set_status(User.Status.BLOCKED)
            else:
                user.set_retention_obligation()
                other_admin = staff_user("keeper.state@example.org", superuser=True)  # the rule of the last administrator
                self.assertTrue(other_admin.is_active)
                user.delete_or_deactivate()
                self.assertEqual(User.objects.get(pk=user.pk).status, User.Status.DEACTIVATED)
            for path in self.candidates():
                for method in ("get", "post"):
                    with self.subTest(state=state, path=path, method=method):
                        response = getattr(client, method)(path)
                        # The session no longer yields the account: a visitor, sent to the login.
                        self.assertEqual(response.status_code, 302)
                        self.assertTrue(response["Location"].startswith(LOGIN), response["Location"])


@tag("mariadb")
class StaffAccountForgedDataTests(TestCase):
    """D-113 review N-3: no field of a customer page can change a privilege, a group, a permission or the number."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.forged@example.org")
        cls.administrator = staff_user("administrator.forged@example.org", superuser=True)
        cls.group = Group.objects.create(name="Forged probe")
        cls.permission = Permission.objects.first()

    def forged(self):
        return {**FORGED, "groups": str(self.group.pk), "user_permissions": str(self.permission.pk)}

    def test_the_profile_form_ignores_every_privilege_field(self):
        for user in (self.staff, self.administrator):
            client = login(user)
            before = privilege_snapshot(user)
            data = {
                "first_name": "Berta", "last_name": "Muster", "street": "Neue Strasse", "house_number": "3",
                "postal_code": "3063", "town": "Ittigen", "country": "CH", **self.forged(),
            }
            with self.subTest(user=user.pk):
                response = client.post(reverse("accounts:profile_edit"), data)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(User.objects.get(pk=user.pk).first_name, "Berta")  # the real fields were saved
                self.assertEqual(privilege_snapshot(user), before)

    def test_the_pages_of_the_wizard_and_the_bank_card_ignore_every_privilege_field(self):
        configure_numbers()
        publish_contract()
        for user in (self.staff, self.administrator):
            client = login(user)
            before = privilege_snapshot(user)
            with self.subTest(user=user.pk):
                client.post(reverse("customer:application_start"), self.forged())
                application = Application.objects.get(person=user)
                for name in ("application_building", "application_metering_points", "application_bank_connection",
                             "application_contract", "application_start_date", "application_cancel"):
                    client.post(reverse(f"customer:{name}", args=[application.pk]), self.forged())
                client.post(reverse("customer:bank_connection"), {**self.forged(), "iban": ""})
                client.post(reverse("customer:deregistration_start"), self.forged())
                self.assertEqual(privilege_snapshot(user), before)
                # Everything that was created belongs to the account itself.
                self.assertFalse(Application.objects.exclude(person=user).filter(pk=application.pk).exists())
