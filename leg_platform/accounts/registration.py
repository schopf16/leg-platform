"""Registration and verification (D-016, D-021).

The caller always shows the same generic response, so nobody learns which addresses have an account.
"""

import logging
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from leg_platform.accounts import emails, invitations, rate_limits
from leg_platform.accounts.models import AccountIntegrityError, MailJob, User, VerificationToken
from leg_platform.accounts.normalization import normalize_email_address

logger = logging.getLogger(__name__)

# Unverified accounts and their data are deleted when the link expires (D-016, D-037).
REGISTRATION_LINK_LIFETIME = timedelta(hours=24)

# Outcome of a registration that created no account because the address already has one.
ADDRESS_HAS_ACCOUNT = object()


class RegistrationConflict(Exception):
    """The pending registration changed while it was being replaced; the job is repeated."""


def process_registration(address):
    """Handle a registration request in the background worker (D-037).

    Returns the function that sends the email, or None. A new or still unverified address gets a
    verification link (a pending registration of the same address is replaced). An address of an
    active, temporarily locked or blocked account gets an information email, at most once a day, and
    no account is created; deactivated accounts do not hold their address (D-016). Any error is raised,
    so the whole step is rolled back and the worker repeats the job.

    The address is the account's canonical one (D-035) and the email goes to it, never to the entered
    spelling: Django's IDNA 2003 codec maps some characters (e.g. "ß") differently from the UTS #46
    form that identifies the account, so the entered spelling could reach another mailbox.
    """
    address = normalize_email_address(address)
    outcome = _register_once(address, address)
    if outcome is ADDRESS_HAS_ACCOUNT:
        if not rate_limits.consume(
            rate_limits.SCOPE_EXISTING_ACCOUNT_NOTICE,
            address,
            rate_limits.EXISTING_ACCOUNT_NOTICE_LIMITS,
        ):
            return None
        return lambda: emails.send_registration_existing_account(address)
    raw_token, lifetime = outcome
    return lambda: emails.send_registration_verification(address, raw_token, lifetime)


def _register_once(email, normalized):
    """Store the registration; return the raw token and its lifetime, or ADDRESS_HAS_ACCOUNT."""
    with transaction.atomic():
        # Read without a lock: an address of a non-pending account is never locked, so requests for
        # it cannot delay that person. A pending registration is locked and re-checked (lock order:
        # account first, then its token, as at completion).
        existing = User._base_manager.filter(normalized_email=normalized).first()
        if existing is not None and existing.status != User.Status.UNVERIFIED:
            return ADDRESS_HAS_ACCOUNT
        if existing is not None:
            pending = (
                User._base_manager.select_for_update()
                .filter(pk=existing.pk, status=User.Status.UNVERIFIED)
                .first()
            )
            if pending is None:
                raise RegistrationConflict
            if pending.invited_at is not None:
                # A person invited by staff asks for a link (D-087): the account and the staff's
                # data stay (registering cannot delete an invitation); the earlier link ends.
                return invitations.reissue_link(pending), invitations.INVITATION_LIFETIME
            pending.delete()
        user = User.objects.create_user(email)
        token = VerificationToken.objects.issue(
            user, VerificationToken.Purpose.REGISTRATION, REGISTRATION_LINK_LIFETIME
        )
        return token, REGISTRATION_LINK_LIFETIME


def delete_expired_registrations():
    """Delete unverified accounts without a valid verification link; return (deleted, failed). One failure does not stop the others."""
    valid_link = VerificationToken.objects.filter(
        user=OuterRef("pk"),
        purpose=VerificationToken.Purpose.REGISTRATION,
        expires_at__gt=timezone.now(),
    )
    # A queued staff invitation (D-087) has no link until sent.
    queued_invitation = MailJob.objects.filter(account=OuterRef("pk"), kind=MailJob.Kind.INVITATION)
    expired_user_ids = list(
        User._base_manager.filter(status=User.Status.UNVERIFIED)
        .annotate(has_valid_link=Exists(valid_link), has_queued_invitation=Exists(queued_invitation))
        .filter(has_valid_link=False, has_queued_invitation=False)
        .values_list("pk", flat=True)
    )
    deleted = failed = 0
    for user_id in expired_user_ids:
        try:
            with transaction.atomic():
                # Status re-checked under the lock; deletion goes through the central account logic.
                user = (
                    User._base_manager.select_for_update()
                    .filter(pk=user_id, status=User.Status.UNVERIFIED)
                    .first()
                )
                if user is not None:
                    user.delete()
                    deleted += 1
        except (AccountIntegrityError, DatabaseError) as error:
            failed += 1
            logger.error("An expired registration could not be deleted (%s).", type(error).__name__)
    return deleted, failed
