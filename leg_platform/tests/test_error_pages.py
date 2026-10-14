"""Error pages (D-054): translated, styled, without technical detail, under the Content Security
Policy, and without a database or a session for the pages that must not need one."""

from django.conf import settings
from django.http import Http404
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.utils import translation
from django.views.defaults import page_not_found, permission_denied, server_error

from leg_platform.tests.test_environment import STRONG_KEY, TEST_DB_PASSWORD
from leg_platform.tests.urls import INTERNAL_DETAIL

SHARP_S = "\u00df"
SECRET_PATH = "/secret-looking-path-17/"


def assert_no_inline_code(case, html):
    case.assertNotIn("<style", html.lower())
    case.assertNotRegex(html, r"(?i)\sstyle\s*=")
    # Scripts only as files of the own origin (the corner buttons, D-056), never inline.
    case.assertNotRegex(html, r"(?i)<script(?![^>]*\ssrc=\"/static/)")
    case.assertNotRegex(html, r"(?i)\son[a-z]+\s*=")
    case.assertNotIn("javascript:", html.lower())


class ErrorPageTests(SimpleTestCase):
    """The pages are rendered through Django's middleware; SimpleTestCase forbids database
    queries, so a page that needs the database or the session would fail here."""

    def check_common(self, response, status):
        html = response.content.decode()
        self.assertEqual(response.status_code, status)
        self.assertIn('lang="de-ch"', html)
        self.assertNotIn(SHARP_S, html)
        assert_no_inline_code(self, html)
        policy = response["Content-Security-Policy"]
        self.assertIn("default-src 'self'", policy)
        self.assertNotIn("'unsafe-inline'", policy)
        # The page is styled by the stylesheets of the portal, served by the instance itself.
        self.assertIn("css/portal.css", html)
        self.assertIn('class="btn btn-primary', html)
        # No detail of the request, the exception or the configuration.
        for detail in ("Traceback", "DEBUG", "CSRF", "Origin", settings.SECRET_KEY, STRONG_KEY,
                       TEST_DB_PASSWORD, SECRET_PATH):
            self.assertNotIn(detail, html)
        return html

    def test_not_found_is_swiss_german_styled_and_gives_a_way_out(self):
        response = Client().get(SECRET_PATH)

        html = self.check_common(response, 404)

        self.assertTemplateUsed(response, "404.html")
        self.assertIn("Seite nicht gefunden", html)
        self.assertIn("Zur Startseite", html)
        self.assertIn('href="/"', html)
        self.assertNotIn("The requested resource", html)

    def test_csrf_failure_is_swiss_german_and_leads_to_the_login_page(self):
        client = Client(enforce_csrf_checks=True)

        with self.assertLogs("django.security.csrf", "WARNING"):
            response = client.post("/login/", {"username": "a@example.org", "password": "x"})

        html = self.check_common(response, 403)
        self.assertTemplateUsed(response, "403_csrf.html")
        self.assertIn("Ihre Eingabe konnte nicht verarbeitet werden", html)
        self.assertIn("Es wurde nichts gespeichert", html)
        self.assertIn('href="/login/"', html)
        self.assertIn("Zurück zur Anmeldeseite", html)

    def test_permission_denied_shows_no_message_of_the_exception(self):
        request = RequestFactory().get("/admin/")

        response = permission_denied(request, PermissionError("detail-of-the-exception"))
        html = response.content.decode()

        self.assertEqual(response.status_code, 403)
        self.assertIn("Zugriff verweigert", html)
        self.assertNotIn("detail-of-the-exception", html)
        self.assertNotIn(SHARP_S, html)

    def test_the_not_found_page_does_not_echo_the_exception_or_the_path(self):
        request = RequestFactory().get(SECRET_PATH)

        html = page_not_found(request, Http404("detail-of-the-exception")).content.decode()

        self.assertNotIn("detail-of-the-exception", html)
        self.assertNotIn(SECRET_PATH, html)

    def test_the_pages_are_English_source_texts_in_another_language(self):
        # The source texts are English: without a translation the page is still readable.
        with translation.override("en"):
            html = page_not_found(RequestFactory().get("/x/"), Http404()).content.decode()

        self.assertIn("Page not found", html)
        self.assertNotIn("Seite nicht gefunden", html)

    @override_settings(
        ROOT_URLCONF="leg_platform.tests.urls", DEBUG=False
    )
    def test_server_error_needs_no_request_database_or_context_processor(self):
        # Django renders 500.html without a request: no context processor, no session, no
        # database. A template that needed one of them would raise here.
        response = server_error(RequestFactory().get("/error/"))
        html = response.content.decode()

        self.assertEqual(response.status_code, 500)
        self.assertIn("Etwas ist schiefgelaufen", html)
        self.assertIn("Das tut uns leid", html)
        self.assertIn("Zur Startseite", html)
        self.assertIn("css/portal.css", html)
        self.assertNotIn(SHARP_S, html)
        assert_no_inline_code(self, html)

    @override_settings(ROOT_URLCONF="leg_platform.tests.urls", DEBUG=False)
    def test_server_error_through_the_middleware_has_the_policy_and_no_detail(self):
        client = Client(raise_request_exception=False)

        with self.assertLogs("django.request", "ERROR"):
            response = client.get("/error/")
        html = response.content.decode()

        self.assertEqual(response.status_code, 500)
        self.assertTemplateUsed(response, "500.html")
        self.assertIn("default-src 'self'", response["Content-Security-Policy"])
        for detail in (INTERNAL_DETAIL, "Traceback", "RuntimeError", "leg_platform",
                       settings.SECRET_KEY, STRONG_KEY, TEST_DB_PASSWORD):
            self.assertNotIn(detail, html)
