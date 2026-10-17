"""The IBAN of a person: validation (python-stdnum), normal form, display and encryption (D-087).

Stored encrypted via ``accounts.encryption``; never searched, sorted or logged."""

import logging

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from stdnum import iban as stdnum_iban
from stdnum.exceptions import ValidationError as StdnumValidationError

from leg_platform.accounts.encryption import DecryptionError, decrypt, encrypt

logger = logging.getLogger(__name__)

PERSON_IBAN_PURPOSE = b"LEG-Platform person IBAN v1"
# Migration 0006 repeats this prefix on purpose.
BOUND_PREFIX = "v2|"
IBAN_MAX_LENGTH = 34


def compact(value):
    """The normal form: upper case, without spaces and hyphens. Not validated."""
    return "".join(str(value or "").replace("-", " ").split()).upper()


def validate(value):
    """The IBAN in normal form; ``ValidationError`` if invalid; empty stays empty (optional)."""
    number = compact(value)
    if not number:
        return ""
    # ASCII only: the package checksum would also accept other digit characters.
    if not number.isascii() or not number.isalnum() or len(number) > IBAN_MAX_LENGTH:
        raise ValidationError(_("Enter a valid IBAN."), code="invalid_iban")
    try:
        return stdnum_iban.validate(number)
    except StdnumValidationError:
        raise ValidationError(_("Enter a valid IBAN."), code="invalid_iban") from None


def display(value):
    """The IBAN in groups of four characters ("CH93 0076 2011 6238 5295 7"); empty stays empty."""
    number = compact(value)
    return " ".join(number[i : i + 4] for i in range(0, len(number), 4))


def encrypt_iban(value, account_id):
    """The stored encrypted text (``v2|<account>|<IBAN>``, bound to the account, L-7); "" for no IBAN."""
    number = compact(value)
    if not number:
        return ""
    return encrypt(f"{BOUND_PREFIX}{int(account_id)}|{number}".encode("ascii"), PERSON_IBAN_PURPOSE)


def decrypt_iban(token, account_id):
    """The IBAN of a stored text; "" for none. ``DecryptionError`` if undecryptable or bound to another account.
    Unbound texts (migration 0005) are accepted."""
    if not token:
        return ""
    payload = decrypt(token, PERSON_IBAN_PURPOSE).decode("ascii")
    return split_payload(payload, account_id)


def split_payload(payload, account_id):
    if not payload.startswith(BOUND_PREFIX):
        return payload  # the form before the binding
    _prefix, bound_to, number = payload.split("|", 2)
    if bound_to != str(int(account_id)):
        raise DecryptionError("The value belongs to another account.")
    return number


def is_bound(token):
    """Whether a stored text holds the account (needs the key)."""
    return decrypt(token, PERSON_IBAN_PURPOSE).decode("ascii").startswith(BOUND_PREFIX)


def readable(record):
    """The IBAN for display, or "" if it cannot be decrypted (logged with the account number only)."""
    try:
        return record.iban
    except DecryptionError:
        logger.error("The IBAN of account %s cannot be decrypted.", record.person_id)
        return ""
