"""Delete the security log entries whose retention has passed (D-018, D-041)."""

from django.core.management.base import BaseCommand

from leg_platform.accounts.models import SecurityEvent


class Command(BaseCommand):
    help = (
        "Delete the security log entries that are older than the retention of the instance "
        "(SECURITY_LOG_RETENTION_DAYS, SECURITY_LOG_LONG_RETENTION_DAYS)."
    )

    def handle(self, *args, **options):
        self.stdout.write(f"Deleted {SecurityEvent.objects.delete_expired()} security log entries.")
