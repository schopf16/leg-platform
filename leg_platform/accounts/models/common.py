"""Constants, errors and validators shared by the account models."""

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

import idna
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.utils.translation import gettext_lazy as _

from leg_platform.branding import HEX_COLOUR
from leg_platform.environment import (
    INSTANCE_NAME_MAX_LENGTH,
    has_unsafe_url_character,
    is_valid_instance_name,
)

EXACT_COLLATION = "utf8mb4_bin"  # exact comparison for identifier columns (D-031)

EMAIL_MAX_LENGTH = 254
SWITZERLAND = "CH"

SWISS_POSTAL_CODE = re.compile(r"\A[1-9][0-9]{3}\Z")
SWISS_POSTAL_CODE_MESSAGE = _("Enter a postal code with 4 digits from 1000 to 9999.")


def parse_swiss_postal_code(value):
    """Return the Swiss postal code as an integer, or None unless it is four digits 1000-9999."""
    value = (value or "").strip()
    return int(value) if SWISS_POSTAL_CODE.match(value) else None


class AccountIntegrityError(Exception):
    """A change would violate an invariant of the account model."""


class CustomerNumberUnavailableError(AccountIntegrityError):
    """No unused customer number could be issued."""


def validate_account_email(email):
    """Raise ValueError unless the address is valid and fits the columns after normalisation."""
    if email is not None and len(str(email).strip()) > EMAIL_MAX_LENGTH:
        raise ValueError(f"The email address is longer than {EMAIL_MAX_LENGTH} characters.")
    value = unicodedata.normalize("NFKC", str(email)).strip().lower() if email is not None else ""
    if not value:
        raise ValueError("An email address is required.")
    if value.count("@") != 1:
        raise ValueError("The email address is not valid.")
    local_part, domain = value.rsplit("@", 1)
    if not local_part or not domain or not local_part.isascii():
        raise ValueError("The email address is not valid.")
    if '"' in local_part or "\\" in local_part:
        raise ValueError("The email address is not valid.")
    if len(value) > EMAIL_MAX_LENGTH or len(domain) > 253:
        raise ValueError(f"The email address is longer than {EMAIL_MAX_LENGTH} characters.")
    # Category C, Z and M characters are rejected before the domain is mapped with IDNA (UTS #46).
    if any(unicodedata.category(character)[0] in {"C", "Z", "M"} for character in value):
        raise ValueError("The email address is not valid.")
    if domain == "localhost" or domain.startswith("[") or domain.endswith("]"):
        raise ValueError("The email address is not valid.")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("The email address is not valid.")
    try:
        ascii_domain = idna.encode(
            domain, uts46=True, std3_rules=True, transitional=False
        ).decode("ascii").lower()
    except idna.IDNAError:
        raise ValueError("The email address is not valid.") from None
    normalized = f"{local_part}@{ascii_domain}"
    try:
        EmailValidator(allowlist=[])(normalized)
    except ValidationError:
        raise ValueError("The email address is not valid.") from None
    # NFKC and IDNA can lengthen an address, so the stored key is checked too.
    if len(normalized) > EMAIL_MAX_LENGTH:
        raise ValueError(f"The email address is longer than {EMAIL_MAX_LENGTH} characters.")


def validate_https_address(value):
    """Accept only a complete https:// address without credentials (D-056)."""
    # Any parsing error (unclosed bracket, port out of range) is a refusal, never an exception.
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        raise ValidationError(
            _("Enter a complete address that starts with https://."), code="https_required"
        ) from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or has_unsafe_url_character(value)
    ):
        raise ValidationError(
            _("Enter a complete address that starts with https://."), code="https_required"
        )


def validate_instance_name(value):
    """The rule of ``environment.is_valid_instance_name``: the name appears in email subjects."""
    if not is_valid_instance_name(value):
        raise ValidationError(
            _("The name must have at most %(max)d characters and no control characters."),
            code="invalid_name",
            params={"max": INSTANCE_NAME_MAX_LENGTH},
        )


def validate_hex_colour(value):
    """A colour as # followed by six hexadecimal digits (the rule of ``leg_platform.branding``)."""
    if HEX_COLOUR.fullmatch(value) is None:
        raise ValidationError(
            _(
                "Enter the colour as # followed by six digits or letters from A to F, for "
                "example #005BA2."
            ),
            code="invalid_colour",
        )
