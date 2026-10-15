"""Local-only advisory matching; never mutate or reject a stored profile."""

import difflib
import logging
import time
import unicodedata

from django.db import DatabaseError
from django.utils.connection import ConnectionDoesNotExist
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from .models import Address, Directory, READ_ALIAS

ATTRIBUTION = gettext_lazy("Federal Office of Topography swisstopo")
FIELDS = ("street", "house_number", "postal_code", "locality")
LIMITS = {"street": 255, "house_number": 32, "postal_code": 4, "locality": 255, "country": 2}
RESULT_LIMIT = 10
# Approximate matching (D-065). It runs only after the prefix search found nothing and only for a
# complete postal code, so the candidates are the streets of one postal code (the query is
# restricted by the index (generation, postal_code, street); whether the database really uses it is
# not verified, see docs/DEPLOYMENT.md). Bounds, all hard, and every one fails closed (no result):
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


def fold(text):
    """Lower case without accents, for comparing street names only (never stored or shown)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


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
    result = {
        "status": "unavailable", "data_date": None, "stale": False,
        "warning": _("The address directory is unavailable. You can continue with the address as entered."),
        "attribution": ATTRIBUTION,
    }
    try:
        directory = Directory.objects.using(READ_ALIAS).filter(pk=1).first()
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
        warning=_("The address directory is more than 92 days old. You can continue with the address as entered.")
        if directory.is_stale else "",
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
    # One more than the limit tells whether the limit was exceeded; then nothing is matched.
    streets = list(
        in_postal_code.order_by("street").values_list("street", flat=True).distinct()
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
        per_street = in_postal_code.filter(street=street)
        if values["house_number"]:
            per_street = per_street.filter(house_number__istartswith=values["house_number"])
        rows.extend(per_street.values(*FIELDS).order_by(*FIELDS).distinct()[:remaining])
    return rows, kind


def lookup(values, *, mode="suggest"):
    values = clean_input(values)
    if mode not in {"suggest", "check"}:
        raise ValueError("Invalid lookup mode.")
    if values["country"] != "CH" or not values["street"]:
        return {
            "status": "not_checked", "data_date": None, "stale": False,
            "warning": "", "attribution": ATTRIBUTION, "results": [], "match": None,
            "kind": "prefix",
        }
    result, directory = directory_status()
    result.update(results=[], match=None, kind="prefix")
    if directory is None:
        return result
    try:
        query = Address.objects.using(READ_ALIAS).filter(generation=directory.generation)
        if mode == "check":
            if len(values["postal_code"]) != 4 or not values["locality"]:
                result["match"] = False
            else:
                result["match"] = query.filter(
                    street__iexact=values["street"], house_number__iexact=values["house_number"],
                    postal_code=values["postal_code"], locality__iexact=values["locality"],
                ).exists()
            if not result["match"]:
                result["warning"] = _(
                    "This address was not found in the official directory. Please check street, "
                    "house number, postal code and town. You can still save it as entered."
                )
        elif len(values["street"]) >= 2:
            prefix_query = query.filter(street__istartswith=values["street"])
            if values["postal_code"]:
                prefix_query = prefix_query.filter(postal_code__startswith=values["postal_code"])
            if values["house_number"]:
                prefix_query = prefix_query.filter(house_number__istartswith=values["house_number"])
            if values["locality"]:
                prefix_query = prefix_query.filter(locality__istartswith=values["locality"])
            result["results"] = list(prefix_query.values(*FIELDS).order_by(*FIELDS).distinct()[:RESULT_LIMIT])
            # Only a complete postal code bounds the candidates (D-065).
            if not result["results"] and len(values["postal_code"]) == 4:
                rows, kind = approximate_matches(query, values)
                if rows:
                    result["results"], result["kind"] = rows, kind
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("Address directory lookup is unavailable.")
        result.update(
            status="unavailable", results=[], match=None, kind="prefix",
            warning=_("The address directory is unavailable. You can continue with the address as entered."),
        )
    return result


def check_address(*, street="", house_number="", postal_code="", locality="", country="CH"):
    """Form integration: return a warning dict, not a ValidationError.

    Call only after the host form's normal validation. Do not alter cleaned_data
    or add_error(); display the returned warning even if the profile is saved.
    """
    try:
        return lookup({
            "street": street, "house_number": house_number,
            "postal_code": str(postal_code or ""), "locality": locality,
            "country": str(country),
        }, mode="check")
    except ValueError:
        return {
            "status": "not_checked", "match": None, "results": [], "kind": "prefix",
            "data_date": None, "stale": False, "attribution": ATTRIBUTION,
            "warning": _("The address could not be checked. You can continue with the address as entered."),
        }
