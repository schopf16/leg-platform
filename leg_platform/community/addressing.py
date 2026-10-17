"""Addresses of the community domain: normalisation, uniqueness key and the directory suggestion (D-033, D-065, D-075).

Comparison uses the folded text of ``addresses.keys``; the directory is read through ``addresses.services``.
A suggestion is only advice: nothing here writes to the directory or changes a stored address.
"""

import hashlib
from dataclasses import dataclass

from django.utils.translation import gettext

from leg_platform.addresses.keys import fold
from leg_platform.addresses.services import directory_status, lookup

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
    """The text that makes an address unique: the four parts, folded, joined with a line break."""
    return "\n".join(fold(part) for part in normalise_address(street, house_number, postal_code, town))


def address_digest(street, house_number, postal_code, town):
    """SHA-256 hex of ``address_key``: the stored unique form (short unique index, MariaDB W003)."""
    return hashlib.sha256(address_key(street, house_number, postal_code, town).encode()).hexdigest()


def address_signature(street, house_number, postal_code, town):
    """The four parts as typed (white space normalised): the memory of a dismissed suggestion."""
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
    """The directory address this address most likely means, or None (no suggestion, no warning).

    Only when the postal code has four digits, the directory is available and not older than 92 days
    (D-066), exactly one directory address has the same house number and a similar street within the
    postal code (D-065), and it differs from the typed one. Exact matches, ambiguity and an
    unreachable directory give None."""
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
    if not candidates or len(candidates) != 1 or (street, house_number, postal_code, town) in candidates:
        return None
    return AddressSuggestion(*next(iter(candidates)))


def not_found_notice(street, house_number, postal_code, town):
    """The notice that the directory does not know this address, or "": only for a complete address,
    never blocks saving (D-066)."""
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
    """A suggestion, or else the not-found notice (a suggestion already explains the deviation)."""
    suggestion = suggest_correction(street, house_number, postal_code, town)
    if suggestion is not None:
        return AddressCheck(suggestion, "")
    return AddressCheck(None, not_found_notice(street, house_number, postal_code, town))


def suggestion_banner_text(suggestion):
    """The sentence of the banner."""
    return gettext(
        "The address differs from the address directory. Suggestion: %(address)s."
    ) % {"address": suggestion.display()}
