"""Verified changes of the email addresses (D-016, D-059).

A new login or second address is confirmed with a link sent to it (valid 24 hours; the old address
stays in use meanwhile). The request makes a ``PendingEmailChange`` per target address, so the
profile and the response do not depend on whether an address can be used; the worker makes and
sends the links; the old addresses get a notice with a link that stops the change without a login.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from leg_platform.accounts import emails, rate_limits, security_log
from leg_platform.accounts.models import (
    AccountIntegrityError,
    MailJob,
    PendingEmailChange,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.normalization import normalize_email_address

EMAIL_CHANGE_LIFETIME = timedelta(hours=24)

Purpose = VerificationToken.Purpose
Kind = PendingEmailChange.Kind
# The token purpose that confirms a request of this kind.
PURPOSE_OF_KIND = {Kind.LOGIN: Purpose.EMAIL_CHANGE, Kind.SECONDARY: Purpose.SECONDARY_EMAIL}
KIND_OF_PURPOSE = {purpose: kind for kind, purpose in PURPOSE_OF_KIND.items()}
CONFIRMATION_PURPOSES = tuple(PURPOSE_OF_KIND.values())


def _drop_stop_tokens_if_idle(user):
    """Delete the stop links when no request is open any more: they only mean something while a change is pending."""
    if not has_open_request(user):
        VerificationToken.objects.filter(user=user, purpose=Purpose.EMAIL_CHANGE_STOP).delete()


def has_open_request(user):
    """Whether the account has a request for a new address that has not expired."""
    return PendingEmailChange.objects.filter(user=user, expires_at__gt=timezone.now()).exists()


def pending_changes(user):
    """The open requests of the account by kind; expired ones are removed (D-059)."""
    expired, _ = PendingEmailChange.objects.filter(user=user, expires_at__lte=timezone.now()).delete()
    if expired:
        _drop_stop_tokens_if_idle(user)
    return {row.kind: row for row in PendingEmailChange.objects.filter(user=user)}


def _remaining(pending):
    return max(pending.expires_at - timezone.now(), timedelta(seconds=1))


class RequestRefused(Exception):
    """The client or instance limit is used up (D-038): nothing was stored."""


def request_changes(request, user, *, login=None, secondary=None, remove_secondary=False, actor=None):
    """Make the requests of one submission of the profile form (D-059).

    ``login`` and ``secondary`` are new addresses (normalised, checked; the caller has asked for the
    proof). A removed second address is removed at once. Every request counts against the mail
    request limits (D-038); the old addresses get the notice with the stop link, and the link goes
    to the new address only. ``actor`` is the staff account acting for the customer (D-087, second
    address only); the security log names it.

    The notice is queued before the links so it is sent first. If a limit refuses a request,
    ``RequestRefused`` is raised and everything of this call is rolled back.
    """
    from leg_platform.accounts.mail_queue import enqueue_for_account, submit_mail_request

    requested = False
    requester = getattr(actor, "pk", None)
    with transaction.atomic():
        locked = User._base_manager.select_for_update().get(pk=user.pk)
        expires_at = timezone.now() + EMAIL_CHANGE_LIFETIME
        submissions = []
        if login:
            PendingEmailChange.objects.update_or_create(
                user=locked, kind=Kind.LOGIN,
                defaults={"address": login, "expires_at": expires_at, "requested_by": requester},
            )
            VerificationToken.objects.filter(user=locked, purpose=Purpose.EMAIL_CHANGE).delete()
            submissions.append((MailJob.Kind.EMAIL_CHANGE_REQUEST, login))
        if secondary:
            PendingEmailChange.objects.update_or_create(
                user=locked,
                kind=Kind.SECONDARY,
                defaults={"address": secondary, "expires_at": expires_at, "requested_by": requester},
            )
            VerificationToken.objects.filter(user=locked, purpose=Purpose.SECONDARY_EMAIL).delete()
            submissions.append((MailJob.Kind.SECONDARY_EMAIL_REQUEST, secondary))
        if remove_secondary and locked.secondary_email:
            recipients = locked.notification_addresses()
            locked.set_secondary_email("")
            PendingEmailChange.objects.filter(user=locked, kind=Kind.SECONDARY).delete()
            VerificationToken.objects.filter(user=locked, purpose=Purpose.SECONDARY_EMAIL).delete()
            enqueue_for_account(MailJob.Kind.SECONDARY_EMAIL_CHANGED, locked, addresses=recipients)
            security_log.record(
                SecurityEvent.Event.SECONDARY_EMAIL_CHANGED, account=locked, actor=actor
            )
        if submissions:
            # An older notice must not stop or report a newer request.
            VerificationToken.objects.filter(user=locked, purpose=Purpose.EMAIL_CHANGE_STOP).delete()
            enqueue_for_account(MailJob.Kind.EMAIL_CHANGE_NOTICE, locked)
            for kind, address in submissions:
                if not submit_mail_request(request, kind, address, account=locked):
                    raise RequestRefused
            security_log.record(SecurityEvent.Event.EMAIL_CHANGE_REQUESTED, account=locked, actor=actor)
            requested = True
    return requested


def process_request(account_id, address):
    """Make the link for a requested login address in the worker (no raw token in the mail job); returns the send function or None."""
    address = normalize_email_address(address)
    if not address:
        return None
    with transaction.atomic():
        user = (
            User._base_manager.select_for_update()
            .filter(pk=account_id, status=User.Status.ACTIVE)
            .first()
        )
        if user is None or user.email == address:
            return None
        pending = PendingEmailChange.objects.filter(
            user=user, kind=Kind.LOGIN, address=address, expires_at__gt=timezone.now()
        ).first()
        if pending is None:
            # Cancelled, replaced or expired since the request: no link.
            return None
        existing = User._base_manager.filter(normalized_email=address).first()
        if existing is not None and existing.status != User.Status.UNVERIFIED:
            if not rate_limits.consume(
                rate_limits.SCOPE_EXISTING_ACCOUNT_NOTICE,
                address,
                rate_limits.EXISTING_ACCOUNT_NOTICE_LIMITS,
            ):
                return None
            return lambda: emails.send_email_change_existing_account(address)
        VerificationToken.objects.filter(user=user, purpose=Purpose.EMAIL_CHANGE).delete()
        lifetime = _remaining(pending)
        raw_token = VerificationToken.objects.issue(
            user, Purpose.EMAIL_CHANGE, lifetime, target_email=address
        )
        return lambda: emails.send_email_change_verification(address, raw_token, lifetime)


def process_secondary_request(account_id, address):
    """Make the link for a requested second address in the worker (D-059)."""
    address = normalize_email_address(address)
    if not address:
        return None
    with transaction.atomic():
        user = (
            User._base_manager.select_for_update()
            .filter(pk=account_id, status=User.Status.ACTIVE)
            .first()
        )
        if user is None or user.email == address:
            return None
        pending = PendingEmailChange.objects.filter(
            user=user, kind=Kind.SECONDARY, address=address, expires_at__gt=timezone.now()
        ).first()
        if pending is None:
            return None
        VerificationToken.objects.filter(user=user, purpose=Purpose.SECONDARY_EMAIL).delete()
        lifetime = _remaining(pending)
        raw_token = VerificationToken.objects.issue(
            user, Purpose.SECONDARY_EMAIL, lifetime, target_email=address
        )
        return lambda: emails.send_secondary_email_verification(address, raw_token, lifetime)


def process_notice(account_id, address, greeting_name=""):
    """Make the stop link for the notice about a requested change and send it to ``address``.

    Nothing is sent when no change is pending any more. One link stops every pending change; it is
    valid as long as the latest one.
    """
    with transaction.atomic():
        user = (
            User._base_manager.select_for_update()
            .filter(pk=account_id, status=User.Status.ACTIVE)
            .first()
        )
        if user is None or address not in user.notification_addresses():
            return None
        rows = list(
            PendingEmailChange.objects.filter(user=user, expires_at__gt=timezone.now())
        )
        if not rows:
            return None
        lifetime = max(_remaining(row) for row in rows)
        raw_token = VerificationToken.objects.issue(user, Purpose.EMAIL_CHANGE_STOP, lifetime)
        return lambda: emails.send_email_change_notice(address, raw_token, lifetime, greeting_name)


def confirm_change(token):
    """Apply a confirmed address and return the account, or None if the link is no longer valid (D-059).

    A login address ends every session; a cancelled or replaced request cannot be confirmed.
    """
    kind = KIND_OF_PURPOSE.get(token.purpose)
    if kind is None:
        return None
    from leg_platform.accounts.mail_queue import enqueue_for_account

    with transaction.atomic():
        user = (
            User._base_manager.select_for_update()
            .filter(pk=token.user_id, status=User.Status.ACTIVE)
            .first()
        )
        locked_token = (
            VerificationToken.objects.select_for_update()
            .filter(pk=token.pk, purpose=token.purpose, expires_at__gt=timezone.now())
            .first()
        )
        if user is None or locked_token is None or not locked_token.target_email:
            return None
        address = normalize_email_address(locked_token.target_email)
        pending = PendingEmailChange.objects.filter(
            user=user, kind=kind, address=address, expires_at__gt=timezone.now()
        ).first()
        if pending is None:
            locked_token.delete()
            return None
        recipients = user.notification_addresses()
        if kind == Kind.SECONDARY:
            try:
                user.set_secondary_email(address)
            except AccountIntegrityError:
                locked_token.delete()
                return None
            locked_token.delete()
            requested_by = pending.requested_by
            pending.delete()
            _drop_stop_tokens_if_idle(user)
            enqueue_for_account(MailJob.Kind.SECONDARY_EMAIL_CHANGED, user, addresses=recipients)
            # Staff-requested changes say so in the log (D-087).
            security_log.record(
                SecurityEvent.Event.SECONDARY_EMAIL_CHANGED, account=user,
                detail=f"requested by staff account {requested_by}" if requested_by else "",
            )
            return user
        duplicate = (
            User._base_manager.select_for_update()
            .filter(normalized_email=address)
            .exclude(pk=user.pk)
            .order_by("pk")
            .first()
        )
        try:
            # Removal of a pending registration and the change in one savepoint; the unique index
            # remains the final protection against concurrent address claims.
            with transaction.atomic():
                if duplicate is not None and duplicate.status != User.Status.UNVERIFIED:
                    locked_token.delete()
                    return None
                if duplicate is not None:
                    duplicate.delete()
                user.change_email(address)
                if user.secondary_email == address:
                    # The same address cannot be both: it is the login address now.
                    user.set_secondary_email("")
        except (IntegrityError, ValueError):
            # ValueError: refused by the account rules; checked at request time, so defensive.
            locked_token.delete()
            return None
        VerificationToken.objects.filter(user=user, purpose=Purpose.EMAIL_CHANGE).delete()
        pending.delete()
        # The stop links went to addresses no longer entitled to the account: all end, also while a second-address request is open.
        VerificationToken.objects.filter(user=user, purpose=Purpose.EMAIL_CHANGE_STOP).delete()
        user.end_all_sessions()
        # The old login address and the second address are told (D-059).
        enqueue_for_account(MailJob.Kind.EMAIL_CHANGED, user, addresses=recipients)
        security_log.record(SecurityEvent.Event.EMAIL_CHANGED, account=user)
    return user


def cancel(user, kind):
    """The person cancels a pending request (D-059). Returns whether there was one."""
    with transaction.atomic():
        locked = User._base_manager.select_for_update().get(pk=user.pk)
        deleted, _ = PendingEmailChange.objects.filter(user=locked, kind=kind).delete()
        VerificationToken.objects.filter(user=locked, purpose=PURPOSE_OF_KIND[kind]).delete()
        if deleted:
            _drop_stop_tokens_if_idle(locked)
            security_log.record(SecurityEvent.Event.EMAIL_CHANGE_CANCELLED, account=locked)
    return bool(deleted)


def stop_all(user):
    """The link of the notice stops every pending change of the account (D-059).

    Returns the account, or None when no request is open (nothing happens, no session ends). The
    confirmation and other stop links end, and so do the sessions: the requester was not the owner,
    or the owner wants to be sure.
    """
    with transaction.atomic():
        locked = User._base_manager.select_for_update().get(pk=user.pk)
        if not has_open_request(locked):
            VerificationToken.objects.filter(user=locked, purpose=Purpose.EMAIL_CHANGE_STOP).delete()
            return None
        PendingEmailChange.objects.filter(user=locked).delete()
        VerificationToken.objects.filter(
            user=locked,
            purpose__in=(*CONFIRMATION_PURPOSES, Purpose.EMAIL_CHANGE_STOP),
        ).delete()
        locked.end_all_sessions()
        security_log.record(SecurityEvent.Event.EMAIL_CHANGE_STOPPED, account=locked)
    return locked
