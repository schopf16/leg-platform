"""Delete the old counts of wrong entries in the confirmations of the profile (D-062, block 10)."""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from leg_platform.accounts.models import ConfirmationFailure, SessionCodeFailure, User
from leg_platform.accounts.second_factor import (
    ACCOUNT_FAILURE_WINDOW,
    SESSION_CODE_FAILURE_RETENTION,
)


def delete_expired_per_account(model, field, cutoff):
    """Delete the rows of ``model`` whose ``field`` is before ``cutoff``; return (deleted, skipped).

    The lock order of the confirmation counters is: account row, then counter rows (D-072); the
    requests keep it (``second_factor``). The accounts that have expired rows are found with a
    read that takes no lock. Then, for each account in a short transaction of its own, the account
    row is locked (``skip_locked``: an account that a request holds is left for the next run, so
    the clean-up never waits for a request and cannot be part of a cycle with it), and only the
    expired rows of this account are deleted, through the index that starts with the account like
    the requests do. A skipped account also counts an account that was deleted in the meantime
    (its rows went with the cascade).
    """
    expired = {f"{field}__lt": cutoff}
    account_ids = list(
        model.objects.filter(**expired).order_by().values_list("account_id", flat=True).distinct()
    )
    deleted_total = skipped = 0
    for account_id in account_ids:
        with transaction.atomic():
            account = (
                User._base_manager.select_for_update(skip_locked=True)
                .filter(pk=account_id)
                .first()
            )
            if account is None:
                skipped += 1
                continue
            deleted, _ = model.objects.filter(account_id=account_id, **expired).delete()
            deleted_total += deleted
    return deleted_total, skipped


class Command(BaseCommand):
    help = (
        "Delete the counts of wrong codes and passwords of sessions that ended without a logout "
        "(after 30 days) and the entries of the cap of the account (after 24 hours)."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        sessions, sessions_skipped = delete_expired_per_account(
            SessionCodeFailure, "updated_at", now - SESSION_CODE_FAILURE_RETENTION
        )
        accounts, accounts_skipped = delete_expired_per_account(
            ConfirmationFailure, "occurred_at", now - ACCOUNT_FAILURE_WINDOW
        )
        message = (
            f"Deleted {sessions} counts of sessions and {accounts} entries of the cap of the "
            "account for wrong entries."
        )
        skipped = sessions_skipped + accounts_skipped
        if skipped:
            message += f" Left {skipped} for the next run (the account was in use)."
        self.stdout.write(message)
