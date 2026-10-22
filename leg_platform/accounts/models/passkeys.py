"""Public WebAuthn credentials and single-use, session-bound ceremonies (D-106).

No biometric data, private key, attestation certificate or device fingerprint is stored.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models.common import EXACT_COLLATION


def new_user_handle():
    """A stable opaque 256-bit handle, independent of the email address."""
    return secrets.token_bytes(32)


class PasskeyIdentity(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="passkey_identity"
    )
    handle = models.BinaryField(default=new_user_handle, max_length=32, unique=True, editable=False)
    # Legacy D-106 metadata only; never read or written by authentication or promotion.
    # Retained to avoid an unnecessary destructive migration (owner clarification 2026-10-22).
    two_devices_confirmed = models.BooleanField(default=False, editable=False)

    class Meta:
        default_permissions = ()


class Passkey(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="passkeys"
    )
    # SHA-256 of the binary credential id: a small exact unique index, including long ids.
    credential_digest = models.CharField(
        max_length=64, unique=True, db_collation=EXACT_COLLATION, editable=False
    )
    credential_id = models.BinaryField(editable=False)
    public_key = models.BinaryField(editable=False)
    sign_count = models.PositiveBigIntegerField(default=0, editable=False)
    name = models.CharField(_("Passkey name"), max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, editable=False)
    backed_up = models.BooleanField(default=False, editable=False)
    backup_eligible = models.BooleanField(default=False, editable=False)
    # Staged credentials cannot authenticate before the atomic method switch.
    active = models.BooleanField(default=False, editable=False)
    setup_binding = models.CharField(max_length=64, blank=True, editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("passkey")
        verbose_name_plural = _("passkeys")

    def __str__(self):
        return f"Passkey {self.pk}"


class PasskeyChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE, related_name="+"
    )
    purpose = models.CharField(max_length=16)
    action = models.CharField(max_length=200, blank=True)
    binding = models.CharField(max_length=64, db_collation=EXACT_COLLATION)
    auth_hash = models.CharField(max_length=64, blank=True)
    challenge = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        default_permissions = ()


class PendingSignInChange(models.Model):
    """A voluntary password return: never changes the active method before completion."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    password_hash = models.CharField(max_length=128, editable=False)
    method = models.CharField(max_length=8, editable=False)
    auth_hash = models.CharField(max_length=64, editable=False)
    binding = models.CharField(max_length=64, editable=False)
    app_confirmed = models.BooleanField(default=False, editable=False)
    expires_at = models.DateTimeField(editable=False)
    ready = models.BooleanField(default=False, editable=False)

    class Meta:
        default_permissions = ()
