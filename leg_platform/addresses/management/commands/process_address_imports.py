import time

from django.core.management.base import BaseCommand, CommandError

from ...importer import ImportFailure
from ...jobs import process_one
from ...storage import mark_writer


class Command(BaseCommand):
    help = "Process admin-requested address imports in the worker process (never in the web process)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one queued job and exit.")

    def handle(self, **options):
        # The worker writes the directory file; every other process opens it read-only (D-075).
        mark_writer()
        while True:
            try:
                processed = process_one(schedule_automatic=not options["once"])
            except ImportFailure:
                if options["once"]:
                    raise CommandError("Address import worker failed; inspect the job status before restarting.") from None
                # process_one recorded FAILED and the automatic-attempt date.
                # Keep servicing manual requests without an early automatic retry.
                processed = False
            except Exception:
                raise CommandError("Address import worker failed; inspect the job status before restarting.") from None
            if options["once"]:
                self.stdout.write("Processed one address import." if processed else "No address import queued.")
                return
            time.sleep(10)
