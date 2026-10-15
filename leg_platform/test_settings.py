"""Application MariaDB tests with isolated synthetic address-directory databases."""

from .settings import *  # noqa: F403

# The tests post the forms at once; the minimum time of the bot check (accounts/bot_check.py) is
# tested with its own value.
MAIL_FORM_MINIMUM_SECONDS = 0

DATABASES = {  # noqa: F405
    **DATABASES,
    "address_import": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    "addresses": {
        "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
        "TEST": {"MIRROR": "address_import"},
    },
}
