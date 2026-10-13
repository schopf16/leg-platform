"""Delete expired request counters, reset links and stale mail jobs (D-037)."""

from django.core.management.base import BaseCommand

from leg_platform.accounts.mail_queue import delete_stale_jobs
from leg_platform.accounts.password_reset import delete_expired_reset_tokens
from leg_platform.accounts.rate_limits import delete_unused_buckets


class Command(BaseCommand):
    help = "Delete unused request counters, expired reset links and unprocessed mail jobs."

    def handle(self, *args, **options):
        buckets = delete_unused_buckets()
        tokens = delete_expired_reset_tokens()
        jobs = delete_stale_jobs()
        self.stdout.write(
            f"Deleted {buckets} unused request counters, {tokens} expired reset links "
            f"and {jobs} stale mail jobs."
        )
