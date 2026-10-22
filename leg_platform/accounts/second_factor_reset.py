"""Reset of a lost second factor (D-004, D-005, D-041); every way is logged and notified to the owner.

* any account: an active staff user or system administrator verifies identity and starts recovery,
  including for their own account or the last administrator; a 24-hour single-use link goes to the
  registered address, and the old methods are removed only after its CSRF-protected confirmation;
* after confirmation: a restricted session allows only complete sign-in-method setup, never normal
  account privileges;
* the emergency command on the server (``manage.py emergency_access``), for any account.

A reset ends every session. A password reset never removes or bypasses the second factor (D-004);
this module is the only place that removes it for another person. It does not import ``totp``,
``second_factor`` or ``mail_queue`` at module level (the mail queue imports this module).
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import emails, security_log
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    MailJob,
    RecoveryCodeDevice,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.normalization import normalize_email_address

Event = SecurityEvent.Event

# As long as a password reset link (D-041).
RESET_LIFETIME = timedelta(hours=24)


class ResetRefused(Exception):
    """The rules do not allow this reset (the account changed in the meantime, wrong kind, ...)."""


def _privileged(user):
    """Staff users and system administrators must use a second factor (D-004)."""
    return user.is_staff or user.is_superuser


def _lock(*users):
    """Lock the account rows in the order of their primary key, so that no two resets deadlock."""
    pks = sorted({user.pk for user in users})
    rows = {
        row.pk: row
        for row in User._base_manager.select_for_update().filter(pk__in=pks).order_by("pk")
    }
    return [rows.get(user.pk) for user in users]


def _enqueue(kind, user, address):
    # The single place that captures the name for the greeting (D-068).
    from leg_platform.accounts.mail_queue import enqueue_for_account

    enqueue_for_account(kind, user, addresses=[address])


def _check_administrator(actor, target):
    """Allow the D-106 recovery exception only to active staff or system administrators."""
    if (
        actor is None
        or target is None
        or not ((actor.is_superuser or actor.is_staff) and actor.status == User.Status.ACTIVE)
        or target.status != User.Status.ACTIVE
    ):
        raise ResetRefused


def remove_second_factor(user, event, *, actor=None):
    """Remove the devices of the account, end its sessions, notify and log; the caller locks.

    Must run in the transaction that locked the account row.
    """
    for device in (
        *AuthenticatorDevice.objects.filter(user=user),
        *RecoveryCodeDevice.objects.filter(user=user),
    ):
        device.delete()
    VerificationToken.objects.filter(
        user=user, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
    ).delete()
    # Requests for a new address made before the reset must not be confirmed afterwards.
    user.discard_email_change_requests()
    user.end_all_sessions()
    from leg_platform.accounts.mail_queue import enqueue_for_account

    enqueue_for_account(MailJob.Kind.TWO_FACTOR_RESET, user)
    security_log.record(event, account=user, actor=actor, long_retention=_privileged(user))


def request_link(actor, target):
    """Active staff start recovery for any account; the owner confirms by link.

    Includes privileged accounts, the actor's own account and the last system administrator.
    Raises ResetRefused unless the actor is active staff or a system administrator and the
    target is active, including password-only and incomplete first-login accounts. No method is removed here.
    """
    with transaction.atomic():
        actor_row, target_row = _lock(actor, target)
        _check_administrator(actor_row, target_row)
        _enqueue(MailJob.Kind.TWO_FACTOR_RESET_LINK, target_row, target_row.email)
        security_log.record(Event.TWO_FACTOR_RESET_REQUESTED, account=target_row, actor=actor_row)


def reset_privileged(actor, target):
    """Compatibility entry point for requesting the same owner-confirmed recovery link.

    The administration requires the official-ID name check for every target. This delegates to
    request_link and never performs an immediate privileged reset; its refusal rules are identical.
    """
    request_link(actor, target)


def process_reset_link(account_id, address, greeting_name=""):
    """Handle the link request in the background worker; return the sending function.

    The account is found by the job's key, not the address (it may have passed to someone else); the
    link goes out only if the account still has the requested address (D-044). The token is created
    here and sent at once, never stored in a job (D-037). An earlier link becomes invalid.
    """
    address = normalize_email_address(address)
    with transaction.atomic():
        user = (
            User._base_manager.select_for_update()
            .filter(pk=account_id, status=User.Status.ACTIVE).first()
        )
        if user is None or user.normalized_email != address:
            return None
        VerificationToken.objects.filter(
            user=user, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
        ).delete()
        raw_token = VerificationToken.objects.issue(
            user, VerificationToken.Purpose.TWO_FACTOR_RESET, RESET_LIFETIME,
            target_email=address,
        )

    def send():
        # Dispatch happens after issuance. Recheck under the same account-first lock so an
        # intervening address change or newer issuance cannot send a stale recovery link.
        with transaction.atomic():
            current = User._base_manager.select_for_update().filter(pk=account_id).first()
            token = find_link(raw_token)
            if current is None or token is None or not _usable_for_link(current, token):
                return
            emails.send_two_factor_reset_link(
                current.email, raw_token, RESET_LIFETIME, greeting_name
            )

    return send


def find_link(raw_token):
    """Return the valid token of a link, or None."""
    token = VerificationToken.objects.find_valid(
        raw_token, VerificationToken.Purpose.TWO_FACTOR_RESET
    )
    return token if token is not None and _usable_for_link(token.user, token) else None


def find_link_by_digest(digest):
    token = VerificationToken.objects.find_valid_by_digest(
        digest, VerificationToken.Purpose.TWO_FACTOR_RESET
    )
    return token if token is not None and _usable_for_link(token.user, token) else None


def _usable_for_link(user, token):
    return (
        user.status == User.Status.ACTIVE
        and bool(token.target_email)
        and normalize_email_address(token.target_email) == user.normalized_email
    )


def confirm_link(token):
    """The owner confirmed the removal with the link; return the account, or None if not valid.

    The account is locked before its token, as everywhere else; the token is consumed here.
    """
    with transaction.atomic():
        user = User._base_manager.select_for_update().filter(pk=token.user_id).first()
        locked_token = (
            VerificationToken.objects.select_for_update()
            .filter(
                pk=token.pk,
                purpose=VerificationToken.Purpose.TWO_FACTOR_RESET,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if user is None or locked_token is None or not _usable_for_link(user, locked_token):
            return None
        from leg_platform.accounts.models import PasskeyIdentity, PendingSignInChange
        from leg_platform.accounts.mail_queue import enqueue_for_account

        user.passkeys.all().delete()
        PasskeyIdentity.objects.filter(user=user).update(two_devices_confirmed=False)
        AuthenticatorDevice.objects.filter(user=user).delete()
        RecoveryCodeDevice.objects.filter(user=user).delete()
        PendingSignInChange.objects.filter(user=user).delete()
        user.set_unusable_password()
        user.password_change_required = False
        user._change(authentication_method=User.AuthenticationMethod.RECOVERY, password=user.password)
        user.save(update_fields=["password_change_required"])
        user.discard_email_change_requests()
        VerificationToken.objects.filter(user=user).delete()
        user.end_all_sessions()
        enqueue_for_account(MailJob.Kind.TWO_FACTOR_RESET, user)
        security_log.record(Event.TWO_FACTOR_RESET, account=user, long_retention=_privileged(user))
    return user
