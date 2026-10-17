"""The IBAN of a person: validation, normal form, display and encryption (D-087).

The checksum and the structure of the country are checked by the established package ``python-stdnum``
(``stdnum.iban``): any IBAN with a valid checksum is accepted, not only Swiss and Liechtenstein ones. The
normal form is upper case without spaces and hyphens; the display form groups of four characters.

The IBAN is stored encrypted (``PersonRecord.iban_encrypted``) with the facility of the project
(``accounts.encryption``: a key derived from ``SECRET_KEY`` with HKDF and a label of its own, Fernet; the keys of
``SECRET_KEY_FALLBACKS`` are tried for decryption, docs/SECURITY.md, section 7). The IBAN is never searched or
sorted, so no keyed lookup digest is stored. It is shown only on the detail page of a person and in the PDF of the
detail, and never written to a log, to the history or to the security log.
"""

import logging

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from stdnum import iban as stdnum_iban
from stdnum.exceptions import ValidationError as StdnumValidationError

from leg_platform.accounts.encryption import DecryptionError, decrypt, encrypt

logger = logging.getLogger(__name__)

PERSON_IBAN_PURPOSE = b"LEG-Platform person IBAN v1"
# The encrypted payload of an IBAN bound to its account (L-7); the migration 0006 repeats it on purpose.
BOUND_PREFIX = "v2|"
IBAN_MAX_LENGTH = 34


def compact(value):
    """The normal form: upper case, without spaces and hyphens. Not validated."""
    return "".join(str(value or "").replace("-", " ").split()).upper()


def validate(value):
    """Return the IBAN in its normal form; raise a ``ValidationError`` if it is not a valid IBAN.

    An empty value is valid and stays empty (the IBAN is optional)."""
    number = compact(value)
    if not number:
        return ""
    # ASCII letters and digits only: the checksum of the package would also accept other digit characters.
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
    """The encrypted text that is stored for an IBAN (normal form); "" for no IBAN.

    The number of the account is part of the encrypted text (``v2|<account>|<IBAN>``, L-7): the stored text
    cannot be copied to the record of another person, ``decrypt_iban`` refuses it there."""
    number = compact(value)
    if not number:
        return ""
    return encrypt(f"{BOUND_PREFIX}{int(account_id)}|{number}".encode("ascii"), PERSON_IBAN_PURPOSE)


def decrypt_iban(token, account_id):
    """The IBAN of a stored text; "" for none. ``DecryptionError`` if no key of the instance decrypts it or if it
    belongs to another account. A text without the account (made by the migration 0005, before L-7) is
    accepted and is bound by migration 0006 or ``reencrypt_ibans``."""
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
    """The IBAN of a person record for display, or "" if it cannot be decrypted (the key was replaced
    without being kept in ``SECRET_KEY_FALLBACKS``). The failure is logged with the account number only."""
    try:
        return record.iban
    except DecryptionError:
        logger.error("The IBAN of account %s cannot be decrypted.", record.person_id)
        return ""
