"""Tests of the instance configuration and of the token redaction in logs."""

import logging
import re

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.urls import get_resolver, reverse
from django.urls.resolvers import RegexPattern

from leg_platform.environment import DEFAULT_INSTANCE_NAME, load_environment
from leg_platform.log_filters import REDACTED, TokenRedactionFilter, redact_tokens
from leg_platform.tests.test_environment import make_env

SECRET = "A" * 43
TOKEN_PARAMETER = re.compile(r"<(?:\w+:)?token>")


def token_routes():
    """Return ``(route, path)`` of every route of the URL configuration with a ``token`` parameter.

    The path is the route with the token replaced by ``SECRET``. The URL configuration is
    walked, so a new token route is found without a change of this test (security review of
    unit A, L-2).
    """
    found = []

    def walk(patterns, prefix):
        for entry in patterns:
            route = prefix + str(entry.pattern)
            if isinstance(entry.pattern, RegexPattern):
                if "token" in route:
                    raise AssertionError(f"A regular-expression route with a token: {route}")
                continue
            if hasattr(entry, "url_patterns"):
                walk(entry.url_patterns, route)
            elif TOKEN_PARAMETER.search(route):
                found.append((route, "/" + TOKEN_PARAMETER.sub(SECRET, route)))

    walk(get_resolver().url_patterns, "")
    return found


class InstanceConfigurationTests(SimpleTestCase):
    def test_local_defaults_need_no_instance_values(self):
        config = load_environment(
            make_env("local", INSTANCE_BASE_URL=None, INSTANCE_PRIVACY_URL=None)
        )

        self.assertEqual(config.instance_name, DEFAULT_INSTANCE_NAME)
        self.assertEqual(config.instance_base_url, "http://127.0.0.1:8000")
        self.assertEqual(config.instance_privacy_url, "")
        self.assertEqual(config.instance_website_url, "")

    def test_outside_local_the_base_url_and_the_privacy_url_are_required(self):
        for app_env in ("stage", "production"):
            for name in ("INSTANCE_BASE_URL", "INSTANCE_PRIVACY_URL"):
                with self.subTest(app_env=app_env, name=name):
                    with self.assertRaisesMessage(ImproperlyConfigured, name):
                        load_environment(make_env(app_env, **{name: None}))

    def test_values_are_read_and_normalised(self):
        config = load_environment(
            make_env(
                INSTANCE_NAME="Example Community",
                INSTANCE_BASE_URL="https://portal.example.org/",
                INSTANCE_WEBSITE_URL="https://www.example.org/",
            )
        )

        self.assertEqual(config.instance_name, "Example Community")
        self.assertEqual(config.instance_base_url, "https://portal.example.org")
        self.assertEqual(config.instance_website_url, "https://www.example.org/")

    def test_the_base_url_must_be_an_https_origin_of_an_allowed_host(self):
        invalid = (
            "http://portal.example.org",
            "https://portal.example.org/login",
            "https://portal.example.org/?a=b",
            "https://portal.example.org#x",
            "https://user:secret@portal.example.org",
            "https://other.example.org",
            "https://portal.example.org:99999",
            "portal.example.org",
            "javascript:alert(1)",
            "https://por tal.example.org",
            # Control and format characters, and an unclosed bracket (review of phase 2, E-1/E-2).
            "https://portal.example.org\x7f",
            "https://portal.example.org\u200b",
            "https://portal.example.org\u2028x",
            "https://[::1",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "INSTANCE_BASE_URL"):
                    load_environment(make_env(INSTANCE_BASE_URL=value))

    def test_local_allows_http_for_the_development_server(self):
        config = load_environment(
            make_env(
                "local",
                DJANGO_ALLOWED_HOSTS="127.0.0.1",
                INSTANCE_BASE_URL="http://127.0.0.1:8000",
            )
        )

        self.assertEqual(config.instance_base_url, "http://127.0.0.1:8000")

    def test_the_host_is_compared_without_case(self):
        config = load_environment(make_env(INSTANCE_BASE_URL="https://PORTAL.example.org"))

        self.assertEqual(config.instance_base_url, "https://PORTAL.example.org")

    def test_links_must_be_absolute_https_urls(self):
        for name in ("INSTANCE_PRIVACY_URL", "INSTANCE_WEBSITE_URL"):
            for value in (
                "http://www.example.org/",
                "/privacy",
                "javascript:alert(1)",
                "https://user:secret@www.example.org/",
                "https://www.example.org/#fragment",
                "https://www.example.org/ x",
            ):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesMessage(ImproperlyConfigured, name):
                        load_environment(make_env("local", **{name: value}))

    def test_the_instance_name_is_limited_and_has_no_control_characters(self):
        for value in ("x" * 101, "Name\nBcc: someone", "Name\x00", "Na\u2028me"):
            with self.subTest(value=repr(value)):
                with self.assertRaisesMessage(ImproperlyConfigured, "INSTANCE_NAME"):
                    load_environment(make_env(INSTANCE_NAME=value))

    def test_error_messages_do_not_contain_the_values(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            load_environment(make_env(INSTANCE_PRIVACY_URL="http://secret-host.example.org/"))

        self.assertNotIn("secret-host", str(caught.exception))


class TokenRedactionTests(SimpleTestCase):
    def test_the_walk_of_the_url_configuration_finds_the_token_routes(self):
        # Guards the enumeration itself: an empty result would let the tests below pass.
        routes = [route for route, _path in token_routes()]

        self.assertGreaterEqual(len(routes), 5, routes)
        self.assertIn("register/verify/<slug:token>/", routes)

    def test_every_route_with_a_token_is_redacted(self):
        for route, path in token_routes():
            with self.subTest(route=route):
                self.assertEqual(
                    redact_tokens(f"Not Found: {path}"),
                    f"Not Found: {path.replace(SECRET, REDACTED)}",
                )

    def test_every_route_with_a_token_is_redacted_with_percent_encoded_slashes(self):
        # The server decodes the path before it routes, so the encoded form reaches the view
        # (security review of unit A, L-1); the request line of the development server is raw.
        for route, path in token_routes():
            variants = (
                path.replace("/", "%2F"),
                path.replace("/", "%2f"),
                path.replace("/", "%2F", 2),
                path.replace("/", "%2f", 1),
            )
            for variant in variants:
                with self.subTest(route=route, variant=variant):
                    for line in (f"Not Found: {variant}", f'"GET {variant} HTTP/1.1" 302 0'):
                        redacted = redact_tokens(line)
                        self.assertNotIn(SECRET, redacted)
                        self.assertIn(REDACTED, redacted)

    def test_the_fixed_pages_and_the_confirmation_pages_are_not_redacted(self):
        for name in (
            "email_change_invalid",
            "email_change_stop_invalid",
            "two_factor_reset_invalid",
            "email_change_confirm",
            "email_change_stop_confirm",
            "two_factor_reset_confirm",
            "registration_invalid",
            "password_reset_invalid",
        ):
            path = reverse(f"accounts:{name}")
            with self.subTest(name=name):
                self.assertEqual(redact_tokens(f"Not Found: {path}"), f"Not Found: {path}")

    def test_the_request_lines_of_the_development_server_are_redacted(self):
        line = f'"GET /register/verify/{SECRET}/ HTTP/1.1" 302 0'

        self.assertNotIn(SECRET, redact_tokens(line))
        self.assertIn("HTTP/1.1", redact_tokens(line))

    def test_the_filter_redacts_messages_arguments_and_exceptions(self):
        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        logger = logging.getLogger("test.redaction")
        logger.propagate = False
        handler = Collect()
        handler.addFilter(TokenRedactionFilter())
        logger.addHandler(handler)
        try:
            logger.warning("Not Found: %s", f"/register/verify/{SECRET}/")
            try:
                raise ValueError(f"/register/verify/{SECRET}/")
            except ValueError:
                logger.exception("failed")
        finally:
            logger.removeHandler(handler)

        self.assertEqual(len(records), 2)
        for record in records:
            self.assertNotIn(SECRET, record)
            self.assertIn(REDACTED, record)

    def test_other_log_messages_are_unchanged(self):
        self.assertEqual(redact_tokens("Not Found: /profile/"), "Not Found: /profile/")

    def test_the_filter_is_attached_to_the_handler_of_every_logger(self):
        from django.conf import settings

        self.assertEqual(settings.LOGGING["handlers"]["console"]["filters"], ["redact_tokens"])
        for name in ("django", "django.server"):
            self.assertEqual(settings.LOGGING["loggers"][name]["handlers"], ["console"])
        self.assertEqual(settings.LOGGING["root"]["handlers"], ["console"])

    def test_msal_response_bodies_are_not_logged_below_critical(self):
        from django.conf import settings

        # msal/oauth2cli/oauth2.py logs a non-JSON token response body at ERROR level.
        entry = settings.LOGGING["loggers"]["msal.oauth2cli.oauth2"]
        self.assertEqual(entry["level"], "CRITICAL")
        self.assertEqual(entry["handlers"], ["console"])
        self.assertFalse(entry["propagate"])
        self.assertFalse(logging.getLogger("msal.oauth2cli.oauth2").isEnabledFor(logging.ERROR))

    def test_msal_signing_exception_details_are_not_logged_below_critical(self):
        from django.conf import settings

        entry = settings.LOGGING["loggers"]["msal.oauth2cli.assertion"]
        self.assertEqual(entry["level"], "CRITICAL")
        self.assertEqual(entry["handlers"], ["console"])
        self.assertFalse(entry["propagate"])
        self.assertFalse(logging.getLogger("msal.oauth2cli.assertion").isEnabledFor(logging.ERROR))
