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
import secrets
from base64 import b32encode
from datetime import timedelta

import segno
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from django_otp import devices_for_user

from leg_platform.accounts import security_log
from leg_platform.accounts.encryption import DecryptionError
from leg_platform.accounts.mail_queue import enqueue_for_account
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    MailJob,
    RecoveryCode,
    RecoveryCodeDevice,
    SecurityEvent,
    User,
)

Event = SecurityEvent.Event

RECOVERY_DEVICE_NAME = "recovery codes"
# Ten single-use codes of 40 bits each; every attempt is counted by the abuse protection.
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 8
# An unconfirmed secret is usable only shortly and only in the session that started the setup.
ENROLMENT_LIFETIME = timedelta(minutes=15)
# Budget of wrong codes per account (D-040): a window of 24 hours from the first wrong code. At
# the notice the owner gets an email, at the pause code entry stops until the window ends or the
# password is reset, which leaves the budget at the relief value. NIST SP 800-63B-4, section
# 3.2.2 allows at most 100 consecutive failures per authenticator; this is much stricter, and
# the Axes lockout (5 failures, 15 minutes) applies in addition.
CODE_BUDGET_WINDOW = timedelta(hours=24)
CODE_NOTICE_AT = 10
CODE_PAUSE_AT = 15
CODE_RELIEF_TO = 10


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


def _current_failures(device, now):
    since = device.failed_codes_since
    if since is None or now - since >= CODE_BUDGET_WINDOW:
        return 0
    return device.failed_codes


def code_attempts_paused(user):
    """Whether the budget of wrong codes of the account is used up (D-040)."""
    device = confirmed_totp_device(user)
    return device is not None and _current_failures(device, timezone.now()) >= CODE_PAUSE_AT


def record_failed_code(user):
    """Count a wrong code against the budget; call it under the lock on the account row.

    The owner is notified once per window, without any link, when the notice is reached.
    """
    device = AuthenticatorDevice.objects.select_for_update().filter(user=user, confirmed=True).first()
    if device is None:
        return
    now = timezone.now()
    count = _current_failures(device, now)
    if count == 0:
        device.failed_codes_since = now
        device.code_budget_relieved = False
    device.failed_codes = count + 1
    device.save(
        update_fields=["failed_codes", "failed_codes_since", "code_budget_relieved"]
    )
    if device.failed_codes == CODE_NOTICE_AT:
        enqueue_for_account(MailJob.Kind.CODE_ATTEMPTS_WARNING, user)
        security_log.record(Event.CODE_ATTEMPTS_WARNING, account=user)
    elif device.failed_codes == CODE_PAUSE_AT:
        security_log.record(Event.CODE_ATTEMPTS_PAUSED, account=user)


def relieve_code_budget(user):
    """After a password reset the budget is at most the relief value, once per window.

    Never more attempts than before, and a second reset in the same window gives none: a person
    who controls the mailbox gets no more than that.
    """
    AuthenticatorDevice.objects.filter(
        user=user, failed_codes__gt=CODE_RELIEF_TO, code_budget_relieved=False
    ).update(failed_codes=CODE_RELIEF_TO, code_budget_relieved=True)


def delete_abandoned_enrolments(now=None):
    """Delete secrets whose setup was never confirmed; return their number."""
    count, _ = AuthenticatorDevice.objects.filter(
        confirmed=False, created_at__lt=(now or timezone.now()) - ENROLMENT_LIFETIME
    ).delete()
    return count


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
        # Other sessions end; the view keeps the session that made the change (D-044).
        user.end_all_sessions()
        enqueue_for_account(MailJob.Kind.TWO_FACTOR_ENABLED, user)
        security_log.record(Event.TWO_FACTOR_ENABLED, account=user)
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
        # Other sessions end; the view keeps the session that made the change (D-044).
        user.end_all_sessions()
        enqueue_for_account(MailJob.Kind.RECOVERY_CODES_RENEWED, user)
        security_log.record(Event.RECOVERY_CODES_RENEWED, account=user)
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
        # Other sessions end; the view keeps the session that made the change (D-044).
        user.end_all_sessions()
        enqueue_for_account(MailJob.Kind.TWO_FACTOR_DISABLED, user)
        security_log.record(Event.TWO_FACTOR_DISABLED, account=user)


def verify_code(user, raw_code):
    """Verify a code of the authenticator app or a recovery code; return the device or None.

    The kind of code follows from its length: six digits for the app, eight characters for a
    recovery code.

    Precondition: the caller holds the row lock of the account, as
    ``second_factor.verify_second_factor()`` does. ``AuthenticatorDevice.verify_token()`` reads
    ``last_t`` and saves it without a lock of its own, so only the account lock keeps a code of
    the app from being accepted twice in parallel. A recovery code is single use by one DELETE
    statement and does not depend on the lock. Repeated failures are limited by the abuse
    protection of the login (``second_factor.verify_second_factor``), not by the device (D-040).
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
                    enqueue_for_account(MailJob.Kind.RECOVERY_CODE_USED, user)
                    security_log.record(Event.RECOVERY_CODE_USED, account=user)
                break
        else:
            return None
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
