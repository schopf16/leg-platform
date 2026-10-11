"""Response-level tests with settings that mirror production (docs/SECURITY.md, section 11).

The security settings are derived from ``security_settings()`` for ``APP_ENV=production``, the
same function the settings module uses, and applied with ``override_settings``. The requests
go through Django's full middleware stack via the test client; no database is needed.
"""

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings

from leg_platform.environment import DEFAULT_HSTS_SECONDS, load_environment, security_settings
from leg_platform.tests.test_environment import STRONG_KEY, TEST_DB_PASSWORD, make_env
from leg_platform.tests.urls import INTERNAL_DETAIL

APP_HOST = "portal.example.org"
# The instance's website on the parent domain; it is never trusted for CSRF (D-025).
WEBSITE_ORIGIN = "https://example.org"

PRODUCTION_SECURITY = security_settings(load_environment(make_env("production")))


@override_settings(
    ROOT_URLCONF="leg_platform.tests.urls",
    ALLOWED_HOSTS=[APP_HOST],
    CSRF_TRUSTED_ORIGINS=[],
    DEBUG=False,
    **PRODUCTION_SECURITY,
)
class ProductionResponseTests(SimpleTestCase):
    def test_settings_mirror_production(self):
        self.assertIs(settings.SECURE_SSL_REDIRECT, True)
        self.assertEqual(settings.CSRF_COOKIE_NAME, "__Host-csrftoken")

    def test_plain_http_is_redirected_to_https(self):
        response = self.client.get("/ok/", headers={"host": APP_HOST})
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], f"https://{APP_HOST}/ok/")

    def test_security_headers(self):
        response = self.client.get("/ok/", secure=True, headers={"host": APP_HOST})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        # Default: no includeSubDomains and never preload (D-028).
        self.assertEqual(
            response["Strict-Transport-Security"], f"max-age={DEFAULT_HSTS_SECONDS}"
        )

    @override_settings(SECURE_HSTS_INCLUDE_SUBDOMAINS=True)
    def test_hsts_include_subdomains_when_enabled(self):
        response = self.client.get("/ok/", secure=True, headers={"host": APP_HOST})
        self.assertEqual(
            response["Strict-Transport-Security"],
            f"max-age={DEFAULT_HSTS_SECONDS}; includeSubDomains",
        )

    def test_csrf_cookie_attributes(self):
        response = self.client.get("/ok/", secure=True, headers={"host": APP_HOST})
        cookie = response.cookies["__Host-csrftoken"]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["domain"], "")

    def _post_with_origin(self, origin):
        client = Client(enforce_csrf_checks=True)
        response = client.get("/ok/", secure=True, headers={"host": APP_HOST})
        token = response.cookies["__Host-csrftoken"].value
        return client.post(
            "/ok/",
            {"csrfmiddlewaretoken": token},
            secure=True,
            headers={"host": APP_HOST, "origin": origin},
        )

    def test_post_with_origin_of_the_website_is_rejected(self):
        with self.assertLogs("django.security.csrf", "WARNING") as logs:
            response = self._post_with_origin(WEBSITE_ORIGIN)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Origin checking failed", logs.output[0])

    def test_post_with_own_origin_is_accepted(self):
        # Control: the same request with the application's own origin passes, so the
        # rejection above is caused by the Origin header alone.
        response = self._post_with_origin(f"https://{APP_HOST}")
        self.assertEqual(response.status_code, 200)

    def test_server_error_page_reveals_no_internal_details(self):
        client = Client(raise_request_exception=False)
        with self.assertLogs("django.request", "ERROR"):
            response = client.get("/error/", secure=True, headers={"host": APP_HOST})
        self.assertEqual(response.status_code, 500)
        content = response.content.decode()
        for detail in (
            INTERNAL_DETAIL,
            "Traceback",
            "RuntimeError",
            "leg_platform",
            settings.SECRET_KEY,
            STRONG_KEY,
            TEST_DB_PASSWORD,
        ):
            self.assertNotIn(detail, content)

    def test_disallowed_host_is_rejected(self):
        with self.assertLogs("django.security.DisallowedHost", "ERROR"):
            response = self.client.get(
                "/ok/", secure=True, headers={"host": "attacker.example"}
            )
        self.assertEqual(response.status_code, 400)
