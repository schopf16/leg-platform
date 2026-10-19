"""Load a few synthetic addresses into the directory file of a development machine (docs/STARTUP.md).

Runs only with ``APP_ENV=local``, needs the migrated directory tables and refuses to replace a
directory without the synthetic marker (real import data).
"""

import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction
from django.utils import timezone

from leg_platform.addresses.models import DIRECTORY_ALIAS, Address, Directory
from leg_platform.addresses.storage import mark_writer

# Not a SHA-256 value, so it never equals the checksum of a real import.
SYNTHETIC_CHECKSUM = "synthetic-local-directory"

# (street, house number, postal code, locality): invented, postal codes at the end of the Swiss range.
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
# Invented building identifiers (a development machine only; the real ones come from the import).
SYNTHETIC_EGID_BASE = 990_000_000


def ensure_replaceable(directory, has_addresses: bool) -> None:
    """Refuse to replace a directory without the synthetic marker; an empty one (no row, no addresses) may be filled."""
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
                    egid=SYNTHETIC_EGID_BASE + number,
                    entrance_id=0,
                )
                for number, (street, house_number, postal_code, locality) in enumerate(rows, start=1)
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
                "has_egid": True,
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
