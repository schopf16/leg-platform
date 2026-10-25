"""The plant register import (D-117): the bounded parser on synthetic files, the download rules, the generation switch,
the shared claim with the address import, the daily run and the security events. No real data is used."""

import hashlib
import io
import socket
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.utils import timezone

from leg_platform.accounts.models import SecurityEvent
from leg_platform.addresses import importer, jobs, plant_lookup, plants, schedule, status
from leg_platform.addresses.models import Directory, ImportRequest, Plant, PlantRegister
from leg_platform.addresses.router import DIRECTORY_MODELS, AddressRouter
from leg_platform.addresses.storage import DIRECTORY_ALIAS

COLUMNS = [
    "xtf_id", "Address", "PostCode", "Municipality", "Canton", "EGID", "BeginningOfOperation", "InitialPower",
    "TotalPower", "MainCategory", "SubCategory", "PlantCategory", "_x", "_y",
]
DEFAULTS = {
    "xtf_id": "1", "Address": "Example 1", "PostCode": "9999", "Municipality": "Example", "Canton": "BE",
    "EGID": "1001", "BeginningOfOperation": "2020-01-01", "InitialPower": "10", "TotalPower": "10",
    "MainCategory": "maincat_1", "SubCategory": "subcat_2", "PlantCategory": "plantcat_1",
    "_x": "2600000.000", "_y": "1200000.000",
}
CHECKSUM = "a" * 64


def plant_line(number, **changes):
    """One row of the file: plant ``number`` in building 1000 + number, unless ``changes`` say otherwise."""
    values = dict(DEFAULTS, xtf_id=str(number), EGID=str(1000 + number))
    values.update(changes)
    return ",".join(values[column] for column in COLUMNS)


def csv_text(lines, columns=COLUMNS):
    return ",".join(columns) + "\r\n" + "".join(line + "\r\n" for line in lines)


def archive(text, name=plants.PLANT_CSV_NAME, extra=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(name, text.encode("utf-8-sig"))
        for other_name, data in extra:
            zipped.writestr(other_name, data)
    output.seek(0)
    return output


def parse(text, **kwargs):
    counts = {"rejected": 0, "skipped": 0}
    return list(plants.plant_rows(archive(text, **kwargs), counts)), counts


class FakeResponse:
    """A streamed answer: status, headers and a body given as a callable that returns an iterable of chunks."""

    def __init__(self, body, headers, status_code=200):
        self._body = body
        self.headers = headers
        self.status_code = status_code

    def iter_content(self, _size):
        return self._body()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeClock:
    """A clock that the tests move by hand; ``importer._chunks`` takes it as ``clock``."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ParserTests(SimpleTestCase):
    def test_a_valid_plant_is_read_with_its_values(self):
        rows, counts = parse(csv_text([plant_line(1, TotalPower="12.5")]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].xtf_id, "1")
        self.assertEqual(rows[0].egid, 1001)
        self.assertEqual(rows[0].subcategory, "subcat_2")
        self.assertEqual(rows[0].total_power_kw, Decimal("12.5"))
        self.assertEqual(rows[0].x, Decimal("2600000.000"))
        self.assertEqual(counts, {"rejected": 0, "skipped": 0, "rows": 1})

    def test_every_technology_is_kept(self):
        rows, _counts = parse(csv_text([plant_line(1, SubCategory="subcat_4"), plant_line(2, SubCategory="subcat_1")]))
        self.assertEqual([row.subcategory for row in rows], ["subcat_4", "subcat_1"])

    def test_a_plant_without_egid_is_skipped_and_counted(self):
        rows, counts = parse(csv_text([plant_line(1, EGID=""), plant_line(2)]))
        self.assertEqual([row.xtf_id for row in rows], ["2"])
        self.assertEqual(counts, {"rejected": 0, "skipped": 1, "rows": 2})

    def test_a_row_without_egid_is_only_skipped_when_its_other_fields_are_valid(self):
        rows, counts = parse(csv_text([
            plant_line(1, EGID="", TotalPower="NaN"),
            plant_line(2, EGID="", SubCategory="wind"),
            plant_line(3, EGID="", _x="2400000.000"),
            plant_line(4, EGID="", xtf_id=""),
            plant_line(5, EGID=""),
        ]))
        self.assertEqual(rows, [])
        self.assertEqual(counts, {"rejected": 4, "skipped": 1, "rows": 5})

    def test_a_plant_without_coordinates_is_kept_without_them(self):
        rows, counts = parse(csv_text([plant_line(1, _x="", _y="")]))
        self.assertIsNone(rows[0].x)
        self.assertIsNone(rows[0].y)
        self.assertEqual(counts["rejected"], 0)

    def test_rows_that_fail_the_checks_are_rejected_not_stored(self):
        bad = {
            "NaN power": plant_line(1, TotalPower="NaN"),
            "infinite power": plant_line(2, TotalPower="Infinity"),
            "negative power": plant_line(3, TotalPower="-5"),
            "power above the bound": plant_line(4, TotalPower="10000001"),
            "power with too many digits": plant_line(5, TotalPower="123456789"),
            "egid with a leading zero": plant_line(6, EGID="0123"),
            "egid with letters": plant_line(7, EGID="10A1"),
            "unknown technology code": plant_line(8, SubCategory="wind"),
            "easting outside the range": plant_line(9, _x="2400000.000"),
            "northing outside the range": plant_line(10, _y="1400000.000"),
            "only one coordinate": plant_line(11, _y=""),
            "empty identifier": plant_line(12, xtf_id=""),
        }
        rows, counts = parse(csv_text(list(bad.values())))
        self.assertEqual(rows, [])
        self.assertEqual(counts["rejected"], len(bad))

    def test_a_repeated_identifier_is_rejected_after_the_first(self):
        rows, counts = parse(csv_text([plant_line(1), plant_line(1, EGID="2002")]))
        self.assertEqual([row.egid for row in rows], [1001])
        self.assertEqual(counts["rejected"], 1)

    def test_a_row_of_the_wrong_width_is_rejected(self):
        rows, counts = parse(csv_text([plant_line(1), "1,only,three"]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(counts["rejected"], 1)

    def test_ignored_rows_are_counted_by_reason_and_add_up_to_the_rows_of_the_file(self):
        rows, counts = parse(csv_text([
            plant_line(1), plant_line(2, EGID=""), plant_line(3, TotalPower="NaN"), plant_line(4, xtf_id="1"),
            "1,only,three", plant_line(5, EGID="", SubCategory="wind"), plant_line(6, EGID=""),
        ]))
        self.assertEqual([row.xtf_id for row in rows], ["1"])
        # Rejected: the NaN power, the repeated identifier, the row of the wrong width, the unknown technology without EGID.
        # Skipped: the two valid rows without EGID.
        self.assertEqual(counts, {"rejected": 4, "skipped": 2, "rows": 7})
        self.assertEqual(len(rows) + counts["rejected"] + counts["skipped"], 7)

    def test_a_missing_required_column_refuses_the_file(self):
        text = csv_text([], columns=[column for column in COLUMNS if column != "_y"])
        with self.assertRaises(importer.ImportFailure) as raised:
            parse(text)
        self.assertEqual(str(raised.exception), plants.FORMAT_FAILURE)

    def test_the_other_official_members_are_allowed_and_not_read(self):
        rows, _counts = parse(
            csv_text([plant_line(1)]),
            extra=[(name, "catalogue") for name in sorted(plants.PLANT_OTHER_MEMBERS)],
        )
        self.assertEqual(len(rows), 1)

    def test_an_unknown_member_refuses_the_file(self):
        with self.assertRaises(importer.ImportFailure) as raised:
            parse(csv_text([plant_line(1)]), extra=[("unexpected.csv", "x")])
        self.assertEqual(str(raised.exception), plants.FORMAT_FAILURE)

    def test_the_csv_member_must_be_there_exactly_once(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            zipped.writestr("PlantDetail.csv", "x")
        output.seek(0)
        with self.assertRaises(importer.ImportFailure) as raised:
            list(plants.plant_rows(output, {"rejected": 0, "skipped": 0}))
        self.assertEqual(str(raised.exception), plants.FORMAT_FAILURE)

    def test_a_member_flagged_as_encrypted_refuses_the_file(self):
        member = SimpleNamespace(
            filename=plants.PLANT_CSV_NAME, is_dir=lambda: False, flag_bits=1,
            compress_type=zipfile.ZIP_DEFLATED, compress_size=10, file_size=10,
        )
        with self.assertRaises(importer.ImportFailure) as raised:
            plants._check_members([member])
        self.assertEqual(str(raised.exception), plants.FORMAT_FAILURE)

    def test_a_member_with_an_implausible_ratio_refuses_the_file(self):
        member = SimpleNamespace(
            filename=plants.PLANT_CSV_NAME, is_dir=lambda: False, flag_bits=0,
            compress_type=zipfile.ZIP_DEFLATED, compress_size=1, file_size=10**6,
        )
        with self.assertRaises(importer.ImportFailure):
            plants._check_members([member])

    def test_a_plant_file_above_the_limit_is_refused(self):
        with patch.object(plants, "MAX_PLANT_CSV_BYTES", 100):
            with self.assertRaises(importer.ImportFailure) as raised:
                parse(csv_text([plant_line(number) for number in range(1, 10)]))
        self.assertEqual(str(raised.exception), plants.SIZE_FAILURE)

    def test_a_broken_archive_is_a_format_failure(self):
        with self.assertRaises(importer.ImportFailure) as raised:
            list(plants.plant_rows(io.BytesIO(b"not a zip"), {"rejected": 0, "skipped": 0}))
        self.assertEqual(str(raised.exception), plants.FORMAT_FAILURE)

    def test_an_archive_that_declares_too_many_members_is_refused_before_it_is_parsed(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            for number in range(plants.MAX_PLANT_MEMBERS + 1):
                zipped.writestr(f"member{number}.txt", "x")
        output.seek(0)
        with patch.object(zipfile, "ZipFile", side_effect=AssertionError("parsed")):
            with self.assertRaises(importer.ImportFailure) as raised:
                list(plants.plant_rows(output, {"rejected": 0, "skipped": 0}))
        self.assertEqual(str(raised.exception), plants.FORMAT_FAILURE)


class ArchiveEntryCountTests(SimpleTestCase):
    """The end record is read from the tail of the file before the archive is parsed (M1)."""

    def test_the_declared_count_is_read_from_the_end_record(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            zipped.writestr("one.csv", "a")
            zipped.writestr("two.csv", "b")
        position = output.seek(0, io.SEEK_END)
        self.assertEqual(importer.declared_entry_count(output), 2)
        self.assertEqual(output.tell(), position)

    def test_a_full_end_record_takes_the_count_from_the_zip64_record(self):
        record = importer.ZIP64_RECORD.pack(importer.ZIP64_RECORD_SIGNATURE, 44, 45, 45, 0, 0, 17, 17, 0, 0)
        locator = importer.ZIP64_LOCATOR.pack(importer.ZIP64_LOCATOR_SIGNATURE, 0, 0, 1)
        end = importer.END_RECORD.pack(importer.EOCD_SIGNATURE, 0, 0, 0xFFFF, 0xFFFF, 0, 0, 0)
        self.assertEqual(importer.declared_entry_count(io.BytesIO(record + locator + end)), 17)

    def test_a_full_end_record_without_a_locator_is_not_a_consistent_archive(self):
        end = importer.END_RECORD.pack(importer.EOCD_SIGNATURE, 0, 0, 0xFFFF, 0xFFFF, 0, 0, 0)
        self.assertIsNone(importer.declared_entry_count(io.BytesIO(end)))

    def test_an_end_record_that_is_not_at_the_end_of_the_file_is_not_used(self):
        end = importer.END_RECORD.pack(importer.EOCD_SIGNATURE, 0, 0, 0, 0, 0, 0, 0)
        self.assertIsNone(importer.declared_entry_count(io.BytesIO(end + b"junk")))
        self.assertIsNone(importer.declared_entry_count(io.BytesIO(b"no record here")))

    def test_an_address_archive_that_declares_two_members_is_refused_before_it_is_parsed(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            zipped.writestr("one.csv", "a")
            zipped.writestr("two.csv", "b")
        output.seek(0)
        with patch.object(zipfile, "ZipFile", side_effect=AssertionError("parsed")):
            with self.assertRaises(importer.ImportFailure) as raised:
                list(importer.address_rows(output))
        self.assertEqual(str(raised.exception), "Expected exactly one CSV member.")


class DeadlineTests(SimpleTestCase):
    """The total deadline of one download (M2): the clock is injected, nothing waits for real minutes."""

    def test_a_body_that_trickles_bytes_is_cut_off_at_the_deadline(self):
        clock = FakeClock()
        read = []

        def trickle():
            for _ in range(1000):
                clock.now += 4
                read.append(1)
                yield b"x"

        session = MagicMock()
        session.get.return_value = FakeResponse(trickle, {"Content-Type": "application/zip"})
        with self.assertRaises(importer.ImportFailure) as raised:
            list(importer._chunks(session, importer.PLANT_URL, 10_000, seconds=10, clock=clock))
        self.assertEqual(str(raised.exception), "Download exceeds its resource limit.")
        self.assertLess(len(read), 100)

    def test_a_body_within_the_deadline_is_read_completely(self):
        clock = FakeClock()
        session = MagicMock()
        session.get.return_value = FakeResponse(
            lambda: iter([b"ab", b"cd"]), {"Content-Type": "application/zip"},
        )
        self.assertEqual(
            b"".join(importer._chunks(session, importer.PLANT_URL, 100, seconds=10, clock=clock)), b"abcd",
        )

    def test_the_wait_for_the_headers_counts_towards_the_deadline(self):
        clock = FakeClock()
        session = MagicMock()

        def slow_connect(*_args, **_kwargs):
            clock.now += 20
            return FakeResponse(lambda: iter([b"data"]), {"Content-Type": "application/zip"})

        session.get.side_effect = slow_connect
        with self.assertRaises(importer.ImportFailure) as raised:
            list(importer._chunks(session, importer.PLANT_URL, 100, seconds=10, clock=clock))
        self.assertEqual(str(raised.exception), "Download exceeds its resource limit.")

    def test_the_deadline_shuts_the_socket_of_a_stalled_read(self):
        clock = FakeClock()
        left, right = socket.socketpair()
        release = threading.Event()
        session = MagicMock()

        def stalled_body():
            # The budget of the request is the one that ``_chunks`` mounted on the session.
            session.mount.call_args.args[1]._budget.add(left)
            clock.now = 100.0
            release.wait(10)
            yield b"late"

        session.get.return_value = FakeResponse(stalled_body, {"Content-Type": "application/zip"})
        started = time.monotonic()
        try:
            with self.assertRaises(importer.ImportFailure) as raised:
                list(importer._chunks(session, importer.PLANT_URL, 100, seconds=10, clock=clock))
        finally:
            release.set()
            right.close()
        self.assertEqual(str(raised.exception), "Download exceeds its resource limit.")
        self.assertEqual(left.fileno(), -1)
        self.assertLess(time.monotonic() - started, 5)


class DownloadRuleTests(SimpleTestCase):
    def session_answering(self, headers, status_code=200):
        session = MagicMock()
        response = MagicMock(status_code=status_code, headers=headers)
        response.iter_content.return_value = [b"data"]
        session.get.return_value.__enter__.return_value = response
        return session

    def test_only_the_fixed_plant_url_is_approved(self):
        self.assertEqual(
            importer.PLANT_URL,
            "https://data.geo.admin.ch/ch.bfe.elektrizitaetsproduktionsanlagen/csv/2056/"
            "ch.bfe.elektrizitaetsproduktionsanlagen.zip",
        )
        with self.assertRaises(importer.ImportFailure) as raised:
            list(importer._chunks(MagicMock(), "https://data.geo.admin.ch/other.zip", 100))
        self.assertEqual(str(raised.exception), "Unapproved download URL.")

    def test_the_request_has_no_redirects_and_verifies_the_certificate(self):
        session = self.session_answering({"Content-Type": "application/zip"})
        list(importer._chunks(session, importer.PLANT_URL, 100, content_types=plants.PLANT_CONTENT_TYPES))
        kwargs = session.get.call_args.kwargs
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertIs(kwargs["verify"], True)

    def test_a_wrong_content_type_is_refused(self):
        session = self.session_answering({"Content-Type": "text/html; charset=utf-8"})
        with self.assertRaises(importer.ImportFailure) as raised:
            list(importer._chunks(session, importer.PLANT_URL, 100, content_types=plants.PLANT_CONTENT_TYPES))
        self.assertEqual(str(raised.exception), "Unexpected HTTP content type.")

    def test_the_content_type_of_the_official_file_is_accepted(self):
        session = self.session_answering({"Content-Type": "application/octet-stream"})
        self.assertEqual(
            b"".join(importer._chunks(session, importer.PLANT_URL, 100, content_types=plants.PLANT_CONTENT_TYPES)),
            b"data",
        )

    def test_a_size_above_the_limit_is_refused(self):
        session = self.session_answering({"Content-Type": "application/zip", "Content-Length": "200"})
        with self.assertRaises(importer.ImportFailure) as raised:
            list(importer._chunks(session, importer.PLANT_URL, 100, content_types=plants.PLANT_CONTENT_TYPES))
        self.assertEqual(str(raised.exception), "Download exceeds the size limit.")

    def test_a_missing_file_is_refused_with_its_own_text(self):
        session = self.session_answering({"Content-Type": "application/zip"}, status_code=404)
        with self.assertRaises(importer.ImportFailure) as raised:
            list(importer._chunks(session, importer.PLANT_URL, 100, content_types=plants.PLANT_CONTENT_TYPES))
        self.assertEqual(str(raised.exception), "Official file not found.")

    def test_the_error_texts_of_the_plant_import_are_allowed_and_have_words_for_the_page(self):
        texts = {
            plants.FORMAT_FAILURE, plants.SIZE_FAILURE, plants.CHANGE_FAILURE,
            plants.EMPTY_FAILURE, plants.UNREADABLE_FAILURE,
        }
        self.assertEqual(texts, set(jobs.PLANT_FAILURES))
        self.assertTrue(texts <= set(status.REASONS))
        self.assertIn("Unexpected HTTP content type.", jobs.SAFE_FAILURES)
        self.assertIn("Unexpected HTTP content type.", status.REASONS)


@tag("mariadb")
@override_settings(LANGUAGE_CODE="en")
class PublicationTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        # The job holds the claim of this publication (the switch checks it, M4).
        self.claim = uuid.uuid4()
        ImportRequest.objects.update_or_create(
            pk=1, defaults={"state": ImportRequest.State.RUNNING, "claim_token": self.claim},
        )

    def publish(self, lines, checksum=CHECKSUM):
        return plants._publish(archive(csv_text(lines)), checksum, None, None, self.claim)

    def publish_with_takeover(self, lines, change):
        """Publishes ``lines``; ``change`` runs after the generation is loaded and before the switch."""
        original = plants._load_generation

        def load_then_change(*args, **kwargs):
            result = original(*args, **kwargs)
            change()
            return result

        with patch.object(plants, "_load_generation", side_effect=load_then_change):
            return self.publish(lines, checksum="f" * 64)

    def test_the_plants_are_published_with_the_building_summary(self):
        lines = [plant_line(1, TotalPower="10.5"), plant_line(2, TotalPower="4.5")]
        lines += [plant_line(number) for number in range(3, 22)]
        lines += [plant_line(22, EGID="")]  # one row without EGID is left out
        count = self.publish(lines)
        self.assertEqual(count, 21)
        register = PlantRegister.objects.get(pk=1)
        self.assertEqual((register.row_count, register.skipped_count, register.rejected_count), (21, 1, 0))
        self.assertEqual(register.file_row_count, 22)
        self.assertEqual(register.checksum, CHECKSUM)
        summary = plant_lookup.plant_summary(1001)
        self.assertEqual(summary.count, 1)
        self.assertEqual(summary.total_kw, Decimal("10.500"))
        self.assertEqual(plant_lookup.plant_summary(1003).count, 1)
        self.assertEqual(plant_lookup.plant_summary(1022).count, 0)

    def test_rows_without_egid_do_not_refuse_the_publication(self):
        lines = [plant_line(number) for number in range(1, 11)]
        lines += [plant_line(number, EGID="") for number in range(200, 290)]  # 90 of 100 rows without EGID
        self.assertEqual(self.publish(lines), 10)
        register = PlantRegister.objects.get(pk=1)
        self.assertEqual((register.row_count, register.skipped_count, register.file_row_count), (10, 90, 100))

    def test_a_claim_lost_before_the_switch_keeps_the_published_generation(self):
        self.publish([plant_line(1), plant_line(2)])
        published = PlantRegister.objects.get(pk=1).generation

        def take_over():
            ImportRequest.objects.filter(pk=1).update(claim_token=uuid.uuid4())

        with self.assertRaises(importer.ImportFailure) as raised:
            self.publish_with_takeover([plant_line(3), plant_line(4)], take_over)
        self.assertEqual(str(raised.exception), plants.LEASE_FAILURE)
        self.assertEqual(PlantRegister.objects.get(pk=1).generation, published)
        self.assertEqual(PlantRegister.objects.get(pk=1).checksum, CHECKSUM)
        self.assertEqual(Plant.objects.count(), 2)
        self.assertEqual(Plant.objects.filter(generation=published).count(), 2)

    def test_a_generation_published_meanwhile_is_not_overwritten(self):
        self.publish([plant_line(1), plant_line(2)])
        newer = uuid.uuid4()

        def publish_newer():
            PlantRegister.objects.filter(pk=1).update(generation=newer)

        with self.assertRaises(importer.ImportFailure) as raised:
            self.publish_with_takeover([plant_line(3), plant_line(4)], publish_newer)
        self.assertEqual(str(raised.exception), plants.LEASE_FAILURE)
        self.assertEqual(PlantRegister.objects.get(pk=1).generation, newer)
        self.assertEqual(Plant.objects.count(), 2)

    def test_every_other_generation_is_deleted_after_the_switch(self):
        stray = uuid.uuid4()
        Plant.objects.create(
            generation=stray, xtf_id="stray", egid=7, subcategory="subcat_1", total_power_kw=Decimal("1"),
        )
        self.publish([plant_line(1), plant_line(2)])
        self.assertFalse(Plant.objects.filter(generation=stray).exists())
        self.assertEqual(Plant.objects.count(), 2)

    def test_several_plants_of_one_building_are_summed_and_their_coordinates_averaged(self):
        self.publish([
            plant_line(1, EGID="5000", TotalPower="10", _x="2600000.000", _y="1200000.000"),
            plant_line(2, EGID="5000", TotalPower="2.5", _x="2600002.000", _y="1200002.000"),
            plant_line(3, EGID="5000", TotalPower="1", _x="", _y=""),
        ])
        summary = plant_lookup.plant_summary(5000)
        self.assertEqual(summary.count, 3)
        self.assertEqual(summary.total_kw, Decimal("13.500"))
        self.assertAlmostEqual(summary.x, 2600001.0)
        self.assertAlmostEqual(summary.y, 1200001.0)

    def test_nothing_is_summarised_before_a_publication(self):
        self.assertIsNone(plant_lookup.plant_summary(1001))

    def test_a_refused_generation_leaves_the_published_one_in_place(self):
        self.publish([plant_line(number) for number in range(1, 101)])
        before = PlantRegister.objects.get(pk=1).generation
        # Every row fails the checks: nothing is stored, so the generation is refused as empty.
        lines = [plant_line(number, TotalPower="x") for number in range(101, 201)]
        with self.assertRaises(importer.ImportFailure) as raised:
            self.publish(lines, checksum="b" * 64)
        self.assertEqual(str(raised.exception), plants.EMPTY_FAILURE)
        self.assertEqual(PlantRegister.objects.get(pk=1).generation, before)
        self.assertEqual(PlantRegister.objects.get(pk=1).checksum, CHECKSUM)
        self.assertEqual(Plant.objects.exclude(generation=before).count(), 0)
        self.assertEqual(plant_lookup.plant_summary(1001).count, 1)

    def test_a_few_rejected_rows_are_counted_and_the_import_goes_on(self):
        lines = [plant_line(number) for number in range(1, 200)] + [plant_line(200, TotalPower="NaN")]
        self.assertEqual(self.publish(lines), 199)
        self.assertEqual(PlantRegister.objects.get(pk=1).rejected_count, 1)

    def test_a_large_share_of_rejected_rows_does_not_refuse_the_publication(self):
        lines = [plant_line(number) for number in range(1, 51)]
        lines += [plant_line(number, TotalPower="x") for number in range(51, 101)]
        self.assertEqual(self.publish(lines), 50)
        self.assertEqual(PlantRegister.objects.get(pk=1).rejected_count, 50)

    def test_the_statistic_is_stored_with_the_import_and_reads_the_published_directory(self):
        Directory.objects.update_or_create(pk=1, defaults={"row_count": 7})
        self.publish([plant_line(1), plant_line(2, EGID=""), plant_line(3, TotalPower="x")])
        register = PlantRegister.objects.get(pk=1)
        self.assertEqual((register.address_count, register.file_row_count, register.row_count), (7, 3, 1))
        self.assertEqual(
            register.row_count + register.rejected_count + register.skipped_count, register.file_row_count,
        )
        self.assertIsNotNone(register.imported_at)
        # The next import reads the directory as it is published then.
        Directory.objects.filter(pk=1).update(row_count=9)
        self.publish([plant_line(1), plant_line(4)], checksum="c" * 64)
        self.assertEqual(PlantRegister.objects.get(pk=1).address_count, 9)

    def test_without_a_published_directory_the_address_count_is_zero(self):
        self.publish([plant_line(1)])
        self.assertEqual(PlantRegister.objects.get(pk=1).address_count, 0)

    def test_a_failed_import_keeps_the_statistic_of_the_last_success(self):
        Directory.objects.update_or_create(pk=1, defaults={"row_count": 7})
        self.publish([plant_line(1), plant_line(2, EGID="")])
        keep = (
            "address_count", "file_row_count", "row_count", "rejected_count", "skipped_count", "imported_at", "checksum",
        )
        before = PlantRegister.objects.values(*keep).get(pk=1)
        Directory.objects.filter(pk=1).update(row_count=9)
        with self.assertRaises(importer.ImportFailure):
            self.publish([plant_line(number, EGID="") for number in range(1, 5)], checksum="b" * 64)
        self.assertEqual(PlantRegister.objects.values(*keep).get(pk=1), before)

    def test_a_row_count_that_moves_by_more_than_the_factor_is_refused(self):
        self.publish([plant_line(number) for number in range(1, 11)])
        with self.assertRaises(importer.ImportFailure) as raised:
            self.publish([plant_line(number) for number in range(1, 5)], checksum="c" * 64)  # 4 < 10 / 2
        self.assertEqual(str(raised.exception), plants.CHANGE_FAILURE)
        with self.assertRaises(importer.ImportFailure):
            self.publish([plant_line(number) for number in range(1, 26)], checksum="d" * 64)  # 25 > 10 * 2
        self.assertEqual(PlantRegister.objects.get(pk=1).row_count, 10)

    def test_an_unchanged_file_keeps_its_plants_and_rewrites_the_statistic(self):
        Directory.objects.update_or_create(pk=1, defaults={"row_count": 7})
        self.publish([plant_line(1), plant_line(2)])
        before = PlantRegister.objects.get(pk=1)
        Directory.objects.filter(pk=1).update(row_count=9)
        # The same file again (same SHA-256): its rows are not read again, the plants of the publication stay.
        self.assertEqual(self.publish([plant_line(7), plant_line(8), plant_line(9)]), 2)
        after = PlantRegister.objects.get(pk=1)
        self.assertEqual(after.generation, before.generation)
        self.assertEqual(after.address_count, 9)
        self.assertGreater(after.imported_at, before.imported_at)
        keep = ("row_count", "file_row_count", "rejected_count", "skipped_count", "checksum")
        self.assertEqual(
            [getattr(after, name) for name in keep], [getattr(before, name) for name in keep],
        )
        self.assertEqual(plant_lookup.plant_summary(1001).count, 1)
        self.assertEqual(plant_lookup.plant_summary(1007).count, 0)

    def publish_on(self, day, lines, checksum=CHECKSUM):
        with patch("django.utils.timezone.localdate", return_value=day):
            return self.publish(lines, checksum)

    def test_the_day_of_the_change_is_set_for_a_new_file_and_kept_for_the_same_one(self):
        day = timezone.localdate() - timedelta(days=40)
        self.publish_on(day, [plant_line(1)])
        self.assertEqual(PlantRegister.objects.get(pk=1).content_changed_at, day)
        self.publish_on(day + timedelta(days=31), [plant_line(1)])
        self.assertEqual(PlantRegister.objects.get(pk=1).content_changed_at, day)
        self.publish_on(day + timedelta(days=31), [plant_line(2)], checksum="e" * 64)
        self.assertEqual(PlantRegister.objects.get(pk=1).content_changed_at, day + timedelta(days=31))

    def test_the_hint_shows_from_thirty_one_days_without_a_change_and_not_before(self):
        day = timezone.localdate() - timedelta(days=40)
        self.publish_on(day, [plant_line(1)])
        for offset, shown in ((29, False), (30, False), (31, True)):
            with patch("django.utils.timezone.localdate", return_value=day + timedelta(days=offset)):
                warning = status.plant_status()["content_warning"]
            self.assertEqual(bool(warning), shown, offset)

    def test_a_changed_file_removes_the_hint(self):
        day = timezone.localdate() - timedelta(days=40)
        self.publish_on(day, [plant_line(1)])
        self.publish_on(day + timedelta(days=31), [plant_line(2)], checksum="e" * 64)
        with patch("django.utils.timezone.localdate", return_value=day + timedelta(days=40)):
            self.assertEqual(status.plant_status()["content_warning"], "")

    def test_a_new_publication_replaces_the_previous_generation(self):
        self.publish([plant_line(1), plant_line(2)])
        old = PlantRegister.objects.get(pk=1).generation
        self.publish([plant_line(3), plant_line(4)], checksum="e" * 64)
        self.assertNotEqual(PlantRegister.objects.get(pk=1).generation, old)
        self.assertFalse(Plant.objects.filter(generation=old).exists())
        self.assertEqual(Plant.objects.count(), 2)

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(importer.ImportFailure) as raised:
            self.publish([plant_line(1, EGID="")])
        self.assertEqual(str(raised.exception), plants.EMPTY_FAILURE)

    def test_the_plant_tables_are_in_the_addresses_file(self):
        self.assertEqual(AddressRouter().db_for_write(Plant), DIRECTORY_ALIAS)
        self.assertEqual(AddressRouter().db_for_read(PlantRegister), DIRECTORY_ALIAS)
        self.assertIn("plant", DIRECTORY_MODELS)
        self.assertIn("plantregister", DIRECTORY_MODELS)
        self.assertFalse(AddressRouter().allow_migrate("default", "addresses", model_name="plant"))


@tag("mariadb")
@override_settings(LANGUAGE_CODE="en")
class SharedClaimTests(TestCase):
    databases = {"default", "addresses"}

    def test_the_plant_register_waits_while_an_address_request_is_queued(self):
        self.assertTrue(jobs.request_import())
        self.assertIsNone(jobs.claim(scheduled=True, web=True, source=ImportRequest.Source.PLANT))
        address = jobs.claim(web=True)
        self.assertIsNotNone(address)
        self.assertEqual(address.source, ImportRequest.Source.ADDRESS)

    def test_the_address_import_waits_for_a_running_plant_import(self):
        plant = jobs.claim(scheduled=True, web=True, source=ImportRequest.Source.PLANT)
        self.assertEqual(plant.source, ImportRequest.Source.PLANT)
        self.assertIsNone(jobs.claim(web=True))
        self.assertFalse(jobs.request_import())
        jobs.record_success(plant, 3)
        self.assertTrue(jobs.request_import())
        self.assertIsNotNone(jobs.claim(web=True))

    def test_a_plant_claim_leaves_the_address_schedule_alone(self):
        jobs.claim(scheduled=True, web=True, source=ImportRequest.Source.PLANT)
        job = ImportRequest.objects.get(pk=1)
        self.assertIsNone(job.last_automatic_attempt_at)
        self.assertEqual(job.source, ImportRequest.Source.PLANT)

    def test_the_plant_register_only_runs_as_the_daily_run(self):
        self.assertIsNone(jobs.claim(web=True, source=ImportRequest.Source.PLANT))

    @override_settings(ADDRESS_IMPORT_AUTOMATIC=True)
    def test_the_daily_plant_run_is_once_a_day_after_its_time(self):
        with patch.object(schedule, "_execute_with_heartbeat") as execute:
            early = timezone.make_aware(datetime(2026, 10, 24, 2, 0))
            self.assertEqual(schedule.run_plant_daily(early), schedule.BEFORE_TIME)
            due = timezone.make_aware(datetime(2026, 10, 24, 4, 0))
            self.assertEqual(schedule.run_plant_daily(due), schedule.IMPORTED)
            self.assertEqual(schedule.run_plant_daily(due), schedule.ALREADY_RAN)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[0].source, ImportRequest.Source.PLANT)

    @override_settings(ADDRESS_IMPORT_AUTOMATIC=True)
    def test_the_daily_plant_run_waits_for_a_queued_address_request(self):
        jobs.request_import()
        with patch.object(schedule, "_execute_with_heartbeat") as execute:
            due = timezone.make_aware(datetime(2026, 10, 24, 4, 0))
            self.assertEqual(schedule.run_plant_daily(due), schedule.BUSY)
            execute.assert_not_called()


@tag("mariadb")
@override_settings(ADDRESS_IMPORT_AUTOMATIC=True, LANGUAGE_CODE="en")
class DailyChainTests(TestCase):
    """The daily run (``schedule.run_daily``) starts the plant register after the address import of the same run (D-117).

    The job and the claims are the real ones; only the download and the import are replaced by a stand-in that records
    the outcome the way ``jobs.execute`` does (``record_success`` or ``record_failure``).
    """

    databases = {"default", "addresses"}
    ERROR = "Official download failed."

    def setUp(self):
        self.started = []
        self.failing = set()

        def execute(claimed, progress=None):
            self.started.append(claimed.source)
            if claimed.source in self.failing:
                error = importer.ImportFailure(self.ERROR)
                jobs.record_failure(claimed, error)
                raise error
            jobs.record_success(claimed, 3)

        self.enterContext(patch.object(schedule, "_execute_with_heartbeat", side_effect=execute))
        # 2026-10-24 is a Saturday: the transformer station scan (D-118) follows the plant register; these tests
        # do not scan (see test_transformers for the order of the three steps).
        self.enterContext(patch.object(schedule, "run_transformer_weekly", return_value=schedule.NOT_SATURDAY))
        self.morning = timezone.make_aware(datetime(2026, 10, 24, 4, 0))
        # The address import is due by the update flag.
        ImportRequest.objects.create(pk=1, update_flag_set_at=timezone.now())

    def test_the_plant_import_starts_after_the_address_import_in_the_same_run(self):
        self.assertEqual(schedule.run_daily(self.morning), schedule.IMPORTED)
        self.assertEqual(self.started, [ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT])
        self.assertEqual(ImportRequest.objects.get(pk=1).source, ImportRequest.Source.PLANT)
        # Later in the day both parts are done: nothing starts again.
        self.assertEqual(schedule.run_daily(self.morning + timedelta(hours=2)), schedule.ALREADY_RAN)
        self.assertEqual(len(self.started), 2)

    def test_a_failed_address_import_does_not_stop_the_plant_import(self):
        self.failing.add(ImportRequest.Source.ADDRESS)
        with self.assertRaises(importer.ImportFailure):
            schedule.run_daily(self.morning)
        self.assertEqual(self.started, [ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT])
        # A later run of the same day does not start either import again (owner decision 2026-10-25: the plant import
        # runs every day, also after a failed address import).
        self.assertEqual(schedule.run_daily(self.morning + timedelta(hours=1)), schedule.ALREADY_RAN)
        self.assertEqual(self.started, [ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT])
        self.assertEqual(ImportRequest.objects.get(pk=1).last_plant_scheduled_run_on, self.morning.date())

    def test_the_next_daily_run_tries_the_address_import_and_then_the_plant_import_again(self):
        self.failing.add(ImportRequest.Source.ADDRESS)
        with self.assertRaises(importer.ImportFailure):
            schedule.run_daily(self.morning)
        self.failing.clear()
        tomorrow = self.morning + timedelta(days=1)
        self.assertEqual(schedule.run_daily(tomorrow), schedule.IMPORTED)
        self.assertEqual(self.started, [ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT, ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT])

    def test_a_failed_plant_import_is_raised_and_recorded_and_not_repeated_that_day(self):
        self.failing.add(ImportRequest.Source.PLANT)
        with self.assertRaises(importer.ImportFailure):
            schedule.run_daily(self.morning)
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual((job.state, job.source, job.last_error), (ImportRequest.State.FAILED, ImportRequest.Source.PLANT, self.ERROR))
        self.assertEqual(schedule.run_daily(self.morning + timedelta(hours=1)), schedule.ALREADY_RAN)
        self.assertEqual(self.started, [ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT])


@tag("mariadb")
@override_settings(LANGUAGE_CODE="en")
class PlantSecurityEventTests(TestCase):
    databases = {"default", "addresses"}

    def test_a_successful_import_is_logged_with_its_count_only(self):
        claimed = jobs.claim(scheduled=True, web=True, source=ImportRequest.Source.PLANT)
        with patch.object(jobs.security_log, "record") as record:
            jobs.record_success(claimed, 3)
        record.assert_called_once_with(SecurityEvent.Event.PV_IMPORT_SUCCEEDED, detail="automatic, 3 plants")

    def test_a_failed_import_is_logged_with_the_fixed_reason_only(self):
        claimed = jobs.claim(scheduled=True, web=True, source=ImportRequest.Source.PLANT)
        with patch.object(jobs.security_log, "record") as record:
            jobs.record_failure(claimed, importer.ImportFailure(plants.FORMAT_FAILURE))
        record.assert_called_once_with(
            SecurityEvent.Event.PV_IMPORT_FAILED, detail=f"automatic, {plants.FORMAT_FAILURE}",
        )
        self.assertEqual(ImportRequest.objects.get(pk=1).last_error, plants.FORMAT_FAILURE)

    def test_a_missing_file_is_logged_with_its_step(self):
        claimed = jobs.claim(scheduled=True, web=True, source=ImportRequest.Source.PLANT)
        with patch.object(jobs.security_log, "record") as record:
            jobs.record_failure(claimed, importer.ImportFailure("Official file not found."))
        record.assert_called_once_with(
            SecurityEvent.Event.PV_IMPORT_FAILED, detail="automatic, Official file not found.",
        )

    def test_an_unknown_failure_text_is_not_stored(self):
        claimed = jobs.claim(scheduled=True, web=True, source=ImportRequest.Source.PLANT)
        with patch.object(jobs.security_log, "record") as record:
            jobs.record_failure(claimed, importer.ImportFailure("A row with private data: 1001"))
        self.assertEqual(ImportRequest.objects.get(pk=1).last_error, jobs.UNEXPECTED_FAILURE)
        self.assertNotIn("private", str(record.call_args))


@tag("mariadb")
@override_settings(LANGUAGE_CODE="en")
class PlantSummaryInputTests(TestCase):
    databases = {"default", "addresses"}

    def test_only_a_building_identifier_of_one_to_nine_digits_is_accepted(self):
        for egid in (0, -1, 1_000_000_000, "1001", 1001.0, None, True):
            with self.subTest(egid=egid), self.assertRaises(ValueError) as raised:
                plant_lookup.plant_summary(egid)
            self.assertEqual(str(raised.exception), "The building identifier is invalid.")
        self.assertIsNone(plant_lookup.plant_summary(1))
        self.assertIsNone(plant_lookup.plant_summary(999_999_999))


@tag("mariadb")
@override_settings(LANGUAGE_CODE="en")
class ImportPlantsTests(TestCase):
    """The whole import (download, checks, publication) with the network closed (D-117).

    The one download is a fake answer; every route to the network is refused by the guard, so a test that reaches the
    network fails instead of connecting.
    """

    databases = {"default", "addresses"}

    def setUp(self):
        self.calls = []
        self.answer = None

        def refuse(*_args, **_kwargs):
            raise AssertionError("The plant import tests must not use the network.")

        def fake_get(_session, url, **_kwargs):
            self.calls.append(url)
            return self.answer

        self.enterContext(patch.object(requests.Session, "get", fake_get))
        self.enterContext(patch.object(requests.Session, "send", refuse))
        self.enterContext(patch("socket.create_connection", refuse))
        self.enterContext(patch("socket.getaddrinfo", refuse))
        # The free-space check depends on the machine; the tests of the check itself are in test_directory.
        self.enterContext(patch.object(plants, "_require_free_disk"))
        self.claim = jobs.claim(scheduled=True, web=True, source=ImportRequest.Source.PLANT).token

    def zip_bytes(self, lines, columns=COLUMNS):
        return archive(csv_text(lines, columns)).getvalue()

    def answer_with(self, data):
        chunks = [data[start:start + 4096] for start in range(0, len(data), 4096)]
        self.answer = FakeResponse(lambda: iter(chunks), {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
            "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
        })

    def test_the_plant_register_is_downloaded_checked_and_published(self):
        data = self.zip_bytes([plant_line(1), plant_line(2)])
        self.answer_with(data)
        self.assertEqual(plants.import_plants(claim_token=self.claim), 2)
        self.assertEqual(self.calls, [importer.PLANT_URL])
        register = PlantRegister.objects.get(pk=1)
        self.assertEqual(register.row_count, 2)
        self.assertEqual(register.checksum, hashlib.sha256(data).hexdigest())
        self.assertEqual(plant_lookup.plant_summary(1001).count, 1)

    def test_a_format_failure_at_import_keeps_the_published_generation(self):
        self.answer_with(self.zip_bytes([plant_line(1)]))
        plants.import_plants(claim_token=self.claim)
        before = PlantRegister.objects.get(pk=1).generation
        self.answer_with(self.zip_bytes([plant_line(2)], columns=[column for column in COLUMNS if column != "_y"]))
        with self.assertRaises(importer.ImportFailure) as raised:
            plants.import_plants(claim_token=self.claim)
        self.assertEqual(str(raised.exception), plants.FORMAT_FAILURE)
        self.assertEqual(PlantRegister.objects.get(pk=1).generation, before)
        self.assertEqual(plant_lookup.plant_summary(1001).count, 1)

    def test_a_download_that_passes_the_deadline_publishes_nothing(self):
        clock = FakeClock()
        original = importer._chunks

        def late_clock(session, url, limit, on_chunk=None, content_types=None, on_headers=None, **_kwargs):
            return original(session, url, limit, on_chunk, content_types, on_headers, seconds=0, clock=clock)

        self.answer_with(self.zip_bytes([plant_line(1)]))
        with patch.object(plants, "_chunks", side_effect=late_clock):
            with self.assertRaises(importer.ImportFailure) as raised:
                plants.import_plants(claim_token=self.claim)
        self.assertEqual(str(raised.exception), "Download exceeds its resource limit.")
        self.assertFalse(PlantRegister.objects.filter(row_count__gt=0).exists())

    def test_a_failed_plant_import_leaves_the_statistic_unchanged(self):
        Directory.objects.update_or_create(pk=1, defaults={"row_count": 7})
        self.answer_with(self.zip_bytes([plant_line(1), plant_line(2)]))
        plants.import_plants(claim_token=self.claim)
        keep = (
            "generation", "address_count", "file_row_count", "row_count", "rejected_count", "skipped_count",
            "imported_at", "checksum",
        )
        before = PlantRegister.objects.values(*keep).get(pk=1)
        Directory.objects.filter(pk=1).update(row_count=9)
        self.answer_with(self.zip_bytes([plant_line(3)], columns=[column for column in COLUMNS if column != "_y"]))
        with self.assertRaises(importer.ImportFailure):
            plants.import_plants(claim_token=self.claim)
        self.assertEqual(PlantRegister.objects.values(*keep).get(pk=1), before)
