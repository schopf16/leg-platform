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
from django.test import Client, RequestFactory, SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from leg_platform.addresses import importer
from leg_platform.addresses.jobs import process_one, request_import
from leg_platform.addresses.limits import allow_request
from leg_platform.addresses.models import Address, Directory, ImportRequest
from leg_platform.addresses.router import AddressRouter
from leg_platform.addresses.services import check_address, clean_input, directory_status, lookup

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
        rate.consume_all = self.consume
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


@override_settings(LANGUAGE_CODE="en", ROOT_URLCONF="leg_platform.addresses.tests.urls")
class DirectoryDatabaseTests(TransactionTestCase):
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
        self.assertTrue(check_address(street="previous street", house_number="1", postal_code=9999, locality="example town")["match"])
        result = check_address(street="Unknown", postal_code=9999, locality="Example Town")
        self.assertFalse(result["match"])
        self.assertIn("continue", result["warning"])

    def test_directory_lookup_never_queries_accounts(self):
        with self.assertNumQueries(0, using="default"):
            result = lookup({"street": "Previous"})
        self.assertEqual(len(result["results"]), 1)

    def test_foreign_po_box_and_invalid_inputs_are_not_blocking(self):
        for values in ({"country": "DE"}, {"street": ""}, {"street": "x" * 256}):
            self.assertEqual(check_address(**values)["status"], "not_checked")

    def test_foreign_and_nonstreet_addresses_skip_directory_and_warning(self):
        with patch("leg_platform.addresses.services.directory_status") as query:
            for values in (
                {"country": "DE", "street": "Foreign Street"},
                {"country": "CH", "street": ""},
            ):
                result = check_address(**values)
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
            self.assertEqual(bool(result["warning"]), stale)

    def test_missing_directory_is_explicit_nonblocking_warning(self):
        Directory.objects.using("address_import").all().delete()
        result = lookup({"street": "Example"})
        self.assertEqual(result["status"], "missing")
        self.assertIn("continue", result["warning"])

    def test_database_error_is_explicit_nonblocking_warning(self):
        with patch("leg_platform.addresses.services.Directory.objects.using", side_effect=DatabaseError("private detail")):
            with self.assertLogs("leg_platform.addresses.services", level="WARNING") as logs:
                result = check_address(street="Example")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["match"])
        self.assertEqual(logs.output, [
            "WARNING:leg_platform.addresses.services:Address directory status is unavailable."
        ])

    def test_entry_query_failure_is_nonblocking(self):
        with patch("leg_platform.addresses.services.Address.objects.using", side_effect=DatabaseError("private detail")):
            with self.assertLogs("leg_platform.addresses.services", level="WARNING") as logs:
                self.assertEqual(lookup({"street": "Example"})["status"], "unavailable")
        self.assertEqual(logs.output, [
            "WARNING:leg_platform.addresses.services:Address directory lookup is unavailable."
        ])

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
            self.assertContains(response, "Federal Office of Topography swisstopo")
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
