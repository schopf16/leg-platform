"""Transformer station scan (D-118): the map reading, the limits and politeness, the weekly step, the stored result.

No test reaches the network or loads an OCR model: the session, the map tiles and the label reader are fakes. The tests
that read pixels need numpy, scipy and Pillow (``requirements/transformers.txt``) and are skipped without them.
"""

import importlib.util
import io
import re
import socketserver
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.db.migrations.loader import MigrationLoader
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.addresses import importer, jobs, runner, schedule, setup, status, transformers
from leg_platform.addresses.admin import SERVICE_AREA_PERMISSION, ServicePostalCodeAdmin
from leg_platform.addresses.models import Address, Directory, ImportRequest, ServicePostalCode, TransformerStation
from leg_platform.addresses.router import AddressRouter

ZURICH = ZoneInfo("Europe/Zurich")
PACKAGES = all(importlib.util.find_spec(name) is not None for name in ("numpy", "scipy", "PIL"))
needs_packages = unittest.skipUnless(
    PACKAGES, "needs the image packages of requirements/transformers.txt (not installed here)",
)


def publish(rows, generation=None):
    """Publish a generation with the given address rows (dicts of the address fields) and return its uuid."""
    generation = generation or uuid.uuid4()
    Directory.objects.update_or_create(
        pk=1, defaults={
            "generation": generation, "data_date": timezone.localdate(), "imported_at": timezone.now(),
            "checksum": "0" * 64, "row_count": len(rows), "has_egid": True,
        },
    )
    for row in rows:
        Address.objects.create(generation=generation, street="Example Street", house_number="1", locality="Town", **row)
    return generation


def running_scan(token=None):
    """The job row as the claim of a transformer scan leaves it, and that claim."""
    token = token or uuid.uuid4()
    ImportRequest.objects.update_or_create(
        pk=1, defaults={
            "state": ImportRequest.State.RUNNING, "source": ImportRequest.Source.TRANSFORMER,
            "claim_token": token, "started_at": timezone.now(), "heartbeat_at": timezone.now(),
        },
    )
    return jobs.Claim(token, False, False, ImportRequest.Source.TRANSFORMER)


class FakeReader:
    """Stands in for the OCR reader: returns the labels it was given for every tile."""

    def __init__(self, labels=()):
        self.labels = list(labels)

    def read_tile(self, img):
        return list(self.labels)


class FakeResponse:
    def __init__(self, body=b"", status=200, content_type="image/png"):
        self.body = body
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        yield self.body


class FakeSession:
    """Answers every request with the given response (a PNG by default) and records the keyword arguments."""

    def __init__(self, png=None, responses=(), error=None):
        self.png = png
        self.responses = list(responses)
        self.error = error
        self.calls = []
        self.mounted = []

    def mount(self, prefix, adapter):
        self.mounted.append(prefix)

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(self.png)


def tile_png(px, dots=((50, 50),)):
    """A synthetic tile: red dots (saturated) and a grey bar (text, not a dot). Returns (PNG bytes, RGBA array)."""
    import numpy as np
    from PIL import Image

    canvas = np.zeros((px, px, 4), dtype=np.uint8)
    yy, xx = np.ogrid[:px, :px]
    for col, row in dots:
        canvas[(xx - col) ** 2 + (yy - row) ** 2 <= 16] = (220, 30, 30, 255)
    canvas[200:204, 200:280] = (90, 90, 90, 255)
    buffer = io.BytesIO()
    Image.fromarray(canvas, "RGBA").save(buffer, format="PNG")
    return buffer.getvalue(), canvas


class ParsingTests(SimpleTestCase):
    """The names and the matching of dots need no image packages, except where a test says so."""

    def test_the_raw_label_is_split_into_place_and_number(self):
        self.assertEqual(transformers.normalise_trafo("Ittigen TRA12"), ("Ittigen", "12"))
        self.assertEqual(transformers.normalise_trafo("Ittigen_TRA12"), ("Ittigen", "12"))
        self.assertIsNone(transformers.normalise_trafo("Strasse 5"))

    def test_only_the_clean_form_can_be_stored(self):
        self.assertTrue(transformers.CLEAN_NAME.fullmatch("Ittigen_TRA12"))
        self.assertTrue(transformers.CLEAN_NAME.fullmatch("TRA7"))
        for raw in ("Ittigen TRA12 Strasse", "TRA1234567", "Ittigen_TRA12; https://example.org", "12 TRA"):
            with self.subTest(raw=raw):
                self.assertFalse(transformers.CLEAN_NAME.fullmatch(raw))

    def test_the_most_common_place_name_of_a_number_wins(self):
        dots = [
            transformers.Dot(0, 0, raw_label="Dtigen TRA12"),
            transformers.Dot(0, 0, raw_label="Ittigen TRA12"),
            transformers.Dot(0, 0, raw_label="Ittigen TRA12"),
            transformers.Dot(0, 0, raw_label="Strasse 5"),
            transformers.Dot(0, 0, raw_label="TRA7"),
        ]
        transformers.settle_names(dots)
        self.assertEqual([dot.name for dot in dots], ["Ittigen_TRA12", "Ittigen_TRA12", "Ittigen_TRA12", "", "TRA7"])

    @needs_packages
    def test_a_label_goes_to_its_nearest_dot_only_within_the_distance(self):
        dots = [transformers.Dot(0, 0, col=50, row=50), transformers.Dot(0, 0, col=500, row=500)]
        transformers.assign_labels(dots, [(60, 52, "Ittigen TRA12"), (900, 900, "Far TRA3")])
        self.assertEqual(dots[0].raw_label, "Ittigen TRA12")
        self.assertEqual(dots[1].raw_label, "")

    @needs_packages
    def test_a_building_matches_an_exact_dot_a_fallback_dot_or_none(self):
        buildings = [
            transformers.Building(1, "3063", 2600000.0, 1200000.0),
            transformers.Building(2, "3063", 2600010.0, 1200000.0),
            transformers.Building(3, "3063", 2600500.0, 1200000.0),
        ]
        dots = [
            transformers.Dot(2600001.0, 1200000.0, name="Ittigen_TRA12"),
            transformers.Dot(2600040.0, 1200000.0, name=""),
        ]
        stations = transformers.match_stations(buildings, dots)
        self.assertEqual(
            [(s.egid, s.match, s.name) for s in stations],
            # Building 2 is 9 m from the first dot and 30 m from the second: the nearest dot within 50 m is its match.
            [(1, "exact", "Ittigen_TRA12"), (2, "fallback", "Ittigen_TRA12"), (3, "none", "")],
        )


@needs_packages
class MapReadingTests(SimpleTestCase):
    def test_dots_are_found_by_colour_and_size_and_grey_text_is_ignored(self):
        _png, canvas = tile_png(300, dots=((50, 50), (150, 120)))
        dots = transformers.find_dots(canvas)
        self.assertEqual(sorted((round(col), round(row)) for col, row in dots), [(50, 50), (150, 120)])

    def test_a_single_pixel_speck_is_not_a_dot(self):
        import numpy as np

        canvas = np.zeros((40, 40, 4), dtype=np.uint8)
        canvas[10, 10] = (220, 30, 30, 255)
        self.assertEqual(transformers.find_dots(canvas), [])

    def test_the_labels_of_a_tile_name_their_dots(self):
        _png, canvas = tile_png(300, dots=((50, 50), (150, 120)))
        dots = [transformers.Dot(0, 0, col=col, row=row) for col, row in transformers.find_dots(canvas)]
        transformers.assign_labels(dots, FakeReader([(62, 50, "Ittigen TRA12"), (160, 120, "Dtigen TRA3")]).read_tile(canvas))
        transformers.settle_names(dots)
        self.assertEqual(sorted(dot.name for dot in dots), ["Dtigen_TRA3", "Ittigen_TRA12"])


class TileLimitTests(SimpleTestCase):
    def test_a_scan_needing_more_tiles_than_the_limit_is_refused_before_the_first_request(self):
        buildings = [
            transformers.Building(index, "3063", 2600000.0 + index * transformers.TILE_SIZE, 1200000.0)
            for index in range(transformers.MAX_TILES_PER_RUN + 1)
        ]
        session = FakeSession()
        with self.assertRaises(transformers.ScanFailure) as raised:
            transformers.scan_tiles(buildings, session=session, reader=None, sleep=lambda seconds: None, clock=lambda: 0.0)
        self.assertEqual(str(raised.exception), jobs.MAP_TOO_MANY_TILES)
        self.assertEqual(session.calls, [])

    def test_the_limits_are_the_documented_values(self):
        self.assertEqual(transformers.MAX_TILES_PER_RUN, 300)
        self.assertEqual(transformers.MAX_SCAN_SECONDS, 3600)
        self.assertEqual(transformers.TILE_PAUSE_SECONDS, 1.0)
        self.assertEqual(transformers.REQUEST_TIMEOUT, (10, 60))
        self.assertEqual(transformers.REQUEST_SECONDS, 60)
        self.assertEqual(transformers.MAX_TILE_BYTES, 16 * 1024 * 1024)


class TileBodyTests(SimpleTestCase):
    """What the answer of one tile may be (D-118): a PNG of the requested size, at most ``MAX_TILE_BYTES``."""

    def fetch(self, session):
        return transformers.fetch_tile(session, (0, 0, 1, 1), transformers.TILE_PX + 102, (10, 60))

    def test_a_tile_larger_than_the_limit_is_refused(self):
        session = FakeSession(responses=[FakeResponse(b"\x89PNG" + bytes(2048))])
        with patch.object(transformers, "MAX_TILE_BYTES", 1024), self.assertRaises(transformers.ScanFailure) as raised:
            self.fetch(session)
        self.assertEqual(str(raised.exception), jobs.MAP_ANSWER)

    @needs_packages
    def test_a_200_answer_typed_as_png_with_another_body_is_refused(self):
        for body in (b"<html>not a picture</html>", b"\x89PNG\r\n\x1a\n" + b"truncated", b""):
            with self.subTest(body=body[:12]), self.assertRaises(transformers.ScanFailure) as raised:
                self.fetch(FakeSession(responses=[FakeResponse(body, content_type="image/png")]))
            self.assertEqual(str(raised.exception), jobs.MAP_ANSWER)

    @needs_packages
    def test_a_png_of_another_size_is_refused(self):
        png, _canvas = tile_png(64)
        with self.assertRaises(transformers.ScanFailure) as raised:
            self.fetch(FakeSession(responses=[FakeResponse(png, content_type="image/png")]))
        self.assertEqual(str(raised.exception), jobs.MAP_ANSWER)


class _TrickleServer(socketserver.ThreadingTCPServer):
    """A test-only server on 127.0.0.1 whose tile trickles: one byte every 0.3 s, as a faulty map server may do."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.closed = threading.Event()  # the client side went away
        self.connected = threading.Event()
        super().__init__(("127.0.0.1", 0), _TrickleHandler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/ogc/"


class _TrickleHandler(socketserver.BaseRequestHandler):
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
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n\r\n")
            while time.monotonic() < ended:
                self.request.sendall(b"\x89")
                time.sleep(self.DRIP)
        except OSError:
            pass
        server.closed.set()


class TileDeadlineTests(SimpleTestCase):
    """The hard total deadline of one tile request (review finding 1 of D-118, the rule of D-117 for downloads).

    A socket timeout limits one wait only; a server that trickles bytes must not hold the scan. The test replaces only
    the approved map address (a seam: the local server speaks plain HTTP); the logic under test is the real one.
    """

    def test_a_tile_that_trickles_is_cut_off_at_the_request_deadline(self):
        server = _TrickleServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        with patch.object(transformers, "WMS_URL", server.url), \
                patch.object(importer, "APPROVED_WMS_URLS", frozenset({server.url})):
            started = time.monotonic()
            with self.assertRaises(transformers.ScanFailure) as raised:
                transformers.fetch_tile(
                    transformers.make_session(), (0, 0, 1, 1), transformers.TILE_PX + 102, (10, 60), seconds=1.0,
                )
            elapsed = time.monotonic() - started
        self.assertEqual(str(raised.exception), jobs.MAP_TOO_LONG)
        self.assertTrue(server.connected.is_set())
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 4.0, f"the tile request was held for {elapsed:.1f} s")
        # The socket was shut down (the server sees the client leave) and no helper thread is left behind.
        self.assertTrue(server.closed.wait(5), "the connection was not closed")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(t.name == "transformer-tile" for t in threading.enumerate()):
            time.sleep(0.05)
        self.assertFalse(any(t.name == "transformer-tile" for t in threading.enumerate()))

    def test_each_request_gets_its_own_limit_and_never_more_than_the_rest_of_the_run(self):
        buildings = [
            transformers.Building(1, "3063", 2600000.0, 1200000.0),
            transformers.Building(2, "3063", 2600000.0 + transformers.TILE_SIZE, 1200000.0),
        ]
        clock_value = [0.0]
        limits = []

        def fetch(session, bbox, px, timeout, seconds=None):
            limits.append(seconds)
            if len(limits) == 1:
                clock_value[0] += transformers.MAX_SCAN_SECONDS - 30.0  # the first request leaves 30 s of the run
            return None

        with patch.object(transformers, "fetch_tile", side_effect=fetch),                 patch.object(transformers, "find_dots", return_value=[]):
            transformers.scan_tiles(
                buildings, session=None, reader=None, sleep=lambda seconds: None, clock=lambda: clock_value[0],
            )
        self.assertEqual(limits, [transformers.REQUEST_SECONDS, 30.0])


@needs_packages
class PolitenessTests(SimpleTestCase):
    def two_cells(self):
        return [
            transformers.Building(1, "3063", 2600000.0, 1200000.0),
            transformers.Building(2, "3063", 2600000.0 + transformers.TILE_SIZE, 1200000.0),
        ]

    def test_one_request_at_a_time_with_a_pause_of_at_least_a_second_and_no_redirects(self):
        png, _canvas = tile_png(transformers.TILE_PX + 102)
        clock_value = [1000.0]
        starts = []
        sleeps = []
        calls = []

        def clock():
            return clock_value[0]

        def sleep(seconds):
            sleeps.append(seconds)
            clock_value[0] += seconds

        def get(url, **kwargs):
            starts.append(clock_value[0])
            calls.append({"url": url, **kwargs})
            clock_value[0] += 0.2  # every request takes 0.2 seconds
            return FakeResponse(png)

        session = FakeSession()
        session.get = get
        dots, tiles = transformers.scan_tiles(self.two_cells(), session=session, reader=None, sleep=sleep, clock=clock)
        self.assertEqual((len(calls), tiles), (2, 2))
        self.assertGreaterEqual(starts[1] - (starts[0] + 0.2), transformers.TILE_PAUSE_SECONDS - 1e-9)
        self.assertEqual(len(sleeps), 1)
        for call in calls:
            self.assertEqual(call["url"], importer.TRANSFORMER_WMS_URL)
            self.assertIs(call["allow_redirects"], False)
            self.assertIs(call["verify"], True)
            self.assertEqual(call["timeout"], transformers.REQUEST_TIMEOUT)

    def test_the_time_limit_stops_the_scan_and_no_request_follows_it(self):
        png, _canvas = tile_png(transformers.TILE_PX + 102)
        clock_value = [0.0]
        calls = []

        def get(url, **kwargs):
            calls.append(kwargs)
            clock_value[0] += transformers.MAX_SCAN_SECONDS  # one request uses up the whole run
            return FakeResponse(png)

        session = FakeSession()
        session.get = get
        with self.assertRaises(transformers.ScanFailure) as raised:
            transformers.scan_tiles(
                self.two_cells(), session=session, reader=None, sleep=lambda seconds: None, clock=lambda: clock_value[0],
            )
        self.assertEqual(str(raised.exception), jobs.MAP_TOO_LONG)
        self.assertEqual(len(calls), 1)

    def test_a_redirect_a_wrong_type_or_an_unreachable_server_gives_a_fixed_reason(self):
        cases = (
            (FakeSession(responses=[FakeResponse(status=302, content_type="text/html")]), jobs.MAP_ANSWER),
            (FakeSession(responses=[FakeResponse(b"<html>", content_type="text/html")]), jobs.MAP_ANSWER),
            (FakeSession(error=requests.ConnectionError("no route")), jobs.MAP_UNREACHABLE),
        )
        for session, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(transformers.ScanFailure) as raised:
                transformers.fetch_tile(session, (0, 0, 1, 1), transformers.TILE_PX + 102, (10, 60))
            self.assertEqual(str(raised.exception), reason)

    def test_the_session_has_no_proxy_or_netrc_and_names_the_purpose(self):
        session = transformers.make_session()
        self.assertIs(session.trust_env, False)
        self.assertIn("LEG-Platform", session.headers["User-Agent"])
        self.assertIn("transformer", session.headers["User-Agent"])

    def test_only_the_approved_map_address_is_requested(self):
        with patch.object(transformers, "WMS_URL", "https://example.org/ogc/"):
            with self.assertRaises(transformers.ScanFailure):
                transformers.fetch_tile(FakeSession(png=b""), (0, 0, 1, 1), transformers.TILE_PX + 102, (10, 60))

    def test_the_allowlist_of_the_import_code_holds_only_the_map_host(self):
        self.assertEqual(importer.APPROVED_WMS_URLS, frozenset({"https://qgisserver-extern.bkw.ch/ogc/"}))


class StoreTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        publish([
            {"egid": 1001, "postal_code": "3063", "easting": 2600001, "northing": 1200001},
            {"egid": 1002, "postal_code": "3063", "easting": 2600002, "northing": 1200002},
            {"egid": 2001, "postal_code": "3048", "easting": 2600003, "northing": 1200003},
        ])
        ServicePostalCode.objects.create(postal_code="3063")
        ServicePostalCode.objects.create(postal_code="3048")
        self.claim = running_scan()

    def store(self, stations, codes):
        result = transformers.Result(
            postal_codes=tuple(codes), buildings=len(stations), with_name=0, tiles=1,
            stations=tuple(transformers.Station(*row) for row in stations),
        )
        transformers.store(result, self.claim.token, timezone.now())
        return result

    def rows(self, code):
        return sorted(TransformerStation.objects.filter(postal_code=code).values_list("egid", "name", "match"))

    def test_a_scan_replaces_only_the_rows_of_the_postal_codes_it_scanned(self):
        self.store([(1001, "3063", "Ittigen_TRA12", "exact"), (1002, "3063", "", "none"),
                    (2001, "3048", "TRA3", "fallback")], ("3048", "3063"))
        self.store([(1001, "3063", "Ittigen_TRA5", "exact")], ("3063",))
        self.assertEqual(self.rows("3063"), [(1001, "Ittigen_TRA5", "exact")])
        self.assertEqual(self.rows("3048"), [(2001, "TRA3", "fallback")])

    def test_a_row_whose_egid_left_the_generation_is_not_shown_by_a_lookup(self):
        self.store([(1001, "3063", "Ittigen_TRA12", "exact"), (9999, "3063", "TRA9", "exact")], ("3063",))
        self.assertEqual(transformers.station_of(1001), {"name": "Ittigen_TRA12", "match": "exact"})
        self.assertIsNone(transformers.station_of(9999))

    def test_a_building_moved_to_another_postal_code_keeps_one_row(self):
        self.store([(1001, "3063", "Ittigen_TRA12", "exact")], ("3063",))
        self.store([(1001, "3048", "TRA4", "exact")], ("3048",))
        self.assertEqual(TransformerStation.objects.filter(egid=1001).count(), 1)

    def test_a_failed_scan_changes_nothing_and_its_reason_stays_visible(self):
        self.store([(1001, "3063", "Ittigen_TRA12", "exact")], ("3063",))
        jobs.record_failure(running_scan(self.claim.token), transformers.ScanFailure(jobs.MAP_UNREACHABLE))
        self.assertEqual(self.rows("3063"), [(1001, "Ittigen_TRA12", "exact")])
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual((job.last_transformer_state, job.last_transformer_error), ("failed", jobs.MAP_UNREACHABLE))
        self.assertEqual(status.transformer_status()["result_reason"], str(status.REASONS[jobs.MAP_UNREACHABLE]))

    def test_a_later_success_replaces_the_failure_and_sets_the_statistic(self):
        jobs.record_failure(running_scan(self.claim.token), transformers.ScanFailure(jobs.MAP_UNREACHABLE))
        claimed = running_scan(self.claim.token)
        result = transformers.Result(
            postal_codes=("3048", "3063"), buildings=2, with_name=1, tiles=2,
            stations=(transformers.Station(1001, "3063", "Ittigen_TRA12", "exact"),
                      transformers.Station(1002, "3063", "", "none")),
        )
        transformers.store(result, claimed.token, timezone.now())
        jobs.record_success(claimed, 2, transformer=result)
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual((job.last_transformer_state, job.last_transformer_error), ("succeeded", ""))
        self.assertEqual((job.last_transformer_buildings, job.last_transformer_with_name), (2, 1))

    def test_a_lost_claim_writes_nothing(self):
        ImportRequest.objects.filter(pk=1).update(claim_token=uuid.uuid4())
        with self.assertRaises(importer.ImportFailure):
            self.store([(1001, "3063", "TRA1", "exact")], ("3063",))
        self.assertEqual(self.rows("3063"), [])

    def test_the_buildings_are_those_of_the_postal_codes_with_an_egid_and_a_position(self):
        Address.objects.all().delete()
        publish([
            {"egid": 1001, "postal_code": "3063", "easting": 2600001, "northing": 1200001},
            {"egid": None, "postal_code": "3063", "easting": 2600002, "northing": 1200002},
            {"egid": 1003, "postal_code": "3063", "easting": None, "northing": None},
            {"egid": 1004, "postal_code": "9999", "easting": 2600004, "northing": 1200004},
        ], generation=Directory.objects.get(pk=1).generation)
        self.assertEqual([(b.egid, b.postal_code) for b in transformers.load_buildings(["3063"])], [(1001, "3063")])


class PostalCodePermissionTests(TestCase):
    def setUp(self):
        self.admin = ServicePostalCodeAdmin(ServicePostalCode, admin.site)

    def request_for(self, *, allowed):
        user = create_active_user(email=f"service-{uuid.uuid4().hex[:8]}@example.org")
        if allowed:
            user.user_permissions.add(Permission.objects.get(codename="manage_service_areas"))
        request = RequestFactory().get("/")
        request.user = type(user).objects.get(pk=user.pk)
        return request

    def test_only_the_service_area_right_gives_access_to_the_postal_codes(self):
        self.assertEqual(SERVICE_AREA_PERMISSION, "addresses.manage_service_areas")
        denied = self.request_for(allowed=False)
        self.assertFalse(self.admin.has_module_permission(denied))
        self.assertFalse(self.admin.has_view_permission(denied))
        self.assertFalse(self.admin.has_add_permission(denied))
        self.assertFalse(self.admin.has_change_permission(denied))
        self.assertFalse(self.admin.has_delete_permission(denied))
        allowed = self.request_for(allowed=True)
        self.assertTrue(self.admin.has_module_permission(allowed))
        self.assertTrue(self.admin.has_view_permission(allowed))
        self.assertTrue(self.admin.has_add_permission(allowed))
        self.assertTrue(self.admin.has_change_permission(allowed))
        self.assertTrue(self.admin.has_delete_permission(allowed))

    def test_the_right_is_declared_on_the_model(self):
        self.assertTrue(Permission.objects.filter(
            codename="manage_service_areas", content_type__app_label="addresses",
        ).exists())


class WeeklyStepTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        ImportRequest.objects.create(pk=1, last_automatic_attempt_at=timezone.now() - timedelta(days=1))
        publish([])

    def run_weekly(self, moment):
        claim = jobs.Claim(uuid.uuid4(), False, False, ImportRequest.Source.TRANSFORMER)
        with override_settings(ADDRESS_IMPORT_AUTOMATIC=True), \
                patch.object(schedule.jobs, "claim", return_value=claim) as claimed, \
                patch.object(schedule, "_execute_with_heartbeat") as executed:
            outcome = schedule.run_transformer_weekly(moment)
        return outcome, claimed.call_count, executed.call_count

    def test_the_scan_runs_only_on_saturday(self):
        friday = datetime(2026, 10, 23, 12, 0, tzinfo=ZURICH)
        saturday = datetime(2026, 10, 24, 12, 0, tzinfo=ZURICH)
        self.assertEqual(self.run_weekly(friday), (schedule.NOT_SATURDAY, 0, 0))
        self.assertEqual(self.run_weekly(saturday), (schedule.IMPORTED, 1, 1))

    def test_once_per_saturday_and_not_before_the_daily_time(self):
        early = datetime(2026, 10, 24, 2, 0, tzinfo=ZURICH)
        self.assertEqual(self.run_weekly(early)[0], schedule.BEFORE_TIME)
        noon = datetime(2026, 10, 24, 12, 0, tzinfo=ZURICH)
        self.assertEqual(self.run_weekly(noon)[0], schedule.IMPORTED)
        self.assertEqual(self.run_weekly(noon)[0], schedule.ALREADY_RAN)

    def test_a_queued_address_request_holds_the_scan_back(self):
        ImportRequest.objects.filter(pk=1).update(state=ImportRequest.State.QUEUED)
        with override_settings(ADDRESS_IMPORT_AUTOMATIC=True):
            outcome = schedule.run_transformer_weekly(datetime(2026, 10, 24, 12, 0, tzinfo=ZURICH))
        self.assertEqual(outcome, schedule.BUSY)
        self.assertIsNone(ImportRequest.objects.get(pk=1).last_transformer_scheduled_run_on)


class DailyOrderTests(TestCase):
    databases = {"default", "addresses"}

    def run_saturday(self, plant_fails=False):
        started = []

        def execute(claimed, progress=None):
            started.append(claimed.source)
            if plant_fails and claimed.source == ImportRequest.Source.PLANT:
                raise importer.ImportFailure("Official download failed.")

        def claim(**kwargs):
            return jobs.Claim(uuid.uuid4(), False, False, kwargs.get("source", ImportRequest.Source.ADDRESS))

        with override_settings(ADDRESS_IMPORT_AUTOMATIC=True), \
                patch.object(schedule, "_run_address", return_value=schedule.NOT_DUE), \
                patch.object(schedule.jobs, "claim", side_effect=claim), \
                patch.object(schedule, "_execute_with_heartbeat", side_effect=execute):
            try:
                schedule.run_daily(datetime(2026, 10, 24, 12, 0, tzinfo=ZURICH))
                failed = False
            except importer.ImportFailure:
                failed = True
        return started, failed

    def test_on_saturday_the_scan_runs_after_the_plant_register(self):
        started, failed = self.run_saturday()
        self.assertEqual(started, [ImportRequest.Source.PLANT, ImportRequest.Source.TRANSFORMER])
        self.assertFalse(failed)

    def test_a_failed_plant_register_stops_nothing_and_the_failure_is_raised_afterwards(self):
        started, failed = self.run_saturday(plant_fails=True)
        self.assertEqual(started, [ImportRequest.Source.PLANT, ImportRequest.Source.TRANSFORMER])
        self.assertTrue(failed)


class StatusPageTests(TestCase):
    databases = {"default", "addresses"}

    def test_the_block_shows_no_map_address_and_no_map_text(self):
        publish([])
        ServicePostalCode.objects.create(postal_code="3063")
        claimed = running_scan()
        result = transformers.Result(
            postal_codes=("3063",), buildings=2, with_name=1, tiles=1,
            stations=(transformers.Station(1001, "3063", "Ittigen_TRA12", "exact"),
                      transformers.Station(1002, "3063", "", "none")),
        )
        jobs.record_success(claimed, 2, transformer=result)
        page = status.transformer_status()
        text = " ".join(str(value) for value in page.values())
        for forbidden in ("http", "qgisserver", "bkw.ch", "TRA12", "/ogc/"):
            self.assertNotIn(forbidden, text)
        self.assertEqual((page["buildings"], page["with_name"], page["without_name"]), ("2", "1", "1"))
        self.assertEqual(page["postal_codes"], "3063")


class MigrationAndRoutingTests(SimpleTestCase):
    def test_the_scan_migration_makes_the_setup_rebuild_the_directory_file(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("addresses", "0011_transformer_scan")
        self.assertTrue(setup._changes_directory(migration))

    def test_the_station_table_lives_in_the_directory_file_and_the_right_list_in_the_application_database(self):
        self.assertEqual(AddressRouter().db_for_write(TransformerStation), "addresses")
        self.assertIsNone(AddressRouter().db_for_write(ServicePostalCode))


class NoWebStartTests(TestCase):
    """No web view and no administration action starts a scan (D-118): only the weekly run and the command do."""

    databases = {"default", "addresses"}

    def setUp(self):
        person = create_active_user(f"scan-{uuid.uuid4().hex[:8]}@example.org")
        person.set_privileges(is_staff=True, is_superuser=False)
        for codename in ("request_directory_import", "manage_service_areas"):
            person.user_permissions.add(Permission.objects.get(codename=codename))
        self.client = VerifiedClient()
        self.client.force_login(person)
        publish([])
        ServicePostalCode.objects.create(postal_code="3063")

    def test_the_pages_and_actions_of_the_administration_never_start_a_scan(self):
        directory = reverse("admin:addresses_importrequest_changelist")
        postal = reverse("admin:addresses_servicepostalcode_changelist")
        code = ServicePostalCode.objects.get(postal_code="3063")
        with patch.object(transformers, "run_scan") as scan, \
                patch.object(schedule, "run_transformer_weekly") as weekly, \
                patch.object(schedule, "run_transformer_now") as now, \
                patch.object(jobs, "claim", wraps=jobs.claim) as claim, \
                patch.object(runner, "start_manual", return_value=True) as start, \
                patch("leg_platform.addresses.admin.allow_request", return_value=True):
            self.assertEqual(self.client.get(directory).status_code, 200)
            self.assertEqual(self.client.get(reverse("admin:addresses_importrequest_status")).status_code, 200)
            self.assertEqual(self.client.post(directory).status_code, 302)
            self.assertEqual(self.client.get(postal).status_code, 200)
            self.client.post(reverse("admin:addresses_servicepostalcode_add"), {"postal_code": "3048"})
            self.client.post(
                reverse("admin:addresses_servicepostalcode_change", args=[code.pk]), {"postal_code": "3049"},
            )
            self.client.post(postal, {"action": "delete_selected", "_selected_action": [code.pk], "post": "yes"})
        scan.assert_not_called()
        weekly.assert_not_called()
        now.assert_not_called()
        start.assert_called_once_with()  # the address import button starts its own job only
        for call in claim.call_args_list:
            self.assertNotEqual(call.kwargs.get("source"), ImportRequest.Source.TRANSFORMER)

    def test_only_the_weekly_run_the_command_and_their_job_code_name_the_scan(self):
        # Every module of the project (tests and migrations aside) that names the scan or its module. A web view, a form
        # or an administration class is not on this list; a new one must be added here on purpose (D-118).
        project = Path(transformers.__file__).resolve().parents[1]  # the package leg_platform
        pattern = re.compile(
            r"\b(run_transformer_weekly|run_transformer_now|run_scan|scan_tiles|fetch_tile)\b"
            r"|\bSource\.TRANSFORMER\b|\bimport transformers\b|\bfrom \.transformers\b"
        )
        named = set()
        for path in project.rglob("*.py"):
            relative = path.relative_to(project).as_posix()
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                named.add(relative)
        self.assertEqual(sorted(named), [
            "addresses/jobs.py",
            "addresses/management/commands/scan_transformer_stations.py",
            "addresses/models.py",
            "addresses/schedule.py",
            "addresses/status.py",
            "addresses/transformers.py",
        ])


class ServiceAreaEventTests(TestCase):
    """Every change of the postal codes of the service area is a security event; the log line names the codes only."""

    databases = {"default", "addresses"}

    def setUp(self):
        person = create_active_user(f"area-{uuid.uuid4().hex[:8]}@example.org")
        person.set_privileges(is_staff=True, is_superuser=False)
        person.user_permissions.add(Permission.objects.get(codename="manage_service_areas"))
        self.person = person
        self.client = VerifiedClient()
        self.client.force_login(person)

    def events(self):
        return SecurityEvent.objects.filter(event=SecurityEvent.Event.SERVICE_AREA_CHANGED).count()

    def logged(self, action):
        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                action()
        return logs.output

    def test_add_change_and_delete_are_each_one_event_and_the_log_names_the_codes_only(self):
        add = reverse("admin:addresses_servicepostalcode_add")
        output = self.logged(lambda: self.client.post(add, {"postal_code": "3063"}))
        self.assertEqual(self.events(), 1)
        self.assertEqual(output, [
            "WARNING:leg_platform.security:The postal codes of the service area were changed "
            f"(actor {self.person.pk}, postal codes: 3063)."
        ])
        code = ServicePostalCode.objects.get(postal_code="3063")
        change = reverse("admin:addresses_servicepostalcode_change", args=[code.pk])
        output = self.logged(lambda: self.client.post(change, {"postal_code": "3048"}))
        self.assertEqual(self.events(), 2)
        self.assertIn("postal codes: 3048)", output[0])
        output = self.logged(
            lambda: self.client.post(
                reverse("admin:addresses_servicepostalcode_changelist"),
                {"action": "delete_selected", "_selected_action": [code.pk], "post": "yes"},
            )
        )
        self.assertEqual(self.events(), 3)
        self.assertIn("postal codes: none)", output[0])

    def test_a_refused_input_changes_nothing_and_records_nothing(self):
        self.client.post(reverse("admin:addresses_servicepostalcode_add"), {"postal_code": "0000"})
        self.assertFalse(ServicePostalCode.objects.exists())
        self.assertEqual(self.events(), 0)
