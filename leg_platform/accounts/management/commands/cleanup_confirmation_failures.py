"""Delete the old counts of wrong entries in the confirmations of the profile (D-062, block 10)."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from leg_platform.accounts.models import ConfirmationFailure, SessionCodeFailure
from leg_platform.accounts.second_factor import (
    ACCOUNT_FAILURE_WINDOW,
    SESSION_CODE_FAILURE_RETENTION,
)


class Command(BaseCommand):
    help = (
        "Delete the counts of wrong codes and passwords of sessions that ended without a logout "
        "(after 30 days) and the entries of the cap of the account (after 24 hours)."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        sessions, _ = SessionCodeFailure.objects.filter(
            updated_at__lt=now - SESSION_CODE_FAILURE_RETENTION
        ).delete()
        accounts, _ = ConfirmationFailure.objects.filter(
            occurred_at__lt=now - ACCOUNT_FAILURE_WINDOW
        ).delete()
        self.stdout.write(
            f"Deleted {sessions} counts of sessions and {accounts} entries of the cap of the "
            "account for wrong entries."
        )
