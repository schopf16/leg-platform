from django.core.management.base import BaseCommand, CommandError

from ...importer import import_directory
from ...storage import mark_writer


class Command(BaseCommand):
    help = "Import the fixed official Swiss address directory (operator command; the worker normally does this)."

    def handle(self, **options):
        # The importer writes the directory file; every other process opens it read-only (D-075).
        mark_writer()
        try:
            count = import_directory()
        except Exception:
            raise CommandError("Address directory import failed; the published directory is unchanged.") from None
        self.stdout.write(self.style.SUCCESS(f"Published address directory: {count} addresses."))
