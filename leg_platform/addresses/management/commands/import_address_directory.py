from django.core.management.base import BaseCommand, CommandError

from ...importer import import_directory


class Command(BaseCommand):
    help = "Import the fixed official Swiss address directory using the isolated writer."

    def handle(self, **options):
        try:
            count = import_directory()
        except Exception:
            raise CommandError("Address directory import failed; the published directory is unchanged.") from None
        self.stdout.write(self.style.SUCCESS(f"Published address directory: {count} addresses."))
