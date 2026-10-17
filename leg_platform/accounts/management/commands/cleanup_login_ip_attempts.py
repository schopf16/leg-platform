"""Delete expired login attempts, unused IP buckets and abandoned second factor setups (D-017, D-040)."""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import totp
from leg_platform.accounts.axes_handler import LoginProtectionHandler
from leg_platform.accounts.login_protection import IP_ATTEMPT_WINDOW
from leg_platform.accounts.models import LoginIpBucket


class Command(BaseCommand):
    help = (
        "Delete expired login attempts (IP counter and Axes), unused IP buckets and "
        "second factor setups that were never confirmed."
    )

    def handle(self, *args, **options):
        cutoff = timezone.now() - IP_ATTEMPT_WINDOW
        expired_count = bucket_count = 0
        # One short transaction per bucket, locked first like ``reserve_client_ip_attempt``, skipping a
        # bucket a request holds: never waits for a request, cannot deadlock with it.
        for digest in list(LoginIpBucket.objects.values_list("digest", flat=True)):
            with transaction.atomic():
                bucket = (
                    LoginIpBucket.objects.select_for_update(skip_locked=True)
                    .filter(digest=digest)
                    .first()
                )
                if bucket is None:
                    continue
                deleted, _ = bucket.attempts.filter(attempted_at__lt=cutoff).delete()
                expired_count += deleted
                if not bucket.attempts.exists():
                    bucket.delete()
                    bucket_count += 1
        axes_count = LoginProtectionHandler().clean_expired_user_attempts()
        setup_count = totp.delete_abandoned_enrolments()
        self.stdout.write(
            f"Deleted {expired_count} expired login IP attempts and "
            f"{bucket_count} unused login IP buckets, {axes_count} expired account attempts "
            f"and {setup_count} abandoned second factor setups."
        )
