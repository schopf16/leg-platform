"""Password reset (OWASP Forgot Password Cheat Sheet; D-037).

A reset email goes only to an active account (a temporarily locked one is still active); nothing is
sent to unknown, unverified, blocked or deactivated addresses, and the request makes no difference
visible to the person who typed the address.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import emails, security_log
from leg_platform.accounts.login_protection import axes_username
from leg_platform.accounts.models import MailJob, SecurityEvent, User, VerificationToken
from leg_platform.accounts.normalization import normalize_email_address

# Valid as long as the longest per-address limit window, so a person who cannot request another
# email (budget used up by someone else) can still use a link already in the mailbox (D-017). Single use (D-037).
PASSWORD_RESET_LIFETIME = timedelta(hours=24)


def process_reset_request(address):
    """Handle a reset request in the background worker; return the sending function or None."""
    address = normalize_email_address(address)
    # The normalised unique column, never ``email__iexact``. The row is locked and the status
    # checked under the lock: parallel requests replace the links one after another.
    with transaction.atomic():
        user = (
            User._base_manager.select_for_update()
            .filter(normalized_email=address, status=User.Status.ACTIVE)
            .first()
        )
        if user is None:
            return None
        # Only the newest link is valid. A request above the per-address limit never reaches here,
        # so links already sent stay valid then.
        VerificationToken.objects.filter(
            user=user, purpose=VerificationToken.Purpose.PASSWORD_RESET
        ).delete()
        raw_token = VerificationToken.objects.issue(
            user, VerificationToken.Purpose.PASSWORD_RESET, PASSWORD_RESET_LIFETIME
        )
        email = user.email
    return lambda: emails.send_password_reset(email, raw_token, PASSWORD_RESET_LIFETIME)


def process_password_changed(address, greeting_name=""):
    """Handle the notification of a changed password; return the sending function."""
    return lambda: emails.send_password_changed(address, greeting_name)


def delete_expired_reset_tokens(now=None):
    """Delete expired reset, second factor, email change and stop links and expired requests for new addresses.

    The links and requests hold the proposed address (``target_email``), which must not stay after
    expiry (D-016). Stop links of an account without an open request go too.
    """
    from leg_platform.accounts.models import PendingEmailChange

    now = now or timezone.now()
    count, _ = VerificationToken.objects.filter(
        purpose__in=(
            VerificationToken.Purpose.PASSWORD_RESET,
            VerificationToken.Purpose.TWO_FACTOR_RESET,
            VerificationToken.Purpose.EMAIL_CHANGE,
            VerificationToken.Purpose.SECONDARY_EMAIL,
            VerificationToken.Purpose.EMAIL_CHANGE_STOP,
        ),
        expires_at__lte=now,
    ).delete()
    expired, _ = PendingEmailChange.objects.filter(expires_at__lte=now).delete()
    open_accounts = PendingEmailChange.objects.values("user_id")
    idle_stops, _ = VerificationToken.objects.filter(
        purpose=VerificationToken.Purpose.EMAIL_CHANGE_STOP
    ).exclude(user_id__in=open_accounts).delete()
    return count + expired + idle_stops


def complete_reset(token, form):
    """Save the new password and consume the token; return the account, or None if not valid.

    Only a password reset link works; the account is locked before its token. A reset ends all other
    sessions, removes every outstanding reset link and the temporary lockout of the account only
    (IP failure counters and lockouts stay, so an attacker cannot lift their IP lockout by resetting
    their own account). The second factor is not touched.
    """
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
                purpose=VerificationToken.Purpose.PASSWORD_RESET,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if user is None or locked_token is None:
            return None
        form.user = user
        user.password_change_required = False
        form.save()
        # Requests for a new address made before the reset must not be confirmed afterwards.
        user.discard_email_change_requests()
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
    # Axes stores the keyed digest of the address (no IP) in its attempt rows; the independent IP counter is another table.
    reset(username=digest)


def notify_password_changed(user):
    """Queue the notification that the password was changed (no link, docs/SECURITY.md)."""
    from leg_platform.accounts.mail_queue import enqueue_for_account

    enqueue_for_account(MailJob.Kind.PASSWORD_CHANGED, user)
