"""Delete the stored answers of the external building search that expired (``buildings``, D-066)."""

from django.core.management.base import BaseCommand

from leg_platform.addresses import buildings


class Command(BaseCommand):
    help = "Delete expired stored building searches."

    def handle(self, *args, **options):
        self.stdout.write(f"Deleted {buildings.delete_expired()} expired stored building searches.")
