"""Run the transformer station scan now (D-118), by hand; the weekly run is part of ``run_scheduled_tasks``.

Scans the postal codes of the service area (``ServicePostalCode``) once, with the same claim as the imports. Writes the
address directory file, so it needs the state directory of the instance (``docs/DEPLOYMENT.md``). Prints one fixed word,
never a building, an address or a map text.
"""

from django.core.management.base import BaseCommand, CommandError

from leg_platform.addresses import schedule
from leg_platform.addresses.storage import mark_writer


class Command(BaseCommand):
    help = "Run the transformer station scan now (manual; the weekly run is on Saturdays)."

    def handle(self, *args, **options):
        mark_writer()
        try:
            outcome = schedule.run_transformer_now()
        except Exception:  # noqa: BLE001 - recorded in the job row and the logs; the command reports no detail
            raise CommandError("The transformer station scan failed; see the status page and the application log.") from None
        self.stdout.write(f"Transformer station scan: {outcome}.")
