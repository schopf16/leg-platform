"""Application MariaDB tests with isolated synthetic address-directory databases."""

from .settings import *  # noqa: F403

DATABASES = {  # noqa: F405
    **DATABASES,
    "address_import": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    "addresses": {
        "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
        "TEST": {"MIRROR": "address_import"},
    },
}
