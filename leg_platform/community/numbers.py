"""Numbers and codes of the community domain: metering points, transformer circuits, settings.

Pure functions (D-077); the database is asked only for the grid operator name in an error message.

The metering point number is the 33-character "Messpunktbezeichnung" of the Swiss Metering Code (VSE/AES,
MC-CH 2025, 4 December 2025, section 4.2.3, "Bildungsregel"): positions 1-2 country (ISO 3166 alpha-2), positions
3-13 identifier (grid number, positions 3-8, and a reserved placeholder, positions 9-13; assigned by VSE/AES),
positions 14-33 a 20-character number assigned by the grid operator (the standard allows A-Z, 0-9 and "-" there;
the portal accepts digits only, D-093). Empty positions are filled with the digit zero (D-093)."""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator

COUNTRY_LENGTH = 2
IDENTIFIER_LENGTH = 11
SUFFIX_LENGTH = 20
METERING_POINT_LENGTH = COUNTRY_LENGTH + IDENTIFIER_LENGTH + SUFFIX_LENGTH  # 33
TRANSFORMER_CIRCUIT_MAX_LENGTH = 20

# Capital letters and digits only.
COUNTRY = re.compile(r"[A-Z]{2}")
IDENTIFIER = re.compile(r"[A-Z0-9]{11}")
METERING_POINT_NUMBER = re.compile(r"[A-Z0-9]{33}")
ENTERED_DIGITS = re.compile(r"[0-9]+")  # ASCII digits only (``str.isdigit`` would accept other scripts)
TRANSFORMER_CIRCUIT_NUMBER = re.compile(r"TRA[0-9]+")

COUNTRY_MESSAGE = _("Enter two capital letters, for example CH.")
IDENTIFIER_MESSAGE = _("Enter 11 characters: capital letters and digits.")
METERING_POINT_MESSAGE = _("Enter a number of 33 characters: capital letters and digits.")
# The typed part of a metering point number: one message per cause (the page script uses the same texts, D-093).
NO_DIGITS_MESSAGE = _("Enter the last digits of the number.")
NOT_DIGITS_MESSAGE = _("Only the digits 0–9 are allowed.")
TOO_MANY_DIGITS_MESSAGE = _("At most %(limit)s digits are allowed (entered: %(count)s).")
# "TRA" and digits is the format of the named grid operator; this rule is the one place to change (D-086).
TRANSFORMER_CIRCUIT_MESSAGE = _("Enter the %(operator)s number: TRA followed by digits.")


def validate_country(value):
    """The country of the metering points: two capital letters."""
    if COUNTRY.fullmatch(value or "") is None:
        raise ValidationError(COUNTRY_MESSAGE, code="invalid_country")


def validate_identifier(value):
    """The identifier that the grid operator assigns: 11 capital letters and digits."""
    if IDENTIFIER.fullmatch(value or "") is None:
        raise ValidationError(IDENTIFIER_MESSAGE, code="invalid_identifier")


def is_valid_metering_point_number(value):
    """A complete number: 33 characters, capital letters and digits."""
    return isinstance(value, str) and METERING_POINT_NUMBER.fullmatch(value) is not None


def validate_metering_point_number(value):
    if not is_valid_metering_point_number(value):
        raise ValidationError(METERING_POINT_MESSAGE, code="invalid_metering_point_number")


def validate_entered_digits(value):
    """The typed last digits of a metering point number; returns them trimmed and zero-padded on the left to 20
    ("23" becomes twenty digits ending in 23). Raises ``ValidationError`` with the code ``empty``, ``not_digits`` or
    ``too_many_digits``."""
    digits = (value or "").strip()
    if not digits:
        raise ValidationError(NO_DIGITS_MESSAGE, code="empty")
    if ENTERED_DIGITS.fullmatch(digits) is None:
        raise ValidationError(NOT_DIGITS_MESSAGE, code="not_digits")
    if len(digits) > SUFFIX_LENGTH:
        raise ValidationError(
            TOO_MANY_DIGITS_MESSAGE, code="too_many_digits", params={"limit": SUFFIX_LENGTH, "count": len(digits)}
        )
    return digits.zfill(SUFFIX_LENGTH)


def compose_metering_point_number(country, identifier, entered_digits):
    """The complete number from country, identifier and typed last digits (zero-padded to 20).

    Country and identifier are checked again; raises ``ValidationError``."""
    validate_country(country)
    validate_identifier(identifier)
    return f"{country}{identifier}{validate_entered_digits(entered_digits)}"


def split_metering_point_number(number):
    """``(country, identifier, digits)`` of a complete number, or None if it is not valid."""
    if not is_valid_metering_point_number(number):
        return None
    return (
        number[:COUNTRY_LENGTH],
        number[COUNTRY_LENGTH : COUNTRY_LENGTH + IDENTIFIER_LENGTH],
        number[COUNTRY_LENGTH + IDENTIFIER_LENGTH :],
    )


def normalise_metering_point_number(value):
    """Surrounding white space removed, capital letters (the stored form)."""
    return (value or "").strip().upper()


def format_metering_point_number(number):
    """The number as the person page shows it: "CH 1A2B 3C4D ..." (country, then groups of four).
    An invalid value is returned as it is; a missing number gives ""."""
    if not number:
        return ""
    if not is_valid_metering_point_number(number):
        return number
    rest = number[COUNTRY_LENGTH:]
    return " ".join([number[:COUNTRY_LENGTH], *(rest[i : i + 4] for i in range(0, len(rest), 4))])


def normalise_transformer_circuit_number(value):
    """The grid number as stored and compared: trimmed, capital letters."""
    return (value or "").strip().upper()


def validate_transformer_circuit_number(value):
    if (
        TRANSFORMER_CIRCUIT_NUMBER.fullmatch(value or "") is None
        or len(value) > TRANSFORMER_CIRCUIT_MAX_LENGTH
    ):
        raise ValidationError(
            TRANSFORMER_CIRCUIT_MESSAGE, code="invalid_transformer_circuit",
            params={"operator": grid_operator.name()},
        )
