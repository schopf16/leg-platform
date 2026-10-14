"""Tests of the loaded settings and of Django's behaviour with them (docs/SECURITY.md, 11)."""

import re

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.views.debug import SafeExceptionReporterFilter, get_default_exception_reporter_filter

from leg_platform.debug import ExceptionReporterFilter
from leg_platform.tests.test_responses import EXPECTED_CSP


class PasswordValidationTests(SimpleTestCase):
    def test_minimum_length_is_15(self):
        with self.assertRaises(ValidationError) as context:
            validate_password("vq7#Lm2!pX9zR4")  # 14 characters
        codes = [error.code for error in context.exception.error_list]
        self.assertIn("password_too_short", codes)
        validate_password("vq7#Lm2!pX9zR4w")  # 15 characters

    def test_64_character_passphrase_is_accepted(self):
        passphrase = "copper kettle hums beside quiet orchard lanterns at dawn, twelve"
        self.assertEqual(len(passphrase), 64)
        validate_password(passphrase)

    def test_long_passphrase_without_composition_rules_is_accepted(self):
        # Only lower-case letters and spaces: no composition rules apply.
        validate_password("meadow larks sing over the old stone bridge " * 3)


class CookieTests(SimpleTestCase):
    """Django accepts the "__Host-" names and sets the attributes the prefix requires."""

    def setUp(self):
        self.factory = RequestFactory()

    def _csrf_cookie(self):
        request = self.factory.get("/", secure=True)

        def view(request):
            get_token(request)
            return HttpResponse()

        response = CsrfViewMiddleware(view)(request)
        return response.cookies[settings.CSRF_COOKIE_NAME]

    def _session_cookie(self):
        request = self.factory.get("/", secure=True)

        def view(request):
            request.session["key"] = "value"
            return HttpResponse()

        response = SessionMiddleware(view)(request)
        return response.cookies[settings.SESSION_COOKIE_NAME]

    def _assert_host_prefix_requirements(self, cookie):
        self.assertTrue(cookie.key.startswith("__Host-"))
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["domain"], "")
        self.assertNotIn("Domain=", cookie.output())

    @override_settings(CSRF_COOKIE_NAME="__Host-csrftoken", CSRF_COOKIE_SECURE=True)
    def test_csrf_cookie_with_host_prefix(self):
        cookie = self._csrf_cookie()
        self._assert_host_prefix_requirements(cookie)
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertTrue(cookie["httponly"])

    @override_settings(
        SESSION_COOKIE_NAME="__Host-sessionid",
        SESSION_COOKIE_SECURE=True,
        # Signed cookie sessions need no database; the cookie attributes are the same.
        SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
    )
    def test_session_cookie_with_host_prefix(self):
        cookie = self._session_cookie()
        self._assert_host_prefix_requirements(cookie)
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")

    def test_cookie_domains_are_not_set(self):
        self.assertIsNone(settings.SESSION_COOKIE_DOMAIN)
        self.assertIsNone(settings.CSRF_COOKIE_DOMAIN)
        self.assertIs(settings.SESSION_COOKIE_HTTPONLY, True)
        self.assertIs(settings.CSRF_COOKIE_HTTPONLY, True)


class HeaderTests(SimpleTestCase):
    def test_security_header_settings(self):
        self.assertIs(settings.SECURE_CONTENT_TYPE_NOSNIFF, True)
        self.assertEqual(settings.SECURE_REFERRER_POLICY, "same-origin")
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")

    @override_settings(ALLOWED_HOSTS=["portal.example.org"])
    def test_disallowed_host_header_is_rejected(self):
        with self.assertLogs("django.security.DisallowedHost", "ERROR"):
            response = self.client.get(
                "/admin/login/", headers={"host": "attacker.example"}
            )
        self.assertEqual(response.status_code, 400)


class ContentSecurityPolicyTests(SimpleTestCase):
    """The policy of docs/SECURITY.md, section 6, with the settings of the test run (D-028).

    The test run uses the local settings; the production settings are covered by
    test_responses and test_production_settings.
    """

    def test_middleware_and_policy(self):
        middleware = list(settings.MIDDLEWARE)
        security = middleware.index("django.middleware.security.SecurityMiddleware")
        self.assertEqual(
            middleware[security + 1], "django.middleware.csp.ContentSecurityPolicyMiddleware"
        )
        self.assertEqual(middleware.count("django.middleware.csp.ContentSecurityPolicyMiddleware"), 1)
        self.assertEqual(
            {directive: [str(value) for value in values] for directive, values in settings.SECURE_CSP.items()},
            {
                "default-src": ["'self'"],
                "script-src": ["'self'"],
                "style-src": ["'self'"],
                "img-src": ["'self'"],
                "font-src": ["'self'"],
                "object-src": ["'none'"],
                "base-uri": ["'none'"],
                "form-action": ["'self'"],
                "frame-ancestors": ["'none'"],
            },
        )
        self.assertEqual(settings.SECURE_CSP_REPORT_ONLY, {})
        values = [value for values in settings.SECURE_CSP.values() for value in values]
        for forbidden in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "*", "data:"):
            self.assertNotIn(forbidden, values)
        # No nonce: inline scripts and styles are not allowed at all.
        self.assertNotIn("<CSP_NONCE_SENTINEL>", values)

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_header_on_the_redirect_from_the_admin_login_page(self):
        # The administration has no login page of its own; it leads to the portal login (D-040).
        response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Content-Security-Policy"], EXPECTED_CSP)


class LoginProtectionSettingsTests(SimpleTestCase):
    def test_axes_uses_a_separate_normalised_account_bucket_and_ip_handler(self):
        self.assertEqual(
            settings.AUTHENTICATION_BACKENDS,
            [
                "axes.backends.AxesStandaloneBackend",
                "leg_platform.accounts.backends.EmailModelBackend",
            ],
        )
        self.assertEqual(
            settings.AXES_HANDLER,
            "leg_platform.accounts.axes_handler.LoginProtectionHandler",
        )
        self.assertEqual(settings.AXES_LOCKOUT_PARAMETERS, ["username"])
        self.assertEqual(settings.AXES_FAILURE_LIMIT, 5)
        self.assertEqual(settings.AXES_COOLOFF_TIME.total_seconds(), 900)
        self.assertIn("axes.W006", settings.SILENCED_SYSTEM_CHECKS)
        self.assertFalse(settings.AXES_ENABLE_ADMIN)


class InternationalizationTests(SimpleTestCase):
    def test_language_and_time_zone(self):
        self.assertEqual(settings.LANGUAGE_CODE, "de-ch")
        self.assertEqual([code for code, _name in settings.LANGUAGES], ["de-ch"])
        self.assertIs(settings.USE_I18N, True)
        self.assertEqual(settings.TIME_ZONE, "Europe/Zurich")
        self.assertIs(settings.USE_TZ, True)
        self.assertEqual(settings.LOCALE_PATHS, [settings.BASE_DIR / "locale"])


class TestRunnerEmailTests(SimpleTestCase):
    """Automated tests never send real email (D-028).

    Django's test runner replaces the configured backend with the locmem backend:
    EMAIL_BACKEND in Django 6.0, every alias of MAILERS in Django 6.1 (django/test/utils.py).
    """

    LOCMEM = "django.core.mail.backends.locmem.EmailBackend"

    def test_backend_is_locmem_during_tests(self):
        if hasattr(settings, "MAILERS"):
            self.assertTrue(settings.MAILERS)
            for alias, config in settings.MAILERS.items():
                with self.subTest(alias=alias):
                    self.assertEqual(config["BACKEND"], self.LOCMEM)
        else:
            self.assertEqual(settings.EMAIL_BACKEND, self.LOCMEM)

    def test_sent_email_stays_in_the_outbox(self):
        sent = mail.send_mail("Subject", "Body", "from@example.org", ["to@example.org"])
        self.assertEqual(sent, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["to@example.org"])


class ExceptionReporterFilterTests(SimpleTestCase):
    """Debug pages and error reports mask the allowed recipients (D-028)."""

    def test_filter_is_configured(self):
        self.assertEqual(
            settings.DEFAULT_EXCEPTION_REPORTER_FILTER,
            "leg_platform.debug.ExceptionReporterFilter",
        )
        self.assertIsInstance(get_default_exception_reporter_filter(), ExceptionReporterFilter)

    def test_allowed_recipients_are_masked(self):
        reporter_filter = ExceptionReporterFilter()
        substitute = reporter_filter.cleansed_substitute
        self.assertEqual(
            reporter_filter.cleanse_setting("EMAIL_ALLOWED_RECIPIENTS", ["tester@example.org"]),
            substitute,
        )
        with override_settings(EMAIL_ALLOWED_RECIPIENTS=["tester@example.org"]):
            safe = reporter_filter.get_safe_settings()
        self.assertEqual(safe["EMAIL_ALLOWED_RECIPIENTS"], substitute)
        request = RequestFactory().get("/", EMAIL_ALLOWED_RECIPIENTS="tester@example.org")
        meta = reporter_filter.get_safe_request_meta(request)
        self.assertEqual(meta["EMAIL_ALLOWED_RECIPIENTS"], substitute)

    def test_pattern_extends_djangos_pattern(self):
        default = SafeExceptionReporterFilter.hidden_settings
        self.assertEqual(
            ExceptionReporterFilter.hidden_settings.pattern, default.pattern + "|RECIPIENTS"
        )
        self.assertTrue(ExceptionReporterFilter.hidden_settings.flags & re.IGNORECASE)

    def test_django_defaults_are_kept(self):
        reporter_filter = ExceptionReporterFilter()
        for name in ("SECRET_KEY", "DB_PASSWORD", "API_TOKEN", "HTTP_COOKIE", "AUTH_HEADER"):
            with self.subTest(name=name):
                self.assertEqual(
                    reporter_filter.cleanse_setting(name, "value"),
                    reporter_filter.cleansed_substitute,
                )
        self.assertEqual(reporter_filter.cleanse_setting("EMAIL_MODE", "console"), "console")
