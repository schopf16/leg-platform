"""Folded search keys of the address directory (D-075): lower case, no accents.

Prefix searches are index ranges over the key columns (``LIKE`` is not used: SQLite ignores case only for ASCII).
"""

import unicodedata
from functools import lru_cache

# Largest code point: every text starting with the prefix is below prefix + this character.
MAX_CHARACTER = "\U0010ffff"
# Column length; a longer key (casefolding can lengthen) is cut.
KEY_LIMIT = 255


def fold(text):
    """Lower case without accents, for comparing and searching (never shown)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


@lru_cache(maxsize=1 << 17)
def key(text):
    """The key of a stored text (cached: streets and localities repeat)."""
    return fold(text)[:KEY_LIMIT]


def prefix_range(field, prefix):
    """Query keyword arguments that select the rows whose key column starts with ``prefix``."""
    return {f"{field}__gte": prefix, f"{field}__lt": prefix + MAX_CHARACTER}
