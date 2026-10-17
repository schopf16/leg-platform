"""The invitation of a person whom staff created (D-087; docs/SECURITY.md, section 3).

Staff create the account (``UserManager.create_invited_user``) and never set a password. The person
gets a single-use, time-limited link (purpose ``registration``) and goes through the registration
steps; only the last step activates the account and issues the customer number (D-021, D-058). Until
then the account is unverified: no login, customer number, privileges or groups.

The email is queued like every other (outbox, D-037; no raw token is stored). Address and instance
limits apply; instead of the client-address limit the staff account has its own limit and each invited
account a pause (D-087).
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import emails, rate_limits
from leg_platform.accounts.models import MailJob, User, VerificationToken
from leg_platform.accounts.normalization import normalize_email_address

# Longer than a registration link (24 hours): the person is not waiting at the device (D-087). The
# unverified account is deleted when the link expired and no new invitation is queued (D-016).
INVITATION_LIFETIME = timedelta(days=7)

Purpose = VerificationToken.Purpose


def is_pending(user):
    """Whether the account is an invited person who has not completed the registration yet."""
    return user.status == User.Status.UNVERIFIED and user.invited_at is not None


def queue(user, actor):
    """Count the invitation against the limits and queue the job; False if a limit refuses it.

    Call it in the transaction of the change that needs the invitation (a refusal leaves nothing behind).
    Nothing is queued for an account that is not a pending invitation.
    """
    if not is_pending(user):
        return False
    address = normalize_email_address(user.email)
    results = rate_limits.consume_all(
        [
            (rate_limits.SCOPE_MAIL_ADDRESS, address, rate_limits.MAIL_ADDRESS_LIMITS),
            (rate_limits.SCOPE_MAIL_GLOBAL, str(MailJob.Kind.INVITATION), rate_limits.MAIL_GLOBAL_LIMITS),
            (rate_limits.SCOPE_INVITATION_ACTOR, str(actor.pk), rate_limits.INVITATION_ACTOR_LIMITS),
            (rate_limits.SCOPE_INVITATION_ACCOUNT, str(user.pk), rate_limits.INVITATION_ACCOUNT_LIMITS),
        ]
    )
    if not all(results):
        return False
    MailJob.objects.create(
        kind=MailJob.Kind.INVITATION, address=address, account=user, next_attempt_at=timezone.now()
    )
    return True


def process_invitation(account_id, address, greeting_name=""):
    """Make the link in the worker and return the function that sends the email, or None.

    The link goes to the account's current canonical address, and only if that is still the job's
    address and the account is still a pending invitation. Earlier links end; no raw token is stored.
    """
    with transaction.atomic():
        user = (
            User._base_manager.select_for_update()
            .filter(pk=account_id, status=User.Status.UNVERIFIED, invited_at__isnull=False)
            .first()
        )
        if user is None or normalize_email_address(user.email) != normalize_email_address(address):
            return None
        raw_token = reissue_link(user)
        address = user.email
    return lambda: emails.send_invitation(address, raw_token, INVITATION_LIFETIME)


def reissue_link(user):
    """Replace the registration links of the pending invitation by a new one and return its raw value."""
    VerificationToken.objects.filter(user=user, purpose=Purpose.REGISTRATION).delete()
    return VerificationToken.objects.issue(user, Purpose.REGISTRATION, INVITATION_LIFETIME)


def link_expires_at(user):
    """The end of the valid link of a pending invitation, or None if there is none."""
    token = (
        VerificationToken.objects.filter(
            user=user, purpose=Purpose.REGISTRATION, expires_at__gt=timezone.now()
        )
        .order_by("-expires_at")
        .first()
    )
    return token.expires_at if token else None


def is_being_sent(user):
    """Whether an invitation email of the account is queued and not sent yet (the link does not exist yet)."""
    return MailJob.objects.filter(account=user, kind=MailJob.Kind.INVITATION).exists()
