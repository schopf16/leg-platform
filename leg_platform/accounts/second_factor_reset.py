"""Reset of a lost second factor (docs/SECURITY.md, section 3; D-004, D-005, D-041).

Three ways, all logged in the security log and notified to the owner by email:

* a user (neither staff user nor system administrator): a system administrator starts the
  removal, the application sends a time-limited, single-use link to the registered address, and
  the second factor is removed only when the owner confirms it with that link; the sender of an
  incoming email is never accepted as proof;
* a staff user or system administrator: a system administrator resets it at once, after
  verifying the person's identity in person or by calling back a phone number known
  independently of the account (the administration asks for that confirmation);
* the emergency command on the server (``manage.py emergency_access``), for any account, also
  the last system administrator.

A reset ends every session of the account. A password reset never removes or bypasses the
second factor (D-004); this module is the only place that removes it for another person.

The module does not import ``totp``, ``second_factor`` or ``mail_queue`` at module level: the mail
queue imports this module for its handler.
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

# A proposal awaiting the project owner's confirmation (D-041): as long as a password reset link.
RESET_LIFETIME = timedelta(hours=24)


class ResetRefused(Exception):
    """The rules do not allow this reset (the account changed in the meantime, wrong kind, ...)."""


def _privileged(user):
    """Staff users and system administrators must use a second factor (D-004)."""
    return user.is_staff or user.is_superuser


def _has_second_factor(user):
    return AuthenticatorDevice.objects.filter(user=user, confirmed=True).exists()


def _lock(*users):
    """Lock the account rows in the order of their primary key, so that no two resets deadlock."""
    pks = sorted({user.pk for user in users})
    rows = {
        row.pk: row
        for row in User._base_manager.select_for_update().filter(pk__in=pks).order_by("pk")
    }
    return [rows.get(user.pk) for user in users]


def _enqueue(kind, address, account=None):
    from leg_platform.accounts.mail_queue import enqueue

    enqueue(kind, address, account=account)


def _check_administrator(actor, target):
    """Refuse unless an active system administrator acts on another, active account."""
    if (
        actor is None
        or target is None
        or actor.pk == target.pk
        or not (actor.is_superuser and actor.is_staff and actor.status == User.Status.ACTIVE)
        or target.status != User.Status.ACTIVE
        or not _has_second_factor(target)
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
    user.end_all_sessions()
    from leg_platform.accounts.mail_queue import enqueue_for_account

    enqueue_for_account(MailJob.Kind.TWO_FACTOR_RESET, user)
    security_log.record(event, account=user, actor=actor, long_retention=_privileged(user))


def request_link(actor, target):
    """A system administrator starts the removal for a user; the owner confirms by link.

    Raises ResetRefused for staff users and system administrators (they are reset directly), for
    an account without a second factor and for the administrator's own account.
    """
    with transaction.atomic():
        actor_row, target_row = _lock(actor, target)
        _check_administrator(actor_row, target_row)
        if _privileged(target_row):
            raise ResetRefused
        _enqueue(MailJob.Kind.TWO_FACTOR_RESET_LINK, target_row.email, account=target_row)
        security_log.record(Event.TWO_FACTOR_RESET_REQUESTED, account=target_row, actor=actor_row)


def reset_privileged(actor, target):
    """A system administrator resets the second factor of a staff user or administrator.

    The administration only offers this after the administrator confirmed that the identity of
    the person was verified (docs/SECURITY.md, section 3). Raises ResetRefused otherwise.
    """
    with transaction.atomic():
        actor_row, target_row = _lock(actor, target)
        _check_administrator(actor_row, target_row)
        if not _privileged(target_row):
            raise ResetRefused
        remove_second_factor(target_row, Event.TWO_FACTOR_RESET, actor=actor_row)


def process_reset_link(account_id, address):
    """Handle the request for a link in the background worker; return the sending function.

    The account is found by the key of the job, not by the address: an address can have been
    given to another person since the request. The link is sent only if the account still has
    the address of the request, so that it never reaches an address that did not request it
    (D-044). The token is created here and sent at once; it is never stored in a job (D-037).
    An earlier link of the account becomes invalid.
    """
    user = User._base_manager.filter(pk=account_id, status=User.Status.ACTIVE).first()
    if (
        user is None
        or user.normalized_email != normalize_email_address(address)
        or _privileged(user)
        or not _has_second_factor(user)
    ):
        return None
    VerificationToken.objects.filter(
        user=user, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
    ).delete()
    raw_token = VerificationToken.objects.issue(
        user, VerificationToken.Purpose.TWO_FACTOR_RESET, RESET_LIFETIME
    )
    email = user.email
    return lambda: emails.send_two_factor_reset_link(email, raw_token, RESET_LIFETIME)


def find_link(raw_token):
    """Return the valid token of a link, or None."""
    token = VerificationToken.objects.find_valid(
        raw_token, VerificationToken.Purpose.TWO_FACTOR_RESET
    )
    return token if token is not None and _usable_for_link(token.user) else None


def find_link_by_digest(digest):
    token = VerificationToken.objects.find_valid_by_digest(
        digest, VerificationToken.Purpose.TWO_FACTOR_RESET
    )
    return token if token is not None and _usable_for_link(token.user) else None


def _usable_for_link(user):
    return user.status == User.Status.ACTIVE and not _privileged(user) and _has_second_factor(user)


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
        if user is None or locked_token is None or not _usable_for_link(user):
            return None
        remove_second_factor(user, Event.TWO_FACTOR_RESET)
    return user
