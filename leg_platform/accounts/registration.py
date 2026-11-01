"""Registration and verification (D-016, D-021, D-136).

A new registration proves the address with a six-digit code (D-136); a person invited by staff keeps the
link (D-087). The caller always shows the same generic response, so nobody learns which addresses have an
account.
"""

import hmac
import logging
import re
from datetime import timedelta
from enum import Enum

from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from leg_platform.accounts import emails, invitations, rate_limits
from leg_platform.accounts.models import (
    AccountIntegrityError,
    MailJob,
    RegistrationChallenge,
    User,
    VerificationToken,
    challenge_digest,
    code_digest,
    new_code,
)
from leg_platform.accounts.normalization import normalize_email_address

logger = logging.getLogger(__name__)

# How long a code is valid; the same for every address (D-136).
CODE_LIFETIME = timedelta(minutes=15)
# Wrong entries per code; the fifth one ends the code.
MAX_CODE_ATTEMPTS = 5
# How long a confirmed address stays proven for the steps 2 and 3. Unverified accounts and their data are
# deleted when it ends (D-016, D-037).
PROOF_LIFETIME = timedelta(hours=2)
# A request that waited longer than this in the queue is dropped, so a code does not arrive nearly expired.
JOB_MAX_AGE = timedelta(minutes=5)
# A request that the bot check caught gets a session value that cannot be a challenge id (no row, D-136).
DECOY_PREFIX = "~"

# Outcome of a registration that created no account because the address already has one.
ADDRESS_HAS_ACCOUNT = object()


class RegistrationConflict(Exception):
    """The pending registration changed while it was being replaced; the job is repeated."""


class CodeResult(Enum):
    CONFIRMED = "confirmed"
    WRONG = "wrong"  # "This code is not correct"
    INVALID = "invalid"  # expired, used, dead or unknown: "This code is no longer valid"


_SEPARATORS = re.compile(r"[\s\-\u2010-\u2015]+")


def normalize_code(value):
    """Remove spaces and hyphens that a person or a mail client adds when copying the code."""
    return _SEPARATORS.sub("", (value or "").strip())


def process_registration(address):
    """Handle a registration request in the background worker (D-037, D-136).

    Returns the function that sends the email, or None. A new or still unverified address gets a
    confirmation code, bound to the newest open challenge of the address (the code is made here and stored
    nowhere but as a digest). An address of an active, temporarily locked or blocked account gets an
    information email, at most once a day, and no code; deactivated accounts do not hold their address
    (D-016). A person invited by staff gets the link of the invitation (D-087). Any error is raised, so the
    whole step is rolled back and the worker repeats the job; a repeat replaces the code, so only one
    code is ever valid per challenge.

    The address is the account's canonical one (D-035) and the email goes to it, never to the entered
    spelling: Django's IDNA 2003 codec maps some characters (e.g. "ß") differently from the UTS #46
    form that identifies the account, so the entered spelling could reach another mailbox.
    """
    address = normalize_email_address(address)
    # Read without a lock: an address of a non-pending account is never locked, so requests for it cannot
    # delay that person.
    existing = User._base_manager.filter(normalized_email=address).first()
    if existing is not None and existing.status != User.Status.UNVERIFIED:
        if not rate_limits.consume(
            rate_limits.SCOPE_EXISTING_ACCOUNT_NOTICE,
            address,
            rate_limits.EXISTING_ACCOUNT_NOTICE_LIMITS,
        ):
            return None
        return lambda: emails.send_registration_existing_account(address)
    if existing is not None and existing.invited_at is not None:
        raw_token = _reissue_invitation(existing)
        return lambda: emails.send_invitation(address, raw_token, invitations.INVITATION_LIFETIME)
    code = _issue_code(address)
    if code is None:
        return None
    return lambda: emails.send_registration_code(address, code, CODE_LIFETIME)


def _reissue_invitation(existing):
    """A person invited by staff asks for a link (D-087): the account and the staff's data stay; the earlier link ends."""
    with transaction.atomic():
        pending = (
            User._base_manager.select_for_update()
            .filter(pk=existing.pk, status=User.Status.UNVERIFIED)
            .first()
        )
        if pending is None or pending.invited_at is None:
            raise RegistrationConflict
        return invitations.reissue_link(pending)


def _issue_code(address):
    """Make the code of the newest open challenge of the address and store its digest; return the raw code or None.

    No account row is involved, so only the challenge row is locked (lock order, D-072); a repeated job
    replaces the digest, so the earlier code of the challenge dies.
    """
    now = timezone.now()
    open_rows = RegistrationChallenge.objects.filter(
        address=address, consumed=False, expires_at__gt=now, failed_attempts__lt=MAX_CODE_ATTEMPTS
    )
    newest = open_rows.order_by("-pk").values_list("pk", flat=True).first()
    if newest is None:
        return None
    with transaction.atomic():
        row = open_rows.select_for_update().filter(pk=newest).first()
        if row is None:
            return None
        code = new_code()
        row.code_digest = code_digest(row.challenge_digest, code)
        # expires_at stays as set at the request (CODE_LIFETIME from then) for every address kind, so the
        # moment of "expired" does not depend on the address; the 5-minute stale-job drop of the queue
        # bounds the delay, so a mailed code arrives with at least 10 minutes left.
        row.save(update_fields=["code_digest"])
    return code


def confirm_code(raw_challenge, entered):
    """Check an entered code against the challenge of the session; return a ``CodeResult`` (D-136).

    A wrong entry is counted under the lock of the challenge also when no code exists (an address with an
    account), so the behaviour does not depend on the address. On success the unverified account is created
    (a pending registration of the same address is replaced, an invited one is never touched) and the
    address counts as proven for ``PROOF_LIFETIME``. Lock order (D-072): the account row first, then the
    challenge row.
    """
    digest = challenge_digest(raw_challenge or "")
    code = normalize_code(entered)
    row = RegistrationChallenge.objects.filter(challenge_digest=digest).first()
    if row is None or not row.is_open(MAX_CODE_ATTEMPTS):
        return CodeResult.INVALID
    if not _matches(row, code):
        return _count_wrong_entry(row.pk, code)
    existing = User._base_manager.filter(normalized_email=row.address).first()
    if existing is not None and (existing.status != User.Status.UNVERIFIED or existing.invited_at is not None):
        return CodeResult.INVALID
    try:
        with transaction.atomic():
            if existing is not None:
                pending = (
                    User._base_manager.select_for_update()
                    .filter(pk=existing.pk, status=User.Status.UNVERIFIED, invited_at__isnull=True)
                    .first()
                )
                if pending is None:
                    return CodeResult.INVALID
            locked = RegistrationChallenge.objects.select_for_update().filter(pk=row.pk).first()
            if locked is None or not locked.is_open(MAX_CODE_ATTEMPTS) or not _matches(locked, code):
                return CodeResult.INVALID
            if existing is not None:
                pending.delete()
            with transaction.atomic():
                user = User.objects.create_user(locked.address)
            locked.user = user
            locked.consumed = True
            locked.code_digest = ""
            locked.proven_until = timezone.now() + PROOF_LIFETIME
            locked.save(update_fields=["user", "consumed", "code_digest", "proven_until"])
    except (IntegrityError, AccountIntegrityError):
        return CodeResult.INVALID
    return CodeResult.CONFIRMED


def _matches(row, code):
    """Compare in constant time; without a stored code the comparison still runs and fails."""
    supplied = code_digest(row.challenge_digest, code)
    return bool(row.code_digest) and hmac.compare_digest(supplied, row.code_digest)


def _count_wrong_entry(pk, code):
    with transaction.atomic():
        row = RegistrationChallenge.objects.select_for_update().filter(pk=pk).first()
        if row is None or not row.is_open(MAX_CODE_ATTEMPTS):
            return CodeResult.INVALID
        if _matches(row, code):
            # Another submission of the right code won the race for the lock: let this one try again as a whole.
            return CodeResult.INVALID
        row.failed_attempts += 1
        row.save(update_fields=["failed_attempts"])
        if row.failed_attempts >= MAX_CODE_ATTEMPTS:
            row.code_digest = ""
            row.save(update_fields=["code_digest"])
            return CodeResult.INVALID
    return CodeResult.WRONG


def find_proof(raw_challenge):
    """Return the challenge of the session if its address is proven and its account still unverified, else None."""
    if not raw_challenge:
        return None
    row = (
        RegistrationChallenge.objects.select_related("user")
        .filter(challenge_digest=challenge_digest(raw_challenge))
        .first()
    )
    if row is None or not row.is_proven() or row.user.status != User.Status.UNVERIFIED:
        return None
    return row


def code_is_live(raw_challenge):
    """True while the challenge of the session still takes a code (not used up, not expired, fewer than five wrong entries).

    Rows of addresses that have an account count the same way as the others, so the page does not tell them apart.
    A decoy (the answer to a request of the bot check, which has no row) counts as live: its page looks like the others.
    """
    if not raw_challenge:
        return False
    if raw_challenge.startswith(DECOY_PREFIX):
        return True
    row = RegistrationChallenge.objects.filter(challenge_digest=challenge_digest(raw_challenge)).first()
    return row is not None and row.is_open(MAX_CODE_ATTEMPTS)


def discard(raw_challenge):
    """Delete the challenge of the session ("Change address", a new code); a proven one takes its unverified account along later (cleanup)."""
    if raw_challenge:
        RegistrationChallenge.objects.filter(challenge_digest=challenge_digest(raw_challenge)).delete()


def delete_expired_registrations():
    """Delete unverified accounts without a valid verification link or proof; return (deleted, failed). One failure does not stop the others."""
    now = timezone.now()
    valid_link = VerificationToken.objects.filter(
        user=OuterRef("pk"),
        purpose=VerificationToken.Purpose.REGISTRATION,
        expires_at__gt=now,
    )
    valid_proof = RegistrationChallenge.objects.filter(user=OuterRef("pk"), proven_until__gt=now)
    # A queued staff invitation (D-087) has no link until sent.
    queued_invitation = MailJob.objects.filter(account=OuterRef("pk"), kind=MailJob.Kind.INVITATION)
    expired_user_ids = list(
        User._base_manager.filter(status=User.Status.UNVERIFIED)
        .annotate(
            has_valid_link=Exists(valid_link),
            has_valid_proof=Exists(valid_proof),
            has_queued_invitation=Exists(queued_invitation),
        )
        .filter(has_valid_link=False, has_valid_proof=False, has_queued_invitation=False)
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
    # Challenges that can no longer be used: a code that expired, a proof that ended.
    RegistrationChallenge.objects.filter(
        Q(consumed=False, expires_at__lt=now) | Q(proven_until__lt=now)
    ).delete()
    return deleted, failed
