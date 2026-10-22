"""D-106 password return: staged hash/app, then one atomic email-confirmed transition."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from leg_platform.accounts import passkeys, security_log, totp
from leg_platform.accounts.models import (
    AuthenticatorDevice, MailJob, PendingSignInChange, RecoveryCodeDevice,
    SecurityEvent, User, VerificationToken,
)

LIFETIME = timedelta(hours=24)


def valid(pending, user):
    return bool(
        pending and user.is_active and pending.expires_at > timezone.now()
        and constant_time_compare(pending.auth_hash, user.get_session_auth_hash())
        and user.authentication_method in (User.AuthenticationMethod.PASSKEY, User.AuthenticationMethod.RECOVERY)
        and pending.method in (("app",) if user.is_staff or user.is_superuser else ("password", "app"))
    )


def start(request, password_hash, method):
    """Caller has checked the current proof (or the restricted recovery setup grant)."""
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if method not in (("app",) if user.is_staff or user.is_superuser else ("password", "app")):
            return None
        if user.authentication_method not in (User.AuthenticationMethod.PASSKEY, User.AuthenticationMethod.RECOVERY):
            return None
        VerificationToken.objects.filter(user=user, purpose=VerificationToken.Purpose.METHOD_RETURN).delete()
        AuthenticatorDevice.objects.filter(user=user, confirmed=False).delete()
        pending, _ = PendingSignInChange.objects.update_or_create(user=user, defaults={
            "password_hash": password_hash, "method": method, "binding": passkeys.binding(request),
            "auth_hash": user.get_session_auth_hash(), "expires_at": timezone.now() + LIFETIME,
            "app_confirmed": False, "ready": method == "password",
        })
        if method == "app":
            device = AuthenticatorDevice.objects.create_device(user, confirmed=False)
            request.session["method_app_device"] = device.pk
        return pending


def session_pending(request):
    pending = PendingSignInChange.objects.filter(user=request.user).first()
    if not valid(pending, request.user) or not constant_time_compare(
        pending.binding, passkeys.binding(request)
    ):
        return None
    return pending


def confirm_app(request, code):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        pending = PendingSignInChange.objects.select_for_update().filter(user=user).first()
        if not valid(pending, user) or pending.method != "app" or not constant_time_compare(
            pending.binding, passkeys.binding(request)
        ):
            return False
        device = totp.unconfirmed_totp_device(user, request.session.get("method_app_device"))
        if device is None:
            return False
        device = AuthenticatorDevice.objects.select_for_update().get(pk=device.pk)
        if not device.verify_token(totp.normalize_code(code)):
            return False
        pending.app_confirmed = pending.ready = True
        pending.save(update_fields=["app_confirmed", "ready"])
        return True


def process_return_link(account_id, address, greeting_name=""):
    """Outbox creates tokens only at delivery time; a replaced request invalidates earlier links."""
    from leg_platform.accounts import emails
    from leg_platform.accounts.normalization import normalize_email_address

    # Serialize with start/complete: replacement must either precede this fresh read or
    # follow issuance and delete its token. Never issue from an unlocked pending snapshot.
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=account_id).first()
        if user is None:
            return None
        pending = PendingSignInChange.objects.select_for_update().filter(user=user).first()
        if not valid(pending, user) or not pending.ready or normalize_email_address(address) not in {
            normalize_email_address(item) for item in user.notification_addresses()
        }:
            return None
        token = VerificationToken.objects.issue(
            user, VerificationToken.Purpose.METHOD_RETURN,
            min(LIFETIME, pending.expires_at - timezone.now()),
        )
    return lambda: emails.send_method_return(address, token, greeting_name)


def complete(user_id, *, token_id=None, recovery=False):
    """Return (account, app device, recovery codes), or None. No plaintext password exists here."""
    from leg_platform.accounts.mail_queue import enqueue_for_account

    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_id).first()
        pending = PendingSignInChange.objects.select_for_update().filter(user=user).first()
        if user is None or not valid(pending, user) or not pending.ready:
            return None
        if recovery:
            if user.authentication_method != User.AuthenticationMethod.RECOVERY:
                return None
        elif not VerificationToken.objects.select_for_update().filter(
            pk=token_id, user=user, purpose=VerificationToken.Purpose.METHOD_RETURN,
            expires_at__gt=timezone.now(),
        ).exists():
            return None
        device = None
        codes = []
        if pending.method == "app":
            device = AuthenticatorDevice.objects.select_for_update().filter(user=user, confirmed=False).first()
            if not pending.app_confirmed or device is None:
                return None
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            codes = totp._replace_recovery_codes(user)
        else:
            AuthenticatorDevice.objects.filter(user=user).delete()
            RecoveryCodeDevice.objects.filter(user=user).delete()
        user.passkeys.all().delete()
        user.password = pending.password_hash
        user.password_change_required = False
        user._change(authentication_method=User.AuthenticationMethod.PASSWORD, password=user.password)
        user.save(update_fields=["password_change_required"])
        user.discard_email_change_requests()
        VerificationToken.objects.filter(user=user).delete()
        pending.delete()
        user.end_all_sessions()
        enqueue_for_account(MailJob.Kind.SIGN_IN_CHANGED, user)
        security_log.record(
            SecurityEvent.Event.SIGN_IN_METHOD_CHANGED, account=user,
            long_retention=user.is_staff or user.is_superuser,
        )
        return user, device, codes
