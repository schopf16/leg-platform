"""Imports the real settings module with APP_ENV=production in a separate process.

The tests in the same process run with the settings of the test environment (usually
APP_ENV=local). This test proves that ``leg_platform.settings`` itself produces the secure
production values and passes Django's deployment checks (docs/SECURITY.md, section 11).

The child process receives only dummy, non-secret values and a few technical variables, never
the configuration of the test process.
"""

import json
import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase

from leg_platform.tests.test_environment import STRONG_KEY, TEST_DB_PASSWORD

CHILD_SCRIPT = """
import json

import django
from django.conf import settings
from django.core.management import call_command

django.setup()
# databases=[] skips the checks that connect to the database; CI runs them separately.
call_command("check", deploy=True, fail_level="WARNING", databases=[], verbosity=0)
names = (
    "DEBUG",
    "SESSION_COOKIE_NAME",
    "CSRF_COOKIE_NAME",
    "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE",
    "SESSION_COOKIE_HTTPONLY",
    "CSRF_COOKIE_HTTPONLY",
    "SESSION_COOKIE_DOMAIN",
    "CSRF_COOKIE_DOMAIN",
    "SECURE_SSL_REDIRECT",
    "SECURE_HSTS_SECONDS",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "SECURE_HSTS_PRELOAD",
    "SECURE_PROXY_SSL_HEADER",
    "SILENCED_SYSTEM_CHECKS",
    "ALLOWED_HOSTS",
    "SECURE_CSP",
    "SECURE_CSP_REPORT_ONLY",
)
values = {name: getattr(settings, name) for name in names}
values["CSP_MIDDLEWARE"] = (
    "django.middleware.csp.ContentSecurityPolicyMiddleware" in settings.MIDDLEWARE
)
print(json.dumps(values))
"""

# Technical variables the interpreter may need; none of them holds configuration or secrets.
PASSED_THROUGH = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONWARNINGS")


def run_settings_in_child(**overrides):
    env = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "leg_platform.settings",
            "APP_ENV": "production",
            "DJANGO_SECRET_KEY": STRONG_KEY,
            "DJANGO_ALLOWED_HOSTS": "portal.example.org",
            "INSTANCE_BASE_URL": "https://portal.example.org",
            "INSTANCE_PRIVACY_URL": "https://www.example.org/privacy",
            "EMAIL_MODE": "graph",
            "DB_NAME": "leg_platform",
            "DB_USER": "leg_platform",
            "DB_PASSWORD": TEST_DB_PASSWORD,
        }
    )
    env.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"Child process failed:\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


class ProductionSettingsModuleTests(SimpleTestCase):
    def test_production_settings_are_secure(self):
        values = run_settings_in_child()
        expected = {
            "DEBUG": False,
            "SESSION_COOKIE_NAME": "__Host-sessionid",
            "CSRF_COOKIE_NAME": "__Host-csrftoken",
            "SESSION_COOKIE_SECURE": True,
            "CSRF_COOKIE_SECURE": True,
            "SESSION_COOKIE_HTTPONLY": True,
            "CSRF_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_DOMAIN": None,
            "CSRF_COOKIE_DOMAIN": None,
            "SECURE_SSL_REDIRECT": True,
            "SECURE_HSTS_SECONDS": 31536000,
            "SECURE_HSTS_INCLUDE_SUBDOMAINS": False,
            "SECURE_HSTS_PRELOAD": False,
            "SECURE_PROXY_SSL_HEADER": None,
            "SILENCED_SYSTEM_CHECKS": ["security.W005", "security.W021", "axes.W006"],
            "ALLOWED_HOSTS": ["portal.example.org"],
            "SECURE_CSP": {
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
            "SECURE_CSP_REPORT_ONLY": {},
            "CSP_MIDDLEWARE": True,
        }
        self.assertEqual(values, expected)

    def test_include_subdomains_unsilences_w005(self):
        values = run_settings_in_child(DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS="True")
        self.assertIs(values["SECURE_HSTS_INCLUDE_SUBDOMAINS"], True)
        self.assertEqual(values["SILENCED_SYSTEM_CHECKS"], ["security.W021", "axes.W006"])

    def test_missing_email_mode_refuses_start(self):
        with self.assertRaisesMessage(AssertionError, "EMAIL_MODE"):
            run_settings_in_child(EMAIL_MODE="")
