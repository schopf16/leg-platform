"""Numbers and codes of the community domain: metering points, transformer circuits, settings.

Pure functions, no database (D-077). The rules come from the owner's design specification
(``messpunkte/VORGABEN.md``, ``einstellungen/VORGABEN.md``, ``standorte/VORGABEN.md``).
"""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

COUNTRY_LENGTH = 2
IDENTIFIER_LENGTH = 11
SUFFIX_LENGTH = 20
METERING_POINT_LENGTH = COUNTRY_LENGTH + IDENTIFIER_LENGTH + SUFFIX_LENGTH  # 33
TRANSFORMER_CIRCUIT_MAX_LENGTH = 20

# The kinds of text, as the specification states them: capital letters and digits only.
COUNTRY = re.compile(r"[A-Z]{2}")
IDENTIFIER = re.compile(r"[A-Z0-9]{11}")
METERING_POINT_NUMBER = re.compile(r"[A-Z0-9]{33}")
ENTERED_DIGITS = re.compile(r"[0-9]{1,20}")
TRANSFORMER_CIRCUIT_NUMBER = re.compile(r"TRA[0-9]+")

COUNTRY_MESSAGE = _("Enter two capital letters, for example CH.")
IDENTIFIER_MESSAGE = _("Enter 11 characters: capital letters and digits.")
METERING_POINT_MESSAGE = _("Enter a number of 33 characters: capital letters and digits.")
ENTERED_DIGITS_MESSAGE = _("Enter up to 20 digits.")
TRANSFORMER_CIRCUIT_MESSAGE = _("Enter the BKW number: TRA followed by digits.")


def validate_country(value):
    """The country of the metering points: two capital letters (``einstellungen/VORGABEN.md``)."""
    if COUNTRY.fullmatch(value or "") is None:
        raise ValidationError(COUNTRY_MESSAGE, code="invalid_country")


def validate_identifier(value):
    """The identifier that the BKW assigns: 11 capital letters and digits."""
    if IDENTIFIER.fullmatch(value or "") is None:
        raise ValidationError(IDENTIFIER_MESSAGE, code="invalid_identifier")


def is_valid_metering_point_number(value):
    """A complete number: 33 characters, capital letters and digits (``messpunkte/VORGABEN.md``)."""
    return isinstance(value, str) and METERING_POINT_NUMBER.fullmatch(value) is not None


def validate_metering_point_number(value):
    if not is_valid_metering_point_number(value):
        raise ValidationError(METERING_POINT_MESSAGE, code="invalid_metering_point_number")


def compose_metering_point_number(country, identifier, entered_digits):
    """The complete number from the country, the identifier and the typed last digits.

    The typed digits are padded on the left with zeros to 20 positions; the result has 33
    characters. Country and identifier come from the settings and are checked again here. Raises
    ``ValidationError`` with a message that names the problem.
    """
    validate_country(country)
    validate_identifier(identifier)
    digits = (entered_digits or "").strip()
    if ENTERED_DIGITS.fullmatch(digits) is None:
        raise ValidationError(ENTERED_DIGITS_MESSAGE, code="invalid_digits")
    return f"{country}{identifier}{digits.zfill(SUFFIX_LENGTH)}"


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


def normalise_transformer_circuit_number(value):
    """The BKW number as it is stored and compared: surrounding white space removed and converted
    to capital letters before the comparison (``standorte/VORGABEN.md``, "Nachtrag: Trafokreis")."""
    return (value or "").strip().upper()


def validate_transformer_circuit_number(value):
    if (
        TRANSFORMER_CIRCUIT_NUMBER.fullmatch(value or "") is None
        or len(value) > TRANSFORMER_CIRCUIT_MAX_LENGTH
    ):
        raise ValidationError(TRANSFORMER_CIRCUIT_MESSAGE, code="invalid_transformer_circuit")
