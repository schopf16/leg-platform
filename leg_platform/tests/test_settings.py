"""Tests of the loaded settings and of Django's behaviour with them (docs/SECURITY.md, 11)."""

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.test import RequestFactory, SimpleTestCase, override_settings

from leg_platform.mail import GraphNotImplementedEmailBackend


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


class InternationalizationTests(SimpleTestCase):
    def test_language_and_time_zone(self):
        self.assertEqual(settings.LANGUAGE_CODE, "de-ch")
        self.assertEqual([code for code, _name in settings.LANGUAGES], ["de-ch"])
        self.assertIs(settings.USE_I18N, True)
        self.assertEqual(settings.TIME_ZONE, "Europe/Zurich")
        self.assertIs(settings.USE_TZ, True)
        self.assertEqual(settings.LOCALE_PATHS, [settings.BASE_DIR / "locale"])


class PlaceholderEmailBackendTests(SimpleTestCase):
    def test_refuses_to_send(self):
        message = mail.EmailMessage("Subject", "Body", "from@example.org", ["to@example.org"])
        with self.assertRaises(NotImplementedError):
            GraphNotImplementedEmailBackend().send_messages([message])
