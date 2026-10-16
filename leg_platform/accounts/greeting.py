"""The greeting of the security notifications (D-068, R-13).

A notification to an address of an account (``MailJob.NOTIFICATION_KINDS``) greets "Guten Tag
First Last" when the account belongs to a person with a suitable name, and "Guten Tag" in every
other case. The name is free text typed by a person, so it passes a guard before it is stored in
a mail job or put in an email:

* NFKC normalisation; control, format, surrogate, private-use, unassigned, line and paragraph
  separator characters are removed (so no line break or zero-width character survives);
  whitespace is collapsed to single spaces;
* allow-list: letters, combining marks, space, hyphen-minus, apostrophe (' or the typographic
  apostrophe) and period; any other character (digits, ``@``, ``:``, ``/``, ``<``, ``%``,
  emoji, ...) gives the neutral greeting, not a name with the character cut out;
* a combining mark is allowed only directly after a letter or another mark, at most three in a
  row; invisible marks (variation selectors, the combining grapheme joiner) and the invisible
  Hangul fillers are removed; after the removal the text is normalised again, so the result is
  a fixed point of the guard;
* a name that looks like a web or email address, also with all combining marks removed, gives
  the neutral greeting;
* a name longer than ``GREETING_NAME_MAX_LENGTH`` gives the neutral greeting.

Only a person with a first and a last name is greeted by name; a company name is never used. The
name is cleaned once, when the job is queued (``mail_queue.enqueue_for_account``), and only for an
address that belongs to the account at that time; ``render`` checks the stored name again. The
functions never log a name or a greeting.
"""

import unicodedata

from django.utils.translation import gettext

from leg_platform.accounts.email_texts import looks_like_an_address
from leg_platform.accounts.models import GREETING_NAME_MAX_LENGTH

# The name that the preview of an email text in the administration uses (D-068).
EXAMPLE_NAME = "Maria Muster"

_REMOVED_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
_ALLOWED_PUNCTUATION = {" ", "-", "'", "\u2019", "."}
# The most combining marks that may follow one letter (Latin and other scripts use one to three).
_MAX_CONSECUTIVE_MARKS = 3
# Combining marks (category Mn) that show nothing: the variation selectors and the combining
# grapheme joiner. They are removed like the format characters.
_INVISIBLE_MARKS = (
    set(range(0xFE00, 0xFE10)) | set(range(0xE0100, 0xE01F0)) | {0x034F}
)
# The Hangul fillers (category Lo) show nothing; NFKC maps U+3164 and U+FFA0 to U+1160. They are
# removed, so a part that has only fillers has no letter left.
_INVISIBLE_LETTERS = {0x115F, 0x1160, 0x3164, 0xFFA0}


def _is_removed(char):
    return (
        unicodedata.category(char) in _REMOVED_CATEGORIES
        or ord(char) in _INVISIBLE_MARKS
        or ord(char) in _INVISIBLE_LETTERS
    )


def _normalised(text):
    """Normalise (NFKC), remove the invisible characters and collapse the whitespace.

    Removing a character can leave text that is no longer in NFKC (a letter, a removed character
    and a mark: the mark now follows the letter and composes with it), and normalising can
    produce a character that is removed. So this repeats until nothing changes: the result is a
    fixed point, which ``is_clean`` relies on.
    """
    while True:
        result = unicodedata.normalize("NFKC", text)
        result = "".join(char for char in result if not _is_removed(char))
        # str.split() splits at every Unicode whitespace character, so this also collapses the
        # separators (category Zs) that NFKC leaves.
        result = " ".join(result.split())
        if result == text:
            return result
        text = result


def _without_marks(text):
    """Return the text without any combining mark (NFKC first), for the address check."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(char for char in text if not unicodedata.category(char).startswith("M"))


def _looks_like_an_address(text):
    """Whether the text, or the text without its combining marks, looks like an address.

    A mark inside a domain name splits the run of letters that the address check looks for, and
    NFKC cannot always join it again, so the check also runs without the marks.
    """
    return looks_like_an_address(text) or looks_like_an_address(_without_marks(text))


def _clean_part(text):
    """Return one cleaned name part, or "" if it has a character that a name does not have."""
    text = _normalised(text or "")
    previous_is_letter_or_mark = False
    marks = 0
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("M"):
            # A mark belongs to the letter before it: after a space, a hyphen or at the start
            # it would attach to something that is no letter.
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
    """Whether a stored name is a fixed point of the guard (it is what ``clean_name`` returns).

    ``render`` runs this on the name of a job, so a name that was written around the queue (a
    data fix, a future caller) gives the neutral greeting instead of reaching the email.
    """
    return bool(
        isinstance(name, str)
        and name
        and len(name) <= GREETING_NAME_MAX_LENGTH
        and " " in name
        and _clean_part(name) == name
        and not _looks_like_an_address(name)
    )


def name_for(user):
    """Return the cleaned name of the person the account belongs to, or "" (neutral).

    A company name is never used: the reader is a person, and a company name next to a person's
    name is ignored.
    """
    return clean_name(user.first_name, user.last_name)


def render(name):
    """Return the greeting for a cleaned name ("" gives the neutral greeting).

    Built from two short catalogue texts, without a comma, like the greeting in the emails before
    this change. Must run with the language of the email active. The name is checked again here
    (``is_clean``): anything the guard would not have produced gives the neutral greeting.
    """
    if is_clean(name):
        return gettext("Hello %(name)s") % {"name": name}
    return gettext("Hello")
