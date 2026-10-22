"""Atomic passkey method activation; current proof is required before opening setup."""

import time

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from leg_platform.accounts import passkeys, security_log
from leg_platform.accounts.mail_queue import enqueue_for_account
from leg_platform.accounts.models import (
    AuthenticatorDevice, MailJob, PasskeyIdentity, PendingSignInChange, RecoveryCodeDevice,
    SecurityEvent, User, VerificationToken,
)

SETUP_KEY = "passkey_setup"


def allow_setup(request):
    """Called only after a successful current proof, never by the method-choice router."""
    request.session[SETUP_KEY] = {
        "user": request.user.pk, "hash": request.user.get_session_auth_hash(),
        "started": time.time(),
    }


def setup_allowed(request, user=None):
    user = user or request.user
    setup = request.session.get(SETUP_KEY, {})
    return (
        setup.get("user") == user.pk
        and isinstance(setup.get("started"), (int, float))
        and 0 <= time.time() - setup["started"] < passkeys.LIFETIME.total_seconds()
        and constant_time_compare(setup.get("hash", ""), user.get_session_auth_hash())
    )


def activate(request, *, two_devices=False):
    """Keep current session only; delete password, app, codes, old links and pending changes."""
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if not user.is_active or not setup_allowed(request, user):
            return None
        user.passkeys.filter(
            active=False, created_at__lte=timezone.now() - passkeys.LIFETIME
        ).delete()
        keys = user.passkeys.filter(setup_binding=passkeys.binding(request))
        existing = user.passkeys.filter(active=True)
        privileged = user.is_staff or user.is_superuser
        if not keys.exists():
            return None
        count = existing.count() + keys.filter(active=False).count()
        identity = PasskeyIdentity.objects.get(user=user)
        if privileged and (not (two_devices or identity.two_devices_confirmed) or count < 2):
            return None
        keys.update(active=True, setup_binding="")
        if privileged and two_devices:
            identity.two_devices_confirmed = True
            identity.save(update_fields=["two_devices_confirmed"])
        changed_method = user.authentication_method != User.AuthenticationMethod.PASSKEY
        if changed_method:
            AuthenticatorDevice.objects.filter(user=user).delete()
            RecoveryCodeDevice.objects.filter(user=user).delete()
            user.set_unusable_password()
            user.password_change_required = False
            user._change(authentication_method=User.AuthenticationMethod.PASSKEY, password=user.password)
            user.save(update_fields=["password_change_required"])
        user.discard_email_change_requests()
        PendingSignInChange.objects.filter(user=user).delete()
        VerificationToken.objects.filter(user=user).delete()
        user.end_all_sessions()
        enqueue_for_account(MailJob.Kind.SIGN_IN_CHANGED, user)
        security_log.record(
            SecurityEvent.Event.SIGN_IN_METHOD_CHANGED if changed_method else SecurityEvent.Event.PASSKEYS_CHANGED,
            account=user, long_retention=privileged,
        )
        request.session.pop(SETUP_KEY, None)
        return user


def remove(user, credential_id):
    """Current proof checked by caller; lock before enforcing the permanent minimum."""
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        if not user.is_active or user.authentication_method != User.AuthenticationMethod.PASSKEY:
            return False
        minimum = 2 if user.is_staff or user.is_superuser else 1
        try:
            key = user.passkeys.filter(pk=credential_id, active=True).first()
        except (TypeError, ValueError):
            return False
        if key is None or user.passkeys.filter(active=True).count() <= minimum:
            return False
        key.delete()
        user.end_all_sessions()
        user.discard_email_change_requests()
        PendingSignInChange.objects.filter(user=user).delete()
        VerificationToken.objects.filter(user=user).delete()
        enqueue_for_account(MailJob.Kind.SIGN_IN_CHANGED, user)
        security_log.record(
            SecurityEvent.Event.PASSKEYS_CHANGED, account=user, long_retention=minimum == 2,
        )
        return True
