"""The one outbound request of a signed-in person's address search: the building identifier (EGID) at swisstopo.

Owner approval 2026-10-19 (KONZEPT 3a, docs/SERVICES.md S-4): the address that a signed-in person entered is
sent to swisstopo, only to determine the EGID, for nothing else. This is the only module of the web process
that talks to a third party about an address; ``buildings`` calls it after the local directory and the stored
answers found nothing, within the rate limits of ``limits.allow_external_lookup``.

Source (checked 2026-10-19): swisstopo, "Amtliches Verzeichnis der Gebaeudeadressen", product information
chapter 3 (find service, layer ``ch.swisstopo.amtliches-gebaeudeadressverzeichnis``, example with
``https://api3.geo.admin.ch/rest/services/api/MapServer/find``); https://docs.geo.admin.ch/access-data/find-features.html
(parameters ``layer``, ``searchField``, ``searchText``, ``contains``, ``returnGeometry``, ``layerDefs``).
The service answers at most 200 features per request and only filters the attributes ``adr_number`` and
``stn_label`` in ``layerDefs``; the postal code is the search text of ``zip_label``.

Same hardening as the importer: one fixed https address, no redirect, timeouts, size limit, no proxy variables
or ``.netrc`` (``trust_env`` is off), strict checks of the JSON. Nothing about the address is ever logged: a
failure carries only a fixed sentence.

**Hard total deadline.** A socket timeout limits one wait, not the whole answer: a server that sends one byte every
few seconds would hold a synchronous worker for hours. The request therefore runs in a helper thread and the caller
waits at most ``TOTAL_SECONDS`` (``Thread.join``). When that expires, every socket of the request is shut down
(``_Budget.expire``: the blocked read ends at once) and ``SearchFailure`` is raised, so neither the worker nor the
helper thread outlive the deadline by more than a moment. The sockets are found through the connection classes of the
adapter (``_connection_classes``); the helper thread does not touch the database.
"""

import json
import re
import socket
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from .keys import key

HOST = "api3.geo.admin.ch"
FIND_URL = "https://" + HOST + "/rest/services/api/MapServer/find"
LAYER = "ch.swisstopo.amtliches-gebaeudeadressverzeichnis"
# (connect, read) seconds of one wait. A normal answer of this service takes well below two seconds
# (measured 2026-10-19 with public addresses: 0.2 to 0.6 s), so a wait of four seconds is already a fault.
TIMEOUT = (3, 4)
# Hard limit of the whole request (connect, headers, body), enforced by ``search``: four times the normal time and
# far below the 30 s of a Gunicorn worker.
TOTAL_SECONDS = 8
MAX_RESPONSE_BYTES = 256 * 1024
CHUNK_SIZE = 16 * 1024
# The service returns at most 200 features; a full answer may have cut the one we look for.
MAX_FEATURES = 200
MAX_BUILDINGS = 10
# Lengths of the official file (STN_LABEL 150, ADR_NUMBER 12, ZIP_LABEL 150).
STREET_LIMIT = 150
HOUSE_NUMBER_LIMIT = 12
TOWN_LIMIT = 150

HOUSE_NUMBER = re.compile(r"[0-9A-Za-z][0-9A-Za-z.\-/]{0,11}")
POSTAL_CODE = re.compile(r"[1-9][0-9]{3}")


class SearchFailure(Exception):
    """The answer is missing, too large, not what the documentation promises or not trustworthy; nothing of it is kept.

    ``counts`` is False for a refusal of the input before any request (it says nothing about the service); every
    other failure counts towards the circuit breaker of ``limits``.
    """

    def __init__(self, message, *, counts=True):
        super().__init__(message)
        self.counts = counts


class _Budget:
    """The sockets of one request, so that the deadline can shut them down from another thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sockets = []
        self.expired = False

    def add(self, sock):
        with self._lock:
            if not self.expired:
                self._sockets.append(sock)
                return
        self._shut(sock)

    def expire(self):
        with self._lock:
            self.expired = True
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            self._shut(sock)

    @staticmethod
    def _shut(sock):
        # ``shutdown`` ends a read that is blocked in another thread; ``close`` alone may not.
        for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
            try:
                action()
            except (OSError, ValueError):
                pass


def _connection_classes(budget):
    """Connection pools whose sockets register with ``budget`` (the raw socket and, after TLS, the wrapped one)."""

    class Connection(HTTPConnection):
        def _new_conn(self):
            sock = super()._new_conn()
            budget.add(sock)
            return sock

    class SecureConnection(HTTPSConnection):
        def _new_conn(self):
            sock = super()._new_conn()
            budget.add(sock)
            return sock

        def connect(self):
            super().connect()
            budget.add(self.sock)

    class Pool(HTTPConnectionPool):
        ConnectionCls = Connection

    class SecurePool(HTTPSConnectionPool):
        ConnectionCls = SecureConnection

    return {"http": Pool, "https": SecurePool}


class _Adapter(HTTPAdapter):
    def __init__(self, budget):
        self._budget = budget  # before ``super().__init__``: it builds the pool manager
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = _connection_classes(self._budget)


def _street_pattern(street):
    """The street as a pattern of ``ilike``: letters, digits, space, point and hyphen as they are, any other character
    as the one-character wildcard ``_`` (so that no quote or other special character reaches the filter). The hits are
    compared again here with the folded text."""
    return "".join(
        character if character.isalpha() or character.isdigit() or character in " .-" else "_"
        for character in street
    )


def _parameters(street, house_number, postal_code):
    expression = f"adr_number ilike '{house_number}' and stn_label ilike '{_street_pattern(street)}'"
    return {
        "layer": LAYER,
        "searchField": "zip_label",
        "searchText": postal_code,
        "contains": "true",
        "returnGeometry": "false",
        "layerDefs": json.dumps({LAYER: expression}),
    }


def _body(response, deadline):
    """The body of an approved answer, read in chunks up to the limit (and, as a second layer, before ``deadline``)."""
    if response.status_code != 200:
        raise SearchFailure("The search did not answer.")
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise SearchFailure("The search answered in an unexpected format.")
    if not response.headers.get("Content-Type", "").lower().startswith("application/json"):
        raise SearchFailure("The search answered in an unexpected format.")
    length = response.headers.get("Content-Length")
    if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
        raise SearchFailure("The answer of the search is too large.")
    chunks, size = [], 0
    for chunk in response.iter_content(CHUNK_SIZE):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
            raise SearchFailure("The answer of the search is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _text(value, limit):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise SearchFailure("The search answered in an unexpected format.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SearchFailure("The search answered in an unexpected format.")
    return value


def _features(content):
    """The checked feature attributes of an answer: street, house number, postal label, the EGID and the entrance (or None)."""
    try:
        document = json.loads(content)
        results = document["results"]
    except (ValueError, KeyError, TypeError, RecursionError):
        # RecursionError: deeply nested JSON (within the size limit) exhausts the parser's stack.
        raise SearchFailure("The search answered in an unexpected format.") from None
    if not isinstance(results, list) or len(results) > MAX_FEATURES:
        raise SearchFailure("The search answered in an unexpected format.")
    features = []
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("attributes"), dict):
            raise SearchFailure("The search answered in an unexpected format.")
        attributes = result["attributes"]
        egid = attributes.get("bdg_egid")
        # null: the address has no building; a number must be a plain integer of at most nine digits.
        if egid is not None and (
            isinstance(egid, bool) or not isinstance(egid, int) or not 1 <= egid <= 999_999_999
        ):
            raise SearchFailure("The search answered in an unexpected format.")
        entrance = attributes.get("adr_edid")
        # null or missing: no entrance given; otherwise a plain integer (a SHORT of the official model).
        if entrance is not None and (
            isinstance(entrance, bool) or not isinstance(entrance, int) or not 0 <= entrance <= 32767
        ):
            raise SearchFailure("The search answered in an unexpected format.")
        features.append((
            _text(attributes.get("stn_label"), STREET_LIMIT),
            _text(attributes.get("adr_number"), HOUSE_NUMBER_LIMIT),
            _text(attributes.get("zip_label"), TOWN_LIMIT + 5),
            egid,
            entrance,
        ))
    return features, len(results) >= MAX_FEATURES


def _matches(features, street, house_number, postal_code, town):
    """The buildings (dicts) of the features that are this address, one per EGID and entrance, in that order."""
    wanted = (key(street), key(house_number))
    town_key = key(town) if town else ""
    found = {}
    for feature_street, feature_number, label, egid, entrance in features:
        code, _separator, locality = label.partition(" ")
        if egid is None or code != postal_code or (key(feature_street), key(feature_number)) != wanted:
            continue
        if town_key and town_key != key(locality) and town_key not in {key(part.strip()) for part in locality.split(",")}:
            continue
        found.setdefault((egid, -1 if entrance is None else entrance), {
            "egid": egid, "entrance_id": entrance, "street": feature_street, "house_number": feature_number,
            "postal_code": code, "locality": locality,
        })
    return [found[identity] for identity in sorted(found)][:MAX_BUILDINGS]


def _fetch(params, budget, deadline):
    """The body of the answer; runs in the helper thread of ``search``. Raises ``SearchFailure``."""
    try:
        with requests.Session() as session:
            # No proxy variables, no .netrc of the operator's environment.
            session.trust_env = False
            adapter = _Adapter(budget)
            session.mount("https://", adapter)
            session.mount("http://", adapter)  # only a test URL can use it: FIND_URL is https
            with session.get(
                FIND_URL, params=params, stream=True, allow_redirects=False, timeout=TIMEOUT,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                return _body(response, deadline)
    except requests.RequestException:
        # The text of the exception contains the address (query string): only the fact is kept.
        raise SearchFailure("The search could not be reached.") from None


def search(street, house_number, postal_code, town=""):
    """Ask swisstopo for the buildings of an address; return a list of dicts (empty: swisstopo does not know it).

    The arguments must have passed ``buildings.clean`` (the house number and the postal code are checked here
    again: nothing else may reach the filter). Raises ``SearchFailure`` when no trustworthy answer was received
    within ``TOTAL_SECONDS``.
    """
    if not HOUSE_NUMBER.fullmatch(house_number) or not POSTAL_CODE.fullmatch(postal_code):
        raise SearchFailure("The address cannot be searched.", counts=False)
    if not street or len(street) > STREET_LIMIT or len(town) > TOWN_LIMIT:
        raise SearchFailure("The address cannot be searched.", counts=False)
    params = _parameters(street, house_number, postal_code)
    budget = _Budget()
    outcome = {}

    def work():
        try:
            outcome["content"] = _fetch(params, budget, time.monotonic() + TOTAL_SECONDS)
        except SearchFailure as failure:
            outcome["failure"] = failure
        except Exception:  # noqa: BLE001 - nothing of it may reach the caller or a log (it can hold the address)
            outcome["failure"] = SearchFailure("The search could not be reached.")

    thread = threading.Thread(target=work, name="swisstopo-search", daemon=True)
    thread.start()
    thread.join(TOTAL_SECONDS)
    if thread.is_alive():
        # The hard deadline: end every socket of the request, then give the thread a moment to finish.
        budget.expire()
        thread.join(1)
        raise SearchFailure("The search took too long.")
    if "failure" in outcome:
        raise outcome["failure"]
    features, full = _features(outcome["content"])
    buildings = _matches(features, street, house_number, postal_code, town)
    if not buildings and full:
        # A full answer may have cut off the address: no conclusion.
        raise SearchFailure("The answer of the search may be incomplete.")
    return buildings
