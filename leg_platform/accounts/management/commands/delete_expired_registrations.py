"""Delete unverified accounts without a valid verification link (D-016)."""

from django.core.management.base import BaseCommand, CommandError

from leg_platform.accounts.registration import delete_expired_registrations


class Command(BaseCommand):
    help = "Delete unverified accounts whose verification link has expired or does not exist."

    def handle(self, *args, **options):
        deleted, failed = delete_expired_registrations()
        self.stdout.write(f"Deleted {deleted} expired unverified accounts.")
        if failed:
            # A non-zero exit status lets a scheduler report the failure.
            raise CommandError(f"{failed} expired unverified accounts could not be deleted.")
