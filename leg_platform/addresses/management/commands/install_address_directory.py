from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from ...install import InstallError, install, record_results
from ...storage import mark_writer


class Command(BaseCommand):
    help = (
        "Install a prebuilt address directory file that was uploaded next to the directory file (operator command, "
        "D-134). Stop the web service, the import worker and the scheduled tasks first."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "file", nargs="?", help="Absolute path of the upload, in the folder of the address directory file.",
        )
        parser.add_argument(
            "--record-only", action="store_true",
            help="Install no file; take over the results of the file in place (times and figures of its sources).",
        )
        parser.add_argument("--sha256", help="Expected SHA-256 of the upload (hexadecimal); the install stops when it differs.")

    def handle(self, **options):
        mark_writer()
        if options["record_only"] == (options["file"] is not None):
            raise CommandError("Give either a file or --record-only.")
        if options["record_only"]:
            try:
                record_results(stdout=self.stdout)
            except DatabaseError as error:
                raise CommandError(f"{error}") from None
            return
        try:
            directory = install(options["file"], sha256=options["sha256"], stdout=self.stdout)
        except InstallError as error:
            raise CommandError(f"{error} Nothing was changed.") from None
        except DatabaseError as error:
            raise CommandError(f"{error}") from None
        self.stdout.write(self.style.SUCCESS(f"Installed the address directory: {directory.row_count} addresses."))
