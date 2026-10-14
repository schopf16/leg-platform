"""Verified email address changes (docs/PROJECT.md, "Changing the Email Address"; D-016)."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from leg_platform.accounts import emails, rate_limits, security_log
from leg_platform.accounts.models import (
    MailJob,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.normalization import normalize_email_address

EMAIL_CHANGE_LIFETIME = timedelta(hours=24)


def process_request(account_id, address):
    """Create a verification link in the worker; no raw token is stored in the mail job."""
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
        existing = User._base_manager.filter(normalized_email=address).first()
        if existing is not None and existing.status != User.Status.UNVERIFIED:
            if not rate_limits.consume(
                rate_limits.SCOPE_EXISTING_ACCOUNT_NOTICE,
                address,
                rate_limits.EXISTING_ACCOUNT_NOTICE_LIMITS,
            ):
                return None
            return lambda: emails.send_email_change_existing_account(address)
        VerificationToken.objects.filter(
            user=user, purpose=VerificationToken.Purpose.EMAIL_CHANGE
        ).delete()
        raw_token = VerificationToken.objects.issue(
            user,
            VerificationToken.Purpose.EMAIL_CHANGE,
            EMAIL_CHANGE_LIFETIME,
            target_email=address,
        )
        return lambda: emails.send_email_change_verification(
            address, raw_token, EMAIL_CHANGE_LIFETIME
        )


def confirm_change(token):
    """Apply a verified address change and return the account, or None if no longer valid."""
    with transaction.atomic():
        user = (
            User._base_manager.select_for_update()
            .filter(pk=token.user_id, status=User.Status.ACTIVE)
            .first()
        )
        locked_token = (
            VerificationToken.objects.select_for_update()
            .filter(
                pk=token.pk,
                purpose=VerificationToken.Purpose.EMAIL_CHANGE,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if user is None or locked_token is None or not locked_token.target_email:
            return None

        address = normalize_email_address(locked_token.target_email)
        duplicate = (
            User._base_manager.select_for_update()
            .filter(normalized_email=address)
            .exclude(pk=user.pk)
            .order_by("pk")
            .first()
        )
        old_address = user.email
        try:
            # Include removal of a pending registration and the change in one savepoint. The
            # unique index remains the final protection against concurrent address claims.
            with transaction.atomic():
                if duplicate is not None and duplicate.status != User.Status.UNVERIFIED:
                    locked_token.delete()
                    return None
                if duplicate is not None:
                    duplicate.delete()
                user.change_email(address)
        except IntegrityError:
            locked_token.delete()
            return None

        VerificationToken.objects.filter(
            user=user, purpose=VerificationToken.Purpose.EMAIL_CHANGE
        ).delete()
        user.end_all_sessions()
        from leg_platform.accounts.mail_queue import enqueue

        enqueue(MailJob.Kind.EMAIL_CHANGED, old_address)
        security_log.record(SecurityEvent.Event.EMAIL_CHANGED, account=user)
    return user


def notify_requested(user):
    """Record a request without exposing its target address in logs."""
    security_log.record(SecurityEvent.Event.EMAIL_CHANGE_REQUESTED, account=user)
