"""The building (EGID) search: the swisstopo client, the local-first service, the stored answers, the update flag and the
rate limits (KONZEPT 3a, D-066). No test reaches the network: the HTTP session is always replaced."""

import json
import logging
import socketserver
import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.utils import timezone

from leg_platform.accounts.models import RateLimitBucket
from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.addresses import buildings, limits, swisstopo
from leg_platform.addresses.models import Address, BuildingLookup, Directory, ImportRequest


def feature(street="Example Street", number="12a", label="9999 Example Town", egid=4711, entrance=0, **extra):
    return {
        "layerBodId": swisstopo.LAYER, "featureId": 1,
        "attributes": {
            "adr_egaid": 1, "bdg_egid": egid, "adr_edid": entrance, "stn_label": street, "adr_number": number,
            "zip_label": label, **extra,
        },
    }


def answer(*features, status=200, headers=None, body=None):
    """A fake ``requests`` response of the find service."""
    content = body if body is not None else json.dumps({"results": list(features)}).encode()
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = status
    response.headers = {"Content-Type": "application/json", "Content-Length": str(len(content)), **(headers or {})}
    response.iter_content.return_value = [content]
    return response


def session_answering(response=None, error=None):
    session = MagicMock()
    session.__enter__.return_value = session
    if error is not None:
        session.get.side_effect = error
    else:
        session.get.return_value = response if response is not None else answer(feature())
    return session


def search_with(session, *args):
    with patch.object(swisstopo.requests, "Session", return_value=session):
        return swisstopo.search(*(args or ("Example Street", "12a", "9999", "Example Town")))


class ClientRequestTests(SimpleTestCase):
    def test_the_hard_limits_are_the_chosen_ones(self):
        self.assertEqual(swisstopo.TIMEOUT, (3, 4))
        self.assertEqual(swisstopo.TOTAL_SECONDS, 8)
        self.assertLess(swisstopo.TOTAL_SECONDS, 30)  # the sync worker of Gunicorn is killed after 30 s

    def test_a_refusal_of_the_input_does_not_count_against_the_service(self):
        with self.assertRaises(swisstopo.SearchFailure) as raised:
            search_with(session_answering(), "Example Street", "1' or 1=1", "9999", "")
        self.assertFalse(raised.exception.counts)
    def test_only_the_fixed_https_address_is_asked_without_redirects_proxies_or_unbounded_waits(self):
        session = session_answering()
        self.assertEqual(search_with(session)[0]["egid"], 4711)
        self.assertEqual(swisstopo.FIND_URL, "https://api3.geo.admin.ch/rest/services/api/MapServer/find")
        self.assertEqual(swisstopo.HOST, "api3.geo.admin.ch")
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.get.call_args.args, (swisstopo.FIND_URL,))
        options = session.get.call_args.kwargs
        self.assertIs(options["allow_redirects"], False)
        self.assertEqual(options["timeout"], swisstopo.TIMEOUT)
        self.assertTrue(options["stream"])
        self.assertEqual(options["headers"]["Accept-Encoding"], "identity")
        self.assertIs(session.trust_env, False)

    def test_only_what_the_documentation_names_is_sent(self):
        session = session_answering()
        search_with(session, "Example Street", "12a", "9999", "Example Town")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(set(params), {"layer", "searchField", "searchText", "contains", "returnGeometry", "layerDefs"})
        self.assertEqual(params["layer"], "ch.swisstopo.amtliches-gebaeudeadressverzeichnis")
        self.assertEqual((params["searchField"], params["searchText"]), ("zip_label", "9999"))
        self.assertEqual(params["returnGeometry"], "false")
        self.assertEqual(
            json.loads(params["layerDefs"]),
            {swisstopo.LAYER: "adr_number ilike '12a' and stn_label ilike 'Example Street'"},
        )

    def test_a_character_outside_the_safe_set_never_reaches_the_filter(self):
        session = session_answering()
        search_with(session, "Rue de l'\u00c9glise\"; DROP%", "1", "9999", "")
        expression = json.loads(session.get.call_args.kwargs["params"]["layerDefs"])[swisstopo.LAYER]
        street = expression.split("stn_label ilike '")[1].removesuffix("'")
        self.assertNotIn("'", street)
        self.assertNotIn('"', street)
        self.assertNotIn("%", street)
        self.assertNotIn(";", street)
        self.assertEqual(street, "Rue de l_\u00c9glise__ DROP_")

    def test_a_house_number_or_postal_code_that_could_change_the_filter_is_refused_before_any_request(self):
        for number, postal in (
            ("1' or 1=1 --", "9999"), ("1%", "9999"), ("1_", "9999"), ("", "9999"), ("1" * 13, "9999"),
            ("1 2", "9999"), ("12a", "999"), ("12a", "99999"), ("12a", "0999"), ("12a", "9999 "), ("12a", "9999'"),
        ):
            session = session_answering()
            with self.subTest(number=number, postal=postal), self.assertRaises(swisstopo.SearchFailure):
                search_with(session, "Example Street", number, postal, "")
            session.get.assert_not_called()

    def test_no_data_of_the_address_is_in_the_failure(self):
        for error in (requests.ConnectionError("https://api3.geo.admin.ch/?searchText=9999 Secret Street"),
                      requests.Timeout("Secret Street 12a")):
            with self.subTest(error=type(error).__name__), self.assertRaises(swisstopo.SearchFailure) as raised:
                search_with(session_answering(error=error), "Secret Street", "12a", "9999", "")
            self.assertNotIn("Secret", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertTrue(raised.exception.__suppress_context__)


class ClientAnswerTests(SimpleTestCase):
    def failing(self, response):
        with self.assertRaises(swisstopo.SearchFailure):
            search_with(session_answering(response))

    def test_a_redirect_or_an_error_status_is_a_failure_and_is_not_followed(self):
        for status in (301, 302, 307, 400, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.failing(answer(feature(), status=status, headers={"Location": "https://example.invalid/"}))

    def test_an_unexpected_format_is_a_failure(self):
        self.failing(answer(feature(), headers={"Content-Type": "text/html"}))
        self.failing(answer(feature(), headers={"Content-Type": ""}))
        self.failing(answer(feature(), headers={"Content-Encoding": "br"}))
        self.failing(answer(feature(), headers={"Content-Encoding": "gzip"}))

    def test_an_oversized_answer_is_a_failure(self):
        self.failing(answer(feature(), headers={"Content-Length": str(swisstopo.MAX_RESPONSE_BYTES + 1)}))
        self.failing(answer(feature(), headers={"Content-Length": "many"}))
        # No announced length: the stream is counted.
        big = answer(body=b"x" * (swisstopo.MAX_RESPONSE_BYTES + 1))
        del big.headers["Content-Length"]
        self.failing(big)

    def test_a_slow_answer_is_refused_by_the_second_layer_of_the_body_reader(self):
        # The hard deadline is ``search`` itself (``DeadlineTests``); the body reader checks the clock between chunks.
        with self.assertRaises(swisstopo.SearchFailure):
            swisstopo._body(answer(feature()), time.monotonic() - 1)
        self.assertTrue(swisstopo._body(answer(feature()), time.monotonic() + 60))

    def test_deeply_nested_json_is_a_failure_not_a_crash(self):
        body = b"[" * swisstopo.MAX_RESPONSE_BYTES  # 256 KiB, within the size limit
        self.assertEqual(len(body), 256 * 1024)
        with self.assertRaises(swisstopo.SearchFailure) as raised:
            search_with(session_answering(answer(body=body)))
        self.assertTrue(raised.exception.counts)
        with self.assertRaises(swisstopo.SearchFailure):
            search_with(session_answering(answer(body=b'{"results": ' + b"[" * 100000 + b"}")))

    def test_invalid_or_unexpected_json_is_a_failure(self):
        for body in (b"", b"{", b"[]", b"null", b'{"results": null}', b'{"results": {}}', b'{"other": []}',
                     b'{"results": [1]}', b'{"results": [{"attributes": null}]}', b'{"results": [{}]}'):
            with self.subTest(body=body):
                self.failing(answer(body=body))

    def test_a_bad_value_is_a_failure(self):
        for bad in (
            feature(egid="4711"), feature(egid=True), feature(egid=0), feature(egid=1_000_000_000), feature(egid=1.5),
            feature(egid=-3), feature(entrance="0"), feature(entrance=True), feature(entrance=-1),
            feature(entrance=32768), feature(entrance=1.5), feature(street=None), feature(street=""), feature(street="S" * 151),
            feature(number=12), feature(number="1" * 13), feature(label=None), feature(label="9999 \x00Town"),
            feature(street="Line\nBreak"),
        ):
            with self.subTest(bad=bad["attributes"]):
                self.failing(answer(bad))

    def test_more_features_than_the_service_returns_is_a_failure(self):
        self.failing(answer(*[feature(egid=n + 1) for n in range(swisstopo.MAX_FEATURES + 1)]))

    def test_a_full_answer_without_the_address_is_no_conclusion_but_with_it_is_one(self):
        others = [feature(street=f"Other {n}", egid=n + 1) for n in range(swisstopo.MAX_FEATURES)]
        self.failing(answer(*others))
        found = search_with(session_answering(answer(*others[:-1], feature())))
        self.assertEqual(found[0]["egid"], 4711)

    def test_an_answer_without_the_address_is_empty_not_a_failure(self):
        self.assertEqual(search_with(session_answering(answer(feature(street="Other Street")))), [])
        self.assertEqual(search_with(session_answering(answer())), [])
        # A feature without a building (null) is no building.
        self.assertEqual(search_with(session_answering(answer(feature(egid=None)))), [])


class _SlowServer(socketserver.ThreadingTCPServer):
    """A test-only server on 127.0.0.1 that answers the way a faulty service does (one byte now and then)."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.closed = threading.Event()  # the client side went away
        self.connected = threading.Event()
        super().__init__(("127.0.0.1", 0), _SlowHandler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/rest/services/api/MapServer/find"


class _SlowHandler(socketserver.BaseRequestHandler):
    DRIP = 0.3
    LIFETIME = 20  # never longer than this, whatever the client does

    def handle(self):
        server = self.server
        server.connected.set()
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = self.request.recv(4096)
            if not chunk:
                server.closed.set()
                return
            request += chunk
        ended = time.monotonic() + self.LIFETIME
        try:
            if server.behaviour == "body":
                self.request.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n"
                )
                while time.monotonic() < ended:
                    self.request.sendall(b"x")
                    time.sleep(self.DRIP)
            elif server.behaviour == "headers":
                self.request.sendall(b"HTTP/1.1 200 OK\r\n")
                while time.monotonic() < ended:
                    self.request.sendall(b"X")
                    time.sleep(self.DRIP)
            else:  # "silent": reads the request and never answers
                while time.monotonic() < ended:
                    if not self.request.recv(1):
                        break
                    time.sleep(self.DRIP)
        except OSError:
            pass
        server.closed.set()


class DeadlineTests(SimpleTestCase):
    """The total deadline is hard (review finding 1): a server that sends one byte every 0.3 s cannot hold the caller.

    The tests talk to a server of their own on 127.0.0.1; the approved-URL rule is kept because the code under test
    reads ``FIND_URL`` and nothing else: the tests replace only that constant (a seam), never the logic.
    """

    TOTAL = 1.0

    def ask(self, behaviour):
        server = _SlowServer(behaviour)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        with patch.object(swisstopo, "FIND_URL", server.url), patch.object(swisstopo, "TOTAL_SECONDS", self.TOTAL), \
                patch.object(swisstopo, "TIMEOUT", (1, 30)):  # the per-wait timeout alone would take 30 s
            started = time.monotonic()
            with self.assertRaises(swisstopo.SearchFailure) as raised:
                swisstopo.search("Example Street", "12a", "9999", "")
            elapsed = time.monotonic() - started
        return server, raised.exception, elapsed

    def check(self, behaviour):
        server, failure, elapsed = self.ask(behaviour)
        self.assertTrue(server.connected.is_set())
        self.assertGreaterEqual(elapsed, self.TOTAL - 0.1)
        self.assertLess(elapsed, self.TOTAL + 2.0, f"the caller was held for {elapsed:.1f} s")
        self.assertTrue(failure.counts)
        self.assertEqual(str(failure), "The search took too long.")
        # The sockets were shut down: the server sees the client leave, and no helper thread is left behind.
        self.assertTrue(server.closed.wait(5), "the connection was not closed")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(t.name == "swisstopo-search" for t in threading.enumerate()):
            time.sleep(0.05)
        self.assertFalse(any(t.name == "swisstopo-search" for t in threading.enumerate()))

    def test_a_body_of_one_byte_every_third_of_a_second_is_cut_off_at_the_deadline(self):
        self.check("body")

    def test_headers_of_one_byte_every_third_of_a_second_are_cut_off_at_the_deadline(self):
        self.check("headers")

    def test_a_server_that_never_answers_is_cut_off_at_the_deadline(self):
        self.check("silent")

    def test_the_approved_address_is_untouched_by_the_seam(self):
        self.assertEqual(swisstopo.FIND_URL, "https://api3.geo.admin.ch/rest/services/api/MapServer/find")
        self.assertEqual(swisstopo.TOTAL_SECONDS, 8)


class ClientMatchTests(SimpleTestCase):
    def test_several_buildings_of_one_address_are_all_returned_once_in_the_order_of_the_egid(self):
        session = session_answering(answer(feature(egid=30), feature(egid=10), feature(egid=30), feature(egid=20)))
        self.assertEqual([found["egid"] for found in search_with(session)], [10, 20, 30])

    def test_one_candidate_per_egid_and_entrance_with_the_entrance_in_it(self):
        session = session_answering(answer(
            feature(egid=10, entrance=2), feature(egid=10, entrance=1), feature(egid=10, entrance=1),
            feature(egid=5, entrance=None),
        ))
        self.assertEqual(
            [(found["egid"], found["entrance_id"]) for found in search_with(session)], [(5, None), (10, 1), (10, 2)],
        )

    def test_an_answer_without_the_entrance_attribute_gives_no_entrance(self):
        item = feature()
        del item["attributes"]["adr_edid"]
        self.assertIsNone(search_with(session_answering(answer(item)))[0]["entrance_id"])

    def test_case_accents_and_a_town_part_are_compared_folded(self):
        session = session_answering(answer(feature(street="Rue de l'\u00c9glise", number="1B", label="1260 Nyon")))
        found = search_with(session, "rue de l'eglise", "1b", "1260", "NYON")
        self.assertEqual((found[0]["street"], found[0]["locality"]), ("Rue de l'\u00c9glise", "Nyon"))
        multi = session_answering(answer(feature(label="9999 Example Town, Other Place")))
        self.assertEqual(len(search_with(multi, "Example Street", "12a", "9999", "other place")), 1)

    def test_another_postal_code_town_street_or_number_is_not_a_match(self):
        for label, street, number, town in (
            ("9998 Example Town", "Example Street", "12a", ""), ("9999 Example Town", "Example Street", "12a", "Elsewhere"),
            ("9999 Example Town", "Example Strasse", "12a", ""), ("9999 Example Town", "Example Street", "12", ""),
        ):
            with self.subTest(label=label, street=street, number=number, town=town):
                session = session_answering(answer(feature(street=street, number=number, label=label)))
                self.assertEqual(search_with(session, "Example Street", "12a", "9999", town or ""), [])

    def test_a_missing_town_matches_every_town_of_the_postal_code(self):
        session = session_answering(answer(feature()))
        self.assertEqual(len(search_with(session, "Example Street", "12a", "9999", "")), 1)


@tag("mariadb")
@override_settings(LANGUAGE_CODE="en")
class BuildingSearchTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=cls.generation, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=5, has_egid=True,
        )
        for street, number, egid, entrance in (
            ("Example Street", "1", 100, 0), ("Example Street", "2", 200, 0), ("Example Street", "2", 201, 1),
            ("Sample Avenue", "5", 200, 0), ("Example Street", "3", None, None),
            ("Example Street", "4", 300, 1), ("Example Street", "4", 300, 0), ("Example Street", "4", 300, 0),
        ):
            Address.objects.create(
                generation=cls.generation, street=street, house_number=number, postal_code="9999",
                locality="Example Town", egid=egid, entrance_id=entrance,
            )
        cls.user = create_active_user("search.person@example.org")

    def setUp(self):
        search = patch.object(swisstopo, "search", return_value=[])
        self.search = search.start()
        self.addCleanup(search.stop)

    def find(self, *values, user=None):
        values = values or ("Example Street", "1", "9999", "Example Town")
        return buildings.find_buildings(*values, user=user)

    # --- local directory first -------------------------------------------------------------------------------

    def test_a_known_address_is_answered_by_the_local_directory_without_any_external_request(self):
        for user in (None, self.user):
            result = self.find(user=user)
            self.assertEqual((result.outcome, result.source), (buildings.FOUND, buildings.SOURCE_DIRECTORY))
            self.assertEqual([b.egid for b in result.buildings], [100])
        self.search.assert_not_called()
        self.assertFalse(BuildingLookup.objects.exists())

    def test_one_address_with_several_buildings_returns_all_of_them(self):
        result = self.find("Example Street", "2", "9999", "Example Town")
        self.assertEqual([b.egid for b in result.buildings], [200, 201])

    def test_a_candidate_carries_the_egid_the_entrance_and_the_address_it_is_listed_under(self):
        result = self.find("Example Street", "2", "9999", "Example Town")
        self.assertEqual(
            [(b.egid, b.entrance_id, b.house_number, b.street, b.postal_code, b.locality) for b in result.buildings],
            [(200, 0, "2", "Example Street", "9999", "Example Town"),
             (201, 1, "2", "Example Street", "9999", "Example Town")],
        )

    def test_the_entrances_of_one_building_are_separate_candidates_in_order_and_each_once(self):
        result = self.find("Example Street", "4", "9999", "Example Town")
        self.assertEqual([(b.egid, b.entrance_id) for b in result.buildings], [(300, 0), (300, 1)])

    def test_a_row_without_an_entrance_gives_a_candidate_with_none(self):
        Address.objects.create(
            generation=self.generation, street="Example Street", house_number="6", postal_code="9999",
            locality="Example Town", egid=600,
        )
        self.assertEqual(
            [(b.egid, b.entrance_id) for b in self.find("Example Street", "6", "9999", "Example Town").buildings],
            [(600, None)],
        )

    def test_several_addresses_of_one_building_share_the_egid(self):
        first = self.find("Example Street", "2", "9999", "Example Town").buildings[0]
        second = self.find("Sample Avenue", "5", "9999", "Example Town").buildings[0]
        self.assertEqual(first.egid, second.egid)
        self.assertNotEqual(first.street, second.street)

    def test_spelling_case_and_accents_do_not_matter_and_the_town_is_optional(self):
        for values in (("example street", "1", "9999", "EXAMPLE TOWN"), ("Example Street", "1", "9999", ""),
                       ("  Example   Street ", "1", "9999", "Example Town")):
            with self.subTest(values=values):
                self.assertEqual([b.egid for b in self.find(*values).buildings], [100])
        self.assertEqual(self.find("Example Street", "1", "9999", "Elsewhere").buildings, ())

    def test_a_row_without_a_building_is_no_candidate(self):
        result = self.find("Example Street", "3", "9999", "Example Town")
        self.assertEqual(result.buildings, ())

    def test_a_directory_without_identifiers_or_without_rows_answers_nothing(self):
        Directory.objects.filter(pk=1).update(has_egid=False)
        self.assertEqual(self.find().buildings, ())
        Directory.objects.filter(pk=1).update(has_egid=True, row_count=0)
        self.assertEqual(self.find().buildings, ())

    def test_only_the_published_generation_is_searched(self):
        Address.objects.create(
            generation=uuid.uuid4(), street="Example Street", house_number="9", postal_code="9999",
            locality="Example Town", egid=999,
        )
        self.assertEqual(self.find("Example Street", "9", "9999", "Example Town").buildings, ())

    def test_an_unreadable_directory_counts_as_nothing_found_locally(self):
        with patch.object(buildings.Directory.objects, "filter", side_effect=buildings.DatabaseError("x")):
            with self.assertLogs("leg_platform.addresses.buildings", "WARNING"):
                self.assertEqual(self.find().outcome, buildings.UNAVAILABLE)

    # --- suggestions for typing mistakes (local only) --------------------------------------------------------

    def test_a_typing_mistake_gets_the_buildings_of_the_similar_address_and_asks_nobody_outside(self):
        found = buildings.suggest("Exampel Street", "1", "9999", "Example Town")
        self.assertEqual([(b.egid, b.street, b.house_number) for b in found], [(100, "Example Street", "1")])
        self.search.assert_not_called()
        self.assertFalse(BuildingLookup.objects.exists())

    def test_no_suggestion_when_the_typed_address_is_known_or_nothing_is_similar(self):
        self.assertEqual(buildings.suggest("Example Street", "1", "9999", "Example Town"), ())
        self.assertEqual(buildings.suggest("Qqqqqqqq Zzzzzzz", "1", "9999", "Example Town"), ())
        self.assertEqual(buildings.suggest("Exampel Street", "", "9999", "Example Town"), ())
        self.search.assert_not_called()

    def test_no_suggestion_from_a_directory_without_identifiers_or_an_unreadable_one(self):
        Directory.objects.filter(pk=1).update(has_egid=False)
        self.assertEqual(buildings.suggest("Exampel Street", "1", "9999", "Example Town"), ())
        Directory.objects.filter(pk=1).update(has_egid=True)
        with patch.object(buildings.Directory.objects, "filter", side_effect=buildings.DatabaseError("x")):
            with self.assertLogs("leg_platform.addresses.buildings", "WARNING"):
                self.assertEqual(buildings.suggest("Exampel Street", "1", "9999", "Example Town"), ())

    def test_suggestions_are_few_and_without_duplicates(self):
        found = buildings.suggest("Exampel Street", "4", "9999", "Example Town")
        self.assertEqual([(b.egid, b.entrance_id) for b in found], [(300, 0), (300, 1)])
        self.assertLessEqual(len(found), buildings.SUGGESTION_LIMIT)

    # --- input -----------------------------------------------------------------------------------------------

    def test_an_incomplete_or_invalid_input_is_invalid_and_asks_nobody(self):
        for values in (
            ("", "1", "9999", "Town"), ("Street", "", "9999", "Town"), ("Street", "1", "", "Town"),
            ("Street", "1", "999", "Town"), ("Street", "1' or '1", "9999", "Town"), ("Street", "1", "9999", "T\x00"),
            ("S" * 151, "1", "9999", "Town"), ("Street", "1", "9999", "T" * 151), ("Street", None, "9999", "Town"),
            ("Street", "1", 9999, "Town"), ("Stre\net", "1", "9999", "Town"),
        ):
            with self.subTest(values=values):
                result = self.find(*values, user=self.user)
                self.assertEqual((result.outcome, result.buildings), (buildings.INVALID, ()))
        self.search.assert_not_called()

    # --- the external search ---------------------------------------------------------------------------------

    def unknown(self):
        return ("Unknown Street", "7", "9999", "Example Town")

    def test_an_anonymous_visitor_and_an_inactive_account_never_cause_an_external_request(self):
        inactive = SimpleNamespace(pk=5, is_authenticated=True, is_active=False)
        for user in (None, inactive, object()):
            with self.subTest(user=user):
                result = self.find(*self.unknown(), user=user)
                self.assertEqual((result.outcome, result.buildings), (buildings.UNAVAILABLE, ()))
        self.search.assert_not_called()
        self.assertFalse(BuildingLookup.objects.exists())
        self.assertFalse(RateLimitBucket.objects.exists())
        self.assertIsNone(ImportRequest.objects.filter(pk=1).values_list("update_flag_set_at", flat=True).first())

    def test_a_signed_in_account_asks_swisstopo_once_and_the_answer_is_stored(self):
        self.search.return_value = [
            {"egid": 777, "entrance_id": 2, "street": "Unknown Street", "house_number": "7", "postal_code": "9999",
             "locality": "Example Town"},
        ]
        first = self.find(*self.unknown(), user=self.user)
        self.assertEqual((first.outcome, first.source), (buildings.FOUND, buildings.SOURCE_SWISSTOPO))
        self.assertEqual((first.buildings[0].egid, first.buildings[0].entrance_id), (777, 2))
        self.search.assert_called_once_with(*self.unknown())
        second = self.find(*self.unknown(), user=self.user)
        anonymous = self.find(*self.unknown())
        for repeat in (second, anonymous):
            self.assertEqual((repeat.outcome, repeat.source), (buildings.FOUND, buildings.SOURCE_STORED))
            self.assertEqual(repeat.buildings, first.buildings)
        self.search.assert_called_once()
        # A found address does not set the flag.
        self.assertFalse(ImportRequest.objects.exclude(update_flag_set_at=None).exists())

    def test_the_stored_answer_holds_a_digest_and_never_the_text_of_a_negative_address(self):
        self.find("Secret Street", "7", "9999", "Secret Town", user=self.user)
        stored = BuildingLookup.objects.get()
        self.assertEqual(stored.buildings, [])
        self.assertRegex(stored.digest, r"^[0-9a-f]{64}$")
        self.assertNotIn("secret", json.dumps([stored.digest, stored.buildings]).lower())
        # Another spelling of the same address is the same digest; another address another one.
        self.assertEqual(
            stored.digest, buildings._digest(*buildings.clean(" secret  street", "7", "9999", "SECRET TOWN")),
        )
        self.assertNotEqual(stored.digest, buildings._digest(*buildings.clean("Secret Street", "8", "9999", "Secret Town")))

    def test_a_negative_answer_sets_the_flag_once_and_is_not_asked_again_for_a_limited_time(self):
        result = self.find(*self.unknown(), user=self.user)
        self.assertEqual((result.outcome, result.source, result.buildings), (buildings.NOT_FOUND, buildings.SOURCE_SWISSTOPO, ()))
        flag = ImportRequest.objects.get(pk=1).update_flag_set_at
        self.assertIsNotNone(flag)
        again = self.find(*self.unknown(), user=self.user)
        self.assertEqual((again.outcome, again.source), (buildings.NOT_FOUND, buildings.SOURCE_STORED))
        self.search.assert_called_once()
        # Further unknown addresses leave the flag as it is: set at most once.
        self.find("Other Street", "9", "9999", "Example Town", user=self.user)
        self.assertEqual(self.search.call_count, 2)
        self.assertEqual(ImportRequest.objects.get(pk=1).update_flag_set_at, flag)

    def test_a_negative_answer_expires_after_seven_days_and_a_positive_one_after_ninety(self):
        self.find(*self.unknown(), user=self.user)
        self.assertEqual(buildings.NEGATIVE_CACHE_DAYS, 7)
        self.assertEqual(buildings.POSITIVE_CACHE_DAYS, 90)
        later = timezone.now() + timedelta(days=7, minutes=1)
        with patch.object(buildings.timezone, "now", return_value=later):
            result = self.find(*self.unknown(), user=self.user)
        self.assertEqual(result.source, buildings.SOURCE_SWISSTOPO)
        self.assertEqual(self.search.call_count, 2)
        self.assertEqual(BuildingLookup.objects.count(), 1)
        # A positive one.
        self.search.return_value = [{"egid": 5, "street": "Unknown Street", "house_number": "7",
                                     "postal_code": "9999", "locality": "Example Town"}]
        self.find("Fresh Street", "1", "9999", "Example Town", user=self.user)
        calls = self.search.call_count
        for days, source in ((89, buildings.SOURCE_STORED), (91, buildings.SOURCE_SWISSTOPO)):
            with patch.object(buildings.timezone, "now", return_value=timezone.now() + timedelta(days=days)):
                self.assertEqual(self.find("Fresh Street", "1", "9999", "Example Town", user=self.user).source, source)
        self.assertEqual(self.search.call_count, calls + 1)

    def test_a_search_that_failed_stores_nothing_and_sets_no_flag(self):
        self.search.side_effect = swisstopo.SearchFailure("The search did not answer.")
        with self.assertLogs("leg_platform.addresses.buildings", "WARNING") as logs:
            result = self.find("Secret Street", "7", "9999", "Secret Town", user=self.user)
        self.assertEqual((result.outcome, result.buildings), (buildings.UNAVAILABLE, ()))
        self.assertFalse(BuildingLookup.objects.exists())
        self.assertFalse(ImportRequest.objects.exclude(update_flag_set_at=None).exists())
        self.assertNotIn("Secret", "\n".join(logs.output))

    def test_a_stored_answer_survives_an_import_of_the_directory(self):
        self.search.return_value = [{"egid": 5, "street": "Unknown Street", "house_number": "7",
                                     "postal_code": "9999", "locality": "Example Town"}]
        self.find(*self.unknown(), user=self.user)
        Directory.objects.filter(pk=1).update(generation=uuid.uuid4())
        Address.objects.filter(generation=self.generation).delete()
        self.assertEqual(self.find(*self.unknown()).source, buildings.SOURCE_STORED)

    def test_a_damaged_stored_answer_is_asked_again(self):
        self.find(*self.unknown(), user=self.user)
        BuildingLookup.objects.update(buildings={"not": "a list"})
        self.find(*self.unknown(), user=self.user)
        self.assertEqual(self.search.call_count, 2)

    def test_nothing_of_the_address_is_logged_by_a_successful_search(self):
        self.search.return_value = []
        with self.assertLogs("leg_platform.addresses", logging.DEBUG) as logs:
            self.find("Secret Street", "7", "9999", "Secret Town", user=self.user)
        self.assertNotIn("Secret", "\n".join(logs.output))
        self.assertNotIn("9999", "\n".join(logs.output))

    # --- rate limits -----------------------------------------------------------------------------------------

    def test_an_account_cannot_use_the_portal_as_a_proxy_beyond_its_budget(self):
        budget = limits.EXTERNAL_USER_LIMITS[0][0]
        for number in range(budget):
            self.assertEqual(self.find("Proxy Street", str(number + 1), "9999", "", user=self.user).outcome, buildings.NOT_FOUND)
        self.assertEqual(self.search.call_count, budget)
        refused = self.find("Proxy Street", "99", "9999", "", user=self.user)
        self.assertEqual((refused.outcome, refused.buildings), (buildings.UNAVAILABLE, ()))
        self.assertEqual(self.search.call_count, budget)
        # The refused one stored nothing and a stored answer is still served.
        self.assertFalse(BuildingLookup.objects.filter(digest=buildings._digest(*buildings.clean("Proxy Street", "99", "9999", ""))).exists())
        self.assertEqual(self.find("Proxy Street", "1", "9999", "", user=self.user).source, buildings.SOURCE_STORED)
        # Another account has its own budget.
        other = create_active_user("other.search@example.org")
        self.assertEqual(self.find("Proxy Street", "99", "9999", "", user=other).outcome, buildings.NOT_FOUND)

    def test_the_whole_instance_has_a_budget_too(self):
        users = [create_active_user(f"many.{n}@example.org") for n in range(3)]
        with patch.object(limits, "EXTERNAL_GLOBAL_LIMITS", ((2, timedelta(minutes=1)),)):
            outcomes = [
                self.find("Global Street", str(n + 1), "9999", "", user=user).outcome for n, user in enumerate(users)
            ]
        self.assertEqual(outcomes, [buildings.NOT_FOUND, buildings.NOT_FOUND, buildings.UNAVAILABLE])
        self.assertEqual(self.search.call_count, 2)

    def test_a_failing_limit_check_fails_closed(self):
        with patch("leg_platform.accounts.rate_limits.exhausted", side_effect=buildings.DatabaseError("x")):
            with self.assertLogs("leg_platform.addresses.limits", "WARNING"):
                self.assertEqual(self.find(*self.unknown(), user=self.user).outcome, buildings.UNAVAILABLE)
        self.search.assert_not_called()

    def test_the_limits_refuse_an_anonymous_caller_directly(self):
        self.assertFalse(limits.allow_external_lookup(None))
        self.assertFalse(limits.allow_external_lookup(object()))
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(limits.allow_external_lookup(AnonymousUser()))
        self.assertTrue(limits.allow_external_lookup(self.user))

    def test_the_budget_per_account_is_six_a_minute_and_ten_an_hour(self):
        self.assertEqual(limits.EXTERNAL_USER_LIMITS, ((6, timedelta(minutes=1)), (10, timedelta(hours=1))))
        with patch.object(limits, "EXTERNAL_USER_LIMITS", ((100, timedelta(minutes=1)),) + limits.EXTERNAL_USER_LIMITS[1:]):
            outcomes = [
                self.find("Hour Street", str(n + 1), "9999", "", user=self.user).outcome for n in range(11)
            ]
        self.assertEqual(outcomes, [buildings.NOT_FOUND] * 10 + [buildings.UNAVAILABLE])
        self.assertEqual(self.search.call_count, 10)

    # --- the circuit breaker ---------------------------------------------------------------------------------

    def failing_search(self):
        self.search.side_effect = swisstopo.SearchFailure("The search could not be reached.")

    def trip(self, count=None):
        """``count`` failed searches by different accounts (so that no account budget gets in the way)."""
        self.failing_search()
        for number in range(count or limits.CIRCUIT_LIMITS[0][0]):
            user = create_active_user(f"trip.{number}.{uuid.uuid4().hex[:6]}@example.org")
            with self.assertLogs("leg_platform.addresses.buildings", "WARNING"):
                result = self.find("Trip Street", str(number + 1), "9999", "", user=user)
            self.assertEqual((result.outcome, result.reason), (buildings.UNAVAILABLE, buildings.REASON_FAILED))

    def test_after_five_failures_nobody_asks_swisstopo_and_no_budget_is_used(self):
        self.assertEqual(limits.CIRCUIT_LIMITS, ((5, timedelta(minutes=5)),))
        self.trip()
        self.assertTrue(limits.circuit_open())
        calls = self.search.call_count
        buckets = RateLimitBucket.objects.count()
        result = self.find("Open Street", "1", "9999", "", user=self.user)
        self.assertEqual((result.outcome, result.reason), (buildings.UNAVAILABLE, buildings.REASON_CIRCUIT_OPEN))
        self.assertEqual(self.search.call_count, calls)
        # No bucket of the account or of the instance budget was touched by the paused search.
        self.assertEqual(RateLimitBucket.objects.count(), buckets)
        # A stored answer and the directory are still served while it is open.
        self.assertEqual(self.find("Example Street", "1", "9999", "", user=self.user).source, buildings.SOURCE_DIRECTORY)

    def test_four_failures_do_not_open_it_and_a_success_forgets_them(self):
        self.trip(4)
        self.assertFalse(limits.circuit_open())
        self.search.side_effect = None
        self.search.return_value = []
        self.assertEqual(self.find("Fine Street", "1", "9999", "", user=self.user).outcome, buildings.NOT_FOUND)
        self.trip(4)
        self.assertFalse(limits.circuit_open())  # 4 + success + 4: never five in a row

    def test_it_closes_again_when_the_window_has_passed(self):
        self.trip()
        self.assertTrue(limits.circuit_open())
        with patch.object(buildings.timezone, "now", return_value=timezone.now() + timedelta(minutes=5, seconds=5)):
            self.assertFalse(limits.circuit_open())
            self.search.side_effect = None
            self.search.return_value = []
            self.assertEqual(self.find("Back Street", "1", "9999", "", user=self.user).outcome, buildings.NOT_FOUND)

    def test_a_refused_input_is_no_failure_and_a_broken_breaker_fails_closed(self):
        self.search.side_effect = swisstopo.SearchFailure("The address cannot be searched.", counts=False)
        for number in range(6):
            user = create_active_user(f"input.{number}@example.org")
            with self.assertLogs("leg_platform.addresses.buildings", "WARNING"):
                self.find("Input Street", str(number + 1), "9999", "", user=user)
        self.assertFalse(limits.circuit_open())
        self.search.side_effect = None
        with patch("leg_platform.accounts.rate_limits.exhausted", side_effect=buildings.DatabaseError("x")):
            with self.assertLogs("leg_platform.addresses.limits", "WARNING"):
                self.assertTrue(limits.circuit_open())

    def test_the_breaker_is_shared_state_in_the_database_not_in_the_process(self):
        self.trip()
        # A fresh read (another worker) sees the same rows.
        self.assertGreaterEqual(RateLimitBucket.objects.count(), 1)
        self.assertTrue(limits.circuit_open())

    # --- the path for an unavailable search (review finding 3) -----------------------------------------------

    def test_needs_review_is_true_when_nothing_was_found_and_when_the_person_cannot_be_served_now(self):
        for outcome, reason, expected in (
            (buildings.NOT_FOUND, "", True),
            (buildings.UNAVAILABLE, buildings.REASON_LIMIT, True),
            (buildings.UNAVAILABLE, buildings.REASON_CIRCUIT_OPEN, True),
            (buildings.UNAVAILABLE, buildings.REASON_FAILED, True),
            (buildings.UNAVAILABLE, buildings.REASON_NOT_SIGNED_IN, False),
            (buildings.INVALID, "", False),
            (buildings.FOUND, "", False),
        ):
            with self.subTest(outcome=outcome, reason=reason):
                self.assertIs(buildings.BuildingSearch((), outcome, reason=reason).needs_review, expected)

    def test_every_refusal_of_the_external_search_says_why(self):
        self.assertEqual(self.find(*self.unknown()).reason, buildings.REASON_NOT_SIGNED_IN)
        self.assertFalse(self.find(*self.unknown()).needs_review)
        with patch.object(limits, "EXTERNAL_USER_LIMITS", ((1, timedelta(minutes=1)),)):
            self.find(*self.unknown(), user=self.user)
            refused = self.find("Other Street", "9", "9999", "", user=self.user)
        self.assertEqual((refused.outcome, refused.reason, refused.needs_review), (buildings.UNAVAILABLE, buildings.REASON_LIMIT, True))
        found_in_directory = self.find()
        self.assertFalse(found_in_directory.needs_review)

    # --- cleanup ---------------------------------------------------------------------------------------------

    def test_the_cleanup_deletes_only_expired_answers(self):
        now = timezone.now()
        BuildingLookup.objects.create(digest="a" * 64, expires_at=now - timedelta(seconds=1), buildings=[])
        BuildingLookup.objects.create(digest="b" * 64, expires_at=now + timedelta(days=1), buildings=[])
        self.assertEqual(buildings.delete_expired(), 1)
        self.assertEqual(list(BuildingLookup.objects.values_list("digest", flat=True)), ["b" * 64])

    def test_the_cleanup_command_reports_a_number_only(self):
        from io import StringIO

        BuildingLookup.objects.create(digest="a" * 64, expires_at=timezone.now() - timedelta(days=1), buildings=[])
        out = StringIO()
        call_command("cleanup_address_lookups", stdout=out)
        self.assertEqual(out.getvalue().strip(), "Deleted 1 expired stored building searches.")
