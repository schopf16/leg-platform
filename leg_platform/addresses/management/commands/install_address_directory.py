from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from ...install import InstallError, install
from ...storage import mark_writer


class Command(BaseCommand):
    help = (
        "Install a prebuilt address directory file that was uploaded next to the directory file (operator command, "
        "D-134). Stop the web service, the import worker and the scheduled tasks first."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", help="Absolute path of the upload, in the folder of the address directory file.")
        parser.add_argument("--sha256", help="Expected SHA-256 of the upload (hexadecimal); the install stops when it differs.")

    def handle(self, **options):
        mark_writer()
        try:
            directory = install(options["file"], sha256=options["sha256"], stdout=self.stdout)
        except InstallError as error:
            raise CommandError(f"{error} Nothing was changed.") from None
        except DatabaseError as error:
            raise CommandError(f"{error}") from None
        self.stdout.write(self.style.SUCCESS(f"Installed the address directory: {directory.row_count} addresses."))
