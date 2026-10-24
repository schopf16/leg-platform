"""The sign-in method of a registration (D-058, D-106): the choice after the link, the setup before the account is active.

Order of the registration: 1 the address and its link, 2 the sign-in method (the choice, then its setup), 3 name,
address and consent, which activates the account (``User.complete_registration``). The account is unverified until
step 3 and cannot sign in. What step 2 prepares is therefore only staged, bound to the link in the session:

* password: the hash in the session (``views.REGISTRATION_PASSWORD_SESSION_KEY``);
* password and app: that, plus an authenticator device of the unverified account that the person confirmed with a
  code; its recovery codes are made and shown in step 3;
* passkey: inactive keys of the unverified account, bound to the session (``passkeys.binding``); step 3 switches them
  on in the same transaction that activates the account.

A new choice drops what an earlier choice staged. Nothing here sends an email, ends a session or writes the security
log: the account does not exist yet for any other purpose.
"""

from datetime import timedelta

from django.db import transaction

from leg_platform.accounts import login_methods, passkeys, rate_limits, totp
from leg_platform.accounts.login_protection import axes_client_ip
from leg_platform.accounts.models import AuthenticatorDevice, User

SESSION_KEY = "registration_method"
APP_DEVICE_KEY = "registration_app_device"

PASSWORD_METHODS = (login_methods.PASSWORD_ONLY, login_methods.PASSWORD_AND_APP)
METHODS = (*PASSWORD_METHODS, login_methods.PASSKEY)


def chosen(request, token):
    """The method chosen for this link ("password", "app" or "passkey"), or None."""
    held = request.session.get(SESSION_KEY)
    if isinstance(held, dict) and held.get("digest") == token.digest and held.get("method") in METHODS:
        return held["method"]
    return None


def choose(request, token, method):
    """Remember the choice and drop everything an earlier choice staged. Returns whether the choice changed."""
    changed = chosen(request, token) != method
    request.session[SESSION_KEY] = {"digest": token.digest, "method": method}
    if changed:
        discard_staged(request, token.user)
    return changed


def forget(request):
    request.session.pop(SESSION_KEY, None)
    request.session.pop(APP_DEVICE_KEY, None)


def discard_staged(request, user):
    """Delete the staged app device and passkeys of the unverified account."""
    request.session.pop(APP_DEVICE_KEY, None)
    with transaction.atomic():
        locked = User._base_manager.select_for_update().filter(pk=user.pk, status=User.Status.UNVERIFIED).first()
        if locked is not None:
            locked.passkeys.filter(active=False).delete()
            AuthenticatorDevice.objects.filter(user=locked).delete()


def limited(request):
    """The request budget of the setup pages (per client address, like the other setup endpoints)."""
    return not rate_limits.consume(
        "registration-method", rate_limits.ip_key(axes_client_ip(request)),
        ((30, timedelta(minutes=5)), (200, timedelta(hours=1))),
    )


# Password and app

def start_app(request, user):
    """The unconfirmed device the person scans; an earlier one is replaced. None if the account is not unverified."""
    with transaction.atomic():
        locked = User._base_manager.select_for_update().filter(pk=user.pk, status=User.Status.UNVERIFIED).first()
        if locked is None:
            return None
        AuthenticatorDevice.objects.filter(user=locked).delete()
        device = AuthenticatorDevice.objects.create_device(locked, confirmed=False)
    request.session[APP_DEVICE_KEY] = device.pk
    return device


def unconfirmed_device(request, user):
    return totp.unconfirmed_totp_device(user, request.session.get(APP_DEVICE_KEY))


def confirm_app(request, user, code):
    """Confirm the staged device with a code from the app. No email, no log entry: the account is not active yet."""
    with transaction.atomic():
        locked = User._base_manager.select_for_update().filter(pk=user.pk, status=User.Status.UNVERIFIED).first()
        if locked is None:
            return False
        device = totp.unconfirmed_totp_device(locked, request.session.get(APP_DEVICE_KEY))
        if device is None:
            return False
        device = AuthenticatorDevice.objects.select_for_update().get(pk=device.pk)
        if not device.verify_token(totp.normalize_code(code)):
            return False
        device.confirmed = True
        device.save()
    request.session.pop(APP_DEVICE_KEY, None)
    return True


def app_confirmed(user):
    return AuthenticatorDevice.objects.filter(user=user, confirmed=True).exists()


# Passkey

def passkey_staged(request, user):
    """Whether this session created a key for the account: exactly what step 3 would switch on."""
    return user.passkeys.filter(active=False, setup_binding=passkeys.binding(request)).exists()


def setup_done(request, token, password_held):
    """Whether the setup of the chosen method is complete, so that step 3 may run. ``password_held``: the hash of step 2."""
    method = chosen(request, token)
    if method == login_methods.PASSKEY:
        return passkey_staged(request, token.user)
    if method == login_methods.PASSWORD_ONLY:
        return password_held
    if method == login_methods.PASSWORD_AND_APP:
        return password_held and app_confirmed(token.user)
    return False
