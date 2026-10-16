"""Application MariaDB tests with isolated synthetic address-directory databases."""

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F403

# These settings are for the automated tests on a development machine or in CI, where the
# configuration is APP_ENV=local. They replace the password hasher with a fast, weak one below;
# a stage or production instance that selected this module (for example through a wrongly set
# DJANGO_SETTINGS_MODULE) would store weak password hashes, so it refuses to start (D-072,
# security review of D-071, L-4). manage.py, wsgi.py and asgi.py default to leg_platform.settings
# (leg_platform/tests/test_settings.py).
if APP_ENV != "local":  # noqa: F405
    raise ImproperlyConfigured(
        "leg_platform.test_settings can only be used with APP_ENV=local "
        "(it replaces the password hasher with a weak one for the tests)."
    )

# The tests post the forms at once; the minimum time of the bot check (accounts/bot_check.py) is
# tested with its own value.
MAIL_FORM_MINIMUM_SECONDS = 0

# Password hashing in the tests only: the production hasher (Django's default PBKDF2 with at least
# 600,000 iterations, D-023; 1.5 million in the installed Django 6.1) takes about 0.2 seconds per
# hash, and nearly every test logs in or sets a password. Django's documentation "Writing and
# running tests" ("Speeding up the tests") recommends a fast hasher for this. The production
# configuration stays untouched (settings.py does not set PASSWORD_HASHERS);
# test_production_settings.py proves in a child process that the real settings module uses
# Django's default hashers and that the production hasher still hashes and verifies (D-071).
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

DATABASES = {  # noqa: F405
    **DATABASES,
    "address_import": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    "addresses": {
        "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
        "TEST": {"MIRROR": "address_import"},
    },
}
