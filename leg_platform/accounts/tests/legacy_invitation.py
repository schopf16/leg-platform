"""A person invited the way staff did before 2026-11-19 (D-087): unverified, marked ``invited_at``, with a queued invitation.

Production code creates no such account any more (``UserManager.create_person_account`` makes an active one); the tests
of the invitations that may still be pending (link, resend, expiry, the state "Invited") build them here."""

from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import invitations, security_log
from leg_platform.accounts.models import SecurityEvent, User


def create_legacy_invitation(email, **profile):
    """The account of an invited person, as ``create_invited_user`` made it: no customer number, no usable password."""
    email = User.objects._prepare(email, profile)
    user = User(email=email, invited_at=timezone.now(), **profile)
    user.set_unusable_password()
    user.full_clean(exclude=["password", "last_login"], validate_unique=False)
    with user._account_change("invited_at"):
        user.save()
    return user


def invite_legacy_person(actor, email, values):
    """What ``persons.create_person`` did before: the account, the queued invitation, the History and the security event."""
    from leg_platform.community import history
    from leg_platform.community.history import HistoryEvent

    with transaction.atomic():
        user = create_legacy_invitation(email, **values)
        assert invitations.queue(user, actor), "The invitation was refused by a limit."
        history.record(user, HistoryEvent.CREATED, actor)
        history.record(user, HistoryEvent.PERSON_INVITATION_SENT, actor)
        security_log.record(SecurityEvent.Event.PERSON_INVITED, account=user, actor=actor)
    return user
