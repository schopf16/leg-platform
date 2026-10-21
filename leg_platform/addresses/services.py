"""Local-only advisory address matching on the current generation of the directory (D-075, D-065).

Nothing here writes. Searches use the folded key columns (``keys``): prefix = range over an
indexed key column, never ``LIKE``.
"""

import difflib
import logging
import time

from django.db import DatabaseError
from django.db.models import Q
from django.utils.connection import ConnectionDoesNotExist
from django.utils.translation import gettext as _

from .keys import fold, key, prefix_range
from .models import Address, Directory

FIELDS = ("street", "house_number", "postal_code", "locality")
# Street key first, so the index addr_generation_street delivers the rows in this order.
ANSWER_ORDER = ("street_key", "street", "house_number", "postal_code", "locality")
# Town first, so the index addr_generation_locality delivers the rows in this order (D-111).
LOCALITY_FIELDS = ("postal_code", "locality")
LOCALITY_ORDER = ("locality_key", "locality", "postal_code")
LIMITS = {"street": 255, "house_number": 32, "postal_code": 4, "locality": 255, "country": 2}
MODES = {"suggest", "check", "locality"}
RESULT_LIMIT = 10
# The typing suggestions of the forms (D-111) start at three characters; ``lookup`` keeps the old two for the
# server-side advice of the community domain (``minimum``).
TYPEAHEAD_MINIMUM = 3
POSTAL_CODE_LENGTH = 4
# Approximate matching (D-065) runs only after the prefix search found nothing, for a complete
# postal code only; every bound fails closed (no result): at most APPROXIMATE_STREET_LIMIT streets
# in the postal code; typed street of APPROXIMATE_MIN/MAX_LENGTH characters before and after
# folding; candidates longer than 2 * MAX are skipped; at most APPROXIMATE_MAX_RATIO_CALLS full
# comparisons and APPROXIMATE_TIME_BUDGET seconds.
APPROXIMATE_STREET_LIMIT = 1000
APPROXIMATE_MIN_LENGTH = 3
APPROXIMATE_MAX_LENGTH = 64
APPROXIMATE_MAX_RATIO_CALLS = 300
APPROXIMATE_TIME_BUDGET = 0.25
# SequenceMatcher.ratio() threshold: a heuristic, calibrated by the tests.
APPROXIMATE_THRESHOLD = 0.75
logger = logging.getLogger(__name__)


def clean_input(values):
    cleaned = {}
    for name, limit in LIMITS.items():
        value = values.get(name, "CH" if name == "country" else "")
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid address input.")
        cleaned[name] = value.strip()
    postal_code = cleaned["postal_code"]
    if postal_code and not (postal_code.isascii() and postal_code.isdigit()):
        raise ValueError("Invalid postal code.")
    return cleaned


def directory_status():
    """``(status dict, Directory or None)`` for the operator page; the ``warning`` is never given to typing users (D-066)."""
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
    """Addresses of streets similar to the typed one within one postal code (``values`` has a 4-digit postal code).

    Returns ``(rows, kind)``: at most RESULT_LIMIT address dicts; ``kind`` is "prefix" when a street
    of the postal code starts with the typed text, else "approximate". Nothing is returned when a
    bound is exceeded. The house number only restricts the per-street address query.
    """
    typed = values["street"]
    if not APPROXIMATE_MIN_LENGTH <= len(typed) <= APPROXIMATE_MAX_LENGTH:
        return [], "approximate"
    # An empty folded text (only combining marks) would be a prefix of every street.
    folded_typed = fold(typed)
    if not APPROXIMATE_MIN_LENGTH <= len(folded_typed) <= APPROXIMATE_MAX_LENGTH:
        return [], "approximate"
    in_postal_code = query.filter(postal_code=values["postal_code"])
    # One more than the limit detects overflow. No ORDER BY: a sort makes SQLite walk the index of
    # the whole directory instead of one postal code.
    streets = list(
        in_postal_code.order_by().values_list("street", flat=True).distinct()
        [:APPROXIMATE_STREET_LIMIT + 1]
    )
    if len(streets) > APPROXIMATE_STREET_LIMIT:
        return [], "approximate"
    matcher = difflib.SequenceMatcher(autojunk=False)
    # SequenceMatcher caches data about seq2: set it once.
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
        # Two cheap upper bounds skip most full comparisons.
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
        # The street key narrows to the index; the exact spelling separates accent variants.
        per_street = in_postal_code.filter(street_key=key(street), street=street)
        if values["house_number"]:
            per_street = per_street.filter(**prefix_range("house_number_key", key(values["house_number"])))
        rows.extend(per_street.order_by(*ANSWER_ORDER).values(*FIELDS).distinct()[:remaining])
    return rows, kind


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


def ranked_rows(query, home, *, order, columns, limit=RESULT_LIMIT):
    """At most ``limit`` distinct rows: those of the home area first, then all others (D-111); each part in ``order``.

    Two index-ordered queries instead of one sort, so the cost stays that of the plain prefix search."""
    condition = home_filter(home)
    if condition is None:
        return list(query.order_by(*order).values(*columns).distinct()[:limit])
    rows = list(query.filter(condition).order_by(*order).values(*columns).distinct()[:limit])
    if len(rows) < limit:
        rows += list(query.exclude(condition).order_by(*order).values(*columns).distinct()[:limit - len(rows)])
    return rows


def locality_matches(query, values, home):
    """Towns and postal codes for the typed postal code and/or town (D-111).

    A complete postal code gives its towns (a town typed besides narrows them); without one, the typed town (at
    least ``TYPEAHEAD_MINIMUM`` characters) gives its postal codes, narrowed by a typed part of the postal code. The home
    area comes first, then all others. Nothing else is returned."""
    postal_code, typed = values["postal_code"], key(values["locality"])
    if len(postal_code) == POSTAL_CODE_LENGTH:
        query = query.filter(postal_code=postal_code)
        if typed:
            query = query.filter(**prefix_range("locality_key", typed))
    elif len(typed) >= TYPEAHEAD_MINIMUM:
        query = query.filter(**prefix_range("locality_key", typed))
        if postal_code:
            query = query.filter(**prefix_range("postal_code", postal_code))
    else:
        return []
    return ranked_rows(query, home, order=LOCALITY_ORDER, columns=LOCALITY_FIELDS)


def lookup(values, *, mode="suggest", home=None, minimum=2):
    """The JSON answer of the lookup endpoint.

    ``warning`` is non-empty only in mode "check" when a current directory does not know the
    complete address (D-066); directory problems are logged and shown to the operator only.
    Mode "locality" (D-111) answers ``results`` of ``{"postal_code", "locality"}`` for the typed postal code or town.
    ``home`` is ``(postal code, town)`` of the LEG: its addresses come first. ``minimum`` is the shortest street
    or town that is searched.
    """
    values = clean_input(values)
    if mode not in MODES:
        raise ValueError("Invalid lookup mode.")
    if mode == "locality":
        asked = len(values["postal_code"]) == POSTAL_CODE_LENGTH or len(key(values["locality"])) >= TYPEAHEAD_MINIMUM
    else:
        asked = bool(values["street"])
    if values["country"] != "CH" or not asked:
        return {
            "status": "not_checked", "data_date": None, "stale": False,
            "warning": "", "results": [], "match": None, "kind": "prefix",
        }
    status, directory = directory_status()
    result = {
        "status": status["status"], "data_date": status["data_date"], "stale": status["stale"],
        "warning": "", "results": [], "match": None, "kind": "locality" if mode == "locality" else "prefix",
    }
    if directory is None:
        return result
    try:
        query = Address.objects.filter(generation=directory.generation)
        if mode == "locality":
            result["results"] = locality_matches(query, values, home)
        elif mode == "check":
            if len(values["postal_code"]) != 4 or not values["locality"]:
                result["match"] = False
            else:
                result["match"] = query.filter(
                    street_key=key(values["street"]), house_number_key=key(values["house_number"]),
                    postal_code=values["postal_code"], locality_key=key(values["locality"]),
                ).exists()
            if not result["match"] and not directory.is_stale:
                # An old directory may lack a new address: no notice then.
                result["warning"] = _(
                    "This address was not found. Please check street, "
                    "house number, postal code and town. You can still save it as entered."
                )
        elif len(values["street"]) >= minimum and key(values["street"]):
            street_query = query.filter(**prefix_range("street_key", key(values["street"])))
            if values["house_number"]:
                street_query = street_query.filter(
                    **prefix_range("house_number_key", key(values["house_number"]))
                )
            prefix_query = street_query
            postal_code = values["postal_code"]
            if len(postal_code) == 4:
                prefix_query = prefix_query.filter(postal_code=postal_code)
            elif postal_code:
                prefix_query = prefix_query.filter(**prefix_range("postal_code", postal_code))
            if values["locality"]:
                prefix_query = prefix_query.filter(**prefix_range("locality_key", key(values["locality"])))
            result["results"] = ranked_rows(prefix_query, home, order=ANSWER_ORDER, columns=FIELDS)
            # Only a complete postal code bounds the candidates.
            if not result["results"] and len(values["postal_code"]) == 4:
                rows, kind = approximate_matches(query, values)
                if rows:
                    result["results"], result["kind"] = sorted(rows, key=lambda row: not in_home(row, home)), kind
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("Address directory lookup is unavailable.")
        result.update(status="unavailable", results=[], match=None, kind="prefix", warning="")
    return result
