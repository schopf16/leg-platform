"""Numbers and codes of the community domain: metering points, transformer circuits, settings.

Pure functions (D-077); the database is asked only for the grid operator name in an error message.

The metering point number is the 33-character "Messpunktbezeichnung" of the Swiss Metering Code (VSE/AES,
MC-CH 2025, 4 December 2025, section 4.2.3, "Bildungsregel"): positions 1-2 country (ISO 3166 alpha-2), positions
3-13 identifier (grid number, positions 3-8, and a reserved placeholder, positions 9-13; assigned by VSE/AES),
positions 14-33 a 20-character number assigned by the grid operator: capital letters A-Z, digits 0-9 and the hyphen
(the standard names exactly these, "Zeichensatz ISO 8859-1 ... die Grossbuchstaben A-Z, die Ziffern 0-9 sowie der
Bindestrich"). Empty positions are filled with the digit zero. The portal accepts what the standard allows, takes small
letters as capitals, fills a shorter entry up with zeros on the left and shortens a longer one only by leading zeros
(D-102, which amends D-093)."""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator

COUNTRY_LENGTH = 2
IDENTIFIER_LENGTH = 11
SUFFIX_LENGTH = 20
METERING_POINT_LENGTH = COUNTRY_LENGTH + IDENTIFIER_LENGTH + SUFFIX_LENGTH  # 33
TRANSFORMER_CIRCUIT_MAX_LENGTH = 20
# The longest entry that is looked at: a longer text is "too long" at once (the field has no ``maxlength``, so that a
# paste is never cut silently).
MAX_ENTRY_LENGTH = 200

# Capital letters and digits; in the 20 characters of the number also the hyphen.
COUNTRY = re.compile(r"[A-Z]{2}")
IDENTIFIER = re.compile(r"[A-Z0-9]{11}")
METERING_POINT_NUMBER = re.compile(r"[A-Z]{2}[A-Z0-9]{11}[A-Z0-9\-]{20}")
# What may be typed for the 20 characters: ASCII only (``str.upper`` would turn a "ß" into "SS"); small letters are
# taken as capitals.
ENTERED_CHARACTERS = re.compile(r"[A-Za-z0-9\-]+")
TRANSFORMER_CIRCUIT_NUMBER = re.compile(r"TRA[0-9]+")

COUNTRY_MESSAGE = _("Enter two capital letters, for example CH.")
IDENTIFIER_MESSAGE = _("Enter 11 characters: capital letters and digits.")
METERING_POINT_MESSAGE = _("Enter a number of 33 characters: capital letters, digits and hyphens.")
# The typed part of a metering point number: one message per cause (the page script uses the same texts, D-102).
NO_DIGITS_MESSAGE = _("Enter the last characters of the number.")
NOT_DIGITS_MESSAGE = _("Only the letters A–Z, the digits 0–9 and the hyphen are allowed.")
TOO_MANY_DIGITS_MESSAGE = _(
    "The metering point number is invalid: it is too long. At most %(limit)s characters are allowed, and only zeros "
    "at the start can be left out (entered: %(count)s)."
)
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


def normalise_entered_number(value):
    """The typed last characters of a metering point number, made exactly ``SUFFIX_LENGTH`` characters long (D-102).

    Letters A-Z (small ones become capitals), digits and the hyphen only. Shorter: zeros on the left ("23" becomes
    eighteen zeros and 23). Longer: **only leading zeros are removed**, as many as needed; if the entry has fewer
    leading zeros than that, or is longer than ``MAX_ENTRY_LENGTH``, it is invalid. No other character is ever removed.
    Raises ``ValidationError`` with the code ``empty``, ``not_allowed`` or ``too_long``. (Zeros are put in front by
    hand, not with ``zfill``: it would put them behind a leading hyphen.)"""
    text = (value or "").strip()
    if not text:
        raise ValidationError(NO_DIGITS_MESSAGE, code="empty")
    if ENTERED_CHARACTERS.fullmatch(text) is None:
        raise ValidationError(NOT_DIGITS_MESSAGE, code="not_allowed")
    too_long = ValidationError(
        TOO_MANY_DIGITS_MESSAGE, code="too_long", params={"limit": SUFFIX_LENGTH, "count": len(text)}
    )
    if len(text) > MAX_ENTRY_LENGTH:
        raise too_long
    text = text.upper()
    if len(text) > SUFFIX_LENGTH:
        extra = len(text) - SUFFIX_LENGTH
        leading_zeros = len(text) - len(text.lstrip("0"))
        if leading_zeros < extra:
            raise too_long
        text = text[extra:]
    return "0" * (SUFFIX_LENGTH - len(text)) + text


def compose_metering_point_number(country, identifier, entered):
    """The complete number from country, identifier and the typed last characters (``normalise_entered_number``).

    Country and identifier are checked again; raises ``ValidationError``."""
    validate_country(country)
    validate_identifier(identifier)
    return f"{country}{identifier}{normalise_entered_number(entered)}"


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
