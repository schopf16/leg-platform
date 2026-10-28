"""Local-only advisory address matching on the current generation of the directory (D-075, D-065).

Nothing here writes. Searches use the folded key columns (``keys``): prefix = range over an
indexed key column, never ``LIKE``.
"""

import logging
import re
import time

from django.db import DatabaseError
from django.utils.connection import ConnectionDoesNotExist
from django.utils.translation import gettext as _

from .fixes import fixes
from .keys import key, prefix_range
from .matching import (
    APPROXIMATE_MAX_LENGTH,
    APPROXIMATE_MIN_LENGTH,
    FIELDS,
    POSTAL_CODE_LENGTH,
    TYPEAHEAD_MINIMUM,
    budget_allows,
    home_filter,
    in_home,
    similar_streets,
)
from .models import Address, AddressPlace, AddressStreet, Directory

# Street key first, so the index addr_generation_street delivers the rows in this order.
ANSWER_ORDER = ("street_key", "street", "house_number", "postal_code", "locality")
# With a house number: the covering index addr_house (street key, house number key, postal code, town key) delivers
# them.
HOUSE_ORDER = ("street_key", "house_number_key", "postal_code", "locality_key")
# Town first, so the index addr_generation_locality delivers the rows in this order (D-111).
LOCALITY_FIELDS = ("postal_code", "locality")
LOCALITY_ORDER = ("locality_key", "locality", "postal_code")
LIMITS = {"street": 255, "house_number": 32, "postal_code": 4, "locality": 255, "country": 2}
MODES = {"suggest", "check", "locality"}
RESULT_LIMIT = 10
# A house number at the end of the street field: a digit first, then letters, digits and the separators that house
# numbers of the directory use ("5", "5a", "12-14", "7.1", "3/4"). The street part must keep a non-space character.
HOUSE_NUMBER_TAIL = re.compile(r"^(?P<street>.*\S)\s+(?P<house_number>\d[\w./,+()-]*)$")
# The approximate street search of one lookup stops after this many seconds (the other bounds: ``matching``).
APPROXIMATE_TIME_BUDGET = 0.25
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
    """``(status dict, Directory or None)`` for the operator page; the ``warning`` is never given to typing users
    (D-066)."""
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


def generation_of(query):
    """The generation a query of addresses is restricted to (one indexed row)."""
    return next(iter(query.order_by().values_list("generation", flat=True)[:1]), None)


def approximate_matches(query, values, *, limit=RESULT_LIMIT, town=None, deadline=None, generation=None):
    """Addresses of streets similar to the typed one within one postal code (``values`` has a 4-digit postal code) or,
    with ``town``, within that town.

    Returns ``(rows, kind)``: at most ``limit`` (default RESULT_LIMIT) address dicts; ``kind`` is "prefix" when a street
    of the place starts with the typed text (not a typing mistake), else "approximate". A typed house
    number only restricts the per-street address query. ``deadline`` is a time of ``time.monotonic()``; the default is
    APPROXIMATE_TIME_BUDGET from now. The streets come from ``AddressStreet``, the addresses of the best from ``query``.
    """
    generation = generation or generation_of(query)
    where = {"locality_key": key(town)} if town else {"postal_code": values["postal_code"]}
    if deadline is None:
        deadline = time.monotonic() + APPROXIMATE_TIME_BUDGET
    streets, kind, _bounded_out = similar_streets(
        AddressStreet.objects.filter(generation=generation, **where), values["street"], deadline
    )
    rows = []
    for street in streets[:limit]:
        remaining = limit - len(rows)
        if remaining <= 0:
            break
        per_street = query.filter(street_key=key(street), street=street, **where)
        if values["house_number"]:
            per_street = per_street.filter(**prefix_range("house_number_key", key(values["house_number"])))
        rows.extend(per_street.order_by(*ANSWER_ORDER).values(*FIELDS).distinct()[:remaining])
    return rows, kind


def ranked_rows(query, home, *, order, columns, limit=RESULT_LIMIT):
    """``(rows, more)``: at most ``limit`` distinct rows, those of the home area first, then all others (D-111); each
    part in ``order``. ``more`` says that the query has further rows (one row beyond ``limit`` is read).

    Two index-ordered queries instead of one sort, so the cost stays that of the plain prefix search."""
    wanted = limit + 1
    condition = home_filter(home)
    if condition is None:
        rows = list(query.order_by(*order).values(*columns).distinct()[:wanted])
    else:
        rows = list(query.filter(condition).order_by(*order).values(*columns).distinct()[:wanted])
        if len(rows) < wanted:
            rows += list(query.exclude(condition).order_by(*order).values(*columns).distinct()[:wanted - len(rows)])
    return rows[:limit], len(rows) > limit


def split_house_number(values):
    """``values`` with a house number split off the end of the street, or None.

    Only for an empty house number field and a street that ends in a house-number-like word after a space
    ("Ittigenstrasse 5", "Bahnhofstrasse 12a"). The caller uses it only after the street as typed found nothing, so a
    street whose name ends in a number ("Strada 53", "Zona Monti 1") or contains digits ("Rue du 1er-Aout") is never
    cut."""
    if values["house_number"]:
        return None
    match = HOUSE_NUMBER_TAIL.match(values["street"])
    if match is None or not key(match["street"]) or not key(match["house_number"]):
        return None
    return {**values, "street": match["street"], "house_number": match["house_number"]}


def locality_matches(generation, values, home):
    """Towns and postal codes for the typed postal code and/or town (D-111), from the small table ``AddressPlace``.

    A complete postal code gives its towns (a town typed besides narrows them); without one, the typed town (at
    least ``TYPEAHEAD_MINIMUM`` characters) gives its postal codes, narrowed by a typed part of the postal code. The
    home
    area comes first, then all others. Returns ``(rows, more)``; nothing else is returned."""
    postal_code, typed = values["postal_code"], key(values["locality"])
    places = AddressPlace.objects.filter(generation=generation)
    if len(postal_code) == POSTAL_CODE_LENGTH:
        places = places.filter(postal_code=postal_code)
        if typed:
            places = places.filter(**prefix_range("locality_key", typed))
    elif len(typed) >= TYPEAHEAD_MINIMUM:
        places = places.filter(**prefix_range("locality_key", typed))
        if postal_code:
            places = places.filter(**prefix_range("postal_code", postal_code))
    else:
        return [], False
    return ranked_rows(places, home, order=LOCALITY_ORDER, columns=LOCALITY_FIELDS)


def prefix_rows(query, values, home):
    """``(rows, more)``: the addresses whose street (and house number, postal code, town) start like the typed text."""
    prefix_query = query.filter(**prefix_range("street_key", key(values["street"])))
    if values["house_number"]:
        prefix_query = prefix_query.filter(**prefix_range("house_number_key", key(values["house_number"])))
    postal_code = values["postal_code"]
    if len(postal_code) == POSTAL_CODE_LENGTH:
        prefix_query = prefix_query.filter(postal_code=postal_code)
    elif postal_code:
        prefix_query = prefix_query.filter(**prefix_range("postal_code", postal_code))
    if values["locality"]:
        prefix_query = prefix_query.filter(**prefix_range("locality_key", key(values["locality"])))
    order = HOUSE_ORDER if values["house_number"] else ANSWER_ORDER
    return ranked_rows(prefix_query, home, order=order, columns=FIELDS)


def _answer(status, data_date=None, stale=False, kind="prefix"):
    """An answer without any result; the lookup fills in what it finds."""
    return {
        "status": status, "data_date": data_date, "stale": stale, "warning": "", "results": [], "match": None,
        "kind": kind, "more": False, "proposals": [], "reason": "",
    }


def _is_asked(values, mode):
    """Whether the typed fields are enough for the mode to search at all."""
    if mode == "locality":
        return len(values["postal_code"]) == POSTAL_CODE_LENGTH or len(key(values["locality"])) >= TYPEAHEAD_MINIMUM
    return bool(values["street"])


def _check(directory, query, values, home, approximate, fix):
    """The parts of the answer of mode "check": ``match`` and, for an address that is not in a current directory, the
    ``warning`` and the ``proposals`` with their ``reason``. An old directory may lack a new address: no notice then,
    and no proposal."""
    if len(values["postal_code"]) != POSTAL_CODE_LENGTH or not values["locality"]:
        match = False
    else:
        match = query.filter(
            street_key=key(values["street"]), house_number_key=key(values["house_number"]),
            postal_code=values["postal_code"], locality_key=key(values["locality"]),
        ).exists()
    answer = {"match": match}
    if not match and not directory.is_stale:
        if fix:
            answer["reason"], answer["proposals"] = fixes(directory.generation, query, values, home, approximate)
        answer["warning"] = _(
            "This address was not found. Please check street, "
            "house number, postal code and town. You can still save it as entered."
        )
    return answer


def _suggest(generation, query, values, home, approximate):
    """The parts of the answer of mode "suggest": the prefix search, with a house number split off the end of the street
    when the street as typed finds nothing ("Ittigenstrasse 5"); when that finds nothing either, the approximate
    street search."""
    rows, more = prefix_rows(query, values, home)
    if not rows and (split := split_house_number(values)) is not None:
        rows, more = prefix_rows(query, split, home)
    answer = {"results": rows, "more": more}
    if not rows:
        answer.update(_approximate(generation, query, values, home, approximate))
    return answer


def _approximate(generation, query, values, home, approximate):
    """The ``results``, ``kind`` and ``more`` of the approximate street search, or nothing when it does not run or finds
    nothing. It is the one expensive step: it needs a complete postal code or, without one, a typed town (the bound of
    the candidates) and a street of a length it can compare. ``approximate`` is asked last, so a budget is counted
    only for a search that happens."""
    postal_complete = len(values["postal_code"]) == POSTAL_CODE_LENGTH
    town_wanted = not postal_complete and len(key(values["locality"])) >= TYPEAHEAD_MINIMUM
    if not (postal_complete or town_wanted):
        return {}
    if not APPROXIMATE_MIN_LENGTH <= len(values["street"]) <= APPROXIMATE_MAX_LENGTH:
        return {}
    if not budget_allows(approximate):
        return {}
    rows, kind = approximate_matches(
        query, values, limit=RESULT_LIMIT + 1, town=values["locality"] if town_wanted else None, generation=generation
    )
    if not rows:
        return {}
    rows = sorted(rows, key=lambda row: not in_home(row, home))
    return {"results": rows[:RESULT_LIMIT], "kind": kind, "more": len(rows) > RESULT_LIMIT}


def lookup(values, *, mode="suggest", home=None, minimum=2, approximate=True, fix=True):
    """The JSON answer of the lookup endpoint.

    ``warning`` is non-empty only in mode "check" when a current directory does not know the complete address (D-066);
    directory problems are logged and shown to the operator only. ``more`` says that the directory has further matches
    than ``results`` holds. ``proposals`` with ``reason`` (mode "check" only) are up to three complete addresses that
    fix what does not fit (``fixes``). A house number typed at the end of the street (house number field empty) is
    split off when the street as typed finds nothing.
    Mode "locality" (D-111) answers ``results`` of ``{"postal_code", "locality"}`` for the typed postal code or town.
    ``home`` is ``(postal code, town)`` of the LEG: its addresses come first. ``minimum`` is the shortest street
    or town that is searched. ``approximate`` allows the approximate street search after a prefix search without a hit
    (True, False, or a function that is called only when the search would run and says whether it may; the endpoint
    gives it a budget and refuses it to an anonymous client, ``limits.allow_approximate``).
    """
    values = clean_input(values)
    if mode not in MODES:
        raise ValueError("Invalid lookup mode.")
    if values["country"] != "CH" or not _is_asked(values, mode):
        return _answer("not_checked")
    status, directory = directory_status()
    result = _answer(
        status["status"], status["data_date"], status["stale"], kind="locality" if mode == "locality" else "prefix"
    )
    if directory is None:
        return result
    try:
        query = Address.objects.filter(generation=directory.generation)
        if mode == "locality":
            result["results"], result["more"] = locality_matches(directory.generation, values, home)
        elif mode == "check":
            result.update(_check(directory, query, values, home, approximate, fix))
        elif len(values["street"]) >= minimum and key(values["street"]):
            result.update(_suggest(directory.generation, query, values, home, approximate))
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("Address directory lookup is unavailable.")
        result.update(_answer("unavailable", result["data_date"], result["stale"]))
    return result
