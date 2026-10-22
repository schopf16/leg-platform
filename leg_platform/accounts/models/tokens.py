"""Single-use email links and requested address changes (D-016, D-059)."""

import secrets

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.models.common import EMAIL_MAX_LENGTH, EXACT_COLLATION
from leg_platform.accounts.models.user import User

# 256 bits from the operating system's random generator; the URL-safe text has 43 characters.
VERIFICATION_TOKEN_BYTES = 32
VERIFICATION_TOKEN_LENGTH = 43
VERIFICATION_TOKEN_PURPOSE = b"LEG-Platform verification token v1"


def verification_token_digest(raw_token):
    """Return the keyed digest under which a token is stored; the token itself never is."""
    return keyed_digest(raw_token, VERIFICATION_TOKEN_PURPOSE)


class VerificationTokenManager(models.Manager):
    def issue(self, user, purpose, lifetime, *, target_email=""):
        """Create a token for the user and return its raw value, which is stored nowhere."""
        raw_token = secrets.token_urlsafe(VERIFICATION_TOKEN_BYTES)
        self.create(
            user=user,
            purpose=purpose,
            digest=verification_token_digest(raw_token),
            expires_at=timezone.now() + lifetime,
            target_email=target_email,
        )
        return raw_token

    def find_valid(self, raw_token, purpose):
        """Return the unexpired token with this raw value and purpose, or None."""
        if len(raw_token or "") != VERIFICATION_TOKEN_LENGTH:
            return None
        return self.find_valid_by_digest(verification_token_digest(raw_token), purpose)

    def find_valid_by_digest(self, digest, purpose):
        return self.filter(
            digest=digest, purpose=purpose, expires_at__gt=timezone.now()
        ).select_related("user").first()


class VerificationToken(models.Model):
    """A single-use, time-limited token that a link sent by email carries (D-016)."""

    class Purpose(models.TextChoices):
        REGISTRATION = "registration", "Registration"
        PASSWORD_RESET = "password_reset", "Password reset"
        TWO_FACTOR_RESET = "two_factor_reset", "Two-factor authentication reset"
        METHOD_RETURN = "method_return", "Sign-in method return"
        EMAIL_CHANGE = "email_change", "Email address change"
        SECONDARY_EMAIL = "secondary_email", "Second email address"
        EMAIL_CHANGE_STOP = "email_change_stop", "Stop an email address change"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="verification_tokens", editable=False
    )
    purpose = models.CharField(max_length=20, choices=Purpose, editable=False)
    digest = models.CharField(
        max_length=64, unique=True, db_collation=EXACT_COLLATION, editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    target_email = models.EmailField(
        max_length=EMAIL_MAX_LENGTH, blank=True, editable=False
    )

    objects = VerificationTokenManager()


class PendingEmailChange(models.Model):
    """A requested address that nobody has confirmed yet (D-059).

    Made by the request itself for every target address, so the response does not reveal whether
    the address can be used (no account enumeration); the worker sends the link only where it can
    work. One request per kind and account; a new request replaces the old one.
    """

    class Kind(models.TextChoices):
        LOGIN = "login", "Login address"
        SECONDARY = "secondary", "Second address"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="pending_email_changes", editable=False
    )
    kind = models.CharField(max_length=9, choices=Kind, editable=False)
    address = models.EmailField(max_length=EMAIL_MAX_LENGTH, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    # Internal number of the staff account that made the request for the customer (D-087), else
    # empty; not a foreign key, like the security log.
    requested_by = models.PositiveBigIntegerField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("pending email change")
        verbose_name_plural = _("pending email changes")
        constraints = [
            models.UniqueConstraint(fields=("user", "kind"), name="accounts_pending_email_unique")
        ]

    def __str__(self):
        return f"{self.kind} change of account {self.user_id}"
