"""Search keys of the address directory (D-075).

SQLite compares text in binary order and its ``LIKE`` ignores case only for ASCII, so the
directory does not rely on ``LIKE`` or on a collation. Every searched text has a key column with
the folded text: lower case without accents. The prefix searches are ranges over these key columns
(``key >= prefix AND key < prefix + MAX_CHARACTER``), which an index can serve; the texts shown to
the people stay the spelling of the directory.
"""

import unicodedata
from functools import lru_cache

# The largest code point: every text that starts with the prefix is below prefix + this character
# (UTF-8 keeps the code point order in a binary comparison).
MAX_CHARACTER = "\U0010ffff"
# The key columns are not longer than the columns of the texts; a longer key (casefolding can
# lengthen a text) is cut, which only affects a street that is already unusually long.
KEY_LIMIT = 255


def fold(text):
    """Lower case without accents, for comparing and searching (never shown)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


@lru_cache(maxsize=1 << 17)
def key(text):
    """The key of a stored text; cached, because streets and localities repeat a lot."""
    return fold(text)[:KEY_LIMIT]


def prefix_range(field, prefix):
    """Query keyword arguments that select the rows whose key column starts with ``prefix``."""
    return {f"{field}__gte": prefix, f"{field}__lt": prefix + MAX_CHARACTER}
