"""Tests of the instance configuration and of the token redaction in logs."""

import logging

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.urls import reverse

from leg_platform.environment import DEFAULT_INSTANCE_NAME, load_environment
from leg_platform.log_filters import REDACTED, TokenRedactionFilter, redact_tokens
from leg_platform.tests.test_environment import make_env

SECRET = "A" * 43


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


class SenderAddressTests(SimpleTestCase):
    def test_the_default_is_noreply_at_the_host_of_the_base_url(self):
        config = load_environment(make_env(INSTANCE_BASE_URL="https://portal.example.org"))

        self.assertEqual(config.email_sender_address, "noreply@portal.example.org")

    def test_local_hosts_without_a_domain_use_localhost(self):
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host=host):
                config = load_environment(
                    make_env(
                        "local",
                        DJANGO_ALLOWED_HOSTS=host,
                        INSTANCE_BASE_URL=f"http://{host}:8000",
                    )
                )
                self.assertEqual(config.email_sender_address, "noreply@localhost")

    def test_the_configured_sender_wins(self):
        config = load_environment(make_env(EMAIL_SENDER_ADDRESS="Portal@Example.org"))

        self.assertEqual(config.email_sender_address, "portal@example.org")

    def test_an_invalid_sender_refuses_the_start(self):
        for value in ("portal", "a@b", "a b@example.org", "x@example.org, y@example.org", "*@example.org"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "EMAIL_SENDER_ADDRESS"):
                    load_environment(make_env(EMAIL_SENDER_ADDRESS=value))


class TokenRedactionTests(SimpleTestCase):
    def test_the_pattern_matches_the_url_of_the_verification_view(self):
        path = reverse("accounts:registration_verify", args=[SECRET])

        self.assertEqual(
            redact_tokens(f"Not Found: {path}"), f"Not Found: /register/verify/{REDACTED}/"
        )

    def test_the_pattern_matches_the_url_of_the_password_reset_view(self):
        path = reverse("accounts:password_reset_verify", args=[SECRET])

        self.assertEqual(
            redact_tokens(f"Not Found: {path}"), f"Not Found: /password-reset/verify/{REDACTED}/"
        )

    def test_the_pattern_matches_the_url_of_the_second_factor_reset_view(self):
        path = reverse("accounts:two_factor_reset_verify", args=[SECRET])

        self.assertEqual(
            redact_tokens(f"Not Found: {path}"),
            f"Not Found: /two-factor-reset/verify/{REDACTED}/",
        )

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
