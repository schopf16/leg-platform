"""Encryption of stored secrets with a key derived from SECRET_KEY (docs/SECURITY.md, section 7).

The key is derived with HKDF and a label for one purpose, and the value is encrypted with Fernet,
both from the ``cryptography`` library (D-006, D-040). Keys derived from SECRET_KEY_FALLBACKS
are tried for decryption, so that SECRET_KEY can be rotated; a value found under an old key is
encrypted again with the current one by the caller (``is_current()``, ``reencrypt()``).
"""

import base64

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings


class DecryptionError(Exception):
    """No key of the instance decrypts the value (it is damaged or SECRET_KEY was replaced)."""


def _fernet(secret, purpose):
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(
        secret.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(key))


def _current(purpose):
    return _fernet(settings.SECRET_KEY, purpose)


def _all(purpose):
    return MultiFernet(
        [_current(purpose), *(_fernet(old, purpose) for old in settings.SECRET_KEY_FALLBACKS)]
    )


def encrypt(plaintext, purpose):
    """Encrypt bytes with the current key; return the token as text."""
    return _current(purpose).encrypt(plaintext).decode("ascii")


def decrypt(token, purpose):
    try:
        return _all(purpose).decrypt(token.encode("ascii"))
    except (InvalidToken, UnicodeEncodeError):
        raise DecryptionError("The value cannot be decrypted.") from None


def is_current(token, purpose):
    """Whether the current key decrypts the token (otherwise it is due to be encrypted again)."""
    try:
        _current(purpose).decrypt(token.encode("ascii"))
    except (InvalidToken, UnicodeEncodeError):
        return False
    return True


def reencrypt(token, purpose):
    try:
        return _all(purpose).rotate(token.encode("ascii")).decode("ascii")
    except (InvalidToken, UnicodeEncodeError):
        raise DecryptionError("The value cannot be decrypted.") from None
