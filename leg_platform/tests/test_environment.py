"""Tests of the environment validation (docs/SECURITY.md, sections 3, 6 and 11)."""

import os
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from leg_platform.environment import (
    DEFAULT_HSTS_SECONDS,
    REPOSITORY_ROOT,
    email_backend,
    load_environment,
    security_settings,
    silenced_system_checks,
)

# Test values only; not secrets of any environment.
STRONG_KEY = "test-key-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ"
OTHER_STRONG_KEY = "other-test-key-" + "ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210zyxw"
TEST_DB_PASSWORD = "test-password"

DEFAULT_EMAIL_MODE = {"local": "console", "stage": "graph", "production": "graph"}


def make_env(app_env="production", **overrides):
    """Return a valid environment for app_env; an override of None removes the variable."""
    env = {
        "APP_ENV": app_env,
        "DJANGO_SECRET_KEY": STRONG_KEY,
        "DJANGO_ALLOWED_HOSTS": "portal.example.org",
        "INSTANCE_BASE_URL": "https://portal.example.org",
        "INSTANCE_PRIVACY_URL": "https://www.example.org/privacy",
        "EMAIL_MODE": DEFAULT_EMAIL_MODE.get(app_env, "console"),
        "DB_NAME": "leg_platform",
        "DB_USER": "leg_platform",
        "DB_PASSWORD": TEST_DB_PASSWORD,
    }
    env.update(overrides)
    return {name: value for name, value in env.items() if value is not None}


class AppEnvTests(SimpleTestCase):
    def test_missing_app_env_refuses_start(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "APP_ENV"):
            load_environment(make_env(APP_ENV=None))

    def test_empty_app_env_refuses_start(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "APP_ENV"):
            load_environment(make_env(APP_ENV=""))

    def test_unknown_app_env_refuses_start(self):
        for value in ("development", "prod", "LOCAL", "test"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "APP_ENV"):
                    load_environment(make_env(APP_ENV=value))

    def test_known_app_envs_are_accepted(self):
        for value in ("local", "stage", "production"):
            with self.subTest(value=value):
                self.assertEqual(load_environment(make_env(value)).app_env, value)


class DebugTests(SimpleTestCase):
    def test_debug_defaults_to_false(self):
        self.assertFalse(load_environment(make_env("local")).debug)

    def test_debug_allowed_locally(self):
        self.assertTrue(load_environment(make_env("local", DJANGO_DEBUG="True")).debug)

    def test_debug_outside_local_refuses_start(self):
        for app_env in ("stage", "production"):
            with self.subTest(app_env=app_env):
                with self.assertRaisesMessage(ImproperlyConfigured, "DJANGO_DEBUG"):
                    load_environment(make_env(app_env, DJANGO_DEBUG="True"))

    def test_invalid_debug_value_refuses_start(self):
        for value in ("1", "yes", "on", "maybe"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "DJANGO_DEBUG"):
                    load_environment(make_env("local", DJANGO_DEBUG=value))


class SecretKeyTests(SimpleTestCase):
    def test_missing_secret_key_refuses_start(self):
        for app_env in ("local", "stage", "production"):
            with self.subTest(app_env=app_env):
                with self.assertRaisesMessage(ImproperlyConfigured, "DJANGO_SECRET_KEY"):
                    load_environment(make_env(app_env, DJANGO_SECRET_KEY=None))

    def test_weak_secret_key_refused_outside_local(self):
        weak_keys = ("short", "a" * 60, "django-insecure-" + STRONG_KEY)
        for app_env in ("stage", "production"):
            for key in weak_keys:
                with self.subTest(app_env=app_env, key_length=len(key)):
                    with self.assertRaises(ImproperlyConfigured):
                        load_environment(make_env(app_env, DJANGO_SECRET_KEY=key))

    def test_error_message_does_not_contain_secret_key(self):
        key = "secret-value-that-is-too-short"
        with self.assertRaises(ImproperlyConfigured) as context:
            load_environment(make_env(DJANGO_SECRET_KEY=key))
        self.assertNotIn(key, str(context.exception))

    def test_repr_does_not_contain_secrets(self):
        config = load_environment(make_env())
        self.assertNotIn(STRONG_KEY, repr(config))
        self.assertNotIn(TEST_DB_PASSWORD, repr(config))

    def test_fallbacks_are_parsed_and_checked(self):
        config = load_environment(make_env(DJANGO_SECRET_KEY_FALLBACKS=OTHER_STRONG_KEY))
        self.assertEqual(config.secret_key_fallbacks, (OTHER_STRONG_KEY,))
        with self.assertRaises(ImproperlyConfigured):
            load_environment(make_env(DJANGO_SECRET_KEY_FALLBACKS="short"))

    def test_error_message_does_not_contain_fallback_keys(self):
        weak_fallback = "weak-fallback-value"
        with self.assertRaises(ImproperlyConfigured) as context:
            load_environment(
                make_env(DJANGO_SECRET_KEY_FALLBACKS=f"{OTHER_STRONG_KEY},{weak_fallback}")
            )
        message = str(context.exception)
        self.assertNotIn(weak_fallback, message)
        self.assertNotIn(OTHER_STRONG_KEY, message)
        self.assertNotIn(STRONG_KEY, message)

    def test_repr_does_not_contain_fallback_keys(self):
        config = load_environment(make_env(DJANGO_SECRET_KEY_FALLBACKS=OTHER_STRONG_KEY))
        self.assertNotIn(OTHER_STRONG_KEY, repr(config))
        self.assertNotIn(OTHER_STRONG_KEY, str(config))

    def test_database_errors_do_not_contain_the_password(self):
        invalid = ({"DB_NAME": None}, {"DB_USER": None}, {"DB_PORT": "abc"})
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ImproperlyConfigured) as context:
                    load_environment(make_env(**overrides))
                self.assertNotIn(TEST_DB_PASSWORD, str(context.exception))

    def test_other_errors_do_not_contain_secrets(self):
        invalid = (
            {"APP_ENV": "unknown"},
            {"DJANGO_DEBUG": "True"},
            {"EMAIL_MODE": "console"},
            {"DJANGO_ALLOWED_HOSTS": "*"},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ImproperlyConfigured) as context:
                    load_environment(
                        make_env(DJANGO_SECRET_KEY_FALLBACKS=OTHER_STRONG_KEY, **overrides)
                    )
                message = str(context.exception)
                for secret in (STRONG_KEY, OTHER_STRONG_KEY, TEST_DB_PASSWORD):
                    self.assertNotIn(secret, message)


class AllowedHostsTests(SimpleTestCase):
    def test_wildcards_are_rejected(self):
        for value in ("*", "*.example.org", ".example.org", "portal.example.org,*"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "DJANGO_ALLOWED_HOSTS"):
                    load_environment(make_env(DJANGO_ALLOWED_HOSTS=value))

    def test_required_outside_local(self):
        for app_env in ("stage", "production"):
            with self.subTest(app_env=app_env):
                with self.assertRaisesMessage(ImproperlyConfigured, "DJANGO_ALLOWED_HOSTS"):
                    load_environment(make_env(app_env, DJANGO_ALLOWED_HOSTS=""))

    def test_list_is_parsed(self):
        config = load_environment(
            make_env(DJANGO_ALLOWED_HOSTS=" portal.example.org , portal2.example.org ")
        )
        self.assertEqual(config.allowed_hosts, ("portal.example.org", "portal2.example.org"))


class CsrfTrustedOriginsTests(SimpleTestCase):
    def test_wildcard_origin_is_rejected(self):
        for value in ("https://*.example.org", "https://*", "*"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "wildcards"):
                    load_environment(make_env(DJANGO_CSRF_TRUSTED_ORIGINS=value))

    def test_invalid_origins_are_rejected(self):
        invalid = (
            "portal.example.org",
            "http://portal.example.org",
            "https://portal.example.org/path",
            "https://user@portal.example.org",
            "ftp://portal.example.org",
            "https://portal.example.org:abc",
            "https://portal.example.org:0",
            "https://portal.example.org:65536",
            "https://portal.example.org:99999",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    load_environment(make_env(DJANGO_CSRF_TRUSTED_ORIGINS=value))

    def test_valid_origins_are_accepted(self):
        config = load_environment(
            make_env(DJANGO_CSRF_TRUSTED_ORIGINS="https://portal.example.org:8443")
        )
        self.assertEqual(config.csrf_trusted_origins, ("https://portal.example.org:8443",))
        local = load_environment(
            make_env("local", DJANGO_CSRF_TRUSTED_ORIGINS="http://localhost:8000")
        )
        self.assertEqual(local.csrf_trusted_origins, ("http://localhost:8000",))

    def test_empty_by_default(self):
        self.assertEqual(load_environment(make_env()).csrf_trusted_origins, ())


class EmailModeTests(SimpleTestCase):
    def test_console_is_the_default_locally(self):
        config = load_environment(make_env("local", EMAIL_MODE=None))
        self.assertEqual(config.email_mode, "console")

    def test_missing_mode_refuses_start_outside_local(self):
        for app_env in ("stage", "production"):
            for value in (None, ""):
                with self.subTest(app_env=app_env, value=value):
                    with self.assertRaisesMessage(ImproperlyConfigured, "EMAIL_MODE"):
                        load_environment(make_env(app_env, EMAIL_MODE=value))

    def test_local_allows_only_console_and_not_graph(self):
        # Unrestricted delivery stays production-only (D-028, D-116: the allowlist mode is gone).
        self.assertEqual(load_environment(make_env("local", EMAIL_MODE="console")).email_mode, "console")
        with self.assertRaisesMessage(ImproperlyConfigured, "EMAIL_MODE=console"):
            load_environment(make_env("local", EMAIL_MODE="graph"))

    def test_unknown_mode_refuses_start(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "EMAIL_MODE"):
            load_environment(make_env("local", EMAIL_MODE="smtp"))

    def test_console_refused_in_stage_and_production(self):
        # Stage behaves exactly like production: unrestricted Graph delivery only (D-070).
        for app_env in ("stage", "production"):
            with self.subTest(app_env=app_env), self.assertRaisesMessage(ImproperlyConfigured, "EMAIL_MODE=graph"):
                load_environment(make_env(app_env, EMAIL_MODE="console"))

    def test_graph_refused_locally(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "EMAIL_MODE"):
            load_environment(make_env("local", EMAIL_MODE="graph"))

    def test_stage_and_production_configuration_is_identical(self):
        # Apart from the label, the validated configuration of stage equals production (D-070).
        stage = load_environment(make_env("stage"))
        production = load_environment(make_env("production"))
        self.assertEqual(
            {**vars(stage), "app_env": None}, {**vars(production), "app_env": None}
        )
        self.assertEqual(security_settings(stage), security_settings(production))
        self.assertEqual(email_backend(stage), email_backend(production))
        self.assertEqual(silenced_system_checks(stage), silenced_system_checks(production))

    def test_allowed_combinations(self):
        allowed = {"local": ("console",), "stage": ("graph",), "production": ("graph",)}
        for app_env, modes in allowed.items():
            for mode in ("console", "graph"):
                with self.subTest(app_env=app_env, mode=mode):
                    env = make_env(app_env, EMAIL_MODE=mode)
                    if mode in modes:
                        self.assertEqual(load_environment(env).email_mode, mode)
                    else:
                        with self.assertRaises(ImproperlyConfigured):
                            load_environment(env)


class AxesProxyCountTests(SimpleTestCase):
    def test_defaults_to_no_trusted_proxies(self):
        self.assertEqual(load_environment(make_env()).axes_ipware_proxy_count, 0)

    def test_accepts_non_negative_ascii_integer(self):
        for value in ("0", "1", "2"):
            with self.subTest(value=value):
                self.assertEqual(
                    load_environment(make_env(AXES_IPWARE_PROXY_COUNT=value)).axes_ipware_proxy_count,
                    int(value),
                )

    def test_rejects_invalid_proxy_counts(self):
        for value in ("-1", "+1", "1.5", "one", "\u00b2"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(
                    ImproperlyConfigured, "AXES_IPWARE_PROXY_COUNT"
                ):
                    load_environment(make_env(AXES_IPWARE_PROXY_COUNT=value))

    def test_backend_is_never_smtp_and_console_only_in_console_mode(self):
        self.assertEqual(
            email_backend(load_environment(make_env("local"))),
            "django.core.mail.backends.console.EmailBackend",
        )
        for app_env in ("stage", "production"):
            with self.subTest(app_env=app_env):
                backend = email_backend(load_environment(make_env(app_env, EMAIL_MODE="graph")))
                self.assertEqual(backend, "leg_platform.mail.GraphEmailBackend")


class SecurityLogRetentionTests(SimpleTestCase):
    def test_defaults_are_the_minimum(self):
        config = load_environment(make_env())
        self.assertEqual(config.security_log_retention_days, 30)
        self.assertEqual(config.security_log_long_retention_days, 365)

    def test_longer_retention_is_accepted(self):
        config = load_environment(
            make_env(SECURITY_LOG_RETENTION_DAYS="90", SECURITY_LOG_LONG_RETENTION_DAYS="730")
        )
        self.assertEqual(config.security_log_retention_days, 90)
        self.assertEqual(config.security_log_long_retention_days, 730)

    def test_shorter_than_the_minimum_and_invalid_values_refuse_start(self):
        for name, value in (
            ("SECURITY_LOG_RETENTION_DAYS", "29"),
            ("SECURITY_LOG_LONG_RETENTION_DAYS", "364"),
            ("SECURITY_LOG_RETENTION_DAYS", "0"),
            ("SECURITY_LOG_RETENTION_DAYS", "-30"),
            ("SECURITY_LOG_RETENTION_DAYS", "thirty"),
            ("SECURITY_LOG_LONG_RETENTION_DAYS", "36501"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, name):
                    load_environment(make_env(**{name: value}))


class MovedToAdministrationTests(SimpleTestCase):
    """D-116: the sender and the contact address are not read from the environment any more; they are in the E-Mail
    chapter of the administration. A value left there refuses the start, so that it is never ignored silently."""

    def test_each_moved_variable_refuses_the_start(self):
        for name in ("EMAIL_SENDER_ADDRESS", "CONTACT_EMAIL_ADDRESS", "EMAIL_ALLOWED_RECIPIENTS"):
            with self.subTest(name=name), self.assertRaisesMessage(ImproperlyConfigured, name):
                load_environment(make_env(**{name: "portal@example.org"}))

    def test_without_them_the_start_is_valid(self):
        self.assertEqual(load_environment(make_env("production")).email_mode, "graph")


class AddressDirectoryFileTests(SimpleTestCase):
    """ADDRESS_DIRECTORY_FILE: the own SQLite file of the address directory (D-075)."""

    def test_the_defaults(self):
        for app_env in ("stage", "production"):
            with self.subTest(app_env=app_env):
                self.assertEqual(
                    load_environment(make_env(app_env)).address_directory_file,
                    "/var/lib/leg-platform/address-directory.sqlite3",
                )
        local = Path(load_environment(make_env("local")).address_directory_file)
        self.assertEqual(local, Path.home() / ".leg-platform-dev" / "address-directory.sqlite3")
        self.assertNotIn(REPOSITORY_ROOT, local.resolve().parents)
        self.assertEqual(
            load_environment(make_env("local", ADDRESS_DIRECTORY_FILE="   ")).address_directory_file, str(local),
        )

    def test_the_default_is_the_same_for_every_missing_or_empty_value(self):
        self.assertEqual(
            load_environment(make_env("production", ADDRESS_DIRECTORY_FILE="")).address_directory_file,
            "/var/lib/leg-platform/address-directory.sqlite3",
        )

    def test_a_configured_absolute_path_outside_the_repository_is_accepted_and_nothing_is_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "state" / "directory.sqlite3")
            for app_env in ("local", "stage", "production"):
                with self.subTest(app_env=app_env):
                    config = load_environment(make_env(app_env, ADDRESS_DIRECTORY_FILE=path))
                    self.assertEqual(config.address_directory_file, path)
            self.assertFalse((Path(folder) / "state").exists())

    def test_relative_paths_and_non_paths_are_refused(self):
        for value in ("directory.sqlite3", "./directory.sqlite3", "var/lib/x.sqlite3", ":memory:",
                      "file:directory.sqlite3?mode=ro", "~/directory.sqlite3", "..\\x"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "ADDRESS_DIRECTORY_FILE must be an absolute path"):
                    load_environment(make_env(ADDRESS_DIRECTORY_FILE=value))

    def test_paths_with_control_characters_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            for character in ("\x00", "\n", "\r", "\t", "\x1b", "\u200b", "\u202e", "\u2028"):
                with self.subTest(character=repr(character)):
                    with self.assertRaisesMessage(ImproperlyConfigured, "control characters"):
                        load_environment(make_env(ADDRESS_DIRECTORY_FILE=str(Path(folder) / f"a{character}b.sqlite3")))

    def test_a_path_inside_the_repository_is_refused_also_through_dot_dot_and_a_link_free_spelling(self):
        for value in (
            str(REPOSITORY_ROOT / "directory.sqlite3"),
            str(REPOSITORY_ROOT / "leg_platform" / "x" / "directory.sqlite3"),
            str(REPOSITORY_ROOT / "docs" / ".." / "directory.sqlite3"),
            str(REPOSITORY_ROOT),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured) as error:
                    load_environment(make_env(ADDRESS_DIRECTORY_FILE=value))
                self.assertNotIn(str(REPOSITORY_ROOT), str(error.exception))

    def test_a_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            for value in (folder, folder + os.sep):
                with self.subTest(value=value):
                    with self.assertRaisesMessage(ImproperlyConfigured, "not a folder"):
                        load_environment(make_env(ADDRESS_DIRECTORY_FILE=value))


class DatabaseSettingsTests(SimpleTestCase):
    def test_missing_database_settings_refuse_start(self):
        for name in ("DB_NAME", "DB_USER", "DB_PASSWORD"):
            with self.subTest(name=name):
                with self.assertRaisesMessage(ImproperlyConfigured, name):
                    load_environment(make_env(**{name: None}))

    def test_defaults_and_port_validation(self):
        config = load_environment(make_env())
        self.assertEqual((config.db_host, config.db_port), ("127.0.0.1", "3306"))
        self.assertEqual(load_environment(make_env(DB_PORT="65535")).db_port, "65535")
        # "\u00b2" (superscript two) and "\u0663" (Arabic-Indic digit three) pass
        # str.isdigit() but are not ASCII digits.
        for value in ("abc", "0", "65536", "-1", "\u00b2", "\u0663306", "33 06"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "DB_PORT"):
                    load_environment(make_env(DB_PORT=value))


class SecuritySettingsTests(SimpleTestCase):
    def test_stage_and_production_are_secure(self):
        for app_env in ("stage", "production"):
            with self.subTest(app_env=app_env):
                settings = security_settings(load_environment(make_env(app_env)))
                self.assertEqual(settings["SESSION_COOKIE_NAME"], "__Host-sessionid")
                self.assertEqual(settings["CSRF_COOKIE_NAME"], "__Host-csrftoken")
                self.assertEqual(settings["LANGUAGE_COOKIE_NAME"], "__Host-language")
                self.assertIs(settings["LANGUAGE_COOKIE_SECURE"], True)
                self.assertIs(settings["SESSION_COOKIE_SECURE"], True)
                self.assertIs(settings["CSRF_COOKIE_SECURE"], True)
                self.assertIs(settings["SECURE_SSL_REDIRECT"], True)
                self.assertEqual(settings["SECURE_HSTS_SECONDS"], DEFAULT_HSTS_SECONDS)
                self.assertIs(settings["SECURE_HSTS_INCLUDE_SUBDOMAINS"], False)
                self.assertIs(settings["SECURE_HSTS_PRELOAD"], False)
                self.assertIsNone(settings["SECURE_PROXY_SSL_HEADER"])

    def test_local_is_relaxed_explicitly(self):
        settings = security_settings(load_environment(make_env("local")))
        self.assertEqual(settings["SESSION_COOKIE_NAME"], "sessionid")
        self.assertEqual(settings["CSRF_COOKIE_NAME"], "csrftoken")
        self.assertEqual(settings["LANGUAGE_COOKIE_NAME"], "language")
        self.assertIs(settings["LANGUAGE_COOKIE_SECURE"], False)
        self.assertIs(settings["SESSION_COOKIE_SECURE"], False)
        self.assertIs(settings["CSRF_COOKIE_SECURE"], False)
        self.assertIs(settings["SECURE_SSL_REDIRECT"], False)
        self.assertEqual(settings["SECURE_HSTS_SECONDS"], 0)

    def test_hsts_seconds_can_be_configured_but_not_disabled(self):
        config = load_environment(make_env(DJANGO_SECURE_HSTS_SECONDS="3600"))
        self.assertEqual(security_settings(config)["SECURE_HSTS_SECONDS"], 3600)
        for value in ("0", "-1", "abc", "\u00b2", "\u0663600", "1.5"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "HSTS"):
                    load_environment(make_env(DJANGO_SECURE_HSTS_SECONDS=value))

    def test_hsts_include_subdomains_is_off_by_default(self):
        config = load_environment(make_env())
        self.assertIs(config.hsts_include_subdomains, False)
        self.assertIs(security_settings(config)["SECURE_HSTS_INCLUDE_SUBDOMAINS"], False)
        self.assertEqual(silenced_system_checks(config), ["security.W005", "security.W021"])

    def test_hsts_include_subdomains_can_be_enabled(self):
        config = load_environment(make_env(DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS="True"))
        settings = security_settings(config)
        self.assertIs(settings["SECURE_HSTS_INCLUDE_SUBDOMAINS"], True)
        self.assertIs(settings["SECURE_HSTS_PRELOAD"], False)
        # security.W005 is no longer silenced; W021 (preload) stays silenced.
        self.assertEqual(silenced_system_checks(config), ["security.W021"])

    def test_hsts_include_subdomains_rejects_invalid_values(self):
        for value in ("1", "yes", "on"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(
                    ImproperlyConfigured, "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS"
                ):
                    load_environment(make_env(DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=value))

    def test_proxy_ssl_header_only_when_configured(self):
        config = load_environment(
            make_env(DJANGO_SECURE_PROXY_SSL_HEADER="HTTP_X_FORWARDED_PROTO,https")
        )
        self.assertEqual(
            security_settings(config)["SECURE_PROXY_SSL_HEADER"],
            ("HTTP_X_FORWARDED_PROTO", "https"),
        )
        for value in ("X_FORWARDED_PROTO,https", "HTTP_X_FORWARDED_PROTO", "HTTP_X,"):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, "PROXY_SSL_HEADER"):
                    load_environment(make_env(DJANGO_SECURE_PROXY_SSL_HEADER=value))
