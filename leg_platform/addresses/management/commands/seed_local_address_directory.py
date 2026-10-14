"""Load a few clearly synthetic addresses into the local throw-away address directory.

Only for a development machine, to try address suggestions without the real directory
(docs/STARTUP.md). The command

* refuses to run unless ``APP_ENV=local``;
* writes only to the SQLite aliases of ``leg_platform.local_directory_settings``, never to a
  MariaDB directory database or with importer credentials;
* refuses to replace a directory that does not carry the synthetic marker, so that data from
  a real import is never overwritten.

The rows are invented; they do not describe real buildings.
"""

import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction
from django.utils import timezone

from leg_platform.addresses.models import READ_ALIAS, WRITE_ALIAS, Address, Directory

SQLITE_ENGINE = "django.db.backends.sqlite3"

# Not a SHA-256 value, so that it can never equal the checksum of a real import (64 hex digits).
SYNTHETIC_CHECKSUM = "synthetic-local-directory"

# (street, house number, postal code, locality): invented names, postal codes at the end of the
# Swiss range, so that nobody mistakes them for real addresses.
SYNTHETIC_ROWS = (
    ("Example Street", "1", "9999", "Example Town"),
    ("Example Street", "2", "9999", "Example Town"),
    ("Example Street", "3a", "9999", "Example Town"),
    ("Sample Avenue", "10", "9998", "Sample City"),
    ("Sample Avenue", "12", "9998", "Sample City"),
    ("Test Lane", "7", "9997", "Test Village"),
    ("Test Lane", "9", "9997", "Test Village"),
    ("Throwaway Road", "21", "9997", "Test Village"),
)

MIGRATE_HINT = (
    "python manage.py migrate addresses --database=address_import "
    "--settings=leg_platform.local_directory_settings"
)


def ensure_local_sqlite_aliases() -> None:
    """Refuse unless both directory aliases are SQLite databases (the local throw-away file)."""
    for alias in (READ_ALIAS, WRITE_ALIAS):
        engine = settings.DATABASES.get(alias, {}).get("ENGINE")
        if engine != SQLITE_ENGINE:
            raise CommandError(
                f"The database alias '{alias}' is not a SQLite database. This command works "
                "only with --settings=leg_platform.local_directory_settings and never writes "
                "to a MariaDB directory."
            )


def ensure_replaceable(directory, has_addresses: bool) -> None:
    """Refuse to replace a directory that does not carry the synthetic marker.

    ``directory`` is the metadata row or None; an empty directory (no row, no addresses) may be
    filled, a synthetic one replaced. Anything else may be the result of a real import.
    """
    if directory is None:
        if has_addresses:
            raise CommandError(
                "The directory holds addresses without metadata and may contain imported "
                "data; refusing to replace it."
            )
        return
    if directory.checksum != SYNTHETIC_CHECKSUM:
        raise CommandError(
            "The directory was not created by this command (its checksum is not the synthetic "
            "marker) and may contain imported data; refusing to replace it. Use another file "
            "through LEG_LOCAL_ADDRESS_DIRECTORY_FILE."
        )


def replace_directory(rows=SYNTHETIC_ROWS, using=WRITE_ALIAS) -> int:
    """Replace all addresses with ``rows`` and publish them as one new generation."""
    generation = uuid.uuid4()
    with transaction.atomic(using=using):
        Address.objects.using(using).all().delete()
        Address.objects.using(using).bulk_create(
            [
                Address(
                    generation=generation,
                    street=street,
                    house_number=house_number,
                    postal_code=postal_code,
                    locality=locality,
                )
                for street, house_number, postal_code, locality in rows
            ]
        )
        Directory.objects.using(using).update_or_create(
            pk=1,
            defaults={
                "generation": generation,
                "data_date": timezone.localdate(),
                "imported_at": timezone.now(),
                "checksum": SYNTHETIC_CHECKSUM,
                "row_count": len(rows),
            },
        )
    return len(rows)


class Command(BaseCommand):
    help = (
        "Load a few synthetic addresses into the local throw-away address directory "
        "(APP_ENV=local and leg_platform.local_directory_settings only)."
    )

    def handle(self, *args, **options):
        if settings.APP_ENV != "local":
            raise CommandError("This command runs only with APP_ENV=local.")
        ensure_local_sqlite_aliases()
        try:
            directory = Directory.objects.using(WRITE_ALIAS).filter(pk=1).first()
            has_addresses = Address.objects.using(WRITE_ALIAS).exists()
        except DatabaseError:
            raise CommandError(
                f"The directory tables do not exist yet. Create them first: {MIGRATE_HINT}"
            ) from None
        ensure_replaceable(directory, has_addresses)
        count = replace_directory()
        self.stdout.write(f"Loaded {count} synthetic addresses (not real data).")
