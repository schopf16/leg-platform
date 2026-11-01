"""Set the EGID of the locations that have none from the address directory (D-135); scheduled by run_scheduled_tasks."""

from django.core.management.base import BaseCommand

from leg_platform.community import location_egid


class Command(BaseCommand):
    help = "Fill the missing EGID of the locations from their address (scheduled by run_scheduled_tasks)."

    def handle(self, *args, **options):
        self.stdout.write(f"Set the EGID of {location_egid.fill_missing()} location(s).")
