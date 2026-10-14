"""Settings for a local, throw-away address directory (never for stage or production).

Adds two SQLite aliases for the address directory, so that address suggestions can be tried
on a development machine without the real directory database and its SELECT-only grants
(docs/STARTUP.md). Both aliases point to the same SQLite file, which holds synthetic data only
(``manage.py seed_local_address_directory``). The file is outside the repository: the path
comes from ``LEG_LOCAL_ADDRESS_DIRECTORY_FILE`` or, by default, it is a file in the home
directory.

This module refuses to be imported unless ``APP_ENV=local``. It changes no security setting:
it only adds the two database aliases to the normal settings. It also keeps the separation of
the importer credentials, because the normal settings refuse importer credentials in the
environment of any other settings module (``leg_platform.address_configuration``).
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# Checked before the normal settings are imported, so that nothing else is evaluated in an
# environment that is not a development machine.
if os.environ.get("APP_ENV", "").strip() != "local":
    raise ImproperlyConfigured(
        "leg_platform.local_directory_settings can only be used with APP_ENV=local."
    )

from leg_platform.settings import *  # noqa: E402,F401,F403
from leg_platform.settings import APP_ENV as _APP_ENV  # noqa: E402
from leg_platform.settings import BASE_DIR as _BASE_DIR  # noqa: E402
from leg_platform.settings import DATABASES as _DATABASES  # noqa: E402

LOCAL_DIRECTORY_FILE_VARIABLE = "LEG_LOCAL_ADDRESS_DIRECTORY_FILE"
LOCAL_DIRECTORY_DEFAULT_FILE = Path.home() / ".leg-platform-local-address-directory.sqlite3"

# Defence in depth: the validated value of the normal settings must be "local" as well.
if _APP_ENV != "local":
    raise ImproperlyConfigured(
        "leg_platform.local_directory_settings can only be used with APP_ENV=local."
    )


def _local_directory_file() -> Path:
    configured = os.environ.get(LOCAL_DIRECTORY_FILE_VARIABLE, "").strip()
    path = Path(configured).expanduser() if configured else LOCAL_DIRECTORY_DEFAULT_FILE
    if not path.is_absolute():
        raise ImproperlyConfigured(f"{LOCAL_DIRECTORY_FILE_VARIABLE} must be an absolute path.")
    path = path.resolve()
    base = Path(_BASE_DIR).resolve()
    if path == base or base in path.parents:
        raise ImproperlyConfigured(
            f"{LOCAL_DIRECTORY_FILE_VARIABLE} must point outside the repository."
        )
    return path


_local_directory_database = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": str(_local_directory_file()),
}

# A new dictionary: the dictionary of the normal settings module stays unchanged.
DATABASES = {  # noqa: F405
    **_DATABASES,
    "addresses": dict(_local_directory_database),
    "address_import": dict(_local_directory_database),
}
