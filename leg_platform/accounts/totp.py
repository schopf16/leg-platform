"""TOTP and recovery codes: enrolment, verification, renewal and removal (D-040).

Only the parts that are specific to the authenticator app are here. Whether a session completed
the second factor is decided in ``second_factor.py`` and does not depend on this module, so that
the method can be replaced or supplemented later (docs/PROJECT.md, "AGOV and Passkeys").

The devices are ``AuthenticatorDevice`` for the authenticator app and ``RecoveryCodeDevice``
with ``RecoveryCode`` for the recovery codes (models of this project that follow the interface
of django-otp; the secrets are stored encrypted or as keyed digests). An account has at most one
of each; the account row is locked while they change, so that two requests cannot create two.
"""

import io
import logging
import secrets
from base64 import b32encode
from datetime import timedelta

import segno
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from django_otp import devices_for_user

from leg_platform.accounts.encryption import DecryptionError
from leg_platform.accounts.mail_queue import enqueue
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    MailJob,
    RecoveryCode,
    RecoveryCodeDevice,
    User,
)

security_logger = logging.getLogger("leg_platform.security")

RECOVERY_DEVICE_NAME = "recovery codes"
# Ten single-use codes of 40 bits each; every attempt is counted by the abuse protection.
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 8
# An unconfirmed secret is usable only shortly and only in the session that started the setup.
ENROLMENT_LIFETIME = timedelta(minutes=15)


class SecondFactorError(Exception):
    """A change of the second factor that the rules do not allow."""


def _lock_account(user):
    """Lock the account row, so that changes of its devices are serialised."""
    return User._base_manager.select_for_update().get(pk=user.pk)


def normalize_code(raw):
    """Remove the spaces and hyphens that people and displays put into codes; lower case."""
    return "".join(str(raw).split()).replace("-", "").lower()


def confirmed_totp_device(user):
    return AuthenticatorDevice.objects.filter(user=user, confirmed=True).first()


def has_authenticator(user):
    """Whether the account has a confirmed authenticator app; recovery codes alone are no factor."""
    return AuthenticatorDevice.objects.filter(user=user, confirmed=True).exists()


def unconfirmed_totp_device(user, device_id):
    """The secret that this session started to set up, if it is still young enough."""
    if device_id is None:
        return None
    return AuthenticatorDevice.objects.filter(
        pk=device_id,
        user=user,
        confirmed=False,
        created_at__gte=timezone.now() - ENROLMENT_LIFETIME,
    ).first()


def remaining_recovery_codes(user):
    return RecoveryCode.objects.filter(device__user=user, device__confirmed=True).count()


def is_recovery_code_device(device):
    return isinstance(device, RecoveryCodeDevice)


def start_enrolment(user):
    """Create the authenticator app secret that the person scans; not active until confirmed.

    An earlier, unconfirmed secret is replaced. Refused if the account already has a second
    factor: it is removed first, which requires re-authentication again.
    """
    with transaction.atomic():
        _lock_account(user)
        if has_authenticator(user):
            raise SecondFactorError("The account already has a second factor.")
        AuthenticatorDevice.objects.filter(user=user, confirmed=False).delete()
        return AuthenticatorDevice.objects.create_device(user, confirmed=False)


def confirm_enrolment(user, device_id, code):
    """Confirm the secret with a code from the app; return (device, recovery codes) or None.

    The account is notified, without any link.
    """
    with transaction.atomic():
        _lock_account(user)
        device = unconfirmed_totp_device(user, device_id)
        if device is None:
            return None
        device = AuthenticatorDevice.objects.select_for_update().get(pk=device.pk)
        if not device.verify_token(normalize_code(code)):
            return None
        device.confirmed = True
        device.save()
        codes = _replace_recovery_codes(user)
        enqueue(MailJob.Kind.TWO_FACTOR_ENABLED, user.email)
    security_logger.warning("Two-factor authentication was set up (account %d).", user.pk)
    return device, codes


def _new_recovery_code():
    return b32encode(secrets.token_bytes(5)).decode("ascii").lower()


def _replace_recovery_codes(user):
    # The device row stays, so that a session that a recovery code verified stays verified.
    device, _created = RecoveryCodeDevice.objects.get_or_create(
        user=user, defaults={"name": RECOVERY_DEVICE_NAME, "confirmed": True}
    )
    device.codes.all().delete()
    codes = set()
    while len(codes) < RECOVERY_CODE_COUNT:
        codes.add(_new_recovery_code())
    device.add_codes(codes)
    return sorted(codes)


def renew_recovery_codes(user):
    """Replace the recovery codes by new ones; the previous codes become invalid."""
    with transaction.atomic():
        _lock_account(user)
        if confirmed_totp_device(user) is None:
            raise SecondFactorError("The account has no second factor.")
        codes = _replace_recovery_codes(user)
        enqueue(MailJob.Kind.RECOVERY_CODES_RENEWED, user.email)
    security_logger.warning("The recovery codes were renewed (account %d).", user.pk)
    return codes


def disable(user):
    """Remove the second factor of the account; staff users and administrators keep theirs.

    The reset of the second factor of another account is a function of the system
    administrators, with its own rules (docs/SECURITY.md, section 3).
    """
    from leg_platform.accounts.second_factor import requires_second_factor

    with transaction.atomic():
        locked = _lock_account(user)
        if requires_second_factor(locked):
            raise SecondFactorError("Staff users and system administrators need a second factor.")
        for device in list(devices_for_user(user, confirmed=None)):
            device.delete()
        enqueue(MailJob.Kind.TWO_FACTOR_DISABLED, user.email)
    security_logger.warning("Two-factor authentication was removed (account %d).", user.pk)


def verify_code(user, raw_code):
    """Verify a code of the authenticator app or a recovery code; return the device or None.

    The kind of code follows from its length: six digits for the app, eight characters for a
    recovery code. The device is locked while the code is checked, so that a code cannot be
    used twice in parallel. Repeated failures are limited by the abuse protection of the login
    (``second_factor.verify_second_factor``), not by the device (D-040).
    """
    code = normalize_code(raw_code)
    with transaction.atomic():
        # Recovery codes replace the app; without an app they are no factor.
        if not has_authenticator(user):
            return None
        for device in devices_for_user(user, confirmed=True, for_verify=True):
            if isinstance(device, AuthenticatorDevice):
                fits = code.isascii() and code.isdigit() and len(code) == device.DIGITS
            else:
                fits = len(code) == RECOVERY_CODE_LENGTH
            if fits and device.verify_token(code):
                if is_recovery_code_device(device):
                    # In the same transaction as the use of the code.
                    enqueue(MailJob.Kind.RECOVERY_CODE_USED, user.email)
                break
        else:
            return None
    if is_recovery_code_device(device):
        security_logger.warning("A recovery code was used (account %d).", user.pk)
    return device


@sensitive_variables()
def secret_for_display(device):
    """The secret in groups of four characters, for entering it by hand."""
    try:
        text = b32encode(device.bin_key).decode()
    except DecryptionError:
        return ""
    return " ".join(text[index : index + 4] for index in range(0, len(text), 4))


def format_recovery_code(code):
    return f"{code[:4]}-{code[4:]}"


@sensitive_variables()
def qr_code_svg(device):
    """The QR code of the secret as an SVG image (served as its own image, see the CSP)."""
    buffer = io.BytesIO()
    segno.make(device.config_url, error="m").save(
        buffer, kind="svg", scale=6, border=2, xmldecl=False, svgns=True, nl=False
    )
    return buffer.getvalue()
