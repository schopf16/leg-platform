"""Load a few clearly synthetic addresses into the address directory file of a development machine.

Only for a development machine, to try address suggestions without the real directory
(docs/STARTUP.md). The command

* refuses to run unless ``APP_ENV=local``;
* writes to the directory file (the alias ``addresses``, D-075), which must have its tables
  (``python manage.py migrate addresses --database=addresses``);
* refuses to replace a directory that does not carry the synthetic marker, so that data from
  a real import is never overwritten.

The rows are invented; they do not describe real buildings.
"""

import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction
from django.utils import timezone

from leg_platform.addresses.models import DIRECTORY_ALIAS, Address, Directory
from leg_platform.addresses.storage import mark_writer

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

MIGRATE_HINT = "python manage.py migrate addresses --database=addresses"


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
            "marker) and may contain imported data; refusing to replace it."
        )


def replace_directory(rows=SYNTHETIC_ROWS) -> int:
    """Replace all addresses with ``rows`` and publish them as one new generation."""
    generation = uuid.uuid4()
    with transaction.atomic(using=DIRECTORY_ALIAS):
        Address.objects.all().delete()
        Address.objects.bulk_create(
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
        Directory.objects.update_or_create(
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
        "Load a few synthetic addresses into the address directory of a development machine "
        "(APP_ENV=local only)."
    )

    def handle(self, *args, **options):
        if settings.APP_ENV != "local":
            raise CommandError("This command runs only with APP_ENV=local.")
        # The seed writes the directory file; every other process opens it read-only (D-075).
        mark_writer()
        try:
            directory = Directory.objects.filter(pk=1).first()
            has_addresses = Address.objects.exists()
        except DatabaseError:
            raise CommandError(
                f"The directory tables do not exist yet. Create them first: {MIGRATE_HINT}"
            ) from None
        ensure_replaceable(directory, has_addresses)
        count = replace_directory()
        self.stdout.write(f"Loaded {count} synthetic addresses (not real data).")
