"""Derive the transformer circuit of the locations from the station of their EGID unless chosen by hand; scheduled by
run_scheduled_tasks after fill_location_egids. Changes nothing while the address directory cannot answer."""

from django.core.management.base import BaseCommand

from leg_platform.community import transformer_circuits


class Command(BaseCommand):
    help = "Derive the transformer circuit of the locations from their building (scheduled by run_scheduled_tasks)."

    def handle(self, *args, **options):
        self.stdout.write(f"Changed the transformer circuit of {transformer_circuits.fill_missing()} location(s).")
