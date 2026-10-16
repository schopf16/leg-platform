"""Local-only advisory matching; never mutate or reject a stored profile.

The directory is read from its own SQLite file (D-075; the router sends these models there).
Every query is restricted to the current generation; nothing in this module writes. The searches
use the folded key columns (``leg_platform.addresses.keys``): case and accents are ignored, a prefix
is a range over an indexed key column and ``LIKE`` is never used.
"""

import difflib
import logging
import time

from django.db import DatabaseError
from django.utils.connection import ConnectionDoesNotExist
from django.utils.translation import gettext as _

from .keys import fold, key, prefix_range
from .models import Address, Directory

FIELDS = ("street", "house_number", "postal_code", "locality")
# The order of an answer: the key of the street first, so that the index of the suggestions can
# deliver the rows in this order (models.Address, index addr_generation_street).
ANSWER_ORDER = ("street_key", "street", "house_number", "postal_code", "locality")
LIMITS = {"street": 255, "house_number": 32, "postal_code": 4, "locality": 255, "country": 2}
RESULT_LIMIT = 10
# Approximate matching (D-065). It runs only after the prefix search found nothing and only for a
# complete postal code, so the candidates are the streets of one postal code (the query is an
# equality on the postal code in the index (generation, postal_code, street_key, house_number_key);
# a test asserts the query plan, tests/test_directory.py). Bounds, all hard, and every one fails
# closed (no result):
#   * at most APPROXIMATE_STREET_LIMIT distinct streets are returned by the candidate query (the
#     LIMIT bounds the streets returned, not the rows the database reads for them); if the postal
#     code has more, no approximate result is returned;
#   * the typed street has APPROXIMATE_MIN_LENGTH to APPROXIMATE_MAX_LENGTH characters, before and
#     after folding (folding can shorten or lengthen a text);
#   * a candidate whose folded text is longer than 2 * APPROXIMATE_MAX_LENGTH is not compared, and
#     at most APPROXIMATE_MAX_RATIO_CALLS full comparisons and APPROXIMATE_TIME_BUDGET seconds
#     (monotonic clock) are spent; exceeding either returns nothing;
#   * at most RESULT_LIMIT streets are looked up for addresses, one query each, restricted to one
#     street (and to the house number if typed).
APPROXIMATE_STREET_LIMIT = 1000
APPROXIMATE_MIN_LENGTH = 3
APPROXIMATE_MAX_LENGTH = 64
APPROXIMATE_MAX_RATIO_CALLS = 300
APPROXIMATE_TIME_BUDGET = 0.25
# difflib.SequenceMatcher.ratio() is 2*M/T (M matching characters, T characters of both strings);
# 1.0 means identical. 0.75 is a heuristic, not a standard value: the tests show which typing errors
# it accepts (one dropped, added, wrong or swapped letter in an ordinary street name) and which
# unrelated names it rejects.
APPROXIMATE_THRESHOLD = 0.75
logger = logging.getLogger(__name__)


def clean_input(values):
    cleaned = {}
    for name, limit in LIMITS.items():
        value = values.get(name, "CH" if name == "country" else "")
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid address input.")
        cleaned[name] = value.strip()
    if cleaned["postal_code"] and not cleaned["postal_code"].isascii():
        raise ValueError("Invalid postal code.")
    if cleaned["postal_code"] and not cleaned["postal_code"].isdigit():
        raise ValueError("Invalid postal code.")
    return cleaned


def directory_status():
    """The state of the directory, for the operator page of the administration.

    The ``warning`` is a text for the operator. It is never given to the people who type an address
    (D-066): ``lookup`` leaves it out of its answer.
    """
    result = {
        "status": "unavailable", "data_date": None, "stale": False,
        "warning": _("The address directory is unavailable."),
    }
    try:
        directory = Directory.objects.filter(pk=1).first()
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("Address directory status is unavailable.")
        return result, None
    if directory is None or directory.imported_at is None or directory.row_count == 0:
        result["status"] = "missing"
        return result, None
    result.update(
        status="available",
        data_date=directory.data_date.isoformat() if directory.data_date else None,
        stale=directory.is_stale,
        warning=_("The address directory is more than 92 days old.") if directory.is_stale else "",
    )
    return result, directory


def approximate_matches(query, values):
    """Addresses of streets that are similar to the typed one, within one postal code.

    ``query`` is the directory query of the current generation; ``values`` is clean input with a
    postal code of exactly four digits. Returns ``(rows, kind)``: ``rows`` is a list of at most
    RESULT_LIMIT address dicts and ``kind`` is "prefix" when a street of the postal code starts with
    the typed text (the first search missed it because it also filtered by the town text or by
    accents), otherwise "approximate". Nothing is returned if the postal code has more than
    APPROXIMATE_STREET_LIMIT streets, the typed street is too short or too long (before or after
    folding) or one of the CPU bounds is exceeded. The house number is not used to choose the
    candidate streets; it only restricts the address query of each street.
    """
    typed = values["street"]
    if not APPROXIMATE_MIN_LENGTH <= len(typed) <= APPROXIMATE_MAX_LENGTH:
        return [], "approximate"
    # The folded text can be empty or very short (for example only combining marks): an empty text
    # would be the start of every street and must never be labelled a prefix match.
    folded_typed = fold(typed)
    if not APPROXIMATE_MIN_LENGTH <= len(folded_typed) <= APPROXIMATE_MAX_LENGTH:
        return [], "approximate"
    in_postal_code = query.filter(postal_code=values["postal_code"])
    # One more than the limit tells whether the limit was exceeded; then nothing is matched. No
    # ORDER BY: the candidates are ranked below, and a sort would make SQLite walk the index of the
    # whole directory instead of the rows of the one postal code (measured with 400,000 rows). The
    # shown text of the street is the spelling of the directory.
    streets = list(
        in_postal_code.order_by().values_list("street", flat=True).distinct()
        [:APPROXIMATE_STREET_LIMIT + 1]
    )
    if len(streets) > APPROXIMATE_STREET_LIMIT:
        return [], "approximate"
    matcher = difflib.SequenceMatcher(autojunk=False)
    # SequenceMatcher caches data about the second sequence: set it once, vary the first.
    matcher.set_seq2(folded_typed)
    prefix, similar = [], []
    started, ratio_calls = time.monotonic(), 0
    for street in streets:
        if time.monotonic() - started > APPROXIMATE_TIME_BUDGET:
            return [], "approximate"
        folded = fold(street)
        if len(folded) > 2 * APPROXIMATE_MAX_LENGTH:
            continue
        if folded.startswith(folded_typed):
            prefix.append((0, street))
            continue
        matcher.set_seq1(folded)
        # The two cheap upper bounds avoid the full comparison for most candidates.
        if matcher.real_quick_ratio() < APPROXIMATE_THRESHOLD or matcher.quick_ratio() < APPROXIMATE_THRESHOLD:
            continue
        ratio_calls += 1
        if ratio_calls > APPROXIMATE_MAX_RATIO_CALLS:
            return [], "approximate"
        ratio = matcher.ratio()
        if ratio >= APPROXIMATE_THRESHOLD:
            similar.append((-ratio, street))
    kind = "prefix" if prefix else "approximate"
    ranked = sorted(prefix or similar)[:RESULT_LIMIT]
    rows = []
    for _score, street in ranked:
        remaining = RESULT_LIMIT - len(rows)
        if remaining <= 0:
            break
        # The key of the street narrows the query to the index; the exact spelling keeps two
        # streets that differ only in accents apart.
        per_street = in_postal_code.filter(street_key=key(street), street=street)
        if values["house_number"]:
            per_street = per_street.filter(**prefix_range("house_number_key", key(values["house_number"])))
        rows.extend(per_street.order_by(*ANSWER_ORDER).values(*FIELDS).distinct()[:remaining])
    return rows, kind


def lookup(values, *, mode="suggest"):
    """The JSON answer of the lookup endpoint.

    The ``warning`` is empty except for one case: the directory is available and not old, the mode is
    "check" and it does not know the complete address (D-066). A directory that is missing,
    unreachable, old or failing is not the problem of the person who types, so nothing is said about
    it (it is logged, and shown to the operator in the administration only).
    """
    values = clean_input(values)
    if mode not in {"suggest", "check"}:
        raise ValueError("Invalid lookup mode.")
    if values["country"] != "CH" or not values["street"]:
        return {
            "status": "not_checked", "data_date": None, "stale": False,
            "warning": "", "results": [], "match": None, "kind": "prefix",
        }
    status, directory = directory_status()
    result = {
        "status": status["status"], "data_date": status["data_date"], "stale": status["stale"],
        "warning": "", "results": [], "match": None, "kind": "prefix",
    }
    if directory is None:
        return result
    try:
        query = Address.objects.filter(generation=directory.generation)
        if mode == "check":
            if len(values["postal_code"]) != 4 or not values["locality"]:
                result["match"] = False
            else:
                result["match"] = query.filter(
                    street_key=key(values["street"]), house_number_key=key(values["house_number"]),
                    postal_code=values["postal_code"], locality_key=key(values["locality"]),
                ).exists()
            if not result["match"] and not directory.is_stale:
                # A directory that is more than 92 days old may lack a new address: no notice then.
                result["warning"] = _(
                    "This address was not found. Please check street, "
                    "house number, postal code and town. You can still save it as entered."
                )
        elif len(values["street"]) >= 2 and key(values["street"]):
            # A prefix is a range over the key columns (never LIKE): case and accents are ignored.
            prefix_query = query.filter(**prefix_range("street_key", key(values["street"])))
            postal_code = values["postal_code"]
            if len(postal_code) == 4:
                prefix_query = prefix_query.filter(postal_code=postal_code)
            elif postal_code:
                prefix_query = prefix_query.filter(**prefix_range("postal_code", postal_code))
            if values["house_number"]:
                prefix_query = prefix_query.filter(**prefix_range("house_number_key", key(values["house_number"])))
            if values["locality"]:
                prefix_query = prefix_query.filter(**prefix_range("locality_key", key(values["locality"])))
            result["results"] = list(
                prefix_query.order_by(*ANSWER_ORDER).values(*FIELDS).distinct()[:RESULT_LIMIT]
            )
            # Only a complete postal code bounds the candidates (D-065).
            if not result["results"] and len(values["postal_code"]) == 4:
                rows, kind = approximate_matches(query, values)
                if rows:
                    result["results"], result["kind"] = rows, kind
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("Address directory lookup is unavailable.")
        result.update(status="unavailable", results=[], match=None, kind="prefix", warning="")
    return result
