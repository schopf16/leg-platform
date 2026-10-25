"""Delete the import error log rows older than the retention (D-122, security review): 180 days, by the scheduled tasks."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from leg_platform.addresses.models import ImportLogEntry

RETENTION = timedelta(days=180)


class Command(BaseCommand):
    help = "Delete the import error log entries older than 180 days (scheduled by run_scheduled_tasks)."

    def handle(self, *args, **options):
        deleted, _ = ImportLogEntry.objects.filter(occurred_at__lt=timezone.now() - RETENTION).delete()
        self.stdout.write(f"Deleted {deleted} import log entries older than 180 days.")
