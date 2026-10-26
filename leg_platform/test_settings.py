"""Application MariaDB tests; the address directory is a temporary SQLite file (D-075)."""

import atexit
import shutil
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from django.db.models.signals import post_migrate

from .addresses.storage import mark_writer
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

# The address directory of the tests: a file of this run in the temporary folder of the system,
# never the configured file of the instance and never inside the repository. Django creates the test
# database at this path (TEST NAME) and removes it when the run ends; the folder is removed at exit
# as well. The file of the instance (settings.DATABASES["addresses"]["NAME"]) is not opened by a
# test run.
_ADDRESS_TEST_FOLDER = tempfile.mkdtemp(prefix="leg-platform-address-tests-")
atexit.register(shutil.rmtree, _ADDRESS_TEST_FOLDER, True)
DATABASES = {
    **DATABASES,  # noqa: F405
    "addresses": {
        **DATABASES["addresses"],  # noqa: F405
        "TEST": {"NAME": str(Path(_ADDRESS_TEST_FOLDER) / "address-directory.sqlite3")},
    },
}

# The test process fills the directory file of the tests itself (creating the test database,
# the fixtures of the tests), so it is a writer. The tests of the read-only default switch the
# flag off explicitly (leg_platform/addresses/tests/test_directory.py).
mark_writer()

# No file logging and no automatic import in the tests (they must neither write into the folder of
# the developer nor download); the tests that need them set the values themselves.
LOG_DIRECTORY = ""
LOGGING = logging_config("")  # noqa: F405
ADDRESS_IMPORT_AUTOMATIC = False

# The E-Mail chapter holds the sender of the instance (D-116). After the migrations of the test database it holds the
# sender of a test instance; the tests that need other values set them (leg_platform.accounts.tests.helpers,
# instance_mail_addresses). The base address of the portal is not stored: it comes from the environment (INSTANCE_BASE_URL,
# local default http://127.0.0.1:8000), and the tests that need another one use override_settings. Django runs the
# TestCase classes before the TransactionTestCase classes, so the row survives until the first flush.
TEST_MAIL_ADDRESSES = {
    "sender_address": "noreply@portal.example.org",
    "contact_address": "",
}


def _store_test_mail_addresses(app_config, using="default", **_kwargs):
    if app_config.label != "accounts" or using != "default":
        return
    from leg_platform.accounts.models import GraphConfiguration

    GraphConfiguration.objects.using(using).update_or_create(pk=1, defaults=TEST_MAIL_ADDRESSES)


post_migrate.connect(_store_test_mail_addresses, dispatch_uid="leg_platform_test_mail_addresses")
