"""Housekeeping of both databases (D-119): compact the address directory file, optimise the application tables.

Without an option this is the weekly task of ``run_scheduled_tasks``: it runs on Sunday from 04:30 (instance time),
once per ISO week, and does nothing otherwise. ``--now`` starts a run at once (manual, counts for the week).
"""

from django.core.management.base import BaseCommand

from leg_platform.addresses import housekeeping
from leg_platform.addresses.storage import mark_writer

MESSAGES = {
    housekeeping.DONE: "The housekeeping ran.",
    housekeeping.BUSY: "Another housekeeping run is active.",
    housekeeping.IMPORT_RUNNING: "An import is running. The housekeeping did not start.",
    housekeeping.SETUP_RUNNING: "The address directory is being set up. The housekeeping did not start.",
    housekeeping.NOT_SUNDAY: "The scheduled housekeeping runs on Sunday only.",
    housekeeping.BEFORE_TIME: "The scheduled housekeeping has not reached its start time.",
    housekeeping.ALREADY_RAN: "The housekeeping already ran this week.",
}


class Command(BaseCommand):
    help = "Housekeeping of both databases (D-119); scheduled by run_scheduled_tasks with --scheduled."

    def add_arguments(self, parser):
        parser.add_argument("--now", action="store_true", help="Start a run now (manual).")

    def handle(self, *args, **options):
        # This process is the one writer of the address directory file for the run (storage.mark_writer).
        mark_writer()
        if options["now"]:
            result = housekeeping.run_now("manual")
        else:
            result = housekeeping.run_scheduled()
        self.stdout.write(MESSAGES.get(result, "The housekeeping did not start."))
