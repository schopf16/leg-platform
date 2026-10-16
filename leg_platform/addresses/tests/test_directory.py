import hashlib
import io
import json
import sys
import types
import uuid
import zipfile
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.middleware.csrf import get_token
from django.db import DatabaseError, connections
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.models import RateLimitBucket, RateLimitEvent
from leg_platform.accounts.rate_limits import RATE_LIMIT_PURPOSE
from leg_platform.addresses import importer
from leg_platform.addresses.jobs import process_one, request_import
from leg_platform.addresses.limits import allow_request
from leg_platform.addresses.models import Address, Directory, ImportRequest
from leg_platform.addresses.router import AddressRouter
from leg_platform.addresses.services import approximate_matches, clean_input, directory_status, lookup

HEADER = "STN_LABEL;ADR_NUMBER;ZIP_LABEL;BDG_NAME;COM_NAME\n"
SYNTHETIC_CSV = HEADER + "Example Street;12a;9999 Example Town;Discarded synthetic building;Discarded municipality\n"


def archive_bytes(csv_text=SYNTHETIC_CSV, name=importer.CSV_NAME):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, csv_text.encode("utf-8-sig"))
    return output.getvalue()


def item_bytes(archive, **changes):
    asset = {
        "href": importer.ASSET_URL,
        "checksum:multihash": "1220" + hashlib.sha256(archive).hexdigest(),
    }
    asset.update(changes)
    return json.dumps({
        "id": "amtliches-gebaeudeadressverzeichnis_ch",
        "properties": {"datetime": timezone.localdate().isoformat() + "T00:00:00Z"},
        "assets": {importer.ASSET_NAME: asset},
    }).encode()


def session_for(archive=None, metadata=None, *, status=200):
    archive = archive if archive is not None else archive_bytes()
    metadata = metadata if metadata is not None else item_bytes(archive)
    session = MagicMock()
    session.__enter__.return_value = session

    def get(url, **kwargs):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        content = metadata if url == importer.ITEM_URL else archive
        response.headers = {"Content-Length": str(len(content))}
        response.iter_content.return_value = [content]
        return response

    session.get.side_effect = get
    return session


class ParserTests(SimpleTestCase):
    def test_only_approved_columns_are_returned_and_postal_locality_is_used(self):
        self.assertEqual(list(importer.address_rows(io.BytesIO(archive_bytes()))), [{
            "street": "Example Street", "house_number": "12a",
            "postal_code": "9999", "locality": "Example Town",
        }])

    def test_dangerous_or_unexpected_zip_members_rejected(self):
        for name in ("../escape.csv", "/escape.csv", "other.csv", "sub/" + importer.CSV_NAME):
            with self.subTest(name=name), self.assertRaises(importer.ImportFailure):
                list(importer.address_rows(io.BytesIO(archive_bytes(name=name))))

    def test_multiple_members_rejected(self):
        content = io.BytesIO(archive_bytes())
        with zipfile.ZipFile(content, "a") as archive:
            archive.writestr("extra.csv", "synthetic")
        with self.assertRaises(importer.ImportFailure):
            list(importer.address_rows(content))

    def test_invalid_csv_rejected(self):
        for text in (
            "", HEADER, "STN_LABEL;ADR_NUMBER\nExample;1\n",
            "STN_LABEL;STN_LABEL;ADR_NUMBER;ZIP_LABEL\nExample;Example;1;9999 Town\n",
            HEADER + "Example;1;invalid;ignored;ignored\n",
            HEADER + "Example;1;9999 ;ignored;ignored\n",
            HEADER + "Example;1;9999 Town;extra\n",
            HEADER + ";1;9999 Town;ignored;ignored\n",
            HEADER + "Example;1;9999 Town;\"unterminated\n",
            HEADER + "Example\x00;1;9999 Town;ignored;ignored\n",
        ):
            with self.subTest(text=text[:60]), self.assertRaises(importer.ImportFailure):
                list(importer.address_rows(io.BytesIO(archive_bytes(text))))

    def test_limits_for_uncompressed_size_line_count_and_field_length(self):
        for constant, value in (("MAX_CSV_BYTES", 20), ("MAX_LINE_BYTES", 20), ("MAX_ROWS", 0)):
            with self.subTest(constant=constant), patch.object(importer, constant, value):
                with self.assertRaises(importer.ImportFailure):
                    list(importer.address_rows(io.BytesIO(archive_bytes())))
        with self.assertRaises(importer.ImportFailure):
            list(importer.address_rows(io.BytesIO(archive_bytes(
                HEADER + ("S" * 256) + ";1;9999 Town;ignored;ignored\n"
            ))))

    def test_empty_house_number_allowed(self):
        rows = list(importer.address_rows(io.BytesIO(archive_bytes(
            HEADER + "Example;;9999 Town;ignored;ignored\n"
        ))))
        self.assertEqual(rows[0]["house_number"], "")

    def test_bad_multihash_and_external_urls_rejected(self):
        for changes in (
            {"href": "https://example.invalid/archive.zip"},
            {"href": importer.ASSET_URL + "?redirect=1"},
            {"checksum:multihash": "1221" + "0" * 64},
            {"checksum:multihash": "1220" + "x" * 64},
            {"checksum:multihash": None},
        ):
            archive = archive_bytes()
            with self.subTest(changes=changes), self.assertRaises(importer.ImportFailure):
                importer._metadata(session_for(archive, item_bytes(archive, **changes)))

    def test_metadata_future_date_wrong_item_and_invalid_json_rejected(self):
        base = json.loads(item_bytes(archive_bytes()))
        variants = [b"{", b"[]", json.dumps({**base, "id": "other"}).encode()]
        base["properties"]["datetime"] = "2999-01-01T00:00:00Z"
        variants.append(json.dumps(base).encode())
        for content in variants:
            with self.subTest(content=content), self.assertRaises(importer.ImportFailure):
                importer._metadata(session_for(metadata=content))

    def test_http_redirect_rejected_without_following(self):
        session = session_for(status=302)
        with self.assertRaises(importer.ImportFailure):
            list(importer._chunks(session, importer.ITEM_URL, 1000))
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.get.call_args.kwargs["timeout"], (10, 60))

    def test_stream_limit_and_wrong_content_length_rejected(self):
        for limit in (1, 10):
            with self.assertRaises(importer.ImportFailure):
                list(importer._chunks(session_for(), importer.ASSET_URL, limit))
        session = session_for()
        response = session.get(importer.ASSET_URL)
        response.headers = {}
        session.get.side_effect = None
        session.get.return_value = response
        with self.assertRaises(importer.ImportFailure):
            list(importer._chunks(session, importer.ASSET_URL, 1))
        response.headers = {"Content-Length": "999999"}
        with self.assertRaises(importer.ImportFailure):
            list(importer._chunks(session, importer.ASSET_URL, 1_000_000))

    def test_encoding_and_download_deadline_rejected(self):
        session = session_for()
        response = session.get(importer.ASSET_URL)
        session.get.side_effect = None
        session.get.return_value = response
        response.headers["Content-Encoding"] = "gzip"
        with self.assertRaises(importer.ImportFailure):
            list(importer._chunks(session, importer.ASSET_URL, 1_000_000))
        response.headers = {}
        with patch.object(importer.time, "monotonic", side_effect=[0, 2000]):
            with self.assertRaises(importer.ImportFailure):
                list(importer._chunks(session, importer.ASSET_URL, 1_000_000))

    def test_no_arbitrary_download_url(self):
        session = session_for()
        with self.assertRaises(importer.ImportFailure):
            list(importer._chunks(session, "http://data.geo.admin.ch/anything", 100))
        session.get.assert_not_called()

    def test_input_bounds(self):
        for values in (
            {"street": "s" * 256}, {"street": "bad\nstreet"},
            {"postal_code": "12345"}, {"postal_code": "１２３４"},
            {"postal_code": "abcd"}, {"country": ["CH"]},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                clean_input(values)


class RouterTests(SimpleTestCase):
    def test_directory_reads_and_queue_writes_routed(self):
        router = AddressRouter()
        self.assertEqual(router.db_for_read(Address), "addresses")
        self.assertEqual(router.db_for_read(Directory), "addresses")
        self.assertEqual(router.db_for_read(ImportRequest), "default")
        self.assertEqual(router.db_for_write(ImportRequest), "default")
        with self.assertRaises(ImproperlyConfigured):
            router.db_for_write(Address)
        with self.assertRaises(ImproperlyConfigured):
            router.db_for_write(Directory)

    def test_migration_isolation(self):
        router = AddressRouter()
        for model in ("address", "directory"):
            self.assertTrue(router.allow_migrate("address_import", "addresses", model))
            self.assertFalse(router.allow_migrate("default", "addresses", model))
            self.assertFalse(router.allow_migrate("addresses", "addresses", model))
        self.assertTrue(router.allow_migrate("default", "addresses", "importrequest"))
        self.assertFalse(router.allow_migrate("address_import", "addresses", "importrequest"))
        self.assertFalse(router.allow_migrate("addresses", "auth", "user"))
        self.assertFalse(router.allow_migrate("address_import", "auth", "user"))
        self.assertFalse(router.allow_relation(Address(), ImportRequest()))

    def test_web_configuration_has_no_writer_and_import_fails_closed(self):
        with patch.dict(connections.databases, {"default": {}}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                importer.require_writer()


class RateLimitAdapterTests(SimpleTestCase):
    def setUp(self):
        self.client_ip = MagicMock(return_value="192.0.2.1")
        self.consume = MagicMock(return_value=[True, True])
        self.ip_key = MagicMock(return_value="synthetic-ip-key")
        login = types.ModuleType("leg_platform.accounts.login_protection")
        login.axes_client_ip = self.client_ip
        rate = types.ModuleType("leg_platform.accounts.rate_limits")
        self.exhausted = MagicMock(return_value=False)
        rate.consume_all = self.consume
        rate.exhausted = self.exhausted
        rate.ip_key = self.ip_key
        self.modules = patch.dict(sys.modules, {
            "leg_platform.accounts.login_protection": login,
            "leg_platform.accounts.rate_limits": rate,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_lookup_uses_shared_ip_resolution_and_both_budgets(self):
        request = object()
        self.assertTrue(allow_request(request))
        self.client_ip.assert_called_once_with(request)
        self.ip_key.assert_called_once_with("192.0.2.1")
        checks = self.consume.call_args.args[0]
        self.assertEqual(checks[0], ("address-lookup-ip", "synthetic-ip-key", ((60, timedelta(minutes=1)),)))
        self.assertEqual(checks[1], ("address-lookup-global", "instance", ((600, timedelta(minutes=1)),)))

    def test_admin_has_separate_lower_budgets(self):
        self.assertTrue(allow_request(object(), admin=True))
        checks = self.consume.call_args.args[0]
        self.assertEqual(checks[0][0], "address-import-ip")
        self.assertEqual(checks[0][2][0][0], 5)
        self.assertEqual(checks[1][2][0][0], 20)

    def test_any_exhausted_budget_refuses(self):
        for results in ([False, True], [True, False], [False, False]):
            self.consume.return_value = results
            self.assertFalse(allow_request(object()))

    def test_read_only_precheck_refuses_before_anything_is_counted(self):
        self.exhausted.return_value = True
        self.assertFalse(allow_request(object()))
        self.consume.assert_not_called()
        checks = self.exhausted.call_args.args[0]
        self.assertEqual(checks[0][0], "address-lookup-ip")
        self.assertEqual(checks[1][0], "address-lookup-global")

    def test_untrusted_ip_and_counter_failure_refuse(self):
        self.client_ip.return_value = None
        self.assertFalse(allow_request(object()))
        self.consume.assert_not_called()
        self.client_ip.return_value = "192.0.2.1"
        for error in (DatabaseError("private database detail"), ValueError("private invalid value")):
            with self.subTest(error=type(error).__name__):
                self.consume.side_effect = error
                with self.assertLogs("leg_platform.addresses.limits", level="WARNING") as logs:
                    self.assertFalse(allow_request(object()))
                self.assertEqual(logs.output, [
                    "WARNING:leg_platform.addresses.limits:Address request limit check failed; request refused."
                ])

    def test_failing_read_only_precheck_fails_closed_without_counting(self):
        for error in (DatabaseError("private database detail"), ValueError("private invalid value")):
            with self.subTest(error=type(error).__name__):
                self.exhausted.side_effect = error
                with self.assertLogs("leg_platform.addresses.limits", level="WARNING") as logs:
                    self.assertFalse(allow_request(object()))
                    self.assertFalse(allow_request(object(), admin=True))
                self.consume.assert_not_called()
                self.assertEqual(logs.output, [
                    "WARNING:leg_platform.addresses.limits:Address request limit check failed; request refused."
                ] * 2)


@override_settings(AXES_IPWARE_PROXY_COUNT=0)
class RateLimitDatabaseTests(TestCase):
    """The real counters: a refused request changes nothing (security review of D-066, N-2)."""

    def snapshot(self):
        return (
            sorted(RateLimitBucket.objects.values_list("digest", "last_seen_at")),
            sorted(RateLimitEvent.objects.values_list("bucket_id", "occurred_at")),
        )

    def test_limits_apply_and_a_refused_request_writes_nothing(self):
        for admin, limit, global_scope in ((False, 60, "address-lookup-global"), (True, 5, "address-import-global")):
            with self.subTest(admin=admin):
                RateLimitBucket.objects.all().delete()
                request = RequestFactory().post("/", REMOTE_ADDR="192.0.2.7")
                for _number in range(limit):
                    self.assertTrue(allow_request(request, admin=admin))
                self.assertEqual(RateLimitEvent.objects.count(), 2 * limit)
                before = self.snapshot()
                self.assertEqual(len(before[0]), 2)
                global_digest = keyed_digest(f"{global_scope}\ninstance", RATE_LIMIT_PURPOSE)
                self.assertEqual(RateLimitEvent.objects.filter(bucket_id=global_digest).count(), limit)
                self.assertFalse(allow_request(request, admin=admin))
                # No bucket, no event, no last-use time changed, so the global bucket is untouched.
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(RateLimitEvent.objects.filter(bucket_id=global_digest).count(), limit)
                # Another client address is not affected by the used-up one.
                other = RequestFactory().post("/", REMOTE_ADDR="192.0.2.8")
                self.assertTrue(allow_request(other, admin=admin))


@override_settings(LANGUAGE_CODE="en", ROOT_URLCONF="leg_platform.addresses.tests.urls")
class DirectoryDatabaseTests(TransactionTestCase):
    # The reader alias "addresses" is a mirror of "address_import" (a second connection to the same
    # in-memory database); it sees only committed rows, so TestCase (one open transaction) cannot be
    # used. Checked in D-071 step 3b: converting it fails 46 of its 59 tests.
    databases = {"default", "addresses", "address_import"}

    def setUp(self):
        # A synthetic previous directory, never an official data fixture.
        self.generation = uuid.uuid4()
        Directory.objects.using("address_import").create(
            pk=1, generation=self.generation, data_date=timezone.localdate(),
            imported_at=timezone.now(), checksum="0" * 64, row_count=1,
        )
        Address.objects.using("address_import").create(
            generation=self.generation, street="Previous Street", house_number="1",
            postal_code="9999", locality="Example Town",
        )

    def test_migration_tables_are_separated(self):
        default_tables = connections["default"].introspection.table_names()
        writer_tables = connections["address_import"].introspection.table_names()
        self.assertIn("addresses_importrequest", default_tables)
        self.assertNotIn("addresses_address", default_tables)
        self.assertNotIn("addresses_directory", default_tables)
        self.assertIn("addresses_address", writer_tables)
        self.assertNotIn("addresses_importrequest", writer_tables)
        self.assertNotIn("auth_user", writer_tables)

    def test_verified_import_publishes_and_preserves_old_rows(self):
        session = session_for()
        with patch.object(importer.requests, "Session", return_value=session):
            self.assertEqual(importer.import_directory(), 1)
        self.assertFalse(session.trust_env)
        self.assertEqual({call.args[0] for call in session.get.call_args_list}, {importer.ITEM_URL, importer.ASSET_URL})
        directory = Directory.objects.using("addresses").get(pk=1)
        self.assertNotEqual(directory.generation, self.generation)
        self.assertEqual(Address.objects.using("addresses").count(), 2)
        self.assertEqual(lookup({"street": "Example"})["results"][0]["locality"], "Example Town")
        self.assertEqual(lookup({"street": "Previous"})["results"], [])

    def test_checksum_failure_keeps_published_directory(self):
        session = session_for(metadata=item_bytes(archive_bytes(), **{"checksum:multihash": "1220" + "f" * 64}))
        with patch.object(importer.requests, "Session", return_value=session), self.assertRaises(importer.ImportFailure):
            importer.import_directory()
        self.assertEqual(Directory.objects.using("addresses").get(pk=1).generation, self.generation)
        self.assertEqual(Address.objects.using("addresses").count(), 1)

    def test_partial_batch_failure_rolls_back_rows_and_pointer(self):
        invalid = HEADER + "New Street;1;9999 Town;x;x\nBad;1;not-a-postal-label;x;x\n"
        with patch.object(importer, "BATCH_SIZE", 1), self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes(invalid)), "1" * 64, timezone.localdate())
        self.assertEqual(Address.objects.using("addresses").count(), 1)
        self.assertEqual(Directory.objects.using("addresses").get(pk=1).generation, self.generation)

    def test_older_catalogue_cannot_replace_published_directory(self):
        with self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate() - timedelta(days=1))
        self.assertEqual(Directory.objects.using("addresses").get(pk=1).generation, self.generation)

    def test_same_checksum_is_idempotent(self):
        self.assertEqual(importer._publish(io.BytesIO(b"not parsed"), "0" * 64, timezone.localdate()), 1)
        self.assertEqual(Address.objects.using("addresses").count(), 1)

    def test_exact_advisory_match_and_unknown_address(self):
        self.assertTrue(lookup({"street": "previous street", "house_number": "1", "postal_code": "9999", "locality": "example town"}, mode="check")["match"])
        result = lookup({"street": "Unknown", "postal_code": "9999", "locality": "Example Town"}, mode="check")
        self.assertFalse(result["match"])
        self.assertEqual(result["status"], "available")
        self.assertIn("save it as entered", result["warning"])
        self.assertEqual(result["kind"], "prefix")

    def test_directory_lookup_never_queries_accounts(self):
        with self.assertNumQueries(0, using="default"):
            result = lookup({"street": "Previous"})
        self.assertEqual(len(result["results"]), 1)

    def test_foreign_po_box_and_invalid_inputs_are_not_blocking(self):
        for values in ({"country": "DE"}, {"street": ""}):
            self.assertEqual(lookup(values, mode="check")["status"], "not_checked")
        with self.assertRaises(ValueError):
            lookup({"street": "x" * 256}, mode="check")

    def test_foreign_and_nonstreet_addresses_skip_directory_and_warning(self):
        with patch("leg_platform.addresses.services.directory_status") as query:
            for values in (
                {"country": "DE", "street": "Foreign Street"},
                {"country": "CH", "street": ""},
            ):
                result = lookup(values, mode="check")
                self.assertEqual(result["status"], "not_checked")
                self.assertEqual(result["warning"], "")
                self.assertIsNone(result["match"])
            query.assert_not_called()

    def test_stale_boundary_uses_data_date_not_import_time(self):
        directory = Directory.objects.using("address_import").get(pk=1)
        for age, stale in ((92, False), (93, True)):
            directory.data_date = timezone.localdate() - timedelta(days=age)
            directory.save(using="address_import")
            result, _directory = directory_status()
            self.assertEqual(result["stale"], stale)
            # The operator page keeps its status text; the people who type never get it.
            self.assertEqual(bool(result["warning"]), stale)
            answer = lookup({"street": "Unknown", "postal_code": "9999", "locality": "Example Town"}, mode="check")
            self.assertEqual(answer["stale"], stale)
            self.assertEqual(answer["warning"] == "", stale)

    def test_missing_directory_gives_no_warning_in_the_answer(self):
        Directory.objects.using("address_import").all().delete()
        for mode in ("suggest", "check"):
            with self.subTest(mode=mode):
                result = lookup({"street": "Example", "postal_code": "9999", "locality": "Example Town"}, mode=mode)
                self.assertEqual(result["status"], "missing")
                self.assertEqual(result["warning"], "")
                self.assertEqual(result["results"], [])
                self.assertIsNone(result["match"])
        # The operator page still gets its text.
        self.assertTrue(directory_status()[0]["warning"])

    def test_database_error_gives_no_warning_in_the_answer_and_is_logged(self):
        with patch("leg_platform.addresses.services.Directory.objects.using", side_effect=DatabaseError("private detail")):
            with self.assertLogs("leg_platform.addresses.services", level="WARNING") as logs:
                result = lookup({"street": "Example"}, mode="check")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["match"])
        self.assertEqual(result["warning"], "")
        self.assertEqual(logs.output, [
            "WARNING:leg_platform.addresses.services:Address directory status is unavailable."
        ])

    def test_entry_query_failure_gives_no_warning_and_is_logged(self):
        with patch("leg_platform.addresses.services.Address.objects.using", side_effect=DatabaseError("private detail")):
            with self.assertLogs("leg_platform.addresses.services", level="WARNING") as logs:
                result = lookup({"street": "Example"})
                check = lookup({"street": "Example", "postal_code": "9999", "locality": "Town"}, mode="check")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["warning"], "")
        self.assertEqual(check["status"], "unavailable")
        self.assertEqual(check["warning"], "")
        self.assertIsNone(check["match"])
        self.assertEqual(logs.output, [
            "WARNING:leg_platform.addresses.services:Address directory lookup is unavailable."
        ] * 2)

    def add_addresses(self, *rows):
        """Synthetic directory rows: (street, house number, postal code, locality)."""
        Address.objects.using("address_import").bulk_create([
            Address(generation=self.generation, street=street, house_number=number,
                    postal_code=postal_code, locality=locality)
            for street, number, postal_code, locality in rows
        ])

    def test_every_answer_has_a_kind_and_a_prefix_match_is_not_approximate(self):
        self.assertEqual(lookup({"street": "Previous"})["kind"], "prefix")
        self.assertEqual(lookup({"street": "Previous"}, mode="check")["kind"], "prefix")
        self.assertEqual(lookup({"street": ""})["kind"], "prefix")
        self.assertEqual(lookup({"street": "Foreign", "country": "DE"})["kind"], "prefix")
        # A typing error with a complete postal code: the prefix search finds nothing, the
        # approximate one finds the street. A prefix match stays a prefix match.
        self.add_addresses(("Previous Road", "2", "9999", "Example Town"))
        result = lookup({"street": "Previous", "postal_code": "9999"})
        self.assertEqual(result["kind"], "prefix")
        self.assertEqual(len(result["results"]), 2)

    def test_approximate_matching_finds_a_typing_error_within_the_postal_code(self):
        self.add_addresses(
            ("Bahnhofstrasse", "5", "9999", "Example Town"),
            ("Bahnhofstrasse", "7", "9999", "Example Town"),
            ("Bahnhofstrasse", "5", "8888", "Other Town"),
        )
        for typed in ("Bahnhofstrase", "bahnohfstrasse", "BAHNHOFSTRASSEN"):
            with self.subTest(typed=typed):
                result = lookup({"street": typed, "postal_code": "9999"})
                self.assertEqual(result["kind"], "approximate")
                self.assertEqual(result["status"], "available")
                self.assertEqual(
                    [(row["street"], row["house_number"], row["postal_code"]) for row in result["results"]],
                    [("Bahnhofstrasse", "5", "9999"), ("Bahnhofstrasse", "7", "9999")],
                )
                self.assertEqual(set(result["results"][0]), {"street", "house_number", "postal_code", "locality"})

    def test_approximate_matching_needs_a_postal_code_of_exactly_four_digits(self):
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"))
        for postal_code in ("", "9", "99", "999"):
            with self.subTest(postal_code=postal_code):
                result = lookup({"street": "Bahnhofstrase", "postal_code": postal_code})
                self.assertEqual(result["results"], [])
                self.assertEqual(result["kind"], "prefix")
        # Another postal code does not find the street, and a street of another postal code is not
        # offered.
        self.assertEqual(lookup({"street": "Bahnhofstrase", "postal_code": "8888"})["results"], [])
        self.assertEqual(len(lookup({"street": "Bahnhofstrase", "postal_code": "9999"})["results"]), 1)

    def test_approximate_matching_makes_no_query_without_a_complete_postal_code(self):
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"))
        with patch("leg_platform.addresses.services.approximate_matches") as approximate:
            lookup({"street": "Bahnhofstrase", "postal_code": "999"})
            lookup({"street": "Bahnhofstrase"})
            lookup({"street": "Bahnhofstrase", "postal_code": "9999"}, mode="check")
            approximate.assert_not_called()
            lookup({"street": "Previous", "postal_code": "9999"})
            approximate.assert_not_called()

    def test_approximate_matching_keeps_the_house_number_filter_and_ignores_the_town(self):
        self.add_addresses(
            ("Bahnhofstrasse", "5", "9999", "Example Town"),
            ("Bahnhofstrasse", "7", "9999", "Example Town"),
        )
        values = {"street": "Bahnhofstrase", "postal_code": "9999"}
        self.assertEqual([row["house_number"] for row in lookup({**values, "house_number": "7"})["results"]], ["7"])
        self.assertEqual(lookup({**values, "house_number": "9"})["results"], [])
        # The postal code fixes the town; a wrong town text does not hide the street.
        result = lookup({**values, "locality": "Wrong Town"})
        self.assertEqual(len(result["results"]), 2)
        # An exact street with a wrong town is a prefix match within the postal code, not a guess.
        result = lookup({"street": "Bahnhofstrasse", "postal_code": "9999", "locality": "Wrong Town"})
        self.assertEqual(result["kind"], "prefix")
        self.assertEqual(len(result["results"]), 2)

    def test_approximate_matching_is_case_and_accent_insensitive(self):
        self.add_addresses(("Rue de l'Église", "1", "9999", "Example Town"))
        for typed in ("RUE DE L'EGLISE", "rue de l'eglis", "rue de l'églsie"):
            with self.subTest(typed=typed):
                result = lookup({"street": typed, "postal_code": "9999"})
                self.assertEqual([row["street"] for row in result["results"]], ["Rue de l'Église"])

    def test_approximate_matching_rejects_unrelated_streets_and_bad_lengths(self):
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"))
        for typed in ("Gartenweg", "Zz", "Bahnhofstrasse" + "x" * 60):
            with self.subTest(typed=typed):
                result = lookup({"street": typed, "postal_code": "9999"})
                self.assertEqual(result["results"], [])
                self.assertEqual(result["kind"], "prefix")

    def test_approximate_matching_sorts_by_similarity_and_is_capped(self):
        self.add_addresses(
            ("Schulstrasse", "1", "9999", "Example Town"),
            ("Schlossstrasse", "1", "9999", "Example Town"),
        )
        result = lookup({"street": "Schulstrase", "postal_code": "9999"})
        self.assertEqual([row["street"] for row in result["results"]], ["Schulstrasse", "Schlossstrasse"])
        self.add_addresses(*[("Schulstrasse", str(number), "9999", "Example Town") for number in range(2, 30)])
        self.assertEqual(len(lookup({"street": "Schulstrase", "postal_code": "9999"})["results"]), 10)

    def test_approximate_candidates_are_bounded_and_nothing_is_returned_above_the_bound(self):
        self.add_addresses(*[(f"Streetname{letter}", "1", "9999", "Example Town") for letter in "ABCD"])
        # A typing error, so that only the approximate path finds the four streets.
        values = {"street": "Strietname", "postal_code": "9999"}
        # The postal code has the four new streets and the street of setUp: five streets.
        with patch("leg_platform.addresses.services.APPROXIMATE_STREET_LIMIT", 5):
            self.assertEqual(len(lookup(values)["results"]), 4)
        with patch("leg_platform.addresses.services.APPROXIMATE_STREET_LIMIT", 4):
            with CaptureQueriesContext(connections["addresses"]) as queries:
                result = lookup(values)
            self.assertEqual(result["results"], [])
            self.assertEqual(result["kind"], "prefix")
        # The candidate query reads at most one more street than the limit; no query searches inside
        # the text of the street (LIKE '%x%'), and no query reads the whole directory.
        statements = [query["sql"] for query in queries]
        self.assertTrue(any("LIMIT 5" in statement for statement in statements), statements)
        self.assertFalse(any("LIKE '%" in statement for statement in statements), statements)

    def test_approximate_matching_reads_through_the_postal_code_not_the_whole_directory(self):
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"))
        with CaptureQueriesContext(connections["addresses"]) as queries:
            lookup({"street": "Bahnhofstrase", "postal_code": "9999"})
        statements = [query["sql"] for query in queries]
        # Directory, prefix search, candidate streets and one address query: bounded, and every
        # directory query is restricted to the generation and, for the candidates, the postal code.
        self.assertLessEqual(len(statements), 4 + 10)
        candidates = [statement for statement in statements if "DISTINCT" in statement and '"street" FROM' in statement]
        self.assertEqual(len(candidates), 1)
        self.assertIn('"postal_code" = \'9999\'', candidates[0])
        self.assertNotIn("LIKE '%", candidates[0])

    def test_the_candidate_query_ignores_the_house_number_and_each_street_query_filters_it(self):
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"), ("Bahnhofstrasse", "7", "9999", "Example Town"))
        with CaptureQueriesContext(connections["addresses"]) as queries:
            result = lookup({"street": "Bahnhofstrase", "postal_code": "9999", "house_number": "7"})
        self.assertEqual([row["house_number"] for row in result["results"]], ["7"])
        statements = [query["sql"] for query in queries]
        candidates = [s for s in statements if "DISTINCT" in s and '"street" FROM' in s]
        self.assertEqual(len(candidates), 1)
        self.assertNotIn("house_number", candidates[0])
        per_street = [s for s in statements if "= 'Bahnhofstrasse'" in s]
        self.assertEqual(len(per_street), 1)
        self.assertIn("LIKE '7%'", per_street[0])
        self.assertIn('"postal_code" = \'9999\'', per_street[0])

    def test_a_typed_street_that_folds_to_less_than_three_characters_finds_nothing(self):
        self.add_addresses(("Abc Street", "1", "9999", "Example Town"), ("\u00c1bc Street", "1", "9999", "Example Town"))
        for typed in ("\u0301" * 3, "a\u0301\u0301", "\u0301" * 64, "e\u0301\u0301" + "\u0301" * 3):
            with self.subTest(typed=typed):
                result = lookup({"street": typed, "postal_code": "9999"})
                self.assertEqual(result["results"], [])
                self.assertEqual(result["kind"], "prefix")
                self.assertEqual(result["status"], "available")
        # The folded text is not labelled a prefix match by the helper either.
        query = Address.objects.using("addresses").filter(generation=self.generation)
        for typed in ("\u0301" * 3, "a\u0301\u0301"):
            self.assertEqual(
                approximate_matches(query, {"street": typed, "postal_code": "9999", "house_number": ""}),
                ([], "approximate"),
            )

    def test_the_length_bounds_are_three_and_sixty_four_characters(self):
        # The accent makes the prefix search miss (the typed text has none), so that only the
        # approximate path can find the street.
        long_name = "a" * 63
        too_long = "b" * 64
        self.add_addresses(
            ("\u00c9ge Street", "1", "9999", "Example Town"),
            ("\u00c9" + long_name, "1", "9999", "Example Town"),
            ("\u00c9" + too_long, "1", "9999", "Example Town"),
        )
        found = lambda typed: [row["street"] for row in lookup({"street": typed, "postal_code": "9999"})["results"]]
        self.assertEqual(found("Ege"), ["\u00c9ge Street"])
        self.assertEqual(found("Eg"), [])
        self.assertEqual(found("E" + long_name), ["\u00c9" + long_name])
        self.assertEqual(found("E" + too_long), [])

    def test_a_street_that_differs_only_by_accents_is_a_prefix_result(self):
        self.add_addresses(("Rue de l'\u00c9glise", "1", "9999", "Example Town"))
        # The prefix search of the directory does not equate the accents (SQLite), the approximate
        # path does; it is labelled a prefix match, because the street starts with the typed text.
        for typed in ("rue de l'eglise", "RUE DE L'EGL"):
            with self.subTest(typed=typed):
                result = lookup({"street": typed, "postal_code": "9999"})
                self.assertEqual(result["kind"], "prefix")
                self.assertEqual([row["street"] for row in result["results"]], ["Rue de l'\u00c9glise"])

    def test_a_candidate_with_a_very_long_folded_text_is_not_compared(self):
        self.add_addresses(("\u00c9gerstrasse-xxxxxxx", "1", "9999", "Example Town"))  # 19 characters
        values = {"street": "Egerstra", "postal_code": "9999"}
        with patch("leg_platform.addresses.services.APPROXIMATE_MAX_LENGTH", 10):  # candidates up to 20
            self.assertEqual(len(lookup(values)["results"]), 1)
        with patch("leg_platform.addresses.services.APPROXIMATE_MAX_LENGTH", 9):  # candidates up to 18
            self.assertEqual(lookup(values)["results"], [])

    def test_the_number_of_full_comparisons_is_capped_and_nothing_is_returned_above_the_cap(self):
        self.add_addresses(*[(f"Schulstrass{letter}", "1", "9999", "Example Town") for letter in "aeo"])
        values = {"street": "Schulstrase", "postal_code": "9999"}
        with patch("leg_platform.addresses.services.APPROXIMATE_MAX_RATIO_CALLS", 3):
            self.assertEqual(len(lookup(values)["results"]), 3)
        with patch("leg_platform.addresses.services.APPROXIMATE_MAX_RATIO_CALLS", 2):
            result = lookup(values)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["kind"], "prefix")

    def test_the_time_budget_is_enforced_and_nothing_is_returned_when_it_is_used_up(self):
        self.add_addresses(("Schulstrasse", "1", "9999", "Example Town"))
        values = {"street": "Schulstrase", "postal_code": "9999"}
        self.assertEqual(len(lookup(values)["results"]), 1)
        # A negative budget is used up at the first candidate, with any clock resolution.
        with patch("leg_platform.addresses.services.APPROXIMATE_TIME_BUDGET", -1):
            result = lookup(values)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["status"], "available")

    def test_a_failing_candidate_or_street_query_after_a_successful_prefix_query_fails_closed(self):
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"))
        values = {"street": "Bahnhofstrase", "postal_code": "9999"}

        def failing_from(number):
            seen = []

            def wrapper(execute, sql, params, many, context):
                if "addresses_address" in sql:
                    seen.append(sql)
                    if len(seen) >= number:
                        raise DatabaseError("private detail")
                return execute(sql, params, many, context)

            return wrapper

        # Address queries in order: 1 prefix search (succeeds, no result), 2 candidate streets,
        # 3 the street's addresses.
        for number in (2, 3):
            with self.subTest(failing_address_query=number):
                with connections["addresses"].execute_wrapper(failing_from(number)):
                    with self.assertLogs("leg_platform.addresses.services", level="WARNING") as logs:
                        result = lookup(values)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["results"], [])
                self.assertEqual(result["kind"], "prefix")
                self.assertEqual(result["warning"], "")
                self.assertEqual(logs.output, [
                    "WARNING:leg_platform.addresses.services:Address directory lookup is unavailable."
                ])
                self.assertNotIn("private detail", str(result))

    def test_results_are_bounded_deduplicated_and_filtered(self):
        for number in range(20):
            Address.objects.using("address_import").create(
                generation=self.generation, street=f"Example {number:02}", house_number="1",
                postal_code="9999", locality="Example Town",
            )
        result = lookup({"street": "Example", "postal_code": "99", "locality": "Example"})
        self.assertEqual(len(result["results"]), 10)
        self.assertEqual(lookup({"street": "Example", "postal_code": "88"})["results"], [])
        self.assertEqual(lookup({"street": "E"})["results"], [])

    def test_queue_coalesces_and_finished_manual_attempt_can_retry_immediately(self):
        with patch("leg_platform.addresses.importer.import_directory") as download:
            self.assertTrue(request_import())
            self.assertFalse(request_import())
            download.assert_not_called()
        job = ImportRequest.objects.get(pk=1)
        job.state = ImportRequest.State.SUCCEEDED
        job.save()
        self.assertTrue(request_import())

    def test_failed_manual_attempt_can_retry_immediately(self):
        request_import()
        with patch("leg_platform.addresses.importer.import_directory", side_effect=importer.ImportFailure):
            with self.assertLogs("leg_platform.addresses.jobs", level="ERROR"):
                with self.assertRaises(importer.ImportFailure):
                    process_one()
        self.assertTrue(request_import())

    def test_first_automatic_attempt_persists_start_before_download(self):
        now = timezone.now()

        def download():
            job = ImportRequest.objects.get(pk=1)
            self.assertEqual(job.state, ImportRequest.State.RUNNING)
            self.assertTrue(job.is_automatic)
            self.assertEqual(job.last_automatic_attempt_at, now)

        with patch("leg_platform.addresses.jobs.timezone.now", return_value=now):
            with patch("leg_platform.addresses.importer.import_directory", side_effect=download):
                self.assertTrue(process_one(schedule_automatic=True))
        self.assertEqual(ImportRequest.objects.get(pk=1).last_automatic_attempt_at, now)

    def test_automatic_exact_90_day_boundary(self):
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=ImportRequest.State.SUCCEEDED,
            last_automatic_attempt_at=now - timedelta(days=90) + timedelta(microseconds=1),
        )
        with patch("leg_platform.addresses.jobs.timezone.now", return_value=now):
            with patch("leg_platform.addresses.importer.import_directory") as download:
                self.assertFalse(process_one(schedule_automatic=True))
                download.assert_not_called()
                ImportRequest.objects.filter(pk=1).update(
                    last_automatic_attempt_at=now - timedelta(days=90),
                )
                self.assertTrue(process_one(schedule_automatic=True))
                download.assert_called_once_with()

    def test_manual_attempts_do_not_shift_independent_automatic_rhythm(self):
        prior = timezone.now() - timedelta(days=100)
        ImportRequest.objects.create(pk=1, last_automatic_attempt_at=prior)
        for fails in (False, True):
            self.assertTrue(request_import())
            with patch(
                "leg_platform.addresses.importer.import_directory",
                side_effect=importer.ImportFailure("private detail") if fails else None,
            ):
                if fails:
                    with self.assertLogs("leg_platform.addresses.jobs", level="ERROR"):
                        with self.assertRaises(importer.ImportFailure):
                            process_one(schedule_automatic=True)
                else:
                    self.assertTrue(process_one(schedule_automatic=True))
            job = ImportRequest.objects.get(pk=1)
            self.assertFalse(job.is_automatic)
            self.assertEqual(job.last_automatic_attempt_at, prior)
        with patch("leg_platform.addresses.importer.import_directory"):
            self.assertTrue(process_one(schedule_automatic=True))

    def test_failed_automatic_attempt_consumes_interval_without_early_retry(self):
        now = timezone.now()
        with patch("leg_platform.addresses.jobs.timezone.now", return_value=now):
            with patch("leg_platform.addresses.importer.import_directory", side_effect=importer.ImportFailure("private detail")):
                with self.assertLogs("leg_platform.addresses.jobs", level="ERROR") as logs:
                    with self.assertRaises(importer.ImportFailure):
                        process_one(schedule_automatic=True)
        self.assertEqual(logs.output, ["ERROR:leg_platform.addresses.jobs:Address directory import attempt failed."])
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual(job.state, ImportRequest.State.FAILED)
        self.assertEqual(job.last_automatic_attempt_at, now)
        with patch("leg_platform.addresses.jobs.timezone.now", return_value=now + timedelta(days=89)):
            with patch("leg_platform.addresses.importer.import_directory") as download:
                self.assertFalse(process_one(schedule_automatic=True))
                download.assert_not_called()
        self.assertTrue(request_import())

    def test_running_job_not_recovered_or_requeued_automatically(self):
        ImportRequest.objects.create(pk=1, state=ImportRequest.State.RUNNING)
        self.assertFalse(request_import())
        with patch("leg_platform.addresses.importer.import_directory") as download:
            self.assertFalse(process_one(schedule_automatic=True))
            download.assert_not_called()

    def test_long_worker_continues_after_expected_failure(self):
        module = "leg_platform.addresses.management.commands.process_address_imports"
        with patch(module + ".process_one", side_effect=[importer.ImportFailure("private detail"), False]) as process:
            with patch(module + ".time.sleep", side_effect=[None, KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):
                    call_command("process_address_imports", stdout=io.StringIO())
        self.assertEqual(process.call_count, 2)
        self.assertEqual(process.call_args.kwargs, {"schedule_automatic": True})

    def test_once_failure_has_safe_nonzero_output_and_does_not_schedule(self):
        module = "leg_platform.addresses.management.commands.process_address_imports"
        with patch(module + ".process_one", side_effect=importer.ImportFailure("private detail")) as process:
            with self.assertRaises(CommandError) as error:
                call_command("process_address_imports", once=True, stdout=io.StringIO())
        self.assertNotIn("private detail", str(error.exception))
        process.assert_called_once_with(schedule_automatic=False)

    def test_worker_claims_and_processes_once(self):
        request_import()
        with patch("leg_platform.addresses.importer.import_directory", return_value=1) as download:
            self.assertTrue(process_one())
            self.assertFalse(process_one())
            download.assert_called_once_with()
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.SUCCEEDED)

    def test_worker_failure_keeps_old_directory_and_marks_failed(self):
        request_import()
        with patch("leg_platform.addresses.importer.import_directory", side_effect=importer.ImportFailure("safe")):
            with self.assertLogs("leg_platform.addresses.jobs", level="ERROR"):
                with self.assertRaises(importer.ImportFailure):
                    process_one()
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.FAILED)
        self.assertEqual(Directory.objects.using("addresses").get(pk=1).generation, self.generation)

    def test_operator_command_generic_error_contains_no_exception_detail(self):
        with patch("leg_platform.addresses.management.commands.import_address_directory.import_directory", side_effect=RuntimeError("private detail")):
            with self.assertRaises(CommandError) as error:
                call_command("import_address_directory", stdout=io.StringIO())
        self.assertNotIn("private detail", str(error.exception))

    def test_worker_command_once_does_not_download_without_a_job(self):
        with patch("leg_platform.addresses.importer.import_directory") as download:
            call_command("process_address_imports", once=True, stdout=io.StringIO())
            download.assert_not_called()

    def test_public_endpoint_post_and_no_store(self):
        with patch("leg_platform.addresses.views.allow_request", return_value=True):
            response = self.client.post(
                "/addresses/lookup/", urlencode({"street": "Previous"}),
                content_type="application/x-www-form-urlencoded",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["street"], "Previous Street")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.get("/addresses/lookup/").status_code, 405)

    def test_public_endpoint_returns_kind_and_only_directory_data_for_an_approximate_match(self):
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"))
        with patch("leg_platform.addresses.views.allow_request", return_value=True):
            response = self.client.post(
                "/addresses/lookup/", urlencode({"street": "Bahnhofstrase", "postal_code": "9999"}),
                content_type="application/x-www-form-urlencoded",
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["kind"], "approximate")
        self.assertEqual(data["results"], [{
            "street": "Bahnhofstrasse", "house_number": "5", "postal_code": "9999", "locality": "Example Town",
        }])
        self.assertEqual(set(data), {"status", "data_date", "stale", "warning", "results", "match", "kind"})
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])

    def test_endpoint_csrf_required_even_anonymous(self):
        client = Client(enforce_csrf_checks=True)
        with patch("leg_platform.addresses.views.allow_request", return_value=True):
            self.assertEqual(client.post("/addresses/lookup/", {"street": "Previous"}).status_code, 403)

    def test_endpoint_valid_csrf_urlencoded_matches_javascript(self):
        client = Client(enforce_csrf_checks=True)
        request = RequestFactory().get("/")
        token = get_token(request)
        client.cookies["csrftoken"] = request.META["CSRF_COOKIE"]
        with patch("leg_platform.addresses.views.allow_request", return_value=True):
            response = client.post(
                "/addresses/lookup/", urlencode({"street": "Previous"}),
                content_type="application/x-www-form-urlencoded", HTTP_X_CSRFTOKEN=token,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["street"], "Previous Street")

    def test_endpoint_valid_csrf_multipart_is_400_not_server_error(self):
        client = Client(enforce_csrf_checks=True)
        request = RequestFactory().get("/")
        token = get_token(request)
        client.cookies["csrftoken"] = request.META["CSRF_COOKIE"]
        with patch("leg_platform.addresses.views.allow_request") as limiter:
            response = client.post(
                "/addresses/lookup/", {"street": "Previous", "csrfmiddlewaretoken": token},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "invalid")
        self.assertIn("no-store", response["Cache-Control"])
        limiter.assert_not_called()

    def test_endpoint_rate_limit_refuses_before_lookup(self):
        with patch("leg_platform.addresses.views.allow_request", return_value=False), patch("leg_platform.addresses.views.lookup") as query:
            response = self.client.post(
                "/addresses/lookup/", urlencode({"street": "Previous"}),
                content_type="application/x-www-form-urlencoded",
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")
        query.assert_not_called()

    def test_endpoint_invalid_input_rejected(self):
        with patch("leg_platform.addresses.views.allow_request", return_value=True):
            for values in ({"street": "x" * 256}, {"street": "xx", "mode": "delete"}, {"street": ["first", "second"]}):
                with self.subTest(values=values):
                    self.assertEqual(self.client.post(
                        "/addresses/lookup/", urlencode(values, doseq=True),
                        content_type="application/x-www-form-urlencoded",
                    ).status_code, 400)
            # 413 from RequestBodyLimitMiddleware in the project settings; 400 from the view
            # alone in the isolated address settings (defence in depth, security review 2026-10-14).
            self.assertIn(self.client.post(
                "/addresses/lookup/", urlencode({"street": "Example", "padding": "x" * 5000}),
                content_type="application/x-www-form-urlencoded",
            ).status_code, {400, 413})

    def post_lookup(self, values, *, allowed=True, **extra):
        with patch("leg_platform.addresses.views.allow_request", return_value=allowed):
            return self.client.post(
                "/addresses/lookup/", urlencode(values), content_type="application/x-www-form-urlencoded", **extra
            )

    def test_endpoint_shows_no_warning_for_any_state_in_which_the_directory_cannot_answer(self):
        # D-066: the person cannot do anything about it, so no text is sent for these states (they
        # are logged on the server). The answers keep their status and HTTP code.
        values = {"street": "Previous Street", "house_number": "1", "postal_code": "9999",
                  "locality": "Example Town", "mode": "check"}
        stale = Directory.objects.using("address_import").get(pk=1)
        stale.data_date = timezone.localdate() - timedelta(days=200)
        stale.save(using="address_import")
        for name, answer in (
            ("stale, found", self.post_lookup(values)),
            ("stale, not found", self.post_lookup({**values, "street": "Unknown"})),
        ):
            with self.subTest(name):
                self.assertEqual(answer.status_code, 200)
                self.assertEqual(answer.json()["status"], "available")
                self.assertTrue(answer.json()["stale"])
                self.assertEqual(answer.json()["warning"], "")
        Directory.objects.using("address_import").all().delete()
        missing = self.post_lookup(values)
        self.assertEqual((missing.status_code, missing.json()["status"], missing.json()["warning"]), (200, "missing", ""))
        with patch("leg_platform.addresses.services.Directory.objects.using", side_effect=DatabaseError("private detail")):
            with self.assertLogs("leg_platform.addresses.services", level="WARNING"):
                unreachable = self.post_lookup(values)
        self.assertEqual(
            (unreachable.status_code, unreachable.json()["status"], unreachable.json()["warning"]), (200, "unavailable", "")
        )
        limited = self.post_lookup(values, allowed=False)
        self.assertEqual((limited.status_code, limited.json()["status"], limited.json()["warning"]), (429, "rate_limited", ""))
        invalid = self.post_lookup({**values, "mode": "delete"})
        self.assertEqual((invalid.status_code, invalid.json()["status"], invalid.json()["warning"]), (400, "invalid", ""))
        multipart = self.client.post("/addresses/lookup/", values)
        self.assertEqual((multipart.status_code, multipart.json()["warning"]), (400, ""))
        for answer in (missing, unreachable, limited, invalid, multipart):
            self.assertNotIn("swisstopo", answer.content.decode())
            self.assertNotIn("attribution", answer.json())

    def test_endpoint_has_a_warning_only_for_an_available_directory_that_does_not_know_the_address(self):
        values = {"street": "Previous Street", "house_number": "1", "postal_code": "9999",
                  "locality": "Example Town", "mode": "check"}

        found = self.post_lookup(values).json()
        unknown = self.post_lookup({**values, "street": "Unknown"}).json()
        suggest = self.post_lookup({**values, "street": "Unknown", "mode": "suggest"}).json()

        self.assertEqual((found["status"], found["match"], found["warning"]), ("available", True, ""))
        self.assertEqual((unknown["status"], unknown["match"]), ("available", False))
        self.assertIn("This address was not found.", unknown["warning"])
        self.assertEqual(suggest["warning"], "")

    def _staff_client(self, with_permission):
        if get_user_model()._meta.app_label == "accounts":
            from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user

            user = create_active_user("directory-staff@example.org")
            user.set_privileges(is_staff=True, is_superuser=False)
            self.client = VerifiedClient()
        else:
            user = get_user_model().objects.create_user(
                username="directory-staff", password="synthetic-password", is_staff=True,
            )
        if with_permission:
            user.user_permissions.add(Permission.objects.get(codename="request_directory_import"))
        self.client.force_login(user)
        return self.client

    def test_admin_dedicated_permission_required(self):
        client = self._staff_client(False)
        url = reverse("admin:addresses_importrequest_changelist")
        self.assertEqual(client.get(url).status_code, 403)
        with patch("leg_platform.addresses.admin.allow_request", return_value=True):
            self.assertEqual(client.post(url).status_code, 403)
        self.assertFalse(ImportRequest.objects.exists())

    def test_admin_status_and_enqueue_never_download(self):
        client = self._staff_client(True)
        url = reverse("admin:addresses_importrequest_changelist")
        with patch("leg_platform.addresses.importer.import_directory") as download:
            response = client.get(url)
            self.assertNotContains(response, "swisstopo")
            self.assertContains(response, "Data date")
            with patch("leg_platform.addresses.admin.allow_request", return_value=True):
                self.assertEqual(client.post(url).status_code, 302)
            download.assert_not_called()
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.QUEUED)

    def test_admin_enqueue_requires_csrf(self):
        self._staff_client(True)
        client = Client(enforce_csrf_checks=True)
        client.cookies = self.client.cookies
        self.assertEqual(client.post(reverse("admin:addresses_importrequest_changelist")).status_code, 403)

    def test_admin_enqueue_rate_limited(self):
        client = self._staff_client(True)
        with patch("leg_platform.addresses.admin.allow_request", return_value=False):
            self.assertEqual(client.post(reverse("admin:addresses_importrequest_changelist")).status_code, 302)
        self.assertFalse(ImportRequest.objects.exists())
