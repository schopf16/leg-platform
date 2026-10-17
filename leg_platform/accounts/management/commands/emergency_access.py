"""Restore access in an emergency (D-004, D-005, D-041).

Run only on the server, in a terminal; every use is written to the security log (long retention).
The result line names the account by internal number only; the prompt shows its email address and
rights. Accounts with staff or administrator rights must be confirmed by typing their email address
and cannot be changed with ``--no-input``.

* ``reset-second-factor N``: remove the second factor, end all sessions, notify the owner. The last
  active system administrator needs ``--allow-last-administrator``.
* ``unblock-account N``: set a blocked account active again.
* ``reencrypt-second-factor-keys``: re-encrypt the second factor keys with the current ``SECRET_KEY``
  after it was replaced and the old one is in ``SECRET_KEY_FALLBACKS``.
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
            if name == "reset-second-factor":
                action.add_argument(
                    "--allow-last-administrator",
                    action="store_true",
                    help="Allow the reset of the second factor of the last active system administrator.",
                )
        actions.add_parser(
            "reencrypt-second-factor-keys", help=SUMMARIES["reencrypt-second-factor-keys"]
        )

    def handle(self, *args, **options):
        action = options["action"]
        number = options.get("internal_number")
        interactive = options["interactive"]
        if action == "reencrypt-second-factor-keys":
            if interactive:
                self.stdout.write(f"This will {SUMMARIES[action]}.")
                self._confirm_text("Type 'yes' to continue: ", "yes")
            self.reencrypt_keys()
            return
        account = User._base_manager.filter(pk=number).first()
        if account is None:
            raise CommandError(f"No account has the internal number {number}.")
        privileged = account.is_staff or account.is_superuser
        if privileged and not interactive:
            raise CommandError(
                "Accounts with staff or administrator rights can only be changed interactively."
            )
        if interactive:
            self._confirm_account(action, account, number, privileged)
        if action == "reset-second-factor":
            self.reset_second_factor(number, options.get("allow_last_administrator", False))
        else:
            self.unblock_account(number)

    def _confirm_account(self, action, account, number, privileged):
        """Show the account before the change; privileged accounts need their email typed."""
        if account.is_superuser:
            rights = "system administrator"
        elif account.is_staff:
            rights = "staff user"
        else:
            rights = "user"
        self.stdout.write(f"Account {number}: {account.email} ({rights}).")
        self.stdout.write(f"This will {SUMMARIES[action].format(number=number)}.")
        if privileged:
            self._confirm_text("Type the email address of the account to continue: ", account.email)
        else:
            self._confirm_text("Type 'yes' to continue: ", "yes")

    def _confirm_text(self, prompt, expected):
        try:
            answer = input(prompt)
        except EOFError:
            raise CommandError("Cancelled.") from None
        if answer.strip().lower() != expected.lower():
            raise CommandError("Cancelled.")

    def _locked_account(self, number):
        account = User._base_manager.select_for_update().filter(pk=number).first()
        if account is None:
            raise CommandError(f"No account has the internal number {number}.")
        return account

    def reset_second_factor(self, number, allow_last_administrator):
        with transaction.atomic():
            account = self._locked_account(number)
            has_devices = (
                AuthenticatorDevice.objects.filter(user=account).exists()
                or RecoveryCodeDevice.objects.filter(user=account).exists()
            )
            if not has_devices:
                raise CommandError(f"Account {number} has no second factor.")
            other_administrators = (
                User._base_manager.filter(is_superuser=True, status=User.Status.ACTIVE)
                .exclude(pk=account.pk)
                .exists()
            )
            if account.is_superuser and not other_administrators and not allow_last_administrator:
                raise CommandError(
                    f"Account {number} is the last active system administrator. Repeat the command "
                    "with --allow-last-administrator only if this is intended."
                )
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
