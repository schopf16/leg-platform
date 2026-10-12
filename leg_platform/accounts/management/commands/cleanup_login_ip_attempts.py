"""Delete expired login IP attempts and unused pseudonymous buckets (D-017)."""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from leg_platform.accounts.login_protection import IP_ATTEMPT_WINDOW
from leg_platform.accounts.models import LoginIpAttempt, LoginIpBucket


class Command(BaseCommand):
    help = "Delete expired login IP attempts and unused buckets."

    def handle(self, *args, **options):
        cutoff = timezone.now() - IP_ATTEMPT_WINDOW
        with transaction.atomic():
            expired_count, _ = LoginIpAttempt.objects.filter(
                attempted_at__lt=cutoff
            ).delete()
            bucket_count, _ = LoginIpBucket.objects.filter(attempts__isnull=True).delete()
        self.stdout.write(
            f"Deleted {expired_count} expired login IP attempts and "
            f"{bucket_count} unused login IP buckets."
        )