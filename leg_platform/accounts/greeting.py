"""The greeting of the security notifications (D-068).

A notification greets "Hello First Last" when the account belongs to a person with a suitable
name, else a neutral "Hello". The name is free text, so it passes a guard before it is stored in a
mail job or put in an email: NFKC; control/format/surrogate/private-use/unassigned/separator
characters and invisible marks and Hangul fillers removed; whitespace collapsed; allow-list of
letters, combining marks (only after a letter, at most three), space, hyphen, apostrophe and period
(anything else gives the neutral greeting, not a name with the character cut out); a name that
looks like a web or email address (also without marks) or is longer than
``GREETING_NAME_MAX_LENGTH`` gives the neutral greeting. The result is a fixed point of the guard.

Only a person with first and last name is greeted; a company name is never used. The name is
cleaned once at queueing (``mail_queue.enqueue_for_account``) and re-checked by ``render``. Nothing
here logs a name or greeting.
"""

import unicodedata

from django.utils.translation import gettext

from leg_platform.accounts.email_texts import looks_like_an_address
from leg_platform.accounts.models import GREETING_NAME_MAX_LENGTH

# The name that the preview of an email text in the administration uses (D-068).
EXAMPLE_NAME = "Maria Muster"

_REMOVED_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
_ALLOWED_PUNCTUATION = {" ", "-", "'", "\u2019", "."}
# The most combining marks that may follow one letter.
_MAX_CONSECUTIVE_MARKS = 3
# Combining marks (Mn) that show nothing (variation selectors, combining grapheme joiner): removed like format characters.
_INVISIBLE_MARKS = (
    set(range(0xFE00, 0xFE10)) | set(range(0xE0100, 0xE01F0)) | {0x034F}
)
# Hangul fillers (Lo) show nothing (NFKC maps U+3164 and U+FFA0 to U+1160): removed.
_INVISIBLE_LETTERS = {0x115F, 0x1160, 0x3164, 0xFFA0}


def _is_removed(char):
    return (
        unicodedata.category(char) in _REMOVED_CATEGORIES
        or ord(char) in _INVISIBLE_MARKS
        or ord(char) in _INVISIBLE_LETTERS
    )


def _normalised(text):
    """Normalise (NFKC), remove the invisible characters and collapse the whitespace.

    Repeats until nothing changes (removing a character can leave non-NFKC text, and normalising
    can produce a removable character): the result is a fixed point, which ``is_clean`` relies on.
    """
    while True:
        result = unicodedata.normalize("NFKC", text)
        result = "".join(char for char in result if not _is_removed(char))
        # str.split() splits at every Unicode whitespace character, also the Zs that NFKC leaves.
        result = " ".join(result.split())
        if result == text:
            return result
        text = result


def _without_marks(text):
    """Return the text without any combining mark (NFKC first), for the address check."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(char for char in text if not unicodedata.category(char).startswith("M"))


def _looks_like_an_address(text):
    """Whether the text, or the text without its combining marks, looks like an address (a mark inside a domain splits the letter run)."""
    return looks_like_an_address(text) or looks_like_an_address(_without_marks(text))


def _clean_part(text):
    """Return one cleaned name part, or "" if it has a character that a name does not have."""
    text = _normalised(text or "")
    previous_is_letter_or_mark = False
    marks = 0
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("M"):
            # A mark belongs to the letter before it.
            marks += 1
            if not previous_is_letter_or_mark or marks > _MAX_CONSECUTIVE_MARKS:
                return ""
            continue
        marks = 0
        if category.startswith("L"):
            previous_is_letter_or_mark = True
        elif char in _ALLOWED_PUNCTUATION:
            previous_is_letter_or_mark = False
        else:
            return ""
    # A part of only dots or hyphens is no name.
    if not any(unicodedata.category(char).startswith("L") for char in text):
        return ""
    return text


def clean_name(first_name, last_name):
    """Return "First Last" for the greeting, or "" for the neutral greeting."""
    first = _clean_part(first_name)
    last = _clean_part(last_name)
    if not first or not last:
        return ""
    name = f"{first} {last}"
    if len(name) > GREETING_NAME_MAX_LENGTH:
        return ""
    if any(_looks_like_an_address(text) for text in (first, last, name)):
        return ""
    return name


def is_clean(name):
    """Whether a stored name is a fixed point of the guard; ``render`` runs this so a name written around the queue gives the neutral greeting."""
    return bool(
        isinstance(name, str)
        and name
        and len(name) <= GREETING_NAME_MAX_LENGTH
        and " " in name
        and _clean_part(name) == name
        and not _looks_like_an_address(name)
    )


def name_for(user):
    """Return the cleaned name of the person the account belongs to, or "" (neutral); a company name is never used."""
    return clean_name(user.first_name, user.last_name)


def render(name):
    """Return the greeting for a cleaned name ("" gives the neutral greeting); run with the email's language active.

    The name is re-checked (``is_clean``): anything the guard would not have produced gives the neutral greeting.
    """
    if is_clean(name):
        return gettext("Hello %(name)s") % {"name": name}
    return gettext("Hello")
