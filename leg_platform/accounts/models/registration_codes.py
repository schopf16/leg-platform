"""The code challenge of a new registration (D-136).

A person who enters an address on the registration page receives a challenge: a random id that only
the session of that browser holds (the table keeps its keyed digest). The six-digit code in the
email is bound to the challenge, so it is useless in another session. A row exists for every address
entered, whether a code is sent or not, so the behaviour of the page does not depend on the address.
"""

import secrets

from django.db import models
from django.utils import timezone

from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.models.common import EMAIL_MAX_LENGTH, EXACT_COLLATION
from leg_platform.accounts.models.user import User

CHALLENGE_BYTES = 32
CHALLENGE_LENGTH = 43
CHALLENGE_PURPOSE = b"LEG-Platform registration challenge v1"
CODE_PURPOSE = b"LEG-Platform registration code v1"
CODE_DIGITS = 6


def challenge_digest(raw_challenge):
    """Return the keyed digest under which a challenge id is stored; the id itself never is."""
    return keyed_digest(raw_challenge, CHALLENGE_PURPOSE)


def code_digest(challenge, code):
    """Return the keyed digest of a code, bound to the digest of its challenge."""
    return keyed_digest(f"{challenge}\n{code}", CODE_PURPOSE)


def new_code():
    """Return a random code of six digits, zero-padded."""
    return str(secrets.randbelow(10**CODE_DIGITS)).zfill(CODE_DIGITS)


class RegistrationChallenge(models.Model):
    """One attempt to prove an email address with a code (D-136)."""

    challenge_digest = models.CharField(
        max_length=64, unique=True, db_collation=EXACT_COLLATION, editable=False
    )
    address = models.EmailField(max_length=EMAIL_MAX_LENGTH, db_collation=EXACT_COLLATION, editable=False)
    # Empty while no code exists (an address with an account, or before the worker has run).
    code_digest = models.CharField(max_length=64, blank=True, db_collation=EXACT_COLLATION, editable=False)
    expires_at = models.DateTimeField(db_index=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    consumed = models.BooleanField(default=False)
    # Set when the code was entered correctly: the address counts as proven until then (steps 2 and 3).
    proven_until = models.DateTimeField(null=True, blank=True, db_index=True)
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.CASCADE, related_name="registration_challenges",
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["address", "created_at"])]

    @classmethod
    def begin(cls, address, lifetime):
        """Create a challenge for the address and return it with the raw id for the session."""
        raw = secrets.token_urlsafe(CHALLENGE_BYTES)
        row = cls.objects.create(
            challenge_digest=challenge_digest(raw), address=address, expires_at=timezone.now() + lifetime
        )
        return row, raw

    @property
    def digest(self):
        """The binding of the staged sign-in method (registration_methods)."""
        return self.challenge_digest

    def is_open(self, max_attempts, now=None):
        """True while a code may still be entered."""
        return (
            not self.consumed and self.failed_attempts < max_attempts and self.expires_at > (now or timezone.now())
        )

    def is_proven(self, now=None):
        return (
            self.consumed
            and self.user_id is not None
            and self.proven_until is not None
            and self.proven_until > (now or timezone.now())
        )
