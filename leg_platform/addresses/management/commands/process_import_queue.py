from django.core.management.base import BaseCommand

from ... import worker
from ...storage import mark_writer


class Command(BaseCommand):
    help = (
        "Run the queued data imports (address directory, PV plants, transformer stations) in this process, never in the "
        "web process. With --loop it keeps waiting for new entries until it is stopped."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running and wait for new queue entries.")

    def handle(self, **options):
        mark_writer()
        worker.stop_requested.clear()
        worker.install_signal_handlers()
        if options["loop"]:
            worker.run_loop()
            return
        ran = worker.drain()
        self.stdout.write(f"Ran {ran} queued import(s).")
