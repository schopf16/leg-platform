"""The Energy Identification Code (EIC) of ENTSO-E: format and check character (D-139).

Source: ENTSO-E, "The Energy Identification Coding Scheme (EIC) Reference Manual", version 5.5 (2022-02-01),
section 5 (16 characters: 2 for the issuing office, 1 object type, 12 characters, 1 check character; permitted
characters 0-9, A-Z and "-"; the check character is never "-"), and the official ENTSO-E check-digit generator,
version 1.5 2025 (https://www.entsoe.eu/data/energy-identification-codes-eic/code-generator/eic_key_generator):
values 0-9 = 0-9, A-Z = 10-35, "-" = 36; weights 16 down to 2 for the first 15 characters; check value
36 - ((sum - 1) mod 37); a check value of 36 ("-") is not allowed; the first two characters are never "-".
Checked against the manual's examples 10X168Y4E6H0041Z and 10X---ENTSOE---L.
"""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-"
LENGTH = 16
FORMAT = re.compile(r"[0-9A-Z]{2}[0-9A-Z-]{13}[0-9A-Z]")


def check_character(first_fifteen):
    """The check character of the first 15 characters, or None when the result would be "-" (not allowed)."""
    total = sum(ALPHABET.index(char) * weight for char, weight in zip(first_fifteen, range(16, 1, -1)))
    value = 36 - ((total - 1) % 37)
    return None if value == 36 else ALPHABET[value]


def is_valid(code):
    return (
        isinstance(code, str)
        and FORMAT.fullmatch(code) is not None
        and check_character(code[:15]) == code[15]
    )


def validate_eic(value):
    if not is_valid(value):
        raise ValidationError(
            _("Enter a valid EIC: 16 characters (digits, capital letters, hyphen) with a correct check character."),
            code="invalid_eic",
        )
