"""The read-only support view "View as <person>" (D-131): deny by default, nothing written for the person, the rules of the
start, every way the mode ends, a tampered session state, the bar and the cache headers. Reserved e-mail domains only."""

import re
import time
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import URLResolver, get_resolver, resolve, reverse
from django.urls.resolvers import RoutePattern

from leg_platform import render_as
from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.session_timeout import LAST_ACTIVITY_KEY
from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community.tests.helpers import staff_user
from leg_platform.customer.tests.helpers import HOME, customer, login, publish_contract

CONVERTER = re.compile(r"<(?:\w+:)?\w+>")
PROFILE = reverse("accounts:profile")
CONTRACT = reverse("customer:contract")
BUILDINGS = reverse("customer:buildings")
ALLOWED_PATHS = [HOME, BUILDINGS, CONTRACT, PROFILE]
END = reverse("accounts:render_as_end")
LOGOUT = reverse("accounts:logout")
BANNER = 'class="render-as-bar'
Event = SecurityEvent.Event


def all_routes():
    """``[(path, view name)]`` of every plain route of the project, converters replaced by 1."""
    found = []

    def walk(patterns, prefix):
        for entry in patterns:
            if not isinstance(entry.pattern, RoutePattern):
                continue
            text = CONVERTER.sub("1", prefix + str(entry.pattern))
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, text)
            else:
                found.append("/" + text)

    walk(get_resolver().url_patterns, "")
    routes = []
    for path in found:
        try:
            routes.append((path, resolve(path).view_name))
        except Exception:  # noqa: BLE001  (a path with a converter that does not accept 1)
            continue
    return routes


def snapshot():
    """Every row of every table of the default database except the security log and the sessions."""
    skipped = {SecurityEvent, Session}
    result = {}
    for model in apps.get_models():
        if model in skipped or model._meta.proxy or not model._meta.managed:
            continue
        result[model._meta.label] = sorted(map(repr, model.objects.values_list()))
    return result


def events(kind):
    return list(SecurityEvent.objects.filter(event=kind).order_by("pk"))


@tag("mariadb")
class ViewAsBase(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("view.as.staff@example.org")
        cls.person = customer("view.as.person@example.org", first_name="Berta", last_name="Viewed")
        publish_contract()

    def setUp(self):
        self.client = login(self.staff)
        # The detail of an event is only in the log line (written on commit), so the calls are recorded.
        recorder = patch.object(render_as.security_log, "record", wraps=security_log.record)
        self.record = recorder.start()
        self.addCleanup(recorder.stop)

    def details(self, kind):
        return [call.kwargs.get("detail") for call in self.record.call_args_list if call.args[0] == kind]

    def start(self, person=None, client=None):
        client = client or self.client
        return client.post(reverse("management:persons_view_as", args=[(person or self.person).pk]))

    def session_state(self):
        return self.client.session.get(render_as.SESSION_KEY)


class StartTests(ViewAsBase):
    def test_a_staff_member_starts_the_view_with_a_post_and_lands_on_the_start_page(self):
        response = self.start()
        self.assertRedirects(response, HOME, fetch_redirect_response=False)
        self.assertEqual(self.session_state()["subject"], self.person.pk)
        started = events(Event.RENDER_AS_STARTED)
        self.assertEqual(len(started), 1)
        self.assertEqual((started[0].account_id, started[0].actor_id), (self.person.pk, self.staff.pk))

    def test_the_start_needs_the_csrf_token(self):
        client = VerifiedClient(enforce_csrf_checks=True)
        client.force_login(self.staff)
        response = client.post(reverse("management:persons_view_as", args=[self.person.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(render_as.SESSION_KEY, client.session)
        self.assertFalse(events(Event.RENDER_AS_STARTED))

    def test_a_get_does_not_start_the_view(self):
        response = self.client.get(reverse("management:persons_view_as", args=[self.person.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertNotIn(render_as.SESSION_KEY, self.client.session)

    def test_a_customer_without_staff_status_cannot_start_it(self):
        other = customer("view.as.other@example.org")
        client = login(other)
        response = self.start(client=client)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(render_as.SESSION_KEY, client.session)
        self.assertFalse(events(Event.RENDER_AS_STARTED))

    def test_a_staff_member_without_the_second_factor_cannot_start_it(self):
        client = Client()
        client.force_login(self.staff)  # no second factor completed
        response = self.start(client=client)
        self.assertNotIn(render_as.SESSION_KEY, client.session)
        self.assertFalse(events(Event.RENDER_AS_STARTED))
        self.assertNotEqual(response.get("Location"), HOME)

    def test_oneself_cannot_be_viewed(self):
        staff_customer = customer("view.as.both@example.org")
        staff_customer.set_privileges(is_staff=True, is_superuser=False)
        client = login(staff_customer)
        self.assertEqual(self.start(staff_customer, client).status_code, 404)
        self.assertNotIn(render_as.SESSION_KEY, client.session)

    def test_a_person_who_is_no_customer_cannot_be_viewed(self):
        unverified = get_user_model().objects.create_user("view.as.unverified@example.org", first_name="Una", last_name="Verified")
        self.assertEqual(self.start(unverified).status_code, 404)
        self.assertNotIn(render_as.SESSION_KEY, self.client.session)

    def test_a_blocked_person_cannot_be_viewed(self):
        blocked = customer("view.as.blocked@example.org")
        blocked.set_status(type(blocked).Status.BLOCKED)
        self.assertEqual(self.start(blocked).status_code, 404)
        self.assertNotIn(render_as.SESSION_KEY, self.client.session)
        self.assertFalse(events(Event.RENDER_AS_STARTED))


class UrlWalkTests(ViewAsBase):
    def setUp(self):
        super().setUp()
        self.start()
        self.routes = all_routes()

    def test_the_walk_finds_the_routes(self):
        names = {name for _path, name in self.routes}
        self.assertLessEqual(render_as.ALLOWED_VIEWS | render_as.POST_VIEWS, names)
        self.assertGreater(len(self.routes), 100)

    def test_only_the_four_pages_answer_a_get_without_a_query_string_everything_else_is_a_403(self):
        refused = 0
        for path, name in self.routes:
            if name in render_as.PASSTHROUGH_VIEWS:
                continue
            response = self.client.get(path)
            with self.subTest(path=path, name=name):
                if name in render_as.ALLOWED_VIEWS:
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertEqual(response.status_code, 403)
                    refused += 1
        self.assertGreater(refused, 50)
        self.assertTrue(self.session_state())

    def test_the_four_allowed_pages_are_the_ones_named(self):
        self.assertEqual({resolve(path).view_name for path in ALLOWED_PATHS}, render_as.ALLOWED_VIEWS)
        for path in ALLOWED_PATHS:
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_a_query_string_is_refused_on_every_allowed_page(self):
        for path in ALLOWED_PATHS:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, {"x": "1"}).status_code, 403)

    def test_every_other_method_is_refused_except_the_end_and_the_logout(self):
        for path, name in self.routes:
            if name in render_as.PASSTHROUGH_VIEWS or name in render_as.POST_VIEWS:
                continue
            for method in ("post", "put", "patch", "delete"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(getattr(self.client, method)(path).status_code, 403)
        # The refusals did not end the view.
        self.assertTrue(self.session_state())

    def test_a_get_of_the_end_or_the_logout_is_refused(self):
        self.assertEqual(self.client.get(END).status_code, 403)
        self.assertEqual(self.client.get(LOGOUT).status_code, 403)

    def test_the_head_of_an_allowed_page_is_answered_and_of_another_one_refused(self):
        self.assertEqual(self.client.head(PROFILE).status_code, 200)
        self.assertEqual(self.client.head(reverse("customer:application_start")).status_code, 403)


class NothingWrittenTests(ViewAsBase):
    def test_a_walk_changes_only_the_security_log_and_the_sessions(self):
        self.start()
        before = snapshot()
        mail.outbox.clear()
        for path in ALLOWED_PATHS:
            self.client.get(path)
        for path, name in all_routes():
            if name in render_as.ALLOWED_VIEWS | render_as.POST_VIEWS | render_as.PASSTHROUGH_VIEWS:
                continue
            self.client.get(path)
            self.client.post(path)
        self.assertEqual(snapshot(), before)
        self.assertEqual(mail.outbox, [])
        self.assertTrue(events(Event.RENDER_AS_PAGE_VIEWED))

    def test_the_session_key_of_the_open_building_is_kept_and_the_person_sees_nothing_marked_as_seen(self):
        self.start()
        session = self.client.session
        session["buildings_open_id"] = 12345
        session.save()
        before = snapshot()
        self.assertEqual(self.client.get(BUILDINGS).status_code, 200)
        self.assertEqual(self.client.session.get("buildings_open_id"), 12345)
        self.assertEqual(snapshot(), before)

    def test_a_page_view_is_logged_once_per_page_and_person_within_the_interval_with_ids_only(self):
        self.start()
        for _ in range(3):
            self.client.get(PROFILE)
        self.client.get(BUILDINGS)
        viewed = events(Event.RENDER_AS_PAGE_VIEWED)
        self.assertEqual(sorted(self.details(Event.RENDER_AS_PAGE_VIEWED)), ["page accounts:profile", "page customer:buildings"])
        for event in viewed:
            self.assertEqual((event.account_id, event.actor_id), (self.person.pk, self.staff.pk))

    def test_the_end_button_ends_the_view_and_goes_to_the_page_of_the_person(self):
        self.start()
        response = self.client.post(END)
        self.assertRedirects(
            response, reverse("management:persons_detail", args=[self.person.pk]), fetch_redirect_response=False
        )
        self.assertIsNone(self.session_state())
        self.assertEqual(self.details(Event.RENDER_AS_ENDED), ["reason explicit"])
        # Afterwards the staff member sees their own pages again.
        self.assertNotContains(self.client.get(PROFILE), BANNER)


class EndTests(ViewAsBase):
    def setUp(self):
        super().setUp()
        self.start()

    def assertEnded(self, reason):
        self.assertIsNone(self.session_state())
        self.assertEqual(self.details(Event.RENDER_AS_ENDED), [f"reason {reason}"])

    def test_a_blocked_person_ends_the_view(self):
        self.person.set_status(type(self.person).Status.BLOCKED)
        self.client.get(PROFILE)
        self.assertEnded("subject_changed")

    def test_a_deactivated_person_ends_the_view(self):
        self.person.set_status(type(self.person).Status.DEACTIVATED)
        self.client.get(PROFILE)
        self.assertEnded("subject_changed")

    def test_a_staff_member_who_lost_the_staff_status_ends_the_view_and_sees_the_own_page(self):
        self.staff.set_privileges(is_staff=False, is_superuser=False)
        response = self.client.get(PROFILE)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, BANNER)
        self.assertEnded("staff_changed")

    def test_a_staff_member_who_lost_the_second_factor_ends_the_view(self):
        session = self.client.session
        for key in [key for key in session.keys() if "device" in key.lower()]:
            del session[key]
        session.save()
        self.client.get(PROFILE)
        self.assertIsNone(self.session_state())
        # The second factor check may sign the session out first; either way the view ended once.
        self.assertEqual(len(self.details(Event.RENDER_AS_ENDED)), 1)

    def test_the_logout_ends_the_view(self):
        self.client.post(LOGOUT)
        self.assertEqual(self.details(Event.RENDER_AS_ENDED), ["reason logout"])
        self.assertEqual(self.client.get(PROFILE).status_code, 302)

    def test_the_timeout_ends_the_view_and_the_session(self):
        session = self.client.session
        session[LAST_ACTIVITY_KEY] = time.time() - 10 * 24 * 3600
        session.save()
        response = self.client.get(PROFILE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.details(Event.RENDER_AS_ENDED), ["reason timeout"])
        self.assertEqual(self.client.get(PROFILE).status_code, 302)


class TamperedStateTests(ViewAsBase):
    def tamper(self, value):
        session = self.client.session
        session[render_as.SESSION_KEY] = value
        session.save()
        response = self.client.get(PROFILE)
        self.assertNotContains(response, BANNER)
        self.assertIsNone(self.session_state())
        return response

    def test_a_state_that_is_no_dict_ends_the_view(self):
        for value in ("text", 5, [self.person.pk], True):
            with self.subTest(value=value):
                self.tamper(value)

    def test_a_subject_of_the_wrong_type_or_the_own_account_or_an_unknown_one_ends_the_view(self):
        for subject in (str(self.person.pk), True, 1.0, None, self.staff.pk, 987654321, [self.person.pk]):
            with self.subTest(subject=subject):
                self.tamper({"subject": subject, "started_at": 1})

    def test_the_tampered_state_shows_the_pages_of_the_staff_member_only(self):
        response = self.tamper({"subject": str(self.person.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.person.display_name)

    def test_a_customer_cannot_use_the_state_to_see_another_customer(self):
        client = login(customer("view.as.intruder@example.org"))
        session = client.session
        session[render_as.SESSION_KEY] = {"subject": self.person.pk, "started_at": int(time.time())}
        session.save()
        response = client.get(PROFILE)
        self.assertNotContains(response, BANNER)
        self.assertNotContains(response, self.person.email)
        self.assertEqual(client.get(reverse("customer:application_start")).status_code, 200)


class BannerAndHeaderTests(ViewAsBase):
    def setUp(self):
        super().setUp()
        self.start()

    def test_the_bar_is_on_every_allowed_page_and_names_the_person(self):
        for path in ALLOWED_PATHS:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertContains(response, BANNER)
                self.assertContains(response, self.person.display_name)
                self.assertContains(response, f'action="{END}"')

    def test_the_refusal_page_has_the_bar_too_and_a_way_back(self):
        response = self.client.get(reverse("customer:application_start"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, BANNER, status_code=403)

    def test_every_response_in_the_view_is_not_stored(self):
        for path in [*ALLOWED_PATHS, reverse("customer:application_start"), "/management/"]:
            with self.subTest(path=path):
                control = self.client.get(path)["Cache-Control"]
                self.assertIn("no-store", control)
                self.assertIn("private", control)

    def test_the_bar_is_gone_after_the_end(self):
        self.client.post(END)
        self.assertNotContains(self.client.get(PROFILE), BANNER)
