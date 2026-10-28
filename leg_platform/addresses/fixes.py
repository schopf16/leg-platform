"""The fixes of the check: up to three complete addresses for an address that does not fit ("Did you mean this
address?", D-111). Local only, nothing here writes.

The minimum for a judgement is a street, a house number, and a complete postal code or a town of three characters
(otherwise there is nothing to compare: no proposal). Everything is an index range: the places of the street
(``AddressStreet``), the addresses of the street in the typed place and the addresses of the typed house number in all
places (index ``addr_house``). An address that fits every field typed (the missing ones are not compared) is correct:
no proposal.

A street that is not known (a typing mistake) brings the streets that look like it from the postal code, then from the
town, then from the start of the name; the caller's budget (``approximate``) allows it, an anonymous client gets none.

The best candidate has the fewest changed fields (a street of ratio SAME_STREET_RATIO counts as a typing mistake and
costs none); then an address with the typed house number, the nearer place (the distance between the centres of the
places, else the difference of the postal codes), the closer number (similar text first, then the numerically nearest)
and the LEG's own area. Only candidates that change the same fields as the best one are offered. Every step checks one
deadline (FIX_TIME_BUDGET): passed, no proposal.
"""

import difflib
import math
import re
import time
from typing import NamedTuple

from .keys import fold, key, prefix_range
from .matching import (
    FIELDS,
    POSTAL_CODE_LENGTH,
    SIMILAR_CAP,
    TYPEAHEAD_MINIMUM,
    budget_allows,
    in_home,
    similar_streets,
)
from .models import AddressPlace, AddressStreet

# At most this many addresses are proposed.
PROPOSAL_LIMIT = 3
# Bounds that only protect against abuse (the lookups are index ranges of small tables): a street is in at most
# PLACES_CAP places, the candidates of one street query are at most CANDIDATE_CAP rows, the whole check has one
# deadline.
PLACES_CAP = 5000
CANDIDATE_CAP = 3000
FIX_TIME_BUDGET = 0.5
# House numbers that are compared by text similarity (a street has up to a few thousand); the others are ranked by
# a cheap prefix / number distance first.
HOUSE_CAP = 200
# Similar streets whose places are read, the length of the street start that bounds the search for a typing mistake.
FIX_STREETS = 6
FIX_PREFIX_LENGTH = 3
# A street of this similarity counts as the same street with a typing mistake (it costs no changed field).
SAME_STREET_RATIO = 0.9
# Place centres are read for this many postal codes per query.
CENTRE_BATCH = 400
# House ranks: 0 is the typed number, 1 and more are numbers with a similar text; numbers that are only ordered by
# their numeric distance start at NUMERIC_RANK, and NO_NUMBER stands for a distance that cannot be computed.
NUMERIC_RANK = 1000
NO_NUMBER = 10**6


def closest(value, candidates, limit=PROPOSAL_LIMIT, cutoff=0.7):
    """The ``limit`` candidates a person most likely meant: those that start with the typed text first, then by
    similarity (after folding), ties in the given order."""
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
    """At most ``cap`` of ``houses``, the ones most likely to look like ``typed`` first (same start, then the
    numerically nearest): the cheap step before the text similarity, so that a street with thousands of numbers costs
    no more."""
    folded, wanted = key(typed), number_of(typed)

    def cheap(house):
        number = number_of(house)
        distance = abs(number - wanted) if number is not None and wanted is not None else NO_NUMBER
        return (not key(house).startswith(folded), distance, house)

    return sorted(houses, key=cheap)[:cap]


def nearest_number(typed, houses):
    """The house number among ``houses`` that looks most like ``typed``, else the numerically nearest, else the
    first."""
    houses = prerank(typed, houses)
    similar = closest(typed, houses, limit=1)
    if similar:
        return similar[0]
    wanted = number_of(typed)
    numbered = [(abs(number_of(house) - wanted), house) for house in houses if number_of(house) is not None]
    if wanted is not None and numbered:
        return min(numbered)[1]
    return houses[0] if houses else None


class Pattern(NamedTuple):
    """Which fields a candidate changes; the candidates with the pattern of the best one are offered."""

    street_differs: bool
    house_differs: bool
    postal_differs: bool
    town_differs: bool
    absent_here: bool  # the street is not in the typed place

    @property
    def reason(self):
        """``street`` (a typing mistake, or the street is not in the typed place and the number is nowhere),
        ``house_number`` (the street is in the typed place, the number is not), ``postal_code`` or ``town``."""
        if self.street_differs or (self.absent_here and self.house_differs):
            return "street"
        if self.house_differs:
            return "house_number"
        if self.postal_differs:
            return "postal_code"
        if self.town_differs:
            return "town"
        return "house_number"


class Rank(NamedTuple):
    """The sort key of a candidate: the smaller, the better (fields in the order of their weight)."""

    changed_fields: int
    unlike_street: float  # minus the similarity of the street to the typed one
    house_differs: bool
    distance: float
    house_rank: int
    outside_home: bool
    postal_code: str
    house_number: str

    @property
    def street_ratio(self):
        return -self.unlike_street


class Scored(NamedTuple):
    rank: Rank
    pattern: Pattern
    candidate: tuple  # (street, house number, postal code, town)


class FixSearch:
    """One search for the proposals of one typed address: ``run`` is the entry point, the state lives in the
    attributes."""

    def __init__(self, generation, query, values, home, approximate, deadline):
        self.query = query
        self.home = home
        self.approximate = approximate
        self.deadline = deadline
        self.street = values["street"]
        self.street_key = key(self.street)
        self.house = values["house_number"]
        self.house_key = key(self.house)
        self.typed_number = number_of(self.house)
        self.postal_code = values["postal_code"]
        self.postal_ok = len(self.postal_code) == POSTAL_CODE_LENGTH
        self.town_key = key(values["locality"])
        self.town_ok = len(self.town_key) >= TYPEAHEAD_MINIMUM
        self.places = AddressStreet.objects.filter(generation=generation)
        self.place_rows = AddressPlace.objects.filter(generation=generation)
        self.typed_centre = None
        self.centres = {}  # (postal code, town) -> (easting, northing) or None
        self.candidates = {}  # (street, house number, postal code, town); a dict keeps the order of insertion
        self.here_of = {}  # street key -> whether the street is in the typed place
        self.house_ranks = {}  # street -> {house number: position by text similarity}
        self.street_matcher = difflib.SequenceMatcher(autojunk=False)
        self.street_matcher.set_seq2(self.street_key)

    def run(self):
        """``(reason, rows)``, or ``("", [])`` when there is nothing to propose."""
        if not (self.street_key and self.house_key and (self.postal_ok or self.town_ok)):
            return "", []
        self.typed_centre = self.centre_of_typed_place()
        if not self.collect_candidates() or not self.candidates or self.address_exists():
            return "", []
        self.rank_house_numbers()
        self.load_centres(code for _street, _house, code, _town in self.candidates)
        scored = sorted((self.score(candidate) for candidate in self.candidates), key=lambda item: item.rank)
        return self.choose(scored)

    def tick(self):
        if time.monotonic() > self.deadline:
            raise TimeoutError

    # The places and their centres.

    def places_of(self, name_key, name=None):
        """The ``(street, postal code, town)`` places of a street, or None when there are more than PLACES_CAP."""
        self.tick()
        found = self.places.filter(street_key=name_key)
        if name is not None:
            found = found.filter(street=name)
        rows = list(found.order_by().values_list("street", "postal_code", "locality")[:PLACES_CAP + 1])
        return None if len(rows) > PLACES_CAP else rows

    def is_here(self, place):
        """Whether a place (street, postal code, town) is the one the person typed."""
        return place[1] == self.postal_code if self.postal_ok else key(place[2]) == self.town_key

    def lies_in_home(self, code, town):
        return in_home({"postal_code": code, "locality": town}, self.home)

    def centre_of_typed_place(self):
        """The mean of the centres of the typed place (``AddressPlace.easting`` / ``northing``, computed with the
        directory), or None without coordinates."""
        if self.postal_ok:
            typed_places = self.place_rows.filter(postal_code=self.postal_code)
        else:
            typed_places = self.place_rows.filter(locality_key=self.town_key)
        points = [(e, n) for e, n in typed_places.values_list("easting", "northing") if None not in (e, n)]
        if not points:
            return None
        return sum(e for e, _ in points) / len(points), sum(n for _, n in points) / len(points)

    def load_centres(self, codes):
        codes = sorted(set(codes) - {code for code, _town in self.centres})
        for start in range(0, len(codes), CENTRE_BATCH):
            self.tick()
            places = self.place_rows.filter(postal_code__in=codes[start:start + CENTRE_BATCH])
            for code, town, easting, northing in places.values_list("postal_code", "locality", "easting", "northing"):
                self.centres[(code, town)] = None if None in (easting, northing) else (easting, northing)

    def distance(self, code, town):
        """The distance of a place to the typed one: km between the centres; without coordinates the difference of the
        postal codes (behind every distance in km)."""
        comparable = self.postal_ok and code.isdigit() and self.postal_code.isdigit()
        numeric = abs(int(code) - int(self.postal_code)) if comparable else 0
        centre = self.centres.get((code, town))
        if self.typed_centre is None:
            return numeric
        if centre is None:
            return 10_000 + numeric
        return math.hypot(centre[0] - self.typed_centre[0], centre[1] - self.typed_centre[1]) / 1000

    # The candidates.

    def add(self, rows):
        for row in rows:
            self.candidates[tuple(row)] = None

    def collect_candidates(self):
        """Fills ``candidates``; False when there is no proposal (too many places, no budget for a typing mistake)."""
        known = self.places_of(self.street_key)
        if known is None:
            return False
        if known:
            return self.collect_known_street(known)
        # Unknown street: a typing mistake, perhaps. The caller's budget is counted once for the whole search.
        if not budget_allows(self.approximate):
            return False
        self.collect_similar_streets()
        return True

    def collect_known_street(self, places):
        """The street as typed; spellings with the same key (other case or accents) follow behind the first."""
        first = places[0][0]
        if not self.collect_street(self.street_key, first):
            return False
        for name in {place[0] for place in places} - {first}:
            self.collect_street(self.street_key, name)
        return True

    def collect_similar_streets(self):
        matcher = difflib.SequenceMatcher(autojunk=False)
        matcher.set_seq2(fold(self.street))
        rated = []
        for name in self.similar_street_names()[:3 * SIMILAR_CAP]:
            self.tick()
            matcher.set_seq1(fold(name))
            rated.append((matcher.ratio(), name))
        for _ratio, name in sorted(rated, reverse=True)[:FIX_STREETS]:
            self.collect_street(key(name), name)

    def similar_street_names(self):
        """Streets that look like the typed one: in the postal code, in the town, then by the start of the name."""
        scopes = []
        if self.postal_ok:
            scopes.append(self.places.filter(postal_code=self.postal_code))
        if self.town_ok:
            scopes.append(self.places.filter(locality_key=self.town_key))
        scopes.append(self.places.filter(**prefix_range("street_key", fold(self.street)[:FIX_PREFIX_LENGTH])))
        names = []
        for scope in scopes:
            found, _kind, bounded_out = similar_streets(scope, self.street, self.deadline)
            names.extend(name for name in found if name not in names)
            if bounded_out:
                break
        return names

    def collect_street(self, name_key, name):
        """The candidates of one street: its addresses in the typed place, its addresses with the typed number, and,
        when the street is not in the typed place, the closest numbers of the nearest places. False when the street is
        in too many places."""
        street_places = self.places_of(name_key, name)
        if street_places is None:
            return False
        here = [place for place in street_places if self.is_here(place)]
        self.here_of[name_key] = bool(here)
        for _name, code, _town in here:
            self.tick()
            in_place = self.query.filter(street_key=name_key, street=name, postal_code=code)
            self.add(in_place.values_list(*FIELDS)[:CANDIDATE_CAP])
        self.tick()
        with_house = self.query.filter(street_key=name_key, house_number_key=self.house_key).values_list(*FIELDS)
        self.add(row for row in with_house[:CANDIDATE_CAP] if row[0] == name)
        if not here:
            self.collect_nearest_places(name_key, name, street_places)
        return True

    def collect_nearest_places(self, name_key, name, street_places):
        """The closest numbers of the nearest places of a street that is not in the typed place."""
        self.load_centres(place[1] for place in street_places)
        nearest = sorted(
            street_places,
            key=lambda place: (self.distance(place[1], place[2]), not self.lies_in_home(place[1], place[2]), place[1]),
        )
        per_place = []
        for _name, code, town in nearest[:PROPOSAL_LIMIT]:
            self.tick()
            in_place = self.query.filter(street_key=name_key, street=name, postal_code=code, locality=town)
            found = {tuple(row)[1]: tuple(row) for row in in_place.values_list(*FIELDS)[:CANDIDATE_CAP]}
            near = prerank(self.house, found)
            wanted = closest(self.house, near, limit=PROPOSAL_LIMIT) or [nearest_number(self.house, found)]
            per_place.append([found[number] for number in wanted if number in found])
        # The nearest place first, the others behind it: one address per place, then the next closest number.
        for index in range(PROPOSAL_LIMIT):
            self.add(group[index] for group in per_place if index < len(group))

    def address_exists(self):
        """Whether a candidate fits every field typed (the missing ones are not compared)."""
        return any(
            key(house) == self.house_key and key(name) == self.street_key
            and (not self.postal_ok or code == self.postal_code) and (not self.town_ok or key(town) == self.town_key)
            for name, house, code, town in self.candidates
        )

    # The ranking.

    def rank_house_numbers(self):
        """Per street: the house numbers in the order of the closest text (the cheapest HOUSE_CAP first)."""
        houses = {}
        for name, house, _code, _town in self.candidates:
            houses.setdefault(name, set()).add(house)
        for name, group in houses.items():
            self.tick()
            near = prerank(self.house, group)
            self.house_ranks[name] = {house: i for i, house in enumerate(closest(self.house, near, limit=len(near)))}

    def house_rank(self, name, house):
        if key(house) == self.house_key:
            return 0
        if house in self.house_ranks[name]:
            return 1 + self.house_ranks[name][house]
        number = number_of(house)
        distance = abs(number - self.typed_number) if None not in (number, self.typed_number) else NO_NUMBER
        return NUMERIC_RANK + distance

    def score(self, candidate):
        self.tick()
        name, house, code, town = candidate
        name_key = key(name)
        if name_key == self.street_key:
            ratio, street_cost = 1.0, 0
        else:
            self.street_matcher.set_seq1(name_key)
            ratio = self.street_matcher.ratio()
            street_cost = 0 if ratio >= SAME_STREET_RATIO else 1
        house_differs = key(house) != self.house_key
        postal_differs = self.postal_ok and code != self.postal_code
        town_differs = self.town_ok and key(town) != self.town_key
        pattern = Pattern(
            name_key != self.street_key, house_differs, postal_differs, town_differs,
            not self.here_of.get(name_key, True),
        )
        rank = Rank(
            street_cost + house_differs + postal_differs + town_differs, -ratio, house_differs,
            self.distance(code, town), self.house_rank(name, house), not self.lies_in_home(code, town), code, house,
        )
        return Scored(rank, pattern, candidate)

    def choose(self, scored):
        """``(reason, rows)``: the best candidates of the best pattern."""
        best = scored[0].pattern
        chosen = [item for item in scored if item.pattern == best]
        if best.house_differs and chosen[0].rank.house_rank < NUMERIC_RANK:
            # Numbers that look like the typed one exist: the numerically nearest ones are not added to them.
            chosen = [item for item in chosen if item.rank.house_rank < NUMERIC_RANK]
        if best.street_differs and chosen[0].rank.street_ratio >= SAME_STREET_RATIO:
            # A street that is the typed one with a typing mistake exists: other, merely similar streets are not added.
            chosen = [item for item in chosen if item.rank.street_ratio >= SAME_STREET_RATIO]
        return best.reason, [dict(zip(FIELDS, item.candidate)) for item in chosen[:PROPOSAL_LIMIT]]


def fixes(generation, query, values, home, approximate):
    """``(reason, rows)``: up to PROPOSAL_LIMIT complete addresses that fix what does not fit, or ``("", [])``.

    ``approximate`` is the caller's budget for the search of a street with a typing mistake (True, False or a function,
    see ``matching.budget_allows``). ``reason`` is ``street``, ``house_number``, ``postal_code`` or ``town``."""
    search = FixSearch(generation, query, values, home, approximate, time.monotonic() + FIX_TIME_BUDGET)
    try:
        return search.run()
    except TimeoutError:
        return "", []
