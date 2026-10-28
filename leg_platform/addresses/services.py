"""Local-only advisory address matching on the current generation of the directory (D-075, D-065).

Nothing here writes. Searches use the folded key columns (``keys``): prefix = range over an
indexed key column, never ``LIKE``.
"""

import difflib
import logging
import math
import re
import time

from django.db import DatabaseError
from django.db.models import Q
from django.utils.connection import ConnectionDoesNotExist
from django.utils.translation import gettext as _

from .keys import fold, key, prefix_range
from .models import Address, AddressPlace, AddressStreet, Directory

FIELDS = ("street", "house_number", "postal_code", "locality")
# Street key first, so the index addr_generation_street delivers the rows in this order.
ANSWER_ORDER = ("street_key", "street", "house_number", "postal_code", "locality")
# With a house number: the covering index addr_house (street key, house number key, postal code, town key) delivers them.
HOUSE_ORDER = ("street_key", "house_number_key", "postal_code", "locality_key")
# Town first, so the index addr_generation_locality delivers the rows in this order (D-111).
LOCALITY_FIELDS = ("postal_code", "locality")
LOCALITY_ORDER = ("locality_key", "locality", "postal_code")
LIMITS = {"street": 255, "house_number": 32, "postal_code": 4, "locality": 255, "country": 2}
MODES = {"suggest", "check", "locality"}
RESULT_LIMIT = 10
# A house number at the end of the street field (B1): a digit first, then letters, digits and the separators that house
# numbers of the directory use ("5", "5a", "12-14", "7.1", "3/4"). The street part must keep a non-space character.
HOUSE_NUMBER_TAIL = re.compile(r"^(?P<street>.*\S)\s+(?P<house_number>\d[\w./,+()-]*)$")
# The wrong postal code card (B3): at most this many addresses with the right postal code.
PROPOSAL_LIMIT = 3
# Bounds of the fixes for an address that does not fit (``fixes``). Not a workaround for the schema (the lookups are index
# ranges of small tables): they only protect against abuse. A street is in at most this many places, and the candidates of
# one check are at most this many rows; the whole check has one deadline.
PLACES_CAP = 5000
CANDIDATE_CAP = 3000
FIX_TIME_BUDGET = 0.5
# House numbers that are compared by text similarity (a street has up to a few thousand); the others are ranked by
# a cheap prefix / number distance first. Similar streets kept from one search (a prefix like "Rue" matches thousands).
HOUSE_CAP = 200
SIMILAR_CAP = 200
# Similar streets whose places are read, the length of the street start that bounds the search for a typing mistake.
FIX_STREETS = 6
FIX_PREFIX_LENGTH = 3
# A street of this similarity counts as the same street with a typing mistake (it costs no changed field).
SAME_STREET_RATIO = 0.9
# The typing suggestions of the forms (D-111) start at three characters; ``lookup`` keeps the old two for the
# server-side advice of the community domain (``minimum``).
TYPEAHEAD_MINIMUM = 3
POSTAL_CODE_LENGTH = 4
# Approximate matching (D-065) runs only after the prefix search found nothing, for a complete
# postal code or a town; the streets come from the small street table, so the bounds only protect against abuse and
# fail closed (no result): at most APPROXIMATE_STREET_LIMIT streets; typed street of APPROXIMATE_MIN/MAX_LENGTH characters before and after
# folding; candidates longer than 2 * MAX are skipped; at most APPROXIMATE_MAX_RATIO_CALLS full
# comparisons and APPROXIMATE_TIME_BUDGET seconds.
APPROXIMATE_STREET_LIMIT = 20_000
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


def generation_of(query):
    """The generation a query of addresses is restricted to (one indexed row)."""
    return next(iter(query.order_by().values_list("generation", flat=True)[:1]), None)


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


def approximate_matches(query, values, *, limit=RESULT_LIMIT, town=None, deadline=None, generation=None):
    """Addresses of streets similar to the typed one within one postal code (``values`` has a 4-digit postal code) or,
    with ``town``, within that town.

    Returns ``(rows, kind)``: at most ``limit`` (default RESULT_LIMIT) address dicts; ``kind`` is "prefix" when a street
    of the place starts with the typed text, else "approximate". Nothing is returned when a bound is exceeded. The house
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
    """``(rows, more)``: at most ``limit`` distinct rows, those of the home area first, then all others (D-111); each
    part in ``order``. ``more`` says that the query has further rows (one row beyond ``limit`` is read, B2).

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
    """``values`` with a house number split off the end of the street, or None (B1).

    Only for an empty house number field and a street that ends in a house-number-like word after a space ("Ittigenstrasse
    5", "Bahnhofstrasse 12a"). The caller uses it only after the street as typed found nothing, so a street whose name
    ends in a number ("Strada 53", "Zona Monti 1") or contains digits ("Rue du 1er-Aout") is never cut."""
    if values["house_number"]:
        return None
    match = HOUSE_NUMBER_TAIL.match(values["street"])
    if match is None or not key(match["street"]) or not key(match["house_number"]):
        return None
    return {**values, "street": match["street"], "house_number": match["house_number"]}


def closest(value, candidates, limit=PROPOSAL_LIMIT, cutoff=0.7):
    """The ``limit`` candidates a person most likely meant: those that start with the typed text first, then by
    similarity (after folding), ties in the given order (the same ranking as ``_closest`` of the LEG software)."""
    typed = key(value)
    if not typed:
        return []
    prefix, similar = [], []
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(typed)
    for candidate in candidates:
        folded = key(candidate)
        if folded.startswith(typed):
            prefix.append(candidate)
            continue
        matcher.set_seq1(folded)
        ratio = matcher.ratio()
        if ratio >= cutoff:
            similar.append((-ratio, candidate))
    similar.sort(key=lambda pair: pair[0])
    return (prefix + [candidate for _score, candidate in similar])[:limit]


def number_of(house_number):
    """The leading digits of a house number as an integer, or None."""
    match = re.match(r"\d+", house_number)
    return int(match.group()) if match else None


def prerank(typed, houses, cap=HOUSE_CAP):
    """At most ``cap`` of ``houses``, the ones most likely to look like ``typed`` first (same start, then the numerically
    nearest): the cheap step before the text similarity, so that a street with thousands of numbers costs no more."""
    folded, wanted = key(typed), number_of(typed)

    def cheap(house):
        number = number_of(house)
        distance = abs(number - wanted) if number is not None and wanted is not None else 10**6
        return (not key(house).startswith(folded), distance, house)

    return sorted(houses, key=cheap)[:cap]


def nearest_number(typed, houses):
    """The house number among ``houses`` that looks most like ``typed``, else the numerically nearest, else the first."""
    houses = prerank(typed, houses)
    similar = closest(typed, houses, limit=1)
    if similar:
        return similar[0]
    wanted = number_of(typed)
    numbered = [(abs(number_of(house) - wanted), house) for house in houses if number_of(house) is not None]
    if wanted is not None and numbered:
        return min(numbered)[1]
    return houses[0] if houses else None


def fixes(generation, query, values, home, approximate):
    """``(reason, rows)``: up to three complete addresses that fix what does not fit (E1, F1 to F9), or ``("", [])``.

    The minimum for a judgement: a street, a house number, and a complete postal code or a town of three characters
    (F7: no house number, no street, nothing to compare: no card). Everything is an index range: the places of the street
    (``AddressStreet``), the addresses of the street in the typed place and the addresses of the typed house number in
    all places (index ``addr_house``). An address that fits every field typed (the missing ones are not compared) is
    correct: nothing. A street that is not known (a typing mistake) brings the streets that look like it from the postal
    code, then from the town, then from the start of the name (the caller's budget ``approximate``; an anonymous client
    gets none). The best candidate has the FEWEST changed fields (a street of ratio SAME_STREET_RATIO counts as a typing
    mistake and costs none); then an address with the typed house number, the NEARER place (the distance between the
    centres of the places from the addresses' coordinates, else the difference of the postal codes), the closer number
    (similar text first, then the numerically nearest) and the LEG's own area. Only candidates that change the same
    fields as the best one are offered (one reason, E4). ``reason``: ``street`` (a typing mistake, or the street is not in
    the typed place and the number is nowhere), ``house_number`` (the street is in the typed place, the number is not),
    ``postal_code``, ``town``. Every step checks one deadline (FIX_TIME_BUDGET): passed, no card."""
    try:
        return _fixes(generation, query, values, home, approximate, time.monotonic() + FIX_TIME_BUDGET)
    except TimeoutError:
        return "", []


def _fixes(generation, query, values, home, approximate, deadline):
    typed_street, typed_house = values["street"], values["house_number"]
    street_key, house_key = key(typed_street), key(typed_house)
    typed_postal = values["postal_code"]
    postal_ok = len(typed_postal) == POSTAL_CODE_LENGTH
    town_key = key(values["locality"])
    town_ok = len(town_key) >= TYPEAHEAD_MINIMUM
    if not street_key or not house_key or not (postal_ok or town_ok):
        return "", []
    places = AddressStreet.objects.filter(generation=generation)
    place_rows = AddressPlace.objects.filter(generation=generation)

    def tick():
        if time.monotonic() > deadline:
            raise TimeoutError

    def places_of(name_key, name=None):
        tick()
        found = places.filter(street_key=name_key)
        if name is not None:
            found = found.filter(street=name)
        rows = list(found.order_by().values_list("street", "postal_code", "locality")[:PLACES_CAP + 1])
        return None if len(rows) > PLACES_CAP else rows

    def is_here(place):  # the place (street, postal code, town) the person typed
        return place[1] == typed_postal if postal_ok else key(place[2]) == town_key

    # The centre of the typed place and of the candidate places (the mean of the coordinates of up to 60 addresses of each
    # place, computed with the directory: AddressPlace.easting / northing). Without coordinates: the postal code number.
    typed_places = place_rows.filter(postal_code=typed_postal) if postal_ok else place_rows.filter(locality_key=town_key)
    points = [(e, n) for e, n in typed_places.values_list("easting", "northing") if e is not None and n is not None]
    typed_centre = (sum(e for e, _ in points) / len(points), sum(n for _, n in points) / len(points)) if points else None
    centres = {}

    def load_centres(codes):
        codes = sorted(set(codes) - {code for code, _town in centres})
        for start in range(0, len(codes), 400):
            tick()
            for code, town, e, n in place_rows.filter(postal_code__in=codes[start:start + 400]).values_list(
                "postal_code", "locality", "easting", "northing"
            ):
                centres[(code, town)] = (e, n) if e is not None and n is not None else None

    def distance(code, town):
        number = abs(int(code) - int(typed_postal)) if postal_ok and code.isdigit() and typed_postal.isdigit() else 0
        centre = centres.get((code, town))
        if typed_centre is not None:
            if centre is not None:
                return math.hypot(centre[0] - typed_centre[0], centre[1] - typed_centre[1]) / 1000
            return 10_000 + number
        return number

    candidates = {}  # (street, house number, postal code, town) -> True when the street has a row in the typed place
    here_of = {}  # street key -> whether the street is in the typed place

    def collect(name_key, name):
        """The candidates of one street: its addresses in the typed place, its addresses with the typed number, and, when
        the street is not in the typed place, the closest number of the nearest places."""
        street_places = places_of(name_key, name)
        if street_places is None:
            return False
        here = [place for place in street_places if is_here(place)]
        here_of[name_key] = bool(here)
        for _name, code, town in here:
            tick()
            rows = query.filter(street_key=name_key, street=name, postal_code=code).values_list(*FIELDS)
            for row in rows[:CANDIDATE_CAP]:
                candidates[tuple(row)] = True
        tick()
        for row in query.filter(street_key=name_key, house_number_key=house_key).values_list(*FIELDS)[:CANDIDATE_CAP]:
            if row[0] == name:
                candidates[tuple(row)] = True
        if not here:
            load_centres(place[1] for place in street_places)
            ranked = sorted(
                street_places,
                key=lambda place: (
                    distance(place[1], place[2]), not in_home({"postal_code": place[1], "locality": place[2]}, home), place[1],
                ),
            )
            per_place = []
            for _name, code, town in ranked[:PROPOSAL_LIMIT]:
                tick()
                rows = query.filter(street_key=name_key, street=name, postal_code=code, locality=town).values_list(*FIELDS)
                found = {tuple(row)[1]: tuple(row) for row in rows[:CANDIDATE_CAP]}
                near = prerank(typed_house, found)
                wanted = closest(typed_house, near, limit=PROPOSAL_LIMIT) or [nearest_number(typed_house, found)]
                per_place.append([found[number] for number in wanted if number in found])
            # The nearest place first, the others behind it: one address per place, then the next closest number.
            for index in range(PROPOSAL_LIMIT):
                for group in per_place:
                    if index < len(group):
                        candidates[group[index]] = True
        return True

    if street_places_known := places_of(street_key):
        if not collect(street_key, street_places_known[0][0]):
            return "", []
        for name in {place[0] for place in street_places_known} - {street_places_known[0][0]}:
            collect(street_key, name)
    else:
        if street_places_known is None:
            return "", []
        # Unknown street: a typing mistake, perhaps. The caller's budget is counted once for the whole search.
        if not (approximate() if callable(approximate) else approximate):
            return "", []
        scopes = []
        if postal_ok:
            scopes.append(places.filter(postal_code=typed_postal))
        if town_ok:
            scopes.append(places.filter(locality_key=town_key))
        scopes.append(places.filter(**prefix_range("street_key", fold(typed_street)[:FIX_PREFIX_LENGTH])))
        names = []
        for scope in scopes:
            found, _kind, bounded_out = similar_streets(scope, typed_street, deadline)
            names.extend(name for name in found if name not in names)
            if bounded_out:
                break
        matcher = difflib.SequenceMatcher(autojunk=False)
        matcher.set_seq2(fold(typed_street))
        rated = []
        for name in names[:3 * SIMILAR_CAP]:
            tick()
            matcher.set_seq1(fold(name))
            rated.append((matcher.ratio(), name))
        for _ratio, name in sorted(rated, reverse=True)[:FIX_STREETS]:
            collect(key(name), name)
    if not candidates:
        return "", []
    if any(
        key(house) == house_key and (not postal_ok or code == typed_postal) and (not town_ok or key(town) == town_key)
        and key(name) == street_key
        for (name, house, code, town) in candidates
    ):
        return "", []

    # Per street: the house numbers in the order of the closest text (the cheapest HOUSE_CAP first); the others by distance.
    typed_number = number_of(typed_house)
    houses = {}
    for (name, house, _code, _town) in candidates:
        houses.setdefault(name, set()).add(house)
    rank_of = {}
    for name, group in houses.items():
        tick()
        near = prerank(typed_house, group)
        rank_of[name] = {h: i for i, h in enumerate(closest(typed_house, near, limit=len(near)))}
    load_centres(code for (_name, _house, code, _town) in candidates)
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(street_key)

    def score(candidate):
        tick()
        name, house, code, town = candidate
        if key(name) == street_key:
            ratio, street_cost = 1.0, 0
        else:
            matcher.set_seq1(key(name))
            ratio = matcher.ratio()
            street_cost = 0 if ratio >= SAME_STREET_RATIO else 1
        house_diff = key(house) != house_key
        postal_diff = postal_ok and code != typed_postal
        town_diff = town_ok and key(town) != town_key
        if not house_diff:
            house_rank = 0
        elif house in rank_of[name]:
            house_rank = 1 + rank_of[name][house]
        else:
            number = number_of(house)
            house_rank = 1000 + (abs(number - typed_number) if None not in (number, typed_number) else 10**6)
        absent_here = not here_of.get(key(name), True)
        pattern = (key(name) != street_key, house_diff, postal_diff, town_diff, absent_here)
        total = street_cost + house_diff + postal_diff + town_diff
        in_area = in_home({"postal_code": code, "locality": town}, home)
        order = (total, -ratio, house_diff, distance(code, town), house_rank, not in_area, code, house)
        return order, pattern

    scored = sorted(((*score(candidate), candidate) for candidate in candidates), key=lambda item: item[0])
    best_pattern = scored[0][1]
    chosen = [item for item in scored if item[1] == best_pattern]
    if best_pattern[1] and chosen[0][0][4] < 1000:
        # Numbers that look like the typed one exist: the numerically nearest ones are not added to them.
        chosen = [item for item in chosen if item[0][4] < 1000]
    if best_pattern[0] and -chosen[0][0][1] >= SAME_STREET_RATIO:
        # A street that is the typed one with a typing mistake exists: other, merely similar streets are not added.
        chosen = [item for item in chosen if -item[0][1] >= SAME_STREET_RATIO]
    chosen = [item[2] for item in chosen][:PROPOSAL_LIMIT]
    street_differs, house_diff, postal_diff, town_diff, absent_here = best_pattern
    if street_differs or (absent_here and house_diff):
        reason = "street"
    elif house_diff:
        reason = "house_number"
    elif postal_diff:
        reason = "postal_code"
    elif town_diff:
        reason = "town"
    else:
        reason = "house_number"
    return reason, [dict(zip(FIELDS, candidate)) for candidate in chosen]


def locality_matches(generation, values, home):
    """Towns and postal codes for the typed postal code and/or town (D-111), from the small table ``AddressPlace``.

    A complete postal code gives its towns (a town typed besides narrows them); without one, the typed town (at
    least ``TYPEAHEAD_MINIMUM`` characters) gives its postal codes, narrowed by a typed part of the postal code. The home
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
    if len(postal_code) == 4:
        prefix_query = prefix_query.filter(postal_code=postal_code)
    elif postal_code:
        prefix_query = prefix_query.filter(**prefix_range("postal_code", postal_code))
    if values["locality"]:
        prefix_query = prefix_query.filter(**prefix_range("locality_key", key(values["locality"])))
    order = HOUSE_ORDER if values["house_number"] else ANSWER_ORDER
    return ranked_rows(prefix_query, home, order=order, columns=FIELDS)


def lookup(values, *, mode="suggest", home=None, minimum=2, approximate=True, fix=True):
    """The JSON answer of the lookup endpoint.

    ``warning`` is non-empty only in mode "check" when a current directory does not know the
    complete address (D-066); directory problems are logged and shown to the operator only. ``more`` says that the
    directory has further matches than ``results`` holds (B2). ``proposals`` with ``reason`` (mode "check" only, B3, E1) are
    up to three complete addresses that fix what does not fit (``fixes``). A house number typed at the end of the street
    (house number field empty) is split off when the street as typed finds nothing (B1).
    Mode "locality" (D-111) answers ``results`` of ``{"postal_code", "locality"}`` for the typed postal code or town.
    ``home`` is ``(postal code, town)`` of the LEG: its addresses come first. ``minimum`` is the shortest street
    or town that is searched. ``approximate`` allows the approximate street search after a prefix search without a hit
    (True, False, or a function that is called only when the search would run and says whether it may; the endpoint
    gives it a budget and refuses it to an anonymous client, ``limits.allow_approximate``).
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
            "warning": "", "results": [], "match": None, "kind": "prefix", "more": False, "proposals": [],
            "reason": "",
        }
    status, directory = directory_status()
    result = {
        "status": status["status"], "data_date": status["data_date"], "stale": status["stale"],
        "warning": "", "results": [], "match": None, "kind": "locality" if mode == "locality" else "prefix",
        "more": False, "proposals": [], "reason": "",
    }
    if directory is None:
        return result
    try:
        query = Address.objects.filter(generation=directory.generation)
        if mode == "locality":
            result["results"], result["more"] = locality_matches(directory.generation, values, home)
        elif mode == "check":
            if len(values["postal_code"]) != 4 or not values["locality"]:
                result["match"] = False
            else:
                result["match"] = query.filter(
                    street_key=key(values["street"]), house_number_key=key(values["house_number"]),
                    postal_code=values["postal_code"], locality_key=key(values["locality"]),
                ).exists()
            if not result["match"] and not directory.is_stale:
                # An old directory may lack a new address: no notice then, and no proposal.
                if fix:
                    result["reason"], result["proposals"] = fixes(directory.generation, query, values, home, approximate)
                result["warning"] = _(
                    "This address was not found. Please check street, "
                    "house number, postal code and town. You can still save it as entered."
                )
        elif len(values["street"]) >= minimum and key(values["street"]):
            rows, more = prefix_rows(query, values, home)
            if not rows and (split := split_house_number(values)) is not None:
                # "Ittigenstrasse 5" in the street field: the same as street "Ittigenstrasse" with house number "5".
                rows, more = prefix_rows(query, split, home)
                if rows:
                    values = split
            result["results"], result["more"] = rows, more
            # Only a complete postal code bounds the candidates. The approximate search is the one expensive step: the
            # caller decides (``approximate``: True, False or a function asked just before the search runs, so a budget
            # is counted only for a search that happens).
            # The candidates are bounded by the complete postal code or, without one, by a typed town (E3); the street
            # list must not exceed APPROXIMATE_STREET_LIMIT either way.
            # Without a complete postal code a typed town bounds the candidates (E3).
            town_wanted = len(values["postal_code"]) != 4 and len(key(values["locality"])) >= TYPEAHEAD_MINIMUM
            if not result["results"] and (len(values["postal_code"]) == 4 or town_wanted) and (
                APPROXIMATE_MIN_LENGTH <= len(values["street"]) <= APPROXIMATE_MAX_LENGTH
            ) and (approximate() if callable(approximate) else approximate):
                rows, kind = approximate_matches(
                    query, values, limit=RESULT_LIMIT + 1, town=values["locality"] if town_wanted else None,
                    generation=directory.generation,
                )
                if rows:
                    rows = sorted(rows, key=lambda row: not in_home(row, home))
                    result.update(results=rows[:RESULT_LIMIT], kind=kind, more=len(rows) > RESULT_LIMIT)
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("Address directory lookup is unavailable.")
        result.update(status="unavailable", results=[], match=None, kind="prefix", warning="", more=False, proposals=[], reason="")
    return result
