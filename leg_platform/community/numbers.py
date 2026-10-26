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
# The display (D-109): country and identifier are one block, the rest follows in blocks of four.
HEAD_LENGTH = COUNTRY_LENGTH + IDENTIFIER_LENGTH  # 13
GROUP_SIZE = 4
NO_BREAK_SPACE = "\u00a0"
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


def compact_number(value):
    """The text without any white space (D-109): the optical separators of the display (the no-break space of
    ``format_metering_point_number``, also a normal, a narrow or a thin space) never belong to the number, so a number
    that was copied from a page and pasted into a field or the search means the same as the plain one."""
    return "".join((value or "").split())


def normalise_entered_number(value, prefix=""):
    """The typed last characters of a metering point number, made exactly ``SUFFIX_LENGTH`` characters long (D-102).

    White space is ignored (D-109: ``compact_number``). A complete number of ``METERING_POINT_LENGTH`` characters that
    starts with ``prefix`` (country and identifier of the settings, when given) is taken as the number that it is: the
    prefix is dropped, so that a pasted number is not "too long". Letters A-Z (small ones become capitals), digits and
    the hyphen only. Shorter: zeros on the left ("23" becomes eighteen zeros and 23). Longer: **only leading zeros are
    removed**, as many as needed; if the entry has fewer leading zeros than that, or is longer than
    ``MAX_ENTRY_LENGTH``, it is invalid. No other character is ever removed. Raises ``ValidationError`` with the code
    ``empty``, ``not_allowed`` or ``too_long``. (Zeros are put in front by hand, not with ``zfill``: it would put them
    behind a leading hyphen.)"""
    text = compact_number(value)
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
    if prefix and len(prefix) == HEAD_LENGTH and len(text) == METERING_POINT_LENGTH and text.startswith(prefix.upper()):
        text = text[len(prefix):]
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
    return f"{country}{identifier}{normalise_entered_number(entered, prefix=f'{country}{identifier}')}"


def normalise_metering_point_number(value):
    """White space removed (D-109), capital letters (the stored form)."""
    return compact_number(value).upper()


def pasted_number(text):
    """The compact capital text of ``text`` if it looks like a number copied from a page (D-109): several words (the blocks
    of the display, separated by spaces or no-break spaces), together more than a block of the display and only the
    characters of a number. None otherwise. The lists use it to search a pasted number as one piece instead of word by
    word; whether a stored number contains it is the list's question."""
    words = (text or "").split()
    compact = normalise_metering_point_number(text)
    if len(words) < 2 or len(compact) <= HEAD_LENGTH or len(compact) > METERING_POINT_LENGTH:
        return None
    return compact if ENTERED_CHARACTERS.fullmatch(compact) else None


def format_metering_point_number(number, separator=NO_BREAK_SPACE):
    """The number as every page shows it (D-109): the first ``HEAD_LENGTH`` characters (country and identifier: the
    grid operator's part) as one block, then the characters the grid operator assigned in blocks of four, separated by a
    no-break space (``NO_BREAK_SPACE``: the number never wraps, and the separator is white space that every field and the
    search ignore, see ``compact_number``). The 33 characters of the standard give 13 + 5 x 4. Any other length is
    grouped the same way (the tail in fours; a value of at most 13 characters is one block). The value is not
    validated and white space in it is ignored, so the function is idempotent; a missing number gives "". ``separator``
    is for a place where the number may wrap between blocks (a PDF cell): pass a normal space."""
    text = compact_number(number)
    if not text:
        return ""
    groups = [text[:HEAD_LENGTH], *(text[i : i + GROUP_SIZE] for i in range(HEAD_LENGTH, len(text), GROUP_SIZE))]
    return separator.join(group for group in groups if group)


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
