"""Imports the real settings module with APP_ENV=production and stage in a separate process.

The tests in the same process run with the settings of the test environment (usually
APP_ENV=local). This test proves that ``leg_platform.settings`` itself produces the secure
production values and passes Django's deployment checks (docs/SECURITY.md, section 11), and
that APP_ENV=stage produces exactly the same values (D-070).

The child processes receive only dummy, non-secret values and a few technical variables, never
the configuration of the test process.

Speed (D-071): a child process takes 2 to 4 seconds (interpreter, Django, the deployment checks).
All scenarios therefore run in four child processes that are started together once for the module
(production and stage, each with the base configuration and with HSTS for subdomains). The
refusals at startup are provoked inside the base child by importing the settings module afresh
with changed variables before Django is set up, which raises the same ``ImproperlyConfigured`` as
the start of the application. Every test keeps its own assertions.
"""

import functools
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from leg_platform.tests.test_environment import STRONG_KEY, TEST_DB_PASSWORD

# The request of the child process is the JSON in argv[1]: the refusal scenarios (name -> changed
# variables) and whether the password hashing is examined. Refusals are tried first, because the
# settings module is imported afresh for each of them (``runpy`` executes it in a new namespace);
# then Django is set up with the unchanged environment, and the deployment checks run.
CHILD_SCRIPT = """
import json
import os
import runpy
import sys

request = json.loads(sys.argv[1])
base_environment = dict(os.environ)
refusals = {}
for name, changes in request["refusals"].items():
    os.environ.clear()
    os.environ.update(base_environment)
    os.environ.update(changes)
    try:
        runpy.run_module("leg_platform.settings", run_name="settings_probe")
    except Exception as error:
        refusals[name] = {"type": type(error).__name__, "message": str(error)}
    else:
        refusals[name] = None
os.environ.clear()
os.environ.update(base_environment)

import django
from django.conf import global_settings, settings
from django.contrib.auth.hashers import check_password, get_hasher, make_password
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
hasher = None
if request["hasher"]:
    # The password hashing of the real settings module (D-023, D-071). The tests run with a fast
    # hasher (leg_platform/test_settings.py); this proves that the production configuration still
    # uses Django's default hashers and that the default hasher works.
    passphrase = "a long passphrase for the hasher test"
    encoded = make_password(passphrase)
    default_hasher = get_hasher("default")
    hasher = {
        "default_hashers": settings.PASSWORD_HASHERS == global_settings.PASSWORD_HASHERS,
        "algorithm": default_hasher.algorithm,
        "iterations": default_hasher.iterations,
        "stored_algorithm": encoded.split("$")[0],
        "right_password_verifies": check_password(passphrase, encoded),
        "wrong_password_verifies": check_password("another passphrase", encoded),
    }
print(json.dumps({"values": values, "refusals": refusals, "hasher": hasher}))
"""

# Technical variables the interpreter may need; none of them holds configuration or secrets.
PASSED_THROUGH = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONWARNINGS")

# The refusals of the base child process (name -> changed variables); see CHILD_SCRIPT.
REFUSALS = {
    "missing_email_mode": {"EMAIL_MODE": ""},
    "console": {"EMAIL_MODE": "console"},
    "sender": {"EMAIL_SENDER_ADDRESS": "noreply@example.org"},
    "contact": {"CONTACT_EMAIL_ADDRESS": "leg@example.org"},
    "allowed_recipients": {"EMAIL_ALLOWED_RECIPIENTS": "tester@example.org"},
}


def child_environment(**overrides):
    """The dummy, non-secret environment of a child process (production-style configuration)."""
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
    return env


def run_child(request, **overrides):
    """Run CHILD_SCRIPT with the dummy environment and return the parsed result."""
    env = child_environment(**overrides)
    result = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT, json.dumps(request)],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"Child process failed:\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


# The stage instance must not differ from production in behaviour; APP_ENV=stage is only a label of
# the instance role (docs/DECISIONS.md, D-070). Every test here therefore runs for both.
PRODUCTION_GRADE_APP_ENVS = ("production", "stage")


@functools.cache
def child_results():
    """Start the four child processes together, once; (app_env, scenario) -> result or error text.

    The scenario "base" is the unchanged configuration with the refusals (and the password hashing
    for production); "subdomains" sets DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=True.
    """
    jobs = {}
    for app_env in PRODUCTION_GRADE_APP_ENVS:
        jobs[(app_env, "base")] = (
            {"refusals": REFUSALS, "hasher": app_env == "production"}, {"APP_ENV": app_env},
        )
        jobs[(app_env, "subdomains")] = (
            {"refusals": {}, "hasher": False},
            {"APP_ENV": app_env, "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS": "True"},
        )

    def run(job):
        request, overrides = job
        try:
            return run_child(request, **overrides)
        except AssertionError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return dict(zip(jobs, pool.map(run, jobs.values())))


def child_result(app_env, scenario="base"):
    result = child_results()[(app_env, scenario)]
    if isinstance(result, str):
        raise AssertionError(result)
    return result


class ProductionSettingsModuleTests(SimpleTestCase):
    def test_production_settings_are_secure(self):
        for app_env in PRODUCTION_GRADE_APP_ENVS:
            with self.subTest(app_env=app_env):
                self.assert_secure_settings(child_result(app_env)["values"])

    def assert_secure_settings(self, values):
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
        for app_env in PRODUCTION_GRADE_APP_ENVS:
            with self.subTest(app_env=app_env):
                values = child_result(app_env, "subdomains")["values"]
                self.assertIs(values["SECURE_HSTS_INCLUDE_SUBDOMAINS"], True)
                self.assertEqual(values["SILENCED_SYSTEM_CHECKS"], ["security.W021", "axes.W006"])

    def assert_refused(self, app_env, scenario, message):
        refusal = child_result(app_env)["refusals"][scenario]
        self.assertIsNotNone(refusal, "The settings module was imported without a refusal.")
        self.assertEqual(refusal["type"], "ImproperlyConfigured")
        self.assertIn(message, refusal["message"])

    def test_missing_email_mode_refuses_start(self):
        for app_env in PRODUCTION_GRADE_APP_ENVS:
            with self.subTest(app_env=app_env):
                self.assert_refused(app_env, "missing_email_mode", "EMAIL_MODE")

    def test_console_refuses_start(self):
        for app_env in PRODUCTION_GRADE_APP_ENVS:
            with self.subTest(app_env=app_env):
                self.assert_refused(app_env, "console", "EMAIL_MODE=graph")

    def test_the_moved_addresses_refuse_start(self):
        # D-116: the sender and the contact address live in the E-Mail chapter; a value left in the environment is refused.
        for app_env in PRODUCTION_GRADE_APP_ENVS:
            for key, message in (
                ("sender", "EMAIL_SENDER_ADDRESS"),
                ("contact", "CONTACT_EMAIL_ADDRESS"),
                ("allowed_recipients", "EMAIL_ALLOWED_RECIPIENTS"),
            ):
                with self.subTest(app_env=app_env, key=key):
                    self.assert_refused(app_env, key, message)


class PasswordHasherTests(SimpleTestCase):
    """The tests use a fast hasher; the production configuration must still be Django's default."""

    def test_the_test_settings_use_a_fast_hasher_and_not_the_production_one(self):
        # Only the test settings replace the hashers (leg_platform/test_settings.py).
        self.assertEqual(
            settings.PASSWORD_HASHERS, ["django.contrib.auth.hashers.MD5PasswordHasher"]
        )

    def test_the_real_settings_use_the_default_hasher_with_the_default_work_factor(self):
        # D-023: Django's default hasher (PBKDF2-HMAC-SHA256) and work factor, never below the
        # OWASP minimum of 600,000 iterations; PASSWORD_HASHERS is not set by settings.py. Stage
        # does not differ from production (D-070), so the production child process is enough.
        values = child_result("production")["hasher"]
        self.assertIs(values["default_hashers"], True)
        self.assertEqual(values["algorithm"], "pbkdf2_sha256")
        self.assertEqual(values["stored_algorithm"], "pbkdf2_sha256")
        self.assertGreaterEqual(values["iterations"], 600_000)
        self.assertIs(values["right_password_verifies"], True)
        self.assertIs(values["wrong_password_verifies"], False)


def import_test_settings(**overrides):
    """Import leg_platform.test_settings in a child process; return the completed process."""
    return subprocess.run(
        [sys.executable, "-c", "import leg_platform.test_settings"],
        cwd=settings.BASE_DIR,
        env=child_environment(**overrides),
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestSettingsGuardTests(SimpleTestCase):
    """The weak test hasher can never be active on a real instance (D-072, review of D-071, L-4)."""

    def test_the_test_settings_are_refused_for_stage_and_production(self):
        for app_env in PRODUCTION_GRADE_APP_ENVS:
            with self.subTest(app_env=app_env):
                result = import_test_settings(APP_ENV=app_env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ImproperlyConfigured", result.stderr)
                self.assertIn("APP_ENV=local", result.stderr)

    def test_the_test_settings_load_locally(self):
        # The control of the two refusals above: the same child process works for APP_ENV=local.
        # The child has no home folder, so it names the address directory file (D-075).
        directory_file = str(Path(tempfile.gettempdir()) / "leg-platform-settings-test.sqlite3")
        result = import_test_settings(
            APP_ENV="local", EMAIL_MODE="console", ADDRESS_DIRECTORY_FILE=directory_file,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(Path(directory_file).exists())

    def test_the_entry_points_default_to_the_real_settings_module(self):
        # Nothing but an explicit DJANGO_SETTINGS_MODULE can select another module.
        for name in ("manage.py", "leg_platform/wsgi.py", "leg_platform/asgi.py"):
            with self.subTest(file=name):
                source = (Path(settings.BASE_DIR) / name).read_text(encoding="utf-8")
                self.assertEqual(
                    re.findall(r"DJANGO_SETTINGS_MODULE[\"'],\s*[\"']([^\"']+)[\"']", source),
                    ["leg_platform.settings"],
                )
                self.assertNotIn("test_settings", source)


def ci_deployment_steps():
    """The "Deployment checks with ..." steps of the CI workflow: name -> (variables, command)."""
    text = (Path(settings.BASE_DIR) / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    steps = {}
    for block in re.split(r"^ {6}- name: ", text, flags=re.MULTILINE)[1:]:
        name = block.splitlines()[0]
        if name.startswith("Deployment checks with"):
            variables = dict(re.findall(r"^ {10}([A-Z_]+): (\S+)$", block, flags=re.MULTILINE))
            command = re.search(r"^ {8}run: (.+)$", block, flags=re.MULTILINE).group(1)
            steps[name] = (variables, command)
    return steps


class CiDeploymentCheckTests(SimpleTestCase):
    """The stage step of the CI workflow differs from the production step only by APP_ENV (D-070).

    CI itself cannot run locally; the real settings module is checked for both environments in
    the child processes above (without the database checks), and this test keeps the workflow
    from drifting away from that: same variables, same command, no allow-list variable.
    """

    def test_the_stage_step_equals_the_production_step_except_for_the_environment_label(self):
        steps = ci_deployment_steps()
        self.assertEqual(len(steps), 2, steps)
        production = next(value for name, value in steps.items() if "production" in name)
        stage = next(value for name, value in steps.items() if "stage" in name)
        self.assertEqual(production[0].pop("APP_ENV"), "production")
        self.assertEqual(stage[0].pop("APP_ENV"), "stage")
        self.assertEqual(stage, production)
        self.assertEqual(stage[0]["EMAIL_MODE"], "graph")
        self.assertNotIn("EMAIL_ALLOWED_RECIPIENTS", stage[0])
        self.assertIn("check --deploy", stage[1])
        self.assertIn("--fail-level WARNING", stage[1])
