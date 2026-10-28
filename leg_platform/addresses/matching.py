"""What the lookup (``services``) and the fixes (``fixes``) share: the typed-field rules, the home area and the search
for streets that look like a typed one (D-111, D-065). Nothing here writes."""

import difflib
import time

from django.db.models import Q

from .keys import fold, key

FIELDS = ("street", "house_number", "postal_code", "locality")
POSTAL_CODE_LENGTH = 4
# The typing suggestions of the forms start at three characters; ``lookup`` keeps the old two for the server-side
# advice of the community domain (``minimum``).
TYPEAHEAD_MINIMUM = 3
# Approximate matching runs only after the prefix search found nothing, for a complete postal code or a town. The
# streets come from the small street table, so the bounds only protect against abuse and fail closed (no result): at
# most APPROXIMATE_STREET_LIMIT streets; a typed street of APPROXIMATE_MIN/MAX_LENGTH characters before and after
# folding; candidates longer than 2 * MAX are skipped; at most APPROXIMATE_MAX_RATIO_CALLS full comparisons and, per
# search, a deadline. SIMILAR_CAP streets are kept from one search (a prefix like "Rue" matches thousands).
APPROXIMATE_STREET_LIMIT = 20_000
APPROXIMATE_MIN_LENGTH = 3
APPROXIMATE_MAX_LENGTH = 64
APPROXIMATE_MAX_RATIO_CALLS = 300
SIMILAR_CAP = 200
# SequenceMatcher.ratio() threshold: a heuristic, calibrated by the tests.
APPROXIMATE_THRESHOLD = 0.75


def budget_allows(approximate):
    """Whether the approximate search may run: ``approximate`` is True, False or a function that is called only now
    (so a budget is counted only for a search that happens) and says whether it may."""
    return bool(approximate() if callable(approximate) else approximate)


def similar_streets(places, typed, deadline):
    """``(streets, kind, bounded_out)``: the streets of ``places`` (a query of ``AddressStreet`` already bounded by a
    postal code, a town or the start of a street) that start like ``typed`` or look like it, best first. ``bounded_out``
    says that a bound was exceeded (APPROXIMATE_STREET_LIMIT streets, the comparisons, or the ``deadline`` of
    ``time.monotonic()``): nothing is returned then (fail closed)."""
    if not APPROXIMATE_MIN_LENGTH <= len(typed) <= APPROXIMATE_MAX_LENGTH:
        return [], "approximate", False
    # An empty folded text (only combining marks) would be a prefix of every street.
    folded_typed = fold(typed)
    if not APPROXIMATE_MIN_LENGTH <= len(folded_typed) <= APPROXIMATE_MAX_LENGTH:
        return [], "approximate", False
    streets = list(places.order_by().values_list("street", flat=True).distinct()[:APPROXIMATE_STREET_LIMIT + 1])
    if len(streets) > APPROXIMATE_STREET_LIMIT:
        return [], "approximate", True
    matcher = difflib.SequenceMatcher(autojunk=False)
    # SequenceMatcher caches data about seq2: set it once.
    matcher.set_seq2(folded_typed)
    prefix, similar = [], []
    ratio_calls = 0
    for street in streets:
        if time.monotonic() > deadline:
            return [], "approximate", True
        folded = fold(street)
        if len(folded) > 2 * APPROXIMATE_MAX_LENGTH:
            continue
        if folded.startswith(folded_typed):
            prefix.append((0, street))
            continue
        matcher.set_seq1(folded)
        # Two cheap upper bounds skip most full comparisons.
        if matcher.real_quick_ratio() < APPROXIMATE_THRESHOLD or matcher.quick_ratio() < APPROXIMATE_THRESHOLD:
            continue
        ratio_calls += 1
        if ratio_calls > APPROXIMATE_MAX_RATIO_CALLS:
            return [], "approximate", True
        ratio = matcher.ratio()
        if ratio >= APPROXIMATE_THRESHOLD:
            similar.append((-ratio, street))
    kind = "prefix" if prefix else "approximate"
    return [street for _score, street in sorted(prefix or similar)][:SIMILAR_CAP], kind, False


def home_filter(home):
    """The ``Q`` of the home area (the LEG's own postal code and town) or None while none is known (D-111).

    The directory holds the postal locality, not the political municipality (D-049: ``COM_NAME`` is not stored), so the
    area is what the LEG's own address names: its postal code and its town."""
    postal_code, town = home or ("", "")
    condition = None
    if len(postal_code) == POSTAL_CODE_LENGTH and postal_code.isascii() and postal_code.isdigit():
        condition = Q(postal_code=postal_code)
    if key(town or ""):
        by_town = Q(locality_key=key(town))
        condition = by_town if condition is None else condition | by_town
    return condition


def in_home(row, home):
    """Whether an answer row (a dict with ``postal_code`` and ``locality``) lies in the home area."""
    postal_code, town = home or ("", "")
    return bool(
        (postal_code and row["postal_code"] == postal_code) or (key(town or "") and key(row["locality"]) == key(town))
    )
