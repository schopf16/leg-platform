"""Restore access in an emergency (docs/DECISIONS.md, D-004, D-005, D-041).

Run only on the server, in a terminal. Every use is written to the security log (kept for the
long period) and printed without personal data. The accounts are named by their internal
number, which the administration shows on the change form of an account.

* ``reset-second-factor N``: remove the second factor of an account, also of the last system
  administrator; ends all sessions of the account and notifies its owner by email.
* ``unblock-account N``: set a blocked account active again.
* ``reencrypt-second-factor-keys``: encrypt the second factor keys again with the current
  ``SECRET_KEY`` after it was replaced and the old one is listed in ``SECRET_KEY_FALLBACKS``.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from leg_platform.accounts import second_factor_reset, security_log
from leg_platform.accounts.encryption import (
    DecryptionError,
    is_current,
    reencrypt,
)
from leg_platform.accounts.models import (
    AUTHENTICATOR_SECRET_PURPOSE,
    AccountIntegrityError,
    AuthenticatorDevice,
    RecoveryCodeDevice,
    SecurityEvent,
    User,
)

Event = SecurityEvent.Event

SUMMARIES = {
    "reset-second-factor": "remove the second factor of account {number} and end its sessions",
    "unblock-account": "set the blocked account {number} active again",
    "reencrypt-second-factor-keys": "encrypt all second factor keys again with the current key",
}


class Command(BaseCommand):
    help = "Emergency functions for the operator on the server; every use is logged."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not ask for confirmation.",
        )
        actions = parser.add_subparsers(dest="action", required=True)
        for name in ("reset-second-factor", "unblock-account"):
            action = actions.add_parser(name, help=SUMMARIES[name].format(number="N"))
            action.add_argument("internal_number", type=int)
        actions.add_parser(
            "reencrypt-second-factor-keys", help=SUMMARIES["reencrypt-second-factor-keys"]
        )

    def handle(self, *args, **options):
        action = options["action"]
        number = options.get("internal_number")
        if options["interactive"]:
            self.stdout.write(f"This will {SUMMARIES[action].format(number=number)}.")
            if input("Type 'yes' to continue: ") != "yes":
                raise CommandError("Cancelled.")
        if action == "reset-second-factor":
            self.reset_second_factor(number)
        elif action == "unblock-account":
            self.unblock_account(number)
        else:
            self.reencrypt_keys()

    def _locked_account(self, number):
        account = User._base_manager.select_for_update().filter(pk=number).first()
        if account is None:
            raise CommandError(f"No account has the internal number {number}.")
        return account

    def reset_second_factor(self, number):
        with transaction.atomic():
            account = self._locked_account(number)
            has_devices = (
                AuthenticatorDevice.objects.filter(user=account).exists()
                or RecoveryCodeDevice.objects.filter(user=account).exists()
            )
            if not has_devices:
                raise CommandError(f"Account {number} has no second factor.")
            second_factor_reset.remove_second_factor(account, Event.EMERGENCY_TWO_FACTOR_RESET)
        self.stdout.write(f"The second factor of account {number} was removed.")

    def unblock_account(self, number):
        with transaction.atomic():
            account = self._locked_account(number)
            if account.status != User.Status.BLOCKED:
                raise CommandError(f"Account {number} is not blocked.")
            try:
                account.set_status(User.Status.ACTIVE)
            except AccountIntegrityError as error:
                raise CommandError(str(error)) from None
            security_log.record(Event.EMERGENCY_ACCOUNT_UNBLOCKED, account=account)
        self.stdout.write(f"Account {number} is active again.")

    def reencrypt_keys(self):
        done = skipped = failed = 0
        for pk in list(AuthenticatorDevice.objects.values_list("pk", flat=True)):
            with transaction.atomic():
                device = AuthenticatorDevice.objects.select_for_update().filter(pk=pk).first()
                if device is None:
                    continue
                if is_current(device.encrypted_key, AUTHENTICATOR_SECRET_PURPOSE):
                    skipped += 1
                    continue
                try:
                    device.encrypted_key = reencrypt(
                        device.encrypted_key, AUTHENTICATOR_SECRET_PURPOSE
                    )
                except DecryptionError:
                    failed += 1
                    continue
                device.save(update_fields=["encrypted_key"])
                done += 1
        security_log.record(Event.EMERGENCY_KEYS_REENCRYPTED)
        self.stdout.write(
            f"{done} keys were encrypted again, {skipped} were already current, "
            f"{failed} cannot be decrypted (their accounts need a reset of the second factor)."
        )
