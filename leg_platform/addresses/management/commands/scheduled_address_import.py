"""The daily run, one of the tasks of ``run_scheduled_tasks`` (KONZEPT 3a, D-090, D-117).

``schedule.run_daily`` runs the address directory and then the plant register in this process. Does nothing before
the configured time of day, once the day's check is done, or when nothing is due. When an import is due it runs it
here (this process writes the directory file, so it needs the state directory and the network of the instance,
``docs/DEPLOYMENT.md`` section 8.3). Prints one fixed word, never an address.
"""

from django.core.management.base import BaseCommand, CommandError

from leg_platform.addresses import schedule
from leg_platform.addresses.storage import mark_writer


class Command(BaseCommand):
    help = "Check the update flag of the address directory once per day and import the directory when it is due."

    def handle(self, *args, **options):
        mark_writer()
        try:
            outcome = schedule.run_daily()
        except Exception:  # noqa: BLE001 - recorded in the job row and the logs; the command reports no detail
            raise CommandError("The daily import run failed; see the status page and the application log.") from None
        self.stdout.write(f"Address directory run: {outcome}.")
