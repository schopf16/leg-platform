"""Keyed digests derived from SECRET_KEY (docs/DECISIONS.md, D-023)."""

import hashlib
import hmac

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings


def keyed_digest(value, purpose):
    """Return the HMAC-SHA256 of a value with a key derived for one purpose via HKDF.

    A distinct purpose label gives a distinct key, so digests of different uses never match.
    """
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=purpose,
    ).derive(settings.SECRET_KEY.encode("utf-8"))
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
