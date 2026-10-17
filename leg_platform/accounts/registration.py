"""Registration and verification (docs/PROJECT.md, "Registration"; D-016, D-021).

The caller always shows the same generic response, whatever happens here, so that nobody can
learn which addresses have an account (OWASP Authentication Cheat Sheet).
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

# Unverified accounts and their data are deleted when the link expires (D-016). The value is a
# proposal awaiting the project owner's confirmation (D-037).
REGISTRATION_LINK_LIFETIME = timedelta(hours=24)

# Outcome of a registration that created no account because the address already has one.
ADDRESS_HAS_ACCOUNT = object()


class RegistrationConflict(Exception):
    """The pending registration changed while it was being replaced; the job is repeated."""


def process_registration(address):
    """Handle a registration request in the background worker (D-037).

    Stores what is needed and returns the function that sends the email, or None. A new or
    still unverified address receives a verification link; a pending registration of
    the same address is replaced. An address of an account that is active, temporarily locked or
    blocked receives an information email, at most once a day, and no account is created.
    Deactivated accounts do not hold their address (D-016). Any error is raised, so that the
    whole step, including the created account, is rolled back; the worker repeats the job.

    The address is the canonical one that the account stores (lower case, domain in IDNA ASCII,
    D-035) and the email goes to it, never to the entered spelling: Django encodes the domain
    of an address with the older IDNA 2003 codec, which maps some characters differently from
    the UTS #46 form that identifies the account (for example "ß"), so the entered spelling
    could reach another mailbox than the one the account is bound to.
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
        # Read without a lock: an address of an account that is not pending is never locked, so
        # requests for it cannot delay that person's own requests. A pending registration is
        # locked, and its status is checked again under the lock (the same lock order as the
        # completion of a registration: account first, then its token).
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
                # A person whom staff invited asks for a link themselves (D-087): the account and the
                # data of the staff stay, nobody can delete an invitation by registering the address; the
                # link goes to the mailbox as always, and the earlier link ends.
                return invitations.reissue_link(pending), invitations.INVITATION_LIFETIME
            pending.delete()
        user = User.objects.create_user(email)
        token = VerificationToken.objects.issue(
            user, VerificationToken.Purpose.REGISTRATION, REGISTRATION_LINK_LIFETIME
        )
        return token, REGISTRATION_LINK_LIFETIME


def delete_expired_registrations():
    """Delete unverified accounts without a valid verification link.

    Returns the numbers of deleted and failed accounts. One failing account does not stop the
    others.
    """
    valid_link = VerificationToken.objects.filter(
        user=OuterRef("pk"),
        purpose=VerificationToken.Purpose.REGISTRATION,
        expires_at__gt=timezone.now(),
    )
    # An invitation of staff (D-087) whose email is queued but not sent yet has no link so far.
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
                # The status is checked again under the lock: only an unverified account is
                # deleted. Each account goes through the central account logic
                # (docs/SECURITY.md, section 4).
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
