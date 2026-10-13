"""Password reset (docs/SECURITY.md, section 3; OWASP Forgot Password Cheat Sheet; D-037).

A reset email is sent only to an account that is active; an account that is temporarily locked
still has the status active, so it can reset its password during the lockout. Nothing is sent to
unknown, unverified, blocked or deactivated addresses, and the request makes no difference
visible to the person who typed the address.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import emails, security_log
from leg_platform.accounts.login_protection import axes_username
from leg_platform.accounts.models import MailJob, SecurityEvent, User, VerificationToken
from leg_platform.accounts.normalization import normalize_email_address

# The link stays valid as long as the longest window of the per-address limit (24 hours), so
# that a person who cannot request another email because someone else used up the budget can
# still use a link already in their mailbox (D-017). The link is single use. A proposal awaiting
# the project owner's confirmation (D-037).
PASSWORD_RESET_LIFETIME = timedelta(hours=24)


def process_reset_request(address):
    """Handle a reset request in the background worker; return the sending function or None."""
    address = normalize_email_address(address)
    # The normalised unique column, never Django's email__iexact lookup (docs/SECURITY.md,
    # section 3).
    user = User._base_manager.filter(
        normalized_email=address, status=User.Status.ACTIVE
    ).first()
    if user is None:
        return None
    raw_token = VerificationToken.objects.issue(
        user, VerificationToken.Purpose.PASSWORD_RESET, PASSWORD_RESET_LIFETIME
    )
    email = user.email
    return lambda: emails.send_password_reset(email, raw_token, PASSWORD_RESET_LIFETIME)


def process_password_changed(address):
    """Handle the notification of a changed password; return the sending function."""
    return lambda: emails.send_password_changed(address)


def delete_expired_reset_tokens(now=None):
    """Delete reset links (of the password and of the second factor) that expired."""
    count, _ = VerificationToken.objects.filter(
        purpose__in=(
            VerificationToken.Purpose.PASSWORD_RESET,
            VerificationToken.Purpose.TWO_FACTOR_RESET,
        ),
        expires_at__lte=now or timezone.now(),
    ).delete()
    return count


def complete_reset(token, form):
    """Save the new password and consume the token; return the account, or None if not valid.

    The account is locked before its token, as everywhere else. A reset ends all other sessions
    (Django derives the session hash from the password), removes every outstanding reset link
    of the account and the temporary lockout of the account only: the failure counters and
    lockouts of IP addresses stay as they are, so that an attacker cannot lift the lockout of
    their IP address by resetting the password of their own account (docs/SECURITY.md,
    section 3). The second factor is not touched.
    """
    with transaction.atomic():
        user = (
            User._base_manager.select_for_update()
            .filter(pk=token.user_id, status=User.Status.ACTIVE)
            .first()
        )
        locked_token = (
            VerificationToken.objects.select_for_update()
            .filter(pk=token.pk, expires_at__gt=timezone.now())
            .first()
        )
        if user is None or locked_token is None:
            return None
        form.user = user
        user.password_change_required = False
        form.save()
        VerificationToken.objects.filter(
            user=user, purpose=VerificationToken.Purpose.PASSWORD_RESET
        ).delete()
        _end_account_lockout(user)
        # The pause of code entry ends, but the budget is not reset completely (D-040).
        from leg_platform.accounts import totp

        totp.relieve_code_budget(user)
        security_log.record(SecurityEvent.Event.PASSWORD_RESET, account=user)
    return user


def _end_account_lockout(user):
    from axes.utils import reset

    digest = axes_username(None, {"username": user.email})
    if digest is None:
        # reset() without a username would delete every failed attempt of every account.
        return
    # Axes stores the keyed digest of the address, no IP address, in its attempt rows, so this
    # only removes the account's rows; the independent IP counter is a different table.
    reset(username=digest)


def notify_password_changed(user):
    """Queue the notification that the password was changed (no link, docs/SECURITY.md)."""
    from leg_platform.accounts.mail_queue import enqueue

    enqueue(MailJob.Kind.PASSWORD_CHANGED, user.email)
