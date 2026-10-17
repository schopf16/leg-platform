"""Second factor devices (D-040).

The devices follow the interface of django-otp (``Device``), but the secrets are not stored in
plain text: the TOTP key is encrypted with a key derived from SECRET_KEY and a recovery code is
stored only as a keyed digest.
"""

import logging
import re
import secrets
import time
from base64 import b32encode
from urllib.parse import quote, urlencode

from django.db import models
from django.views.decorators.debug import sensitive_variables
from django_otp.models import Device, DeviceManager, TimestampMixin
from django_otp.oath import TOTP

from leg_platform.accounts.digests import keyed_digest, keyed_digests_with_fallbacks
from leg_platform.accounts.encryption import (
    DecryptionError,
    decrypt,
    encrypt,
    is_current,
    reencrypt,
)
from leg_platform.accounts.models.common import EXACT_COLLATION

logger = logging.getLogger(__name__)

AUTHENTICATOR_SECRET_PURPOSE = b"LEG-Platform authenticator secret v1"
RECOVERY_CODE_PURPOSE = b"LEG-Platform recovery code v1"
AUTHENTICATOR_KEY_BYTES = 20
AUTHENTICATOR_CODE = re.compile(r"[0-9]{6}")


def recovery_code_digest(code):
    return keyed_digest(code, RECOVERY_CODE_PURPOSE)


class AuthenticatorDeviceManager(DeviceManager):
    def create_device(self, user, *, confirmed):
        """Create a device with a new random key (160 bits, RFC 4226, section 4)."""
        return self.create(
            user=user,
            name="authenticator app",
            confirmed=confirmed,
            encrypted_key=encrypt(secrets.token_bytes(AUTHENTICATOR_KEY_BYTES), AUTHENTICATOR_SECRET_PURPOSE),
        )


class AuthenticatorDevice(TimestampMixin, Device):
    """An authenticator app (TOTP, RFC 6238): 30 seconds, six digits, SHA-1.

    One step of tolerance in each direction; a code at or below the step of the last accepted
    one is refused, so that a code works once. Failures are limited by the abuse protection of
    the login (D-017), not by the device.
    """

    STEP = 30
    DIGITS = 6
    TOLERANCE = 1

    encrypted_key = models.CharField(max_length=255, editable=False)
    drift = models.SmallIntegerField(default=0, editable=False)
    last_t = models.BigIntegerField(default=-1, editable=False)
    # Wrong codes in a window of 24 hours that starts with the first one (D-040). Not cleared by
    # a successful login, so the budget of an attacker does not grow with the owner's logins.
    failed_codes = models.PositiveSmallIntegerField(default=0, editable=False)
    failed_codes_since = models.DateTimeField(null=True, editable=False)
    # A password reset lowers the budget once per window (D-040).
    code_budget_relieved = models.BooleanField(default=False, editable=False)

    objects = AuthenticatorDeviceManager()

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["user"], name="accounts_one_authenticator_per_user")
        ]

    @property
    def bin_key(self):
        return decrypt(self.encrypted_key, AUTHENTICATOR_SECRET_PURPOSE)

    @sensitive_variables()
    def verify_token(self, token):
        # Exactly six ASCII digits: int() would also accept signs, underscores and other scripts.
        if not isinstance(token, str) or not AUTHENTICATOR_CODE.fullmatch(token):
            return False
        try:
            key = self.bin_key
        except DecryptionError:
            logger.error("An authenticator secret cannot be decrypted (SECRET_KEY changed?).")
            return False
        number = int(token)
        generator = TOTP(key, self.STEP, 0, self.DIGITS, self.drift)
        generator.time = time.time()
        if not generator.verify(number, self.TOLERANCE, self.last_t + 1):
            return False
        self.last_t = generator.t()
        self.drift = generator.drift
        if not is_current(self.encrypted_key, AUTHENTICATOR_SECRET_PURPOSE):
            self.encrypted_key = reencrypt(self.encrypted_key, AUTHENTICATOR_SECRET_PURPOSE)
        self.set_last_used_timestamp(commit=False)
        # Not the budget fields: they change only under their own lock (D-040).
        self.save(update_fields=["last_t", "drift", "encrypted_key", "last_used_at"])
        return True

    @property
    @sensitive_variables()
    def config_url(self):
        """The ``otpauth`` key URI that authenticator apps read from the QR code
        (https://github.com/google/google-authenticator/wiki/Key-Uri-Format)."""
        # Existing enrolments keep the issuer they were made with (D-073), which is cosmetic.
        from leg_platform.accounts.portal_settings import portal_values

        issuer = portal_values().name.replace(":", "")
        label = f"{issuer}:{self.user.get_username()}"
        parameters = urlencode(
            {
                "secret": b32encode(self.bin_key),
                "algorithm": "SHA1",
                "digits": self.DIGITS,
                "period": self.STEP,
                "issuer": issuer,
            },
            quote_via=quote,
        )
        return f"otpauth://totp/{quote(label)}?{parameters}"


class RecoveryCodeDevice(TimestampMixin, Device):
    """The recovery codes of an account; each code is deleted when it is used."""

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["user"], name="accounts_one_recovery_device_per_user")
        ]

    def verify_token(self, token):
        # Digests under SECRET_KEY_FALLBACKS also match, so a key rotation keeps the codes valid.
        # The delete is one statement: of two parallel requests only one deletes a row.
        digests = keyed_digests_with_fallbacks(token, RECOVERY_CODE_PURPOSE)
        deleted, _unused = self.codes.filter(digest__in=digests).delete()
        if not deleted:
            return False
        self.set_last_used_timestamp(commit=False)
        self.save()
        return True

    def add_codes(self, codes):
        RecoveryCode.objects.bulk_create(
            RecoveryCode(device=self, digest=recovery_code_digest(code)) for code in codes
        )


class RecoveryCode(models.Model):
    device = models.ForeignKey(
        RecoveryCodeDevice, on_delete=models.CASCADE, related_name="codes", editable=False
    )
    digest = models.CharField(max_length=64, db_collation=EXACT_COLLATION, editable=False)

    class Meta:
        default_permissions = ()
        # Unique per device, not across the instance: a random match with a code of another
        # account (40 bits each) must not make the setup or the renewal fail.
        constraints = [
            models.UniqueConstraint(
                fields=["device", "digest"], name="accounts_recovery_code_unique_per_device"
            )
        ]
