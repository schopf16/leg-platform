"""Settings exclusively for the isolated address import/migration process."""

import os
import sys

from django.core.exceptions import ImproperlyConfigured

if len(sys.argv) < 2 or sys.argv[1] not in {"import_address_directory", "process_address_imports", "migrate"}:
    raise ImproperlyConfigured("Importer settings are restricted to import, worker and migration commands.")

from .address_configuration import importer_database
from .settings import *  # noqa: F403

DATABASES = {  # noqa: F405
    **DATABASES,
    "address_import": importer_database(os.environ, DATABASES.get("addresses")),
}
