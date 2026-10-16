"""Addresses of the community domain: normalisation, uniqueness key and the directory suggestion.

The normalisation and the check use the machinery of ``leg_platform.addresses`` (D-033, D-065,
D-075): the folded text of ``addresses.keys`` (lower case, no accents) for the comparison, and the
local copy of the official directory of building addresses, read through ``addresses.services``.
Nothing here writes to the directory, and nothing here changes a stored address by itself: a
suggestion is only advice that a person with the right accepts or dismisses
(``standorte/adresskorrektur/VORGABEN.md``).
"""

import hashlib
import logging
from dataclasses import dataclass

from django.utils.translation import gettext

from leg_platform.addresses.keys import fold
from leg_platform.addresses.services import directory_status, lookup

logger = logging.getLogger(__name__)

SWISS_POSTAL_CODE_LENGTH = 4


def collapse(text):
    """Surrounding white space removed and inner runs of white space reduced to one space."""
    return " ".join((text or "").split())


def normalise_address(street, house_number, postal_code, town):
    """The four parts as they are stored."""
    return (
        collapse(street),
        collapse(house_number),
        collapse(str(postal_code or "")),
        collapse(town),
    )


def address_key(street, house_number, postal_code, town):
    """The text that makes an address unique: the four parts, folded (case and accents ignored)
    and joined with a line break (a character that no part contains)."""
    return "\n".join(fold(part) for part in normalise_address(street, house_number, postal_code, town))


def address_digest(street, house_number, postal_code, town):
    """The SHA-256 of ``address_key`` in hexadecimal: the stored, unique form (64 characters, so that
    the unique index stays small; MariaDB limits unique character columns, Django check mysql.W003)."""
    return hashlib.sha256(address_key(street, house_number, postal_code, town).encode()).hexdigest()


def address_signature(street, house_number, postal_code, town):
    """The four parts as typed (white space normalised): the memory of a dismissed suggestion.

    The suggestion comes back only when street, house number, postal code or town changes
    (``standorte/adresskorrektur/VORGABEN.md``), also a change of the spelling."""
    return "\n".join(normalise_address(street, house_number, postal_code, town))


@dataclass(frozen=True)
class AddressSuggestion:
    street: str
    house_number: str
    postal_code: str
    town: str

    def as_tuple(self):
        return (self.street, self.house_number, self.postal_code, self.town)

    def display(self):
        """"Street house number, postal code town" as the banner shows it."""
        first = f"{self.street} {self.house_number}".strip()
        second = f"{self.postal_code} {self.town}".strip()
        return f"{first}, {second}" if first and second else first or second


def suggest_correction(street, house_number, postal_code, town):
    """The address of the directory that this address most likely means, or None.

    None means: no suggestion, which is no warning (``standorte/VORGABEN.md``, section 10). A
    suggestion is returned only when

    * the postal code has four digits and the directory is available and not older than 92 days
      (an old copy may lack a new address, the same rule as the notice of the profile, D-066),
    * the directory has exactly one address with the same house number whose street starts with
      or is similar to the typed street within that postal code (the suggestions of the profile,
      D-065), and
    * that address differs from the typed one in the spelling of the street, the house number
      or the town.

    An address that the directory has exactly as typed, an ambiguous answer and an unreachable
    directory all give None.
    """
    street, house_number, postal_code, town = normalise_address(street, house_number, postal_code, town)
    if not street or not town or len(postal_code) != SWISS_POSTAL_CODE_LENGTH or not postal_code.isdigit():
        return None
    try:
        status, _directory = directory_status()
        if status["status"] != "available" or status["stale"]:
            return None
        # The town is left out: the postal code fixes it, and a wrong town is what we correct.
        answer = lookup(
            {"street": street, "house_number": house_number, "postal_code": postal_code,
             "locality": "", "country": "CH"},
            mode="suggest",
        )
    except ValueError:
        return None
    wanted_house_number = fold(house_number)
    candidates = {
        (row["street"], row["house_number"], row["postal_code"], row["locality"])
        for row in answer["results"]
        if fold(row["house_number"]) == wanted_house_number
    }
    if not candidates or (street, house_number, postal_code, town) in candidates or len(candidates) != 1:
        return None
    return AddressSuggestion(*next(iter(candidates)))


def not_found_notice(street, house_number, postal_code, town):
    """The notice that the directory does not know this address, or "" (no notice).

    It is the text of the check of the profile (D-066): given only for a complete address, when
    the directory is available and not old. It never blocks saving."""
    street, house_number, postal_code, town = normalise_address(street, house_number, postal_code, town)
    if not street or not town:
        return ""
    try:
        return lookup(
            {"street": street, "house_number": house_number, "postal_code": postal_code,
             "locality": town, "country": "CH"},
            mode="check",
        )["warning"]
    except ValueError:
        return ""


@dataclass(frozen=True)
class AddressCheck:
    suggestion: AddressSuggestion | None
    not_found_notice: str


def check_address(street, house_number, postal_code, town):
    """The directory check of one address: a suggestion, or else the not-found notice.

    A suggestion explains the deviation, so the not-found notice is not given with it."""
    suggestion = suggest_correction(street, house_number, postal_code, town)
    if suggestion is not None:
        return AddressCheck(suggestion, "")
    return AddressCheck(None, not_found_notice(street, house_number, postal_code, town))


def suggestion_banner_text(suggestion):
    """The sentence of the banner (``standorte/adresskorrektur/VORGABEN.md``)."""
    return gettext(
        "The address differs from the address directory. Suggestion: %(address)s."
    ) % {"address": suggestion.display()}
