import ast
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.core.management.base import CommandError
from django.middleware.csrf import get_token
from django.db import DatabaseError, OperationalError, connections
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.models import RateLimitBucket, RateLimitEvent, SecurityEvent
from leg_platform.accounts.rate_limits import RATE_LIMIT_PURPOSE
from leg_platform.addresses import importer, jobs, storage
from leg_platform.addresses.jobs import process_one, request_import
from leg_platform.addresses.keys import fold, key, prefix_range
from leg_platform.addresses.limits import allow_request
from leg_platform.addresses.models import Address, Directory, ImportRequest
from leg_platform.addresses.router import AddressRouter
from leg_platform.addresses.services import approximate_matches, clean_input, directory_status, lookup

HEADER = "STN_LABEL;ADR_NUMBER;ZIP_LABEL;BDG_NAME;COM_NAME\n"
EGID_HEADER = HEADER.rstrip("\n") + ";BDG_EGID;ADR_EDID\n"
SYNTHETIC_CSV = HEADER + "Example Street;12a;9999 Example Town;Discarded synthetic building;Discarded municipality\n"


def with_egid(text):
    """A test table written with ``HEADER`` gets the columns BDG_EGID and ADR_EDID last: every row its own EGID
    (1001, 1002, ...) and entrance 0.

    A text that does not start with ``HEADER`` (a broken header, an empty file) stays as it is.
    """
    if not text.startswith(HEADER):
        return text
    rows = text[len(HEADER):].split("\n")
    lines = [EGID_HEADER.rstrip("\n")]
    lines.extend(f"{row};{1000 + number};0" if row else row for number, row in enumerate(rows, start=1))
    return "\n".join(lines)


def archive_bytes(csv_text=SYNTHETIC_CSV, name=importer.CSV_NAME):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, with_egid(csv_text).encode("utf-8-sig"))
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


def sql_of(queries):
    """The executed statements."""
    return [query["sql"] for query in queries]


class ParserTests(SimpleTestCase):
    def test_only_approved_columns_are_returned_and_postal_locality_is_used(self):
        self.assertEqual(list(importer.address_rows(io.BytesIO(archive_bytes()))), [{
            "street": "Example Street", "house_number": "12a",
            "postal_code": "9999", "locality": "Example Town", "egid": 1001, "entrance_id": 0,
        }])

    def test_the_official_names_of_the_building_and_entrance_columns(self):
        # Verified against the published file and the product information of swisstopo (2026-10-19).
        self.assertEqual((importer.EGID_COLUMN, importer.ENTRANCE_COLUMN), ("BDG_EGID", "ADR_EDID"))

    def test_the_egid_and_the_entrance_are_read_from_their_columns_wherever_the_columns_are(self):
        text = (
            "ADR_EGAID;BDG_EGID;ADR_EDID;STN_LABEL;ADR_NUMBER;ZIP_LABEL\n"
            "1;123456789;3;Example Street;1;9999 Example Town\n"
            "2;7;32767;Example Street;2;9999 Example Town\n"
        )
        rows = list(importer.address_rows(io.BytesIO(archive_bytes(text))))
        self.assertEqual([(row["egid"], row["entrance_id"]) for row in rows], [(123456789, 3), (7, 32767)])

    def test_several_addresses_may_share_an_egid_and_one_address_may_have_several(self):
        text = (
            "BDG_EGID;ADR_EDID;STN_LABEL;ADR_NUMBER;ZIP_LABEL\n"
            "500;0;Example Street;1;9999 Example Town\n"
            "500;1;Example Street;1a;9999 Example Town\n"
            "501;0;Example Street;1;9999 Example Town\n"
        )
        rows = list(importer.address_rows(io.BytesIO(archive_bytes(text))))
        self.assertEqual(
            [(row["house_number"], row["egid"], row["entrance_id"]) for row in rows],
            [("1", 500, 0), ("1a", 500, 1), ("1", 501, 0)],
        )

    def test_a_file_without_the_egid_or_the_entrance_column_is_refused_with_a_fixed_sentence(self):
        for csv_text, message in (
            ("STN_LABEL;ADR_NUMBER;ZIP_LABEL;ADR_EDID;EGID\nExample;1;9999 Town;0;5\n", "Missing EGID column."),
            ("STN_LABEL;ADR_NUMBER;ZIP_LABEL\nExample;1;9999 Town\n", "Missing EGID column."),
            ("STN_LABEL;ADR_NUMBER;ZIP_LABEL;BDG_EGID\nExample;1;9999 Town;5\n", "Missing entrance column."),
            ("STN_LABEL;ADR_NUMBER;ZIP_LABEL;BDG_EGID;EDID\nExample;1;9999 Town;5;0\n", "Missing entrance column."),
        ):
            with self.subTest(csv_text=csv_text[:50]):
                with self.assertRaisesMessage(importer.ImportFailure, message) as raised:
                    list(importer.address_rows(io.BytesIO(archive_bytes(csv_text))))
                self.assertIn(str(raised.exception), jobs.SAFE_FAILURES)

    def test_an_empty_egid_or_entrance_is_stored_as_none_and_an_invalid_one_refuses_the_file(self):
        head = "BDG_EGID;ADR_EDID;STN_LABEL;ADR_NUMBER;ZIP_LABEL\n"
        rows = list(importer.address_rows(io.BytesIO(archive_bytes(head + ";;Example;1;9999 Town\n"))))
        self.assertEqual((rows[0]["egid"], rows[0]["entrance_id"]), (None, None))
        for value in ("0", "012", "1000000000", "-5", "12.0", "1e3", "abc", " 5", "5 ", "\u0663"):
            with self.subTest(egid=value), self.assertRaisesMessage(importer.ImportFailure, "Invalid address field."):
                list(importer.address_rows(io.BytesIO(archive_bytes(head + f"{value};0;Example;1;9999 Town\n"))))
        for value in ("00", "01", "32768", "100000", "-1", "1.0", "x", " 1", "\u0663"):
            with self.subTest(entrance=value), self.assertRaisesMessage(importer.ImportFailure, "Invalid address field."):
                list(importer.address_rows(io.BytesIO(archive_bytes(head + f"5;{value};Example;1;9999 Town\n"))))

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


class DirectoryFileSettingsTests(SimpleTestCase):
    """The directory is an own SQLite file next to the application database (D-075)."""

    def test_there_are_two_aliases_and_the_router_separates_the_models(self):
        self.assertEqual(set(settings.DATABASES), {"default", "addresses"})
        self.assertEqual(settings.DATABASE_ROUTERS, ["leg_platform.addresses.router.AddressRouter"])
        self.assertEqual(settings.DATABASES["addresses"]["ENGINE"], "leg_platform.addresses.sqlite")
        self.assertEqual(settings.DATABASES["default"]["ENGINE"], "django.db.backends.mysql")
        router = AddressRouter()
        for model in (Address, Directory):
            self.assertEqual(router.db_for_read(model), "addresses")
            self.assertEqual(router.db_for_write(model), "addresses")
        self.assertIsNone(router.db_for_read(ImportRequest))
        self.assertIsNone(router.db_for_write(ImportRequest))
        self.assertFalse(hasattr(importer, "WRITE_ALIAS"))
        self.assertFalse(hasattr(importer, "require_writer"))

    def test_migrations_are_allowed_only_in_the_file_for_the_directory_tables(self):
        router = AddressRouter()
        for model in ("address", "directory"):
            self.assertTrue(router.allow_migrate("addresses", "addresses", model))
            self.assertFalse(router.allow_migrate("default", "addresses", model))
        self.assertFalse(router.allow_migrate("addresses", "addresses", "importrequest"))
        self.assertIsNone(router.allow_migrate("default", "addresses", "importrequest"))
        # Nothing else is created in the file, whatever the app or the operation.
        for app_label, model in (("accounts", "user"), ("auth", "permission"), ("contenttypes", "contenttype"),
                                 ("addresses", None), ("accounts", None)):
            with self.subTest(app_label=app_label, model=model):
                self.assertFalse(router.allow_migrate("addresses", app_label, model))
                self.assertIsNone(router.allow_migrate("default", app_label, model))
        self.assertFalse(router.allow_relation(Address(), ImportRequest()))
        self.assertTrue(router.allow_relation(Address(), Directory()))

    def test_the_connection_options_set_wal_the_busy_timeout_and_the_transaction_mode(self):
        options = settings.DATABASES["addresses"]["OPTIONS"]
        self.assertEqual(options["timeout"], storage.BUSY_TIMEOUT_SECONDS)
        self.assertEqual(options["transaction_mode"], "IMMEDIATE")
        commands = [command.strip() for command in options["init_command"].split(";")]
        self.assertEqual(commands[:2], ["PRAGMA journal_mode = WAL", "PRAGMA synchronous = NORMAL"])
        self.assertEqual(commands[2], f"PRAGMA busy_timeout = {storage.BUSY_TIMEOUT_SECONDS * 1000}")

    def test_the_test_run_never_opens_the_file_of_the_instance_or_a_file_in_the_repository(self):
        test_file = Path(settings.DATABASES["addresses"]["NAME"]).resolve()
        # While the tests run, Django has put the test database path into NAME (TEST NAME).
        self.assertEqual(test_file, Path(settings.DATABASES["addresses"]["TEST"]["NAME"]).resolve())
        self.assertNotIn(Path(settings.BASE_DIR).resolve(), test_file.parents)
        self.assertIn(Path(tempfile.gettempdir()).resolve(), test_file.parents)

    def test_web_modules_never_import_the_importer_or_the_downloader(self):
        # A web request never downloads (D-075, D-090): the modules that a request reaches must not import
        # the importer or the HTTP client, neither at the top nor inside a function. The one door is the
        # import thread of ``runner.py`` (and ``jobs.py`` for the failure text), which import the
        # importer only inside a function that runs in that thread or in the worker.
        directory = Path(importer.__file__).parent
        for name in ("admin.py", "views.py", "urls.py", "services.py", "limits.py", "models.py", "apps.py",
                     "router.py", "storage.py", "keys.py", "status.py", "progress.py"):
            tree = ast.parse((directory / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [(node.module or "") + "." + alias.name for alias in node.names]
                    modules.append(node.module or "")
                for module in modules:
                    with self.subTest(file=name, module=module):
                        self.assertFalse(module.split(".")[0] == "requests", module)
                        self.assertNotIn("importer", module.split("."))
        for name in ("jobs.py", "runner.py"):
            tree = ast.parse((directory / name).read_text(encoding="utf-8"))
            top_level = [
                node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
            ]
            for node in top_level:
                self.assertNotIn("importer", ast.dump(node), name)
                self.assertNotIn("requests", ast.dump(node), name)
        callers = sorted(
            path.relative_to(directory.parent).as_posix()
            for path in directory.parent.rglob("*.py")
            if "tests" not in path.parts and path.name not in {"importer.py", "jobs.py"}
            and re.search(r"\bprocess_one\b|addresses\.importer|import_directory\b", path.read_text(encoding="utf-8"))
        )
        self.assertEqual(callers, [
            "addresses/management/commands/import_address_directory.py",
            "addresses/management/commands/process_address_imports.py",
        ])
        # The one outbound door of a request (KONZEPT 3a, D-066): only ``swisstopo.py`` talks to a third party
        # about an address, and only ``buildings.py`` (signed-in accounts, rate limits) calls it.
        sources = {path.name: path.read_text(encoding="utf-8") for path in directory.glob("*.py")}
        self.assertEqual(
            sorted(name for name, text in sources.items() if re.search(r"^\s*(import|from) requests\b", text, re.M)),
            ["importer.py", "swisstopo.py"],
        )
        self.assertEqual(
            sorted(name for name, text in sources.items() if re.search(r"^\s*(from \. import .*\bswisstopo\b|from \.swisstopo)", text, re.M)),
            ["buildings.py"],
        )
        # The running thread is the only web-side code that may write the file, and only its own connection.
        runner_text = (directory / "runner.py").read_text(encoding="utf-8")
        self.assertIn("mark_thread_writer", runner_text)
        self.assertNotIn("mark_writer()", runner_text)


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


@tag("mariadb")
@override_settings(LANGUAGE_CODE="en", ROOT_URLCONF="leg_platform.addresses.tests.urls")
class DirectoryDatabaseTests(TestCase):
    # The directory is in its own SQLite file (alias "addresses"), the queue in the application
    # database. The tests run against a temporary copy of the file (test_settings); a TestCase
    # keeps one open transaction per alias, so the rows are visible to the code under test (one
    # connection per alias, also for the lookup).
    databases = {"default", "addresses"}

    def setUp(self):
        # An import needs MIN_FREE_DISK_BYTES of free space (M-4); the tests do not depend on the
        # free space of the machine that runs them. The tests of the guard set their own value.
        self.disk = patch.object(
            importer.shutil, "disk_usage",
            return_value=types.SimpleNamespace(total=10**12, used=0, free=importer.MIN_FREE_DISK_BYTES * 10),
        )
        self.disk.start()
        self.addCleanup(self.disk.stop)
        # A synthetic previous directory, never an official data fixture.
        self.generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=self.generation, data_date=timezone.localdate(),
            imported_at=timezone.now(), checksum="0" * 64, row_count=1, has_egid=True,
        )
        Address.objects.create(
            generation=self.generation, street="Previous Street", house_number="1",
            postal_code="9999", locality="Example Town",
        )

    def test_the_tables_are_separated_between_the_application_database_and_the_file(self):
        default_tables = connections["default"].introspection.table_names()
        file_tables = connections["addresses"].introspection.table_names()
        self.assertIn("addresses_importrequest", default_tables)
        self.assertIn("accounts_user", default_tables)
        self.assertNotIn("addresses_address", default_tables)
        self.assertNotIn("addresses_directory", default_tables)
        self.assertIn("addresses_address", file_tables)
        self.assertIn("addresses_directory", file_tables)
        self.assertNotIn("addresses_importrequest", file_tables)
        self.assertNotIn("accounts_user", file_tables)
        self.assertNotIn("auth_permission", file_tables)

    def test_the_search_keys_are_stored_and_the_directory_spelling_is_kept(self):
        Address.objects.create(
            generation=self.generation, street="Rue de l'\u00c9glise", house_number="12A",
            postal_code="9999", locality="Gen\u00e8ve",
        )
        row = Address.objects.get(house_number="12A")
        self.assertEqual((row.street_key, row.house_number_key, row.locality_key), ("rue de l'eglise", "12a", "geneve"))
        self.assertEqual((row.street, row.locality), ("Rue de l'\u00c9glise", "Gen\u00e8ve"))
        Address.objects.bulk_create([Address(
            generation=self.generation, street="\u00c4rzte Weg", postal_code="9999", locality="Z\u00fcrich",
        )])
        self.assertEqual(Address.objects.get(street="\u00c4rzte Weg").street_key, "arzte weg")

    def test_the_pragmas_of_the_file_are_wal_busy_timeout_and_synchronous_normal(self):
        with connections["addresses"].cursor() as cursor:
            cursor.execute("PRAGMA journal_mode")
            journal_mode = cursor.fetchone()[0]
            cursor.execute("PRAGMA busy_timeout")
            busy_timeout = cursor.fetchone()[0]
            cursor.execute("PRAGMA synchronous")
            synchronous = cursor.fetchone()[0]
        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(busy_timeout, storage.BUSY_TIMEOUT_SECONDS * 1000)
        self.assertEqual(synchronous, 1)  # NORMAL

    def test_verified_import_publishes_and_keeps_the_previous_generation(self):
        session = session_for()
        with patch.object(importer.requests, "Session", return_value=session):
            self.assertEqual(importer.import_directory(), 1)
        self.assertFalse(session.trust_env)
        self.assertEqual({call.args[0] for call in session.get.call_args_list}, {importer.ITEM_URL, importer.ASSET_URL})
        directory = Directory.objects.get(pk=1)
        self.assertNotEqual(directory.generation, self.generation)
        # The previous generation is kept (retention of D-075): two rows, two generations.
        self.assertEqual(Address.objects.count(), 2)
        self.assertEqual(Address.objects.filter(generation=self.generation).count(), 1)
        self.assertEqual(lookup({"street": "Example"})["results"][0]["locality"], "Example Town")
        self.assertEqual(lookup({"street": "Previous"})["results"], [])
        # The lease is released after the import.
        self.assertIsNone(directory.import_token)
        self.assertIsNone(directory.import_lease_until)

    def test_only_the_current_and_the_previous_generation_are_kept(self):
        oldest, older = uuid.uuid4(), uuid.uuid4()
        Address.objects.bulk_create([
            Address(generation=generation, street=f"Old {number}", house_number="1",
                    postal_code="9999", locality="Example Town")
            for generation in (oldest, older) for number in range(3)
        ])
        with patch.object(importer, "PRUNE_BATCH_SIZE", 2):
            importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate())
        current = Directory.objects.get(pk=1).generation
        self.assertEqual(
            set(Address.objects.values_list("generation", flat=True)), {current, self.generation},
        )
        self.assertEqual(Address.objects.filter(generation=current).count(), 1)
        self.assertEqual(Address.objects.filter(generation=self.generation).count(), 1)

    def test_old_generations_are_deleted_in_batches_of_the_configured_size(self):
        old = uuid.uuid4()
        Address.objects.bulk_create([
            Address(generation=old, street=f"Old {number}", house_number="1",
                    postal_code="9999", locality="Example Town")
            for number in range(7)
        ])
        with CaptureQueriesContext(connections["addresses"]) as queries, patch.object(importer, "PRUNE_BATCH_SIZE", 3):
            deleted = importer.prune_old_generations({self.generation})
        self.assertEqual(deleted, 7)
        deletes = [query["sql"] for query in queries if query["sql"].startswith("DELETE")]
        self.assertEqual(len(deletes), 3)  # 3 + 3 + 1 rows
        self.assertTrue(all("addresses_address" in statement for statement in deletes))
        self.assertEqual(list(Address.objects.values_list("generation", flat=True)), [self.generation])

    def test_nothing_is_deleted_when_every_generation_is_kept(self):
        with CaptureQueriesContext(connections["addresses"]) as queries:
            self.assertEqual(importer.prune_old_generations({self.generation}), 0)
        self.assertFalse([query for query in queries if query["sql"].startswith("DELETE")])
        self.assertEqual(Address.objects.count(), 1)

    def test_a_failing_cleanup_does_not_fail_the_published_import(self):
        old = uuid.uuid4()
        Address.objects.create(
            generation=old, street="Old", house_number="1", postal_code="9999", locality="Example Town",
        )
        with patch.object(importer, "prune_old_generations", side_effect=DatabaseError("private detail")), \
                patch.object(importer, "_finish_file"):
            with self.assertLogs("leg_platform.addresses.importer", level="WARNING") as logs:
                count = importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate())
        self.assertEqual(count, 1)
        self.assertNotEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(logs.output, [
            "WARNING:leg_platform.addresses.importer:Old address directory generations could not be deleted."
        ])
        self.assertTrue(Address.objects.filter(generation=old).exists())

    def test_a_failed_import_deletes_nothing(self):
        old = uuid.uuid4()
        Address.objects.create(
            generation=old, street="Old", house_number="1", postal_code="9999", locality="Example Town",
        )
        with patch.object(importer, "prune_old_generations") as prune, self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes(HEADER + "Bad;1;invalid;x;x\n")), "1" * 64, timezone.localdate())
        prune.assert_not_called()
        self.assertEqual(Address.objects.count(), 2)

    def test_the_same_checksum_deletes_nothing(self):
        old = uuid.uuid4()
        Address.objects.create(
            generation=old, street="Old", house_number="1", postal_code="9999", locality="Example Town",
        )
        with patch.object(importer, "prune_old_generations") as prune:
            importer._publish(io.BytesIO(b"not parsed"), "0" * 64, timezone.localdate())
        prune.assert_not_called()

    def test_an_import_stores_the_egid_and_the_entrance_per_address_and_marks_the_generation(self):
        text = (
            "BDG_EGID;ADR_EDID;STN_LABEL;ADR_NUMBER;ZIP_LABEL\n"
            "500;0;Example Street;1;9999 Example Town\n"
            "501;1;Example Street;1;9999 Example Town\n"
            "500;2;Example Street;1a;9999 Example Town\n"
        )
        self.assertEqual(importer._publish(io.BytesIO(archive_bytes(text)), "1" * 64, timezone.localdate()), 3)
        directory = Directory.objects.get(pk=1)
        self.assertTrue(directory.has_egid)
        rows = Address.objects.filter(generation=directory.generation)
        # One address with two buildings, one building with two addresses: both are kept.
        self.assertEqual(sorted(rows.filter(house_number="1").values_list("egid", "entrance_id")), [(500, 0), (501, 1)])
        self.assertEqual(sorted(rows.filter(egid=500).values_list("house_number", "entrance_id")), [("1", 0), ("1a", 2)])
        # The suggestions do not show an address twice because of its buildings.
        self.assertEqual(len(lookup({"street": "Example Street", "house_number": "1"})["results"]), 2)

    def test_a_file_without_the_egid_or_entrance_column_keeps_the_old_data_and_leaves_no_rows(self):
        old_rows = list(Address.objects.values_list("pk", "street", "egid"))
        for text, message in (
            ("STN_LABEL;ADR_NUMBER;ZIP_LABEL\nNew Street;1;9999 Town\n", "Missing EGID column."),
            ("STN_LABEL;ADR_NUMBER;ZIP_LABEL;BDG_EGID\nNew Street;1;9999 Town;5\n", "Missing entrance column."),
        ):
            with self.subTest(message=message), self.assertRaisesMessage(importer.ImportFailure, message):
                importer._publish(io.BytesIO(archive_bytes(text)), "1" * 64, timezone.localdate())
            directory = Directory.objects.get(pk=1)
            self.assertEqual(
                (directory.generation, directory.checksum, directory.has_egid), (self.generation, "0" * 64, True),
            )
            self.assertEqual(list(Address.objects.values_list("pk", "street", "egid")), old_rows)
            self.assertIsNone(directory.import_token)

    def test_the_failure_of_a_missing_column_is_recorded_with_its_fixed_sentence(self):
        claimed = jobs.Claim(uuid.uuid4(), manual=False, took_over=False)
        ImportRequest.objects.create(pk=1, state=ImportRequest.State.RUNNING, claim_token=claimed.token)
        text = "STN_LABEL;ADR_NUMBER;ZIP_LABEL\nNew Street;1;9999 Town\n"
        session = session_for(archive_bytes(text))
        with patch.object(importer.requests, "Session", return_value=session), \
                self.assertLogs("leg_platform.addresses.jobs", "ERROR"):
            with self.assertRaises(importer.ImportFailure) as raised:
                importer.import_directory()
            jobs.record_failure(claimed, raised.exception)
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual((job.state, job.last_error), (ImportRequest.State.FAILED, "Missing EGID column."))
        self.assertEqual(Address.objects.count(), 1)

    def test_a_directory_without_identifiers_is_replaced_although_the_checksum_is_unchanged(self):
        # A generation imported before the EGID was read (migration 0004 sets has_egid False).
        Directory.objects.filter(pk=1).update(has_egid=False)
        directory = Directory.objects.get(pk=1)
        self.assertFalse(importer._is_published(directory, "0" * 64, repair=False))
        self.assertFalse(importer._is_published(directory, "0" * 64, repair=True))
        count = importer._publish(io.BytesIO(archive_bytes()), "0" * 64, timezone.localdate())
        self.assertEqual(count, 1)
        directory = Directory.objects.get(pk=1)
        self.assertTrue(directory.has_egid)
        self.assertNotEqual(directory.generation, self.generation)
        self.assertEqual(Address.objects.filter(generation=directory.generation).get().egid, 1001)
        self.assertEqual(Address.objects.filter(generation=directory.generation).get().entrance_id, 0)
        # Now the same checksum is "published" again.
        self.assertTrue(importer._is_published(directory, "0" * 64, repair=False))

    def test_checksum_failure_keeps_published_directory(self):
        session = session_for(metadata=item_bytes(archive_bytes(), **{"checksum:multihash": "1220" + "f" * 64}))
        with patch.object(importer.requests, "Session", return_value=session), self.assertRaises(importer.ImportFailure):
            importer.import_directory()
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(Address.objects.count(), 1)

    def test_partial_batch_failure_rolls_back_rows_and_pointer(self):
        invalid = HEADER + "New Street;1;9999 Town;x;x\nBad;1;not-a-postal-label;x;x\n"
        with patch.object(importer, "BATCH_SIZE", 1), self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes(invalid)), "1" * 64, timezone.localdate())
        self.assertEqual(Address.objects.count(), 1)
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)

    def test_the_rows_are_inserted_in_chunks_that_stay_invisible_until_the_pointer_is_switched(self):
        rows = "".join(f"Chunk Street {number};1;9999 Town;x;x\n" for number in range(5))
        seen = []
        original = importer._insert_chunk

        def watching(chunk, token):
            original(chunk, token)
            # After each committed chunk the lookup still serves the previous generation only.
            seen.append((len(chunk), Address.objects.count(), lookup({"street": "Chunk"})["results"]))

        with patch.object(importer, "CHUNK_ROWS", 2), patch.object(importer, "_insert_chunk", watching):
            self.assertEqual(importer._publish(io.BytesIO(archive_bytes(HEADER + rows)), "1" * 64, timezone.localdate()), 5)
        self.assertEqual([chunk for chunk, _count, _results in seen], [2, 2, 1])
        self.assertEqual([count for _chunk, count, _results in seen], [3, 5, 6])
        self.assertTrue(all(results == [] for _chunk, _count, results in seen))
        self.assertEqual(len(lookup({"street": "Chunk"})["results"]), 5)

    def test_a_failure_after_committed_chunks_leaves_the_previous_generation_serving(self):
        text = HEADER + "Chunk Street 1;1;9999 Town;x;x\nChunk Street 2;1;9999 Town;x;x\nBad;1;invalid;x;x\n"
        with patch.object(importer, "CHUNK_ROWS", 1), self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes(text)), "1" * 64, timezone.localdate())
        # The two committed chunks are deleted again, the previous generation serves.
        self.assertEqual(Address.objects.count(), 1)
        directory = Directory.objects.get(pk=1)
        self.assertEqual(directory.generation, self.generation)
        self.assertIsNone(directory.import_token)
        self.assertEqual(lookup({"street": "Previous"})["results"][0]["street"], "Previous Street")

    def test_after_a_hard_kill_the_orphan_rows_are_invisible_and_the_next_import_deletes_them(self):
        # A killed importer leaves the rows of its unpublished generation and a lease that expires.
        orphan = uuid.uuid4()
        Address.objects.create(generation=orphan, street="Chunk Street 1", postal_code="9999", locality="Town")
        Directory.objects.filter(pk=1).update(
            import_token=uuid.uuid4(), import_lease_until=timezone.now() + timedelta(hours=1),
        )
        self.assertEqual(lookup({"street": "Chunk"})["results"], [])
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        # While the lease lasts, no second import starts; after it expired, the next one does.
        with self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes()), "2" * 64, timezone.localdate())
        self.assertTrue(Address.objects.filter(generation=orphan).exists())
        Directory.objects.filter(pk=1).update(import_lease_until=timezone.now() - timedelta(seconds=1))
        importer._publish(io.BytesIO(archive_bytes()), "2" * 64, timezone.localdate())
        self.assertFalse(Address.objects.filter(generation=orphan).exists())
        self.assertEqual(Address.objects.count(), 2)
    def test_a_second_import_cannot_run_while_the_lease_is_held_and_takes_over_an_expired_lease(self):
        token = importer._acquire_lease()
        with self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate())
        # The failing second import did not release the lease of the first one.
        self.assertEqual(Directory.objects.get(pk=1).import_token, token)
        Directory.objects.filter(pk=1).update(import_lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate()), 1)
        self.assertIsNone(Directory.objects.get(pk=1).import_token)

    def test_a_lost_lease_stops_the_import_and_deletes_its_rows(self):
        text = HEADER + "Chunk Street 1;1;9999 Town;x;x\nChunk Street 2;1;9999 Town;x;x\n"
        original = importer._insert_chunk

        def stealing(chunk, token):
            original(chunk, token)
            Directory.objects.filter(pk=1).update(import_token=uuid.uuid4())

        with patch.object(importer, "CHUNK_ROWS", 1), patch.object(importer, "_insert_chunk", stealing):
            with self.assertRaises(importer.ImportFailure):
                importer._publish(io.BytesIO(archive_bytes(text)), "1" * 64, timezone.localdate())
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(Address.objects.count(), 1)

    def test_every_chunk_renews_the_lease(self):
        seen = []
        original = importer._insert_chunk

        def watching(chunk, token):
            original(chunk, token)
            seen.append(Directory.objects.get(pk=1).import_lease_until)

        text = HEADER + "Chunk Street 1;1;9999 Town;x;x\nChunk Street 2;1;9999 Town;x;x\n"
        with patch.object(importer, "CHUNK_ROWS", 1), patch.object(importer, "_insert_chunk", watching):
            importer._publish(io.BytesIO(archive_bytes(text)), "1" * 64, timezone.localdate())
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(until > timezone.now() + timedelta(hours=5) for until in seen))
        self.assertLessEqual(seen[0], seen[1])
    def test_the_file_is_analysed_after_a_published_import_and_not_inside_an_open_transaction(self):
        with patch.object(importer, "_finish_file") as finish:
            importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate())
        finish.assert_called_once_with()
        # A TestCase keeps a transaction open: the maintenance does nothing there (the real run is
        # tested in ImportFileMaintenanceTests).
        with CaptureQueriesContext(connections["addresses"]) as queries:
            importer._finish_file()
        self.assertEqual(sql_of(queries), [])

    def test_older_catalogue_cannot_replace_published_directory(self):
        with self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate() - timedelta(days=1))
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertIsNone(Directory.objects.get(pk=1).import_token)

    def test_same_checksum_is_idempotent(self):
        self.assertEqual(importer._publish(io.BytesIO(b"not parsed"), "0" * 64, timezone.localdate()), 1)
        self.assertEqual(Address.objects.count(), 1)

    def test_a_repairing_publish_ends_early_when_the_published_directory_is_intact(self):
        # M-3: the checksum is unchanged and the integrity checks pass, so nothing is loaded again
        # (the archive is not even parsed).
        with patch.object(importer, "_load_generation") as load:
            self.assertEqual(
                importer._publish(io.BytesIO(b"not parsed"), "0" * 64, timezone.localdate(), repair=True), 1,
            )
        load.assert_not_called()
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(Address.objects.count(), 1)

    def test_a_repairing_publish_reloads_a_damaged_directory_although_the_checksum_is_unchanged(self):
        # The published generation lost its rows: Directory.row_count no longer equals the count.
        Address.objects.all().delete()
        self.assertEqual(
            importer._publish(io.BytesIO(archive_bytes()), "0" * 64, timezone.localdate(), repair=True), 1,
        )
        directory = Directory.objects.get(pk=1)
        self.assertNotEqual(directory.generation, self.generation)
        self.assertEqual(directory.checksum, "0" * 64)
        self.assertEqual(Address.objects.filter(generation=directory.generation).count(), 1)

    def test_a_repairing_publish_loads_a_changed_checksum_although_the_directory_is_intact(self):
        self.assertEqual(
            importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate(), repair=True), 1,
        )
        directory = Directory.objects.get(pk=1)
        self.assertEqual(directory.checksum, "1" * 64)
        self.assertNotEqual(directory.generation, self.generation)
        # Retention of D-075 is unchanged: the previous generation is kept.
        self.assertEqual(Address.objects.filter(generation=self.generation).count(), 1)

    def test_the_integrity_check_needs_every_one_of_its_conditions(self):
        directory = Directory.objects.get(pk=1)
        self.assertTrue(importer._is_intact(directory))
        # The rows no longer match the recorded count.
        Address.objects.create(
            generation=self.generation, street="Extra", house_number="1", postal_code="9999", locality="Example Town",
        )
        self.assertFalse(importer._is_intact(directory))
        Address.objects.filter(street="Extra").delete()
        self.assertTrue(importer._is_intact(directory))
        # An empty directory is never intact.
        Directory.objects.filter(pk=1).update(row_count=0)
        Address.objects.all().delete()
        self.assertFalse(importer._is_intact(Directory.objects.get(pk=1)))

    def test_the_integrity_check_fails_when_quick_check_reports_a_problem_or_the_file_cannot_be_read(self):
        directory = Directory.objects.get(pk=1)
        fake = MagicMock()
        cursor = fake.__getitem__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [("row 1 missing from index addr_generation_street",)]
        with patch.object(importer, "connections", fake):
            self.assertFalse(importer._is_intact(directory))
        cursor.fetchall.return_value = [("ok",)]
        with patch.object(importer, "connections", fake):
            self.assertTrue(importer._is_intact(directory))
        fake.__getitem__.return_value.cursor.side_effect = DatabaseError("private detail")
        with patch.object(importer, "connections", fake):
            self.assertFalse(importer._is_intact(directory))

    def test_the_integrity_check_runs_quick_check_on_the_file(self):
        with CaptureQueriesContext(connections["addresses"]) as queries:
            importer._is_intact(Directory.objects.get(pk=1))
        self.assertIn("PRAGMA quick_check", sql_of(queries))

    def prepare_damaged_directory(self):
        """The published directory has the checksum of the official file but lost its rows."""
        archive = archive_bytes()
        Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(archive).hexdigest())
        Address.objects.all().delete()
        self.assertEqual(lookup({"street": "Example"})["results"], [])
        return archive

    def test_a_manual_request_repairs_a_damaged_directory_with_an_unchanged_checksum(self):
        archive = self.prepare_damaged_directory()
        self.assertTrue(request_import())
        session = session_for(archive)
        with patch.object(importer.requests, "Session", return_value=session):
            self.assertTrue(process_one())
        self.assertEqual({call.args[0] for call in session.get.call_args_list}, {importer.ITEM_URL, importer.ASSET_URL})
        directory = Directory.objects.get(pk=1)
        self.assertNotEqual(directory.generation, self.generation)
        self.assertEqual(directory.row_count, 1)
        self.assertEqual(lookup({"street": "Example"})["results"][0]["locality"], "Example Town")
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.SUCCEEDED)

    def test_a_manual_request_for_an_intact_directory_ends_before_the_download(self):
        # M-3: the official file is the published one and the directory is intact, so a repeated
        # request costs one small catalogue request and nothing else.
        archive = archive_bytes()
        Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(archive).hexdigest())
        for _repeat in range(2):
            self.assertTrue(request_import())
            session = session_for(archive)
            with patch.object(importer.requests, "Session", return_value=session):
                self.assertTrue(process_one())
            self.assertEqual([call.args[0] for call in session.get.call_args_list], [importer.ITEM_URL])
            self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
            self.assertEqual(Address.objects.count(), 1)
            self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.SUCCEEDED)

    def test_a_manual_request_loads_a_new_official_file_although_the_directory_is_intact(self):
        self.assertTrue(request_import())
        session = session_for(archive_bytes())
        with patch.object(importer.requests, "Session", return_value=session):
            self.assertTrue(process_one())
        self.assertEqual({call.args[0] for call in session.get.call_args_list}, {importer.ITEM_URL, importer.ASSET_URL})
        self.assertNotEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(lookup({"street": "Example"})["results"][0]["locality"], "Example Town")

    def test_a_manual_request_repairs_a_directory_that_fails_the_file_check(self):
        archive = archive_bytes()
        Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(archive).hexdigest())
        self.assertTrue(request_import())
        with patch.object(importer, "_is_intact", return_value=False):
            with patch.object(importer.requests, "Session", return_value=session_for(archive)):
                self.assertTrue(process_one())
        self.assertNotEqual(Directory.objects.get(pk=1).generation, self.generation)

    def test_the_automatic_attempt_for_the_same_file_does_not_download_either(self):
        archive = archive_bytes()
        Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(archive).hexdigest())
        session = session_for(archive)
        with patch.object(importer.requests, "Session", return_value=session):
            self.assertEqual(importer.import_directory(), 1)
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [importer.ITEM_URL])

    def test_an_unreadable_directory_is_imported_instead_of_ending_early(self):
        archive = archive_bytes()
        Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(archive).hexdigest())
        with patch.object(importer.Directory.objects, "filter", side_effect=DatabaseError("private detail")):
            self.assertIsNone(importer._published_row_count(hashlib.sha256(archive).hexdigest(), repair=True))

    def test_the_free_disk_is_checked_before_the_download_and_a_nearly_full_disk_refuses_the_import(self):
        session = session_for()
        too_little = types.SimpleNamespace(total=10**12, used=0, free=importer.MIN_FREE_DISK_BYTES - 1)
        with patch.object(importer.shutil, "disk_usage", return_value=too_little):
            with patch.object(importer.requests, "Session", return_value=session):
                with self.assertLogs("leg_platform.addresses.importer", level="WARNING") as logs:
                    with self.assertRaises(importer.ImportFailure):
                        importer.import_directory()
        # Only the catalogue entry was requested; nothing was downloaded or written.
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [importer.ITEM_URL])
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(Address.objects.count(), 1)
        self.assertEqual(logs.output, [
            "WARNING:leg_platform.addresses.importer:Address directory import refused: "
            "less than 4 GB free on the disk of the directory file."
        ])
        folder = str(Path(str(connections["addresses"].settings_dict["NAME"])).parent)
        self.assertNotIn(folder, logs.output[0])

    def test_exactly_the_minimum_of_free_disk_is_enough_and_only_the_directory_disk_is_measured(self):
        self.assertEqual(importer.MIN_FREE_DISK_BYTES, 4 * 1024**3)
        enough = types.SimpleNamespace(total=10**12, used=0, free=importer.MIN_FREE_DISK_BYTES)
        with patch.object(importer.shutil, "disk_usage", return_value=enough) as usage:
            with patch.object(importer.requests, "Session", return_value=session_for()):
                self.assertEqual(importer.import_directory(), 1)
        # One disk: the folder of the directory file. The system temporary folder is not measured (it can be
        # a small memory file system) because nothing is written there.
        self.assertEqual(
            [call.args[0] for call in usage.call_args_list],
            [Path(str(connections["addresses"].settings_dict["NAME"])).parent],
        )

    def test_the_downloaded_archive_is_written_next_to_the_directory_file_not_to_the_system_temporary_folder(self):
        directory_folder = Path(str(connections["addresses"].settings_dict["NAME"])).parent
        created = []
        real = tempfile.TemporaryFile

        def recording(*args, **kwargs):
            created.append(kwargs.get("dir"))
            return real(*args, **kwargs)

        with patch.object(importer.tempfile, "TemporaryFile", side_effect=recording):
            with patch.object(importer.tempfile, "gettempdir", side_effect=AssertionError("system temporary folder")):
                with patch.object(importer.requests, "Session", return_value=session_for()):
                    self.assertEqual(importer.import_directory(), 1)
        self.assertEqual(created, [directory_folder])
        # The archive is an unnamed or self-deleting file: nothing is left in the folder.
        self.assertEqual(
            sorted(entry.name for entry in directory_folder.iterdir() if "sqlite" not in entry.name),
            [],
        )

    def test_the_disk_check_uses_the_nearest_existing_folder_and_fails_closed(self):
        file_in_missing_folders = Path(tempfile.gettempdir()) / "leg-missing-a" / "leg-missing-b" / "x.sqlite3"
        with patch.dict(connections["addresses"].settings_dict, {"NAME": str(file_in_missing_folders)}):
            with patch.object(importer.shutil, "disk_usage") as usage:
                usage.return_value = types.SimpleNamespace(total=1, used=0, free=importer.MIN_FREE_DISK_BYTES)
                importer._require_free_disk()
        self.assertEqual(usage.call_args_list[0].args[0], Path(tempfile.gettempdir()))
        with patch.object(importer.shutil, "disk_usage", side_effect=OSError("/private/path")):
            with self.assertRaises(importer.ImportFailure) as raised:
                importer._require_free_disk()
        self.assertNotIn("/private/path", str(raised.exception))

    def test_an_unavailable_directory_file_refuses_the_import_before_the_download(self):
        session = session_for()
        with patch.object(
            connections["addresses"], "ensure_connection", side_effect=OperationalError("/private/path"),
        ), patch.object(importer.requests, "Session", return_value=session):
            with self.assertRaises(importer.ImportFailure) as raised:
                importer.import_directory()
        self.assertNotIn("/private/path", str(raised.exception))
        self.assertEqual(session.get.call_args_list, [])

    def test_a_refused_manual_import_marks_the_job_failed_and_keeps_the_directory(self):
        too_little = types.SimpleNamespace(total=10**12, used=0, free=0)
        self.assertTrue(request_import())
        with patch.object(importer.shutil, "disk_usage", return_value=too_little):
            with patch.object(importer.requests, "Session", return_value=session_for()):
                with self.assertLogs("leg_platform.addresses", level="WARNING"):
                    with self.assertRaises(importer.ImportFailure):
                        process_one()
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.FAILED)
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(Address.objects.count(), 1)
        self.assertIsNone(Directory.objects.get(pk=1).import_token)

    def test_the_prune_batches_stay_below_the_variable_limit_of_old_sqlite_versions(self):
        self.assertLessEqual(importer.PRUNE_BATCH_SIZE, 900)
        old = uuid.uuid4()
        Address.objects.bulk_create([
            Address(generation=old, street=f"Old {number}", house_number="1",
                    postal_code="9999", locality="Example Town")
            for number in range(2000)
        ])
        with CaptureQueriesContext(connections["addresses"]) as queries:
            self.assertEqual(importer.prune_old_generations({self.generation}), 2000)
        deletes = [query["sql"] for query in queries if query["sql"].startswith("DELETE")]
        self.assertEqual(len(deletes), 3)  # 900 + 900 + 200
        self.assertEqual(Address.objects.count(), 1)

    def test_a_failed_prune_is_logged_without_detail(self):
        old = uuid.uuid4()
        Address.objects.create(
            generation=old, street="Old", house_number="1", postal_code="9999", locality="Example Town",
        )
        error = OperationalError("too many SQL variables")
        with patch.object(importer, "prune_old_generations", side_effect=error), patch.object(importer, "_finish_file"):
            with self.assertLogs("leg_platform.addresses.importer", level="WARNING") as logs:
                importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate())
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("too many SQL variables", logs.output[0])
        self.assertTrue(Address.objects.filter(generation=old).exists())

    def test_the_automatic_attempt_keeps_the_shortcut_for_an_unchanged_checksum(self):
        archive = self.prepare_damaged_directory()
        ImportRequest.objects.create(
            pk=1, state=ImportRequest.State.SUCCEEDED,
            last_automatic_attempt_at=timezone.now() - timedelta(days=100),
        )
        with patch.object(importer.requests, "Session", return_value=session_for(archive)):
            self.assertTrue(process_one(schedule_automatic=True))
        job = ImportRequest.objects.get(pk=1)
        self.assertTrue(job.is_automatic)
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(Address.objects.count(), 0)

    def test_a_manual_request_still_verifies_the_checksum_and_keeps_the_directory_on_failure(self):
        archive = self.prepare_damaged_directory()
        bad = item_bytes(archive, **{"checksum:multihash": "1220" + "f" * 64})
        self.assertTrue(request_import())
        with patch.object(importer.requests, "Session", return_value=session_for(archive, metadata=bad)):
            with self.assertLogs("leg_platform.addresses.jobs", level="ERROR"):
                with self.assertRaises(importer.ImportFailure):
                    process_one()
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.FAILED)

    def test_a_repairing_publish_still_refuses_an_older_catalogue(self):
        with self.assertRaises(importer.ImportFailure):
            importer._publish(
                io.BytesIO(archive_bytes()), "0" * 64, timezone.localdate() - timedelta(days=1), repair=True,
            )
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(Address.objects.count(), 1)

    def test_a_repairing_publish_with_a_broken_archive_keeps_the_published_directory(self):
        # A damaged directory (no rows) is the case in which a repair loads the archive at all.
        Address.objects.all().delete()
        invalid = HEADER + "Bad;1;not-a-postal-label;x;x\n"
        with self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes(invalid)), "0" * 64, timezone.localdate(), repair=True)
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
        self.assertEqual(Address.objects.count(), 0)

    def older_metadata(self, archive):
        """The catalogue entry of the official file, dated before the published directory."""
        item = json.loads(item_bytes(archive))
        item["properties"]["datetime"] = (timezone.localdate() - timedelta(days=1)).isoformat() + "T00:00:00Z"
        return json.dumps(item).encode()

    def test_an_older_catalogue_is_refused_before_the_download(self):
        # N-2: the refusal costs one catalogue request, not the archive and its hash.
        archive = archive_bytes()
        for how in ("a new checksum", "a damaged directory with the same checksum"):
            with self.subTest(how=how):
                if how != "a new checksum":
                    Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(archive).hexdigest())
                    Address.objects.all().delete()
                session = session_for(archive, metadata=self.older_metadata(archive))
                with patch.object(importer.requests, "Session", return_value=session):
                    with patch.object(importer, "_require_free_disk") as disk:
                        with self.assertRaisesMessage(importer.ImportFailure, "predates the published directory"):
                            importer.import_directory(repair=True)
                self.assertEqual([call.args[0] for call in session.get.call_args_list], [importer.ITEM_URL])
                disk.assert_not_called()
                self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)
                self.assertIsNone(Directory.objects.get(pk=1).import_token)

    def test_an_older_catalogue_is_refused_before_the_download_in_a_manual_request_and_an_automatic_attempt(self):
        archive = archive_bytes()
        self.assertTrue(request_import())
        session = session_for(archive, metadata=self.older_metadata(archive))
        with patch.object(importer.requests, "Session", return_value=session):
            with self.assertLogs("leg_platform.addresses.jobs", level="ERROR"):
                with self.assertRaises(importer.ImportFailure):
                    process_one()
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [importer.ITEM_URL])
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.FAILED)
        session = session_for(archive, metadata=self.older_metadata(archive))
        with patch.object(importer.requests, "Session", return_value=session):
            with self.assertRaises(importer.ImportFailure):
                importer.import_directory()
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [importer.ITEM_URL])

    def test_a_catalogue_of_the_same_date_or_a_newer_one_is_not_refused_and_an_unreadable_file_is_replaced(self):
        importer._require_catalogue_not_older(timezone.localdate())
        importer._require_catalogue_not_older(timezone.localdate() + timedelta(days=1))
        with patch.object(importer.Directory.objects, "filter", side_effect=DatabaseError("private detail")):
            importer._require_catalogue_not_older(timezone.localdate() - timedelta(days=30))

    def repair(self, archive):
        with patch.object(importer.requests, "Session", return_value=session_for(archive)) as session:
            try:
                return importer.import_directory(repair=True), session.return_value
            except importer.ImportFailure:
                return None, session.return_value

    def last_failed_repair(self):
        return ImportRequest.objects.get(pk=1).last_failed_repair_at

    def test_a_repair_that_ends_with_a_failing_quick_check_blocks_the_next_one_for_six_hours(self):
        # N-2: a defect that survives a new generation must not cost a download on every click.
        archive = self.prepare_damaged_directory()
        ImportRequest.objects.create(pk=1)
        now = timezone.now()
        with patch.object(importer, "_is_intact", return_value=False):
            with patch("django.utils.timezone.now", return_value=now):
                count, session = self.repair(archive)
                self.assertEqual(count, 1)
                self.assertEqual({call.args[0] for call in session.get.call_args_list}, {importer.ITEM_URL, importer.ASSET_URL})
                self.assertEqual(self.last_failed_repair(), now)
                generation = Directory.objects.get(pk=1).generation
            for waited in (timedelta(0), timedelta(hours=6) - timedelta(seconds=1)):
                with patch("django.utils.timezone.now", return_value=now + waited):
                    with self.assertLogs("leg_platform.addresses.importer", level="WARNING") as logs:
                        count, session = self.repair(archive)
                self.assertIsNone(count)
                # One catalogue request only: no download, no new generation.
                self.assertEqual([call.args[0] for call in session.get.call_args_list], [importer.ITEM_URL])
                self.assertEqual(Directory.objects.get(pk=1).generation, generation)
                self.assertEqual(logs.output, [
                    "WARNING:leg_platform.addresses.importer:Address directory repair refused: "
                    "the last repair did not succeed less than 6 hours ago."
                ])
                # The refusal does not extend the time.
                self.assertEqual(self.last_failed_repair(), now)
            with patch("django.utils.timezone.now", return_value=now + timedelta(hours=6)):
                count, session = self.repair(archive)
                self.assertEqual(count, 1)
                self.assertEqual({call.args[0] for call in session.get.call_args_list}, {importer.ITEM_URL, importer.ASSET_URL})
                self.assertEqual(self.last_failed_repair(), now + timedelta(hours=6))

    def test_a_failed_repair_blocks_the_next_one_and_an_intact_result_clears_the_block(self):
        # The official file (by its checksum) is the published one, but the directory lost its rows
        # and the archive cannot be imported: the repair fails.
        broken = archive_bytes(HEADER + "Bad;1;not-a-postal-label;x;x\n")
        Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(broken).hexdigest())
        Address.objects.all().delete()
        ImportRequest.objects.create(pk=1)
        count, _session = self.repair(broken)
        self.assertIsNone(count)
        self.assertIsNotNone(self.last_failed_repair())
        count, session = self.repair(broken)
        self.assertIsNone(count)
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [importer.ITEM_URL])
        # Six hours later the repair is allowed, and a repair that ends intact clears the block.
        valid = archive_bytes()
        Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(valid).hexdigest())
        ImportRequest.objects.filter(pk=1).update(last_failed_repair_at=timezone.now() - timedelta(hours=6))
        count, session = self.repair(valid)
        self.assertEqual(count, 1)
        self.assertEqual({call.args[0] for call in session.get.call_args_list}, {importer.ITEM_URL, importer.ASSET_URL})
        self.assertIsNone(self.last_failed_repair())
        self.assertTrue(importer._is_intact(Directory.objects.get(pk=1)))

    def test_the_block_applies_to_a_repair_only(self):
        # An intact directory ends at the shortcut, new data is loaded and an automatic attempt
        # is not a repair: none of them is refused or changes the time.
        blocked = timezone.now() - timedelta(hours=1)
        ImportRequest.objects.create(pk=1, last_failed_repair_at=blocked)
        archive = archive_bytes()
        Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(archive).hexdigest())
        count, session = self.repair(archive)
        self.assertEqual(count, 1)
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [importer.ITEM_URL])
        # New official data although the directory is damaged and a repair was refused a moment ago.
        Directory.objects.filter(pk=1).update(checksum="1" * 64)
        Address.objects.all().delete()
        count, session = self.repair(archive)
        self.assertEqual(count, 1)
        self.assertEqual({call.args[0] for call in session.get.call_args_list}, {importer.ITEM_URL, importer.ASSET_URL})
        # The automatic attempt trusts the checksum and is never a repair.
        Directory.objects.filter(pk=1).update(checksum=hashlib.sha256(archive).hexdigest())
        with patch.object(importer.requests, "Session", return_value=session_for(archive)):
            self.assertEqual(importer.import_directory(), 1)
        self.assertEqual(self.last_failed_repair(), blocked)

    def test_a_failed_disk_check_of_a_repair_does_not_start_the_block(self):
        archive = self.prepare_damaged_directory()
        ImportRequest.objects.create(pk=1)
        too_little = types.SimpleNamespace(total=10**12, used=0, free=0)
        with patch.object(importer.shutil, "disk_usage", return_value=too_little):
            with self.assertLogs("leg_platform.addresses.importer", level="WARNING"):
                count, _session = self.repair(archive)
        self.assertIsNone(count)
        self.assertIsNone(self.last_failed_repair())

    def test_an_empty_published_directory_is_never_taken_as_published(self):
        # N-5: "empty" means the same here as in jobs.directory_is_empty: row_count 0.
        Address.objects.all().delete()
        Directory.objects.filter(pk=1).update(row_count=0)
        directory = Directory.objects.get(pk=1)
        self.assertTrue(directory.imported_at)
        for repair in (False, True):
            self.assertFalse(importer._is_published(directory, directory.checksum, repair=repair))
        self.assertIsNone(importer._published_row_count(directory.checksum, repair=False))
        self.assertEqual(
            importer._publish(io.BytesIO(archive_bytes()), directory.checksum, timezone.localdate()), 1,
        )
        self.assertEqual(Directory.objects.get(pk=1).row_count, 1)
        self.assertTrue(importer._is_published(Directory.objects.get(pk=1), directory.checksum, repair=False))

    def test_exact_advisory_match_and_unknown_address(self):
        self.assertTrue(lookup({"street": "previous street", "house_number": "1", "postal_code": "9999", "locality": "example town"}, mode="check")["match"])
        result = lookup({"street": "Unknown", "postal_code": "9999", "locality": "Example Town"}, mode="check")
        self.assertFalse(result["match"])
        self.assertEqual(result["status"], "available")
        self.assertIn("save it as entered", result["warning"])
        self.assertEqual(result["kind"], "prefix")

    def test_directory_lookup_never_queries_accounts(self):
        # The directory is a separate file: a lookup makes no query at all in the application
        # database, and the queries in the file read only the two directory tables.
        with self.assertNumQueries(0, using="default"):
            with CaptureQueriesContext(connections["addresses"]) as queries:
                result = lookup({"street": "Previous"})
        self.assertEqual(len(result["results"]), 1)
        statements = sql_of(queries)
        self.assertTrue(statements)
        for statement in statements:
            tables = set(re.findall(r'(?:FROM|JOIN)\s+"?(\w+)"?', statement))
            self.assertTrue(tables)
            self.assertLessEqual(tables, {"addresses_address", "addresses_directory"}, statement)
        self.assertFalse([s for s in statements if s.split(None, 1)[0] != "SELECT"], statements)

    def test_no_directory_query_uses_like(self):
        # SQLite's LIKE ignores case only for ASCII and does not use a range index (D-075): the
        # prefix searches are ranges over the key columns.
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"))
        with CaptureQueriesContext(connections["addresses"]) as queries:
            lookup({"street": "Bahnhof", "postal_code": "99", "house_number": "5", "locality": "Exa"})
            lookup({"street": "Bahnhofstrase", "postal_code": "9999", "house_number": "5"})
            lookup({"street": "Bahnhofstrasse", "house_number": "5", "postal_code": "9999",
                    "locality": "Example Town"}, mode="check")
        statements = sql_of(queries)
        self.assertGreater(len(statements), 4)
        self.assertFalse([s for s in statements if "LIKE" in s.upper() or "GLOB" in s.upper()], statements)

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
        directory = Directory.objects.get(pk=1)
        for age, stale in ((92, False), (93, True)):
            directory.data_date = timezone.localdate() - timedelta(days=age)
            directory.save()
            result, _directory = directory_status()
            self.assertEqual(result["stale"], stale)
            # The operator page keeps its status text; the people who type never get it.
            self.assertEqual(bool(result["warning"]), stale)
            answer = lookup({"street": "Unknown", "postal_code": "9999", "locality": "Example Town"}, mode="check")
            self.assertEqual(answer["stale"], stale)
            self.assertEqual(answer["warning"] == "", stale)

    def test_missing_directory_gives_no_warning_in_the_answer(self):
        Directory.objects.all().delete()
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
        with patch("leg_platform.addresses.services.Directory.objects.filter", side_effect=DatabaseError("private detail")):
            with self.assertLogs("leg_platform.addresses.services", level="WARNING") as logs:
                result = lookup({"street": "Example"}, mode="check")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["match"])
        self.assertEqual(result["warning"], "")
        self.assertEqual(logs.output, [
            "WARNING:leg_platform.addresses.services:Address directory status is unavailable."
        ])

    def test_entry_query_failure_gives_no_warning_and_is_logged(self):
        with patch("leg_platform.addresses.services.Address.objects.filter", side_effect=DatabaseError("private detail")):
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
        Address.objects.bulk_create([
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
        statements = sql_of(queries)
        self.assertTrue(any("LIMIT 5" in statement for statement in statements), statements)
        self.assertFalse(any("LIKE" in statement for statement in statements), statements)

    def test_approximate_matching_reads_through_the_postal_code_not_the_whole_directory(self):
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"))
        with CaptureQueriesContext(connections["addresses"]) as queries:
            lookup({"street": "Bahnhofstrase", "postal_code": "9999"})
        statements = sql_of(queries)
        # Directory, prefix search, candidate streets and one address query: bounded, and every
        # directory query is restricted to the generation and, for the candidates, the postal code.
        self.assertLessEqual(len(statements), 4 + 10)
        candidates = [statement for statement in statements if "DISTINCT" in statement and '"street" FROM' in statement]
        self.assertEqual(len(candidates), 1)
        self.assertIn('"postal_code" = \'9999\'', candidates[0])
        self.assertNotIn("LIKE", candidates[0])
        self.assertNotIn("ORDER BY", candidates[0])

    def test_the_candidate_query_ignores_the_house_number_and_each_street_query_filters_it(self):
        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"), ("Bahnhofstrasse", "7", "9999", "Example Town"))
        with CaptureQueriesContext(connections["addresses"]) as queries:
            result = lookup({"street": "Bahnhofstrase", "postal_code": "9999", "house_number": "7"})
        self.assertEqual([row["house_number"] for row in result["results"]], ["7"])
        statements = sql_of(queries)
        candidates = [s for s in statements if "DISTINCT" in s and '"street" FROM' in s]
        self.assertEqual(len(candidates), 1)
        self.assertNotIn("house_number", candidates[0])
        per_street = [s for s in statements if "= 'Bahnhofstrasse'" in s]
        self.assertEqual(len(per_street), 1)
        self.assertIn("\"house_number_key\" >= '7'", per_street[0])
        self.assertIn('"postal_code" = \'9999\'', per_street[0])

    def test_a_typed_street_that_folds_to_less_than_three_characters_finds_nothing(self):
        self.add_addresses(("Abc Street", "1", "9999", "Example Town"), ("\u00c1bc Street", "1", "9999", "Example Town"))
        # Only combining marks: the key is empty, so no prefix search and no approximate search.
        for typed in ("\u0301" * 3, "\u0301" * 64):
            with self.subTest(typed=typed):
                result = lookup({"street": typed, "postal_code": "9999"})
                self.assertEqual(result["results"], [])
                self.assertEqual(result["kind"], "prefix")
                self.assertEqual(result["status"], "available")
        # The folded text is not labelled a prefix match by the helper either.
        query = Address.objects.filter(generation=self.generation)
        for typed in ("\u0301" * 3, "a\u0301\u0301", "e\u0301\u0301" + "\u0301" * 3):
            self.assertEqual(
                approximate_matches(query, {"street": typed, "postal_code": "9999", "house_number": ""}),
                ([], "approximate"),
            )

    def test_the_length_bounds_are_three_and_sixty_four_characters(self):
        # The helper is called directly: the prefix search of the lookup ignores accents and finds
        # an accented street by itself, so only the helper shows the bounds of the approximate path.
        long_name = "a" * 63
        too_long = "b" * 64
        self.add_addresses(
            ("\u00c9ge Street", "1", "9999", "Example Town"),
            ("\u00c9" + long_name, "1", "9999", "Example Town"),
            ("\u00c9" + too_long, "1", "9999", "Example Town"),
        )
        query = Address.objects.filter(generation=self.generation)
        found = lambda typed: [
            row["street"]
            for row in approximate_matches(query, {"street": typed, "postal_code": "9999", "house_number": ""})[0]
        ]
        self.assertEqual(found("Ege"), ["\u00c9ge Street"])
        self.assertEqual(found("Eg"), [])
        self.assertEqual(found("E" + long_name), ["\u00c9" + long_name])
        self.assertEqual(found("E" + too_long), [])

    def test_a_street_that_differs_only_by_accents_is_a_prefix_result(self):
        self.add_addresses(("Rue de l'\u00c9glise", "1", "9999", "Example Town"))
        # Found by the prefix search, which ignores case and accents (the search key); it is labelled
        # a prefix match, because the street starts with the typed text.
        for typed in ("rue de l'eglise", "RUE DE L'EGL"):
            with self.subTest(typed=typed):
                result = lookup({"street": typed, "postal_code": "9999"})
                self.assertEqual(result["kind"], "prefix")
                self.assertEqual([row["street"] for row in result["results"]], ["Rue de l'\u00c9glise"])

    def test_a_candidate_with_a_very_long_folded_text_is_not_compared(self):
        self.add_addresses(("\u00c9gerstrasse-xxxxxxx", "1", "9999", "Example Town"))  # 19 characters
        query = Address.objects.filter(generation=self.generation)
        values = {"street": "Egerstra", "postal_code": "9999", "house_number": ""}
        with patch("leg_platform.addresses.services.APPROXIMATE_MAX_LENGTH", 10):  # candidates up to 20
            self.assertEqual(len(approximate_matches(query, values)[0]), 1)
        with patch("leg_platform.addresses.services.APPROXIMATE_MAX_LENGTH", 9):  # candidates up to 18
            self.assertEqual(approximate_matches(query, values)[0], [])

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
            Address.objects.create(
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

    def make_directory_empty(self, how):
        if how == "no row":
            Directory.objects.all().delete()
        elif how == "no rows":
            Directory.objects.filter(pk=1).update(row_count=0)
        Address.objects.all().delete()

    def test_an_empty_directory_makes_the_automatic_attempt_due_after_24_hours_instead_of_90_days(self):
        # After a restore the file is gone (it is not part of the backup, D-075) but the queue row
        # in the application database is restored with its last attempt (security review, M-5).
        for how in ("no row", "no rows"):
            with self.subTest(how=how):
                self.make_directory_empty(how)
                now = timezone.now()
                ImportRequest.objects.all().delete()
                ImportRequest.objects.create(
                    pk=1, state=ImportRequest.State.SUCCEEDED,
                    last_automatic_attempt_at=now - timedelta(hours=24) + timedelta(microseconds=1),
                )
                with patch("leg_platform.addresses.jobs.timezone.now", return_value=now):
                    with patch("leg_platform.addresses.importer.import_directory") as download:
                        self.assertFalse(process_one(schedule_automatic=True))
                        download.assert_not_called()
                        ImportRequest.objects.filter(pk=1).update(last_automatic_attempt_at=now - timedelta(hours=24))
                        self.assertTrue(process_one(schedule_automatic=True))
                        download.assert_called_once_with()
                self.assertEqual(ImportRequest.objects.get(pk=1).last_automatic_attempt_at, now)

    def test_a_directory_with_addresses_keeps_the_interval_of_90_days(self):
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=ImportRequest.State.SUCCEEDED, last_automatic_attempt_at=now - timedelta(days=2),
        )
        with patch("leg_platform.addresses.jobs.timezone.now", return_value=now):
            with patch("leg_platform.addresses.importer.import_directory") as download:
                self.assertFalse(process_one(schedule_automatic=True))
        download.assert_not_called()

    def test_a_failed_automatic_attempt_of_an_empty_directory_is_retried_after_24_hours_not_earlier(self):
        self.make_directory_empty("no row")
        now = timezone.now()
        with patch("leg_platform.addresses.jobs.timezone.now", return_value=now):
            with patch("leg_platform.addresses.importer.import_directory", side_effect=importer.ImportFailure("x")):
                with self.assertLogs("leg_platform.addresses.jobs", level="ERROR"):
                    with self.assertRaises(importer.ImportFailure):
                        process_one(schedule_automatic=True)
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.FAILED)
        with patch("leg_platform.addresses.importer.import_directory") as download:
            with patch("leg_platform.addresses.jobs.timezone.now", return_value=now + timedelta(hours=23)):
                self.assertFalse(process_one(schedule_automatic=True))
            with patch("leg_platform.addresses.jobs.timezone.now", return_value=now + timedelta(hours=24)):
                self.assertTrue(process_one(schedule_automatic=True))
        download.assert_called_once_with()

    def test_a_directory_file_that_cannot_be_read_counts_as_empty(self):
        with patch("leg_platform.addresses.jobs.Directory.objects.filter", side_effect=DatabaseError("private detail")):
            self.assertTrue(jobs.directory_is_empty())
        self.assertFalse(jobs.directory_is_empty())
        self.make_directory_empty("no rows")
        self.assertTrue(jobs.directory_is_empty())

    def test_a_first_start_without_any_attempt_imports_at_once(self):
        self.make_directory_empty("no row")
        with patch("leg_platform.addresses.importer.import_directory") as download:
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
        self.assertEqual(
            logs.output,
            ["ERROR:leg_platform.addresses.jobs:Address directory import failed (automatic): "
             "The import failed unexpectedly. (ImportFailure)"],
        )
        job = ImportRequest.objects.get(pk=1)
        # Only the fixed sentences of the importer reach the row, the log and the page (D-090).
        self.assertEqual(job.last_error, jobs.UNEXPECTED_FAILURE)
        self.assertNotIn("private detail", job.last_error)
        self.assertEqual(job.state, ImportRequest.State.FAILED)
        self.assertEqual(job.last_automatic_attempt_at, now)
        with patch("leg_platform.addresses.jobs.timezone.now", return_value=now + timedelta(days=89)):
            with patch("leg_platform.addresses.importer.import_directory") as download:
                self.assertFalse(process_one(schedule_automatic=True))
                download.assert_not_called()
        self.assertTrue(request_import())

    def test_a_running_job_with_a_valid_lease_is_not_recovered_or_requeued(self):
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=ImportRequest.State.RUNNING, started_at=now - jobs.RUNNING_JOB_LEASE + timedelta(seconds=1),
        )
        with patch("leg_platform.addresses.jobs.timezone.now", return_value=now):
            self.assertFalse(request_import())
            with patch("leg_platform.addresses.importer.import_directory") as download:
                self.assertFalse(process_one(schedule_automatic=True))
                download.assert_not_called()
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.RUNNING)

    def test_the_lease_of_a_running_job_is_the_lease_of_the_importer(self):
        # jobs.py must not import the importer (the web process reaches jobs), so the value is
        # repeated there and kept equal here.
        self.assertEqual(jobs.RUNNING_JOB_LEASE, importer.IMPORT_LEASE)
        self.assertEqual(jobs.RUNNING_JOB_LEASE, timedelta(hours=6))

    def test_a_hard_killed_running_job_is_taken_over_when_its_lease_has_expired(self):
        # N-3: after an out-of-memory kill or a power loss the state stays "running".
        now = timezone.now()
        for started_at in (now - jobs.RUNNING_JOB_LEASE, now - timedelta(days=3), None):
            with self.subTest(started_at=started_at):
                ImportRequest.objects.all().delete()
                ImportRequest.objects.create(
                    pk=1, state=ImportRequest.State.RUNNING, started_at=started_at,
                    last_automatic_attempt_at=now - timedelta(days=100),
                )
                with patch("leg_platform.addresses.jobs.timezone.now", return_value=now):
                    with patch("leg_platform.addresses.importer.import_directory") as download:
                        with self.assertLogs("leg_platform.addresses.jobs", level="WARNING") as logs:
                            self.assertTrue(process_one(schedule_automatic=True))
                download.assert_called_once_with()
                self.assertEqual(
                    logs.output,
                    ["WARNING:leg_platform.addresses.jobs:An address directory import job exceeded its lease and was taken over."],
                )
                job = ImportRequest.objects.get(pk=1)
                self.assertEqual(job.state, ImportRequest.State.SUCCEEDED)
                self.assertTrue(job.is_automatic)

    def test_an_expired_running_job_does_not_start_an_import_before_the_automatic_interval(self):
        now = timezone.now()
        ImportRequest.objects.create(
            pk=1, state=ImportRequest.State.RUNNING, started_at=now - timedelta(days=1),
            last_automatic_attempt_at=now - timedelta(days=1),
        )
        with patch("leg_platform.addresses.jobs.timezone.now", return_value=now):
            with patch("leg_platform.addresses.importer.import_directory") as download:
                with self.assertLogs("leg_platform.addresses.jobs", level="WARNING"):
                    self.assertFalse(process_one(schedule_automatic=True))
        download.assert_not_called()
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.FAILED)

    def test_the_button_works_again_after_the_lease_of_a_running_job_expired(self):
        now = timezone.now()
        ImportRequest.objects.create(pk=1, state=ImportRequest.State.RUNNING, started_at=now - timedelta(hours=7))
        self.assertTrue(request_import())
        job = ImportRequest.objects.get(pk=1)
        self.assertEqual(job.state, ImportRequest.State.QUEUED)
        self.assertIsNone(job.started_at)
        with patch("leg_platform.addresses.importer.import_directory") as download:
            self.assertTrue(process_one())
        download.assert_called_once_with(repair=True)

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
            download.assert_called_once_with(repair=True)
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.SUCCEEDED)

    def test_worker_failure_keeps_old_directory_and_marks_failed(self):
        request_import()
        with patch("leg_platform.addresses.importer.import_directory", side_effect=importer.ImportFailure("safe")):
            with self.assertLogs("leg_platform.addresses.jobs", level="ERROR"):
                with self.assertRaises(importer.ImportFailure):
                    process_one()
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.FAILED)
        self.assertEqual(Directory.objects.get(pk=1).generation, self.generation)

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
        # The approximate search is for a signed-in account only (L-1): an anonymous client gets the prefix search.
        from leg_platform.accounts.tests.helpers import create_active_user

        self.add_addresses(("Bahnhofstrasse", "5", "9999", "Example Town"))
        self.client.force_login(create_active_user("approximate-endpoint@example.org"))
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
            # 413 from RequestBodyLimitMiddleware in the project settings; 400 from the view alone
            # (defence in depth, security review 2026-10-14).
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
        stale = Directory.objects.get(pk=1)
        stale.data_date = timezone.localdate() - timedelta(days=200)
        stale.save()
        for name, answer in (
            ("stale, found", self.post_lookup(values)),
            ("stale, not found", self.post_lookup({**values, "street": "Unknown"})),
        ):
            with self.subTest(name):
                self.assertEqual(answer.status_code, 200)
                self.assertEqual(answer.json()["status"], "available")
                self.assertTrue(answer.json()["stale"])
                self.assertEqual(answer.json()["warning"], "")
        Directory.objects.all().delete()
        missing = self.post_lookup(values)
        self.assertEqual((missing.status_code, missing.json()["status"], missing.json()["warning"]), (200, "missing", ""))
        with patch("leg_platform.addresses.services.Directory.objects.filter", side_effect=DatabaseError("private detail")):
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

    def test_the_admin_shell_renders_under_this_url_configuration(self):
        # The shell of the administration links into the portal ("home", "management:overview", D-113): the pages render
        # under the URL configuration of these tests, so a new ``{% url %}`` of the shell surfaces here at once.
        client = self._staff_client(True)
        for url in (reverse("admin:index"), reverse("admin:addresses_importrequest_changelist")):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'class="admin-workspace"')

    def test_admin_status_and_enqueue_never_download(self):
        client = self._staff_client(True)
        url = reverse("admin:addresses_importrequest_changelist")
        with patch("leg_platform.addresses.importer.import_directory") as download, \
                patch("leg_platform.addresses.runner.threading.Thread") as thread:
            response = client.get(url)
            self.assertNotContains(response, "swisstopo")
            self.assertContains(response, "Data date")
            with patch("leg_platform.addresses.admin.allow_request", return_value=True):
                self.assertEqual(client.post(url).status_code, 302)
            # The request itself never downloads: it claims the job and starts the import thread (D-090).
            download.assert_not_called()
            thread.assert_called_once()
            self.assertIs(thread.call_args.kwargs["daemon"], True)
            thread.return_value.start.assert_called_once_with()
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.RUNNING)

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
        # A refused request is not an import request: nothing is logged.
        self.assertFalse(SecurityEvent.objects.filter(event="address_import_requested").exists())

    def test_every_manual_import_request_is_a_security_event_with_the_account_number_only(self):
        client = self._staff_client(True)
        user = get_user_model().objects.get(email="directory-staff@example.org")
        url = reverse("admin:addresses_importrequest_changelist")
        with patch("leg_platform.addresses.admin.allow_request", return_value=True), \
                patch("leg_platform.addresses.runner.threading.Thread"):
            with self.assertLogs("leg_platform.security", level="WARNING") as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    self.assertEqual(client.post(url).status_code, 302)
                    # The second request finds the import running, but it is a request too.
                    self.assertEqual(client.post(url).status_code, 302)
        entries = list(SecurityEvent.objects.filter(event="address_import_requested"))
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual((entry.actor_id, entry.account_id, entry.long_retention), (user.pk, None, False))
        self.assertEqual(
            logs.output,
            [f"WARNING:leg_platform.security:An address directory import was requested (actor {user.pk})."] * 2,
        )
        self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.RUNNING)

    def test_the_event_has_a_swiss_german_label_in_the_security_log(self):
        from django.utils import translation

        with translation.override("de-ch"):
            self.assertEqual(
                str(SecurityEvent.Event.ADDRESS_IMPORT_REQUESTED.label),
                "Import des Adressverzeichnisses angefordert",
            )

    def test_a_request_is_not_queued_when_its_security_event_cannot_be_written(self):
        client = self._staff_client(True)
        with patch("leg_platform.addresses.admin.allow_request", return_value=True):
            with patch("leg_platform.addresses.admin.security_log.record", side_effect=DatabaseError("x")):
                with self.assertRaises(DatabaseError):
                    client.post(reverse("admin:addresses_importrequest_changelist"))
        self.assertFalse(ImportRequest.objects.exists())


@tag("mariadb")
class PrefixSearchTests(TestCase):
    """Prefix searches ignore case and accents and read through the indexes (D-075)."""

    databases = {"default", "addresses"}

    def setUp(self):
        self.generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=self.generation, data_date=timezone.localdate(),
            imported_at=timezone.now(), checksum="0" * 64, row_count=1,
        )
        Address.objects.bulk_create([
            Address(generation=self.generation, street=street, house_number=number,
                    postal_code=postal_code, locality=locality)
            for street, number, postal_code, locality in (
                ("Rue de l'\u00c9glise", "1", "1200", "Gen\u00e8ve"),
                ("\u00c4rztegasse", "3", "8001", "Z\u00fcrich"),
                ("Bahnhofstrasse", "5", "8001", "Z\u00fcrich"),
                ("Bahnhofstrasse", "7", "8001", "Z\u00fcrich"),
                ("Bahnhofplatz", "1", "3000", "Bern"),
                ("Strasse der Stra\u00dfe", "2", "3000", "Bern"),
            )
        ])

    def streets(self, values, mode="suggest"):
        return [row["street"] for row in lookup(values, mode=mode)["results"]]

    def test_the_prefix_ignores_case_and_accents_and_shows_the_directory_spelling(self):
        for typed in ("rue de l'eglise", "RUE DE L'\u00c9GLISE", "Rue de l'\u00e9gl", "RUE"):
            with self.subTest(typed=typed):
                self.assertEqual(self.streets({"street": typed}), ["Rue de l'\u00c9glise"])
        for typed in ("arzte", "\u00c4RZTE", "\u00e4rzt"):
            with self.subTest(typed=typed):
                self.assertEqual(self.streets({"street": typed}), ["\u00c4rztegasse"])

    def test_the_town_and_the_house_number_ignore_case_and_accents_too(self):
        self.assertEqual(
            [(row["street"], row["house_number"]) for row in lookup({"street": "bahn", "locality": "zur"})["results"]],
            [("Bahnhofstrasse", "5"), ("Bahnhofstrasse", "7")],
        )
        self.assertEqual(
            [(row["house_number"], row["locality"]) for row in lookup({"street": "Bahnhofstrasse", "house_number": "7", "locality": "ZURICH"})["results"]],
            [("7", "Z\u00fcrich")],
        )

    def test_the_prefix_of_the_postal_code_and_the_complete_postal_code_filter(self):
        self.assertEqual(self.streets({"street": "bahnhof", "postal_code": "30"}), ["Bahnhofplatz"])
        self.assertEqual(self.streets({"street": "bahnhof", "postal_code": "8001"}), ["Bahnhofstrasse", "Bahnhofstrasse"])
        self.assertEqual(self.streets({"street": "bahnhof", "postal_code": "9"}), [])

    def test_a_prefix_is_a_prefix_not_a_substring_and_the_wildcards_of_like_are_plain_characters(self):
        self.assertEqual(self.streets({"street": "hof"}), [])
        self.assertEqual(self.streets({"street": "b%"}), [])
        self.assertEqual(self.streets({"street": "b_h"}), [])
        self.assertEqual(self.streets({"street": "_ahnhof"}), [])

    def test_the_answer_is_ordered_by_the_key_of_the_street_and_deduplicated(self):
        # "Ä" sorts as "a": the key, not the spelling, decides the order.
        self.assertEqual(self.streets({"street": "ba"}), ["Bahnhofplatz", "Bahnhofstrasse", "Bahnhofstrasse"])
        self.assertEqual(self.streets({"street": "ar"}), ["\u00c4rztegasse"])
        distinct = lookup({"street": "bahnhofstrasse", "postal_code": "8001", "house_number": "5"})["results"]
        self.assertEqual(len(distinct), 1)

    def test_the_check_ignores_case_and_accents(self):
        values = {"street": "RUE DE L'EGLISE", "house_number": "1", "postal_code": "1200", "locality": "GENEVE"}
        self.assertTrue(lookup(values, mode="check")["match"])
        self.assertFalse(lookup({**values, "postal_code": "1201"}, mode="check")["match"])
        self.assertFalse(lookup({**values, "house_number": "2"}, mode="check")["match"])
        self.assertFalse(lookup({**values, "locality": "Bern"}, mode="check")["match"])

    def test_the_keys_use_one_function_for_the_stored_and_the_typed_text(self):
        self.assertEqual(key("Stra\u00dfe"), fold("Stra\u00dfe")[:255])
        self.assertEqual(key("\u00c4 \u00d6 \u00dc"), "a o u")
        self.assertEqual(prefix_range("street_key", "ab"), {"street_key__gte": "ab", "street_key__lt": "ab\U0010ffff"})
        self.assertEqual(len(key("x" * 300)), 255)

    def test_the_main_queries_search_the_indexes_and_never_scan_the_table(self):
        scenarios = {
            "suggest": ({"street": "bahnhof"}, "suggest"),
            "suggest and a postal code prefix": ({"street": "bahnhof", "postal_code": "80"}, "suggest"),
            "suggest and a complete postal code": ({"street": "bahnhof", "postal_code": "8001"}, "suggest"),
            "suggest with a house number and a town": (
                {"street": "bahnhof", "house_number": "5", "locality": "zur"}, "suggest"),
            "check": ({"street": "Bahnhofstrasse", "house_number": "5", "postal_code": "8001",
                       "locality": "Z\u00fcrich"}, "check"),
            "approximate": ({"street": "Bahnhofstrase", "postal_code": "8001"}, "suggest"),
            "approximate with a house number": (
                {"street": "Bahnhofstrase", "postal_code": "8001", "house_number": "7"}, "suggest"),
        }
        for name, (values, mode) in scenarios.items():
            with self.subTest(name), CaptureQueriesContext(connections["addresses"]) as queries:
                lookup(values, mode=mode)
            selects = [sql for sql in sql_of(queries) if 'FROM "addresses_address"' in sql]
            self.assertTrue(selects, name)
            for sql in selects:
                with connections["addresses"].cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + sql)
                    plan = " | ".join(row[3] for row in cursor.fetchall())
                with self.subTest(name, sql=sql[:80]):
                    self.assertRegex(plan, r"SEARCH addresses_address USING (COVERING )?INDEX addr_generation_")
                    self.assertNotIn("SCAN addresses_address", plan)

    def test_the_check_and_the_candidates_use_the_index_of_the_postal_code(self):
        for values, mode in (
            ({"street": "Bahnhofstrasse", "house_number": "5", "postal_code": "8001", "locality": "Z\u00fcrich"}, "check"),
            ({"street": "Bahnhofstrase", "postal_code": "8001"}, "suggest"),
        ):
            with CaptureQueriesContext(connections["addresses"]) as queries:
                lookup(values, mode=mode)
            plans = []
            for sql in sql_of(queries):
                if 'FROM "addresses_address"' in sql and ("DISTINCT" in sql and '"street" FROM' in sql or mode == "check"):
                    with connections["addresses"].cursor() as cursor:
                        cursor.execute("EXPLAIN QUERY PLAN " + sql)
                        plans.append(" | ".join(row[3] for row in cursor.fetchall()))
            self.assertTrue(plans)
            for plan in plans:
                self.assertRegex(
                    plan, r"USING (COVERING )?INDEX addr_generation_postal_street \(generation=\? AND postal_code=\?",
                )


@tag("mariadb")
class ImportFileMaintenanceTests(TransactionTestCase):
    """After an import the planner statistics exist and the write-ahead log is folded in (D-075).

    A TransactionTestCase: the checkpoint cannot run inside the open transaction of a TestCase.
    """

    databases = {"default", "addresses"}

    def test_a_real_import_analyses_the_file_and_empties_the_log(self):
        with self.assertNoLogs("leg_platform.addresses.importer", level="WARNING"):
            self.assertEqual(
                importer._publish(io.BytesIO(archive_bytes()), "1" * 64, timezone.localdate()), 1,
            )
        with connections["addresses"].cursor() as cursor:
            cursor.execute("SELECT tbl FROM sqlite_stat1 WHERE tbl = 'addresses_address'")
            self.assertTrue(cursor.fetchall())
        log = Path(str(settings.DATABASES["addresses"]["NAME"]) + "-wal")
        self.assertTrue(not log.exists() or log.stat().st_size == 0)
        self.assertEqual(Directory.objects.get(pk=1).row_count, 1)
        self.assertEqual(lookup({"street": "Example"})["results"][0]["street"], "Example Street")


@tag("mariadb")
class ReadOnlyDefaultTests(TransactionTestCase):
    """The directory file is read-only unless the process declares itself a writer (D-075, L-1)."""

    databases = {"default", "addresses"}
    # The modules whose command writes the file (or creates it): each one must declare itself.
    WRITER_COMMANDS = {
        "addresses/management/commands/process_address_imports.py",
        "addresses/management/commands/import_address_directory.py",
        "addresses/management/commands/scheduled_address_import.py",
        "addresses/management/commands/seed_local_address_directory.py",
        "addresses/management/commands/migrate.py",
        "accounts/management/commands/setup_instance.py",
    }

    def tearDown(self):
        connections["addresses"].close()

    def test_the_test_process_is_a_writer_because_the_test_settings_say_so(self):
        self.assertTrue(storage.is_writer())

    def test_a_connection_is_read_only_unless_the_process_is_a_writer(self):
        Directory.objects.create(pk=1, checksum="0" * 64)
        connections["addresses"].close()
        with patch.object(storage, "_writer", False):
            self.assertFalse(storage.is_writer())
            self.assertEqual(Directory.objects.count(), 1)
            with self.assertRaises(OperationalError) as error:
                Directory.objects.create(pk=2, checksum="1" * 64)
            self.assertIn("readonly", str(error.exception).lower())
            with self.assertRaises(OperationalError):
                Address.objects.create(generation=uuid.uuid4(), street="x", postal_code="9999", locality="y")
            with self.assertRaises(OperationalError):
                Directory.objects.filter(pk=1).delete()
            connections["addresses"].close()
        # A process that declared itself a writer (the worker, an operator command, migrate) writes.
        self.assertTrue(storage.is_writer())
        Directory.objects.create(pk=2, checksum="1" * 64)
        self.assertEqual(Directory.objects.count(), 2)

    def test_mark_writer_switches_the_default_for_the_connections_opened_afterwards(self):
        connections["addresses"].close()
        with patch.object(storage, "_writer", False):
            storage.mark_writer()
            self.assertTrue(storage.is_writer())
            Directory.objects.create(pk=1, checksum="0" * 64)
            connections["addresses"].close()

    def test_the_guard_applies_to_the_directory_alias_only(self):
        with patch.object(storage, "_writer", False):
            connections["default"].close()
            self.assertFalse(ImportRequest.objects.filter(pk=1).exists())
            ImportRequest.objects.create(pk=1)
            self.assertTrue(ImportRequest.objects.filter(pk=1).exists())

    def test_the_web_entry_points_never_declare_themselves_a_writer(self):
        for name in ("wsgi.py", "asgi.py"):
            text = (Path(settings.BASE_DIR) / "leg_platform" / name).read_text(encoding="utf-8")
            self.assertNotIn("mark_writer", text, name)
            self.assertNotIn("mark_web_process", text, name)

    @staticmethod
    def _handle_calls_mark_writer(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "handle":
                return any(
                    isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "mark_writer"
                    for call in ast.walk(node)
                )
        return False

    def test_every_command_that_writes_the_directory_file_marks_itself_a_writer_in_handle(self):
        directory = Path(settings.BASE_DIR) / "leg_platform"
        for relative in sorted(self.WRITER_COMMANDS):
            with self.subTest(command=relative):
                self.assertTrue(self._handle_calls_mark_writer(directory / relative))

    def test_no_other_module_declares_itself_a_writer(self):
        directory = Path(settings.BASE_DIR) / "leg_platform"
        allowed = {*self.WRITER_COMMANDS, "addresses/storage.py", "test_settings.py"}
        for path in directory.rglob("*.py"):
            relative = path.relative_to(directory).as_posix()
            if "tests" in path.parts or relative in allowed:
                continue
            self.assertNotIn("mark_writer", path.read_text(encoding="utf-8"), relative)

    def test_the_migrate_command_of_this_app_replaces_the_one_of_django(self):
        from django.core.management import get_commands, load_command_class
        from django.core.management.commands.migrate import Command as DjangoMigrate

        self.assertEqual(get_commands()["migrate"], "leg_platform.addresses")
        command = load_command_class("leg_platform.addresses", "migrate")
        self.assertIsInstance(command, DjangoMigrate)
        with patch.object(storage, "_writer", False), patch.object(DjangoMigrate, "handle", return_value=""):
            command.handle()
            self.assertTrue(storage.is_writer())


@tag("mariadb")
class DirectoryFileTests(SimpleTestCase):
    """The backend creates the directory file with mode 0600 (D-075)."""

    # The test opens its own connection to a temporary file under the alias name "addresses".
    databases = {"addresses"}

    def prepare(self, path):
        from leg_platform.addresses.sqlite.base import DatabaseWrapper

        DatabaseWrapper._prepare_file(path)

    def test_sqlite_temporary_files_go_to_the_folder_of_the_file_unless_the_operator_chose_a_folder(self):
        from django.db.backends.sqlite3 import base

        from leg_platform.addresses.sqlite.base import DatabaseWrapper

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state" / "address-directory.sqlite3"
            wrapper = DatabaseWrapper(dict(connections["addresses"].settings_dict, NAME=str(path)), alias="x")
            with patch.object(base.DatabaseWrapper, "get_new_connection", return_value=object()):
                with patch.dict(os.environ):
                    os.environ.pop("SQLITE_TMPDIR", None)
                    wrapper.get_new_connection({"database": str(path)})
                    self.assertEqual(os.environ["SQLITE_TMPDIR"], str(path.parent))
                with patch.dict(os.environ, {"SQLITE_TMPDIR": "/operator/choice"}):
                    wrapper.get_new_connection({"database": str(path)})
                    self.assertEqual(os.environ["SQLITE_TMPDIR"], "/operator/choice")
                # An in-memory database has no folder.
                with patch.dict(os.environ):
                    os.environ.pop("SQLITE_TMPDIR", None)
                    wrapper.get_new_connection({"database": ":memory:"})
                    self.assertNotIn("SQLITE_TMPDIR", os.environ)

    def test_the_file_and_its_folder_are_created_private(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state" / "address-directory.sqlite3"
            self.prepare(path)
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_size, 0)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_every_missing_folder_of_the_path_is_created_private(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "one" / "two" / "three" / "address-directory.sqlite3"
            self.prepare(path)
            self.assertTrue(path.is_file())
            if os.name == "posix":
                for created in (path.parent, path.parent.parent, path.parent.parent.parent):
                    self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o700, created.name)
            # Existing folders are left as they are.
            self.assertTrue(Path(folder).is_dir())

    def test_a_symbolic_link_at_the_file_path_is_refused_and_its_target_is_not_touched(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "other-file"
            target.write_bytes(b"content")
            if os.name == "posix":
                target.chmod(0o644)
            link = Path(folder) / "address-directory.sqlite3"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("The system does not allow symbolic links for this user.")
            with self.assertRaises(OperationalError) as error:
                self.prepare(link)
            self.assertNotIn(folder, str(error.exception))
            self.assertEqual(target.read_bytes(), b"content")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_a_dangling_symbolic_link_at_the_file_path_is_refused_and_nothing_is_created(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "does-not-exist"
            link = Path(folder) / "address-directory.sqlite3"
            try:
                link.symlink_to(missing)
            except (OSError, NotImplementedError):
                self.skipTest("The system does not allow symbolic links for this user.")
            with self.assertRaises(OperationalError):
                self.prepare(link)
            self.assertFalse(missing.exists())

    def test_a_path_that_is_a_symbolic_link_is_refused_even_where_links_cannot_be_created(self):
        # The same check without a real link (a system that does not allow links to this user).
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "address-directory.sqlite3"
            with patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaises(OperationalError):
                    self.prepare(path)
            self.assertFalse(path.exists())

    def test_the_file_is_created_exclusively(self):
        # O_EXCL: an existing file is never opened for creation (its content stays).
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "address-directory.sqlite3"
            with patch("leg_platform.addresses.sqlite.base.os.open", wraps=os.open) as opened:
                self.prepare(path)
            self.assertTrue(opened.call_args_list[0].args[1] & os.O_EXCL)
            self.assertTrue(opened.call_args_list[0].args[1] & os.O_CREAT)

    def test_an_existing_file_keeps_its_content_and_a_wider_mode_is_narrowed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "address-directory.sqlite3"
            path.write_bytes(b"content")
            if os.name == "posix":
                path.chmod(0o644)
            self.prepare(path)
            self.assertEqual(path.read_bytes(), b"content")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_file_that_cannot_be_prepared_is_a_database_error_without_the_path(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "blocker"
            blocker.write_text("a file, not a folder")
            with self.assertRaises(OperationalError) as error:
                self.prepare(blocker / "address-directory.sqlite3")
            self.assertNotIn(folder, str(error.exception))

    def test_a_connection_to_the_file_uses_the_configured_pragmas_and_creates_it_private(self):
        from django.db.utils import ConnectionHandler

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "address-directory.sqlite3"
            databases = {
                "default": {"ENGINE": "django.db.backends.dummy"},
                "addresses": storage.directory_database(str(path)),
            }
            own = ConnectionHandler(databases)
            try:
                with own["addresses"].cursor() as cursor:
                    cursor.execute("PRAGMA journal_mode")
                    self.assertEqual(cursor.fetchone()[0].lower(), "wal")
                    cursor.execute("PRAGMA busy_timeout")
                    self.assertEqual(cursor.fetchone()[0], storage.BUSY_TIMEOUT_SECONDS * 1000)
                    cursor.execute("PRAGMA synchronous")
                    self.assertEqual(cursor.fetchone()[0], 1)
            finally:
                own["addresses"].close()
            self.assertTrue(path.is_file())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


@tag("mariadb")
class MigrateTests(SimpleTestCase):
    """``migrate --database=addresses`` creates only the directory tables, in the file."""

    def test_the_full_migration_graph_creates_only_the_current_directory_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state" / "address-directory.sqlite3"
            environment = {**os.environ, "ADDRESS_DIRECTORY_FILE": str(path)}
            manage = [sys.executable, str(Path(settings.BASE_DIR) / "manage.py")]
            # Exercise every app's migration operations against this alias, not just the
            # addresses target. The router must exclude all application/permission tables.
            command = [*manage, "migrate", "--database=addresses", "--noinput"]
            first = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=300, check=False)
            self.assertEqual(first.returncode, 0, first.stderr[-500:])
            self.assertIn("Applying addresses.0001_initial... OK", first.stdout)
            # 0002 only changes the queue table, which is not in the file: recorded, nothing else.
            self.assertIn("Applying addresses.0002_importrequest_last_failed_repair_at... OK", first.stdout)
            self.assertIn("Applying addresses.0003_import_progress_and_error... OK", first.stdout)
            # 0004 adds the building number and the flag of the directory in the file; the queue and the stored
            # building searches are in the application database.
            self.assertIn("Applying addresses.0004_building_identifier_update_flag_and_lookups... OK", first.stdout)
            self.assertIn("Applying addresses.0005_entrance_identifier... OK", first.stdout)
            self.assertIn("Applying addresses.0006_columns_missing_backoff... OK", first.stdout)
            # 0007 adds the index of the towns (the suggestions of the postal code and the town, D-111).
            self.assertIn("Applying addresses.0007_locality_index... OK", first.stdout)
            second = subprocess.run(
                [*command, "--plan"], env=environment, capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr[-500:])
            self.assertIn("No planned migration operations.", second.stdout)
            database = sqlite3.connect(path)
            try:
                tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                recorded = {row for row in database.execute("SELECT app, name FROM django_migrations")}
                journal_mode = database.execute("PRAGMA journal_mode").fetchone()[0]
                address_columns = {row[1] for row in database.execute("PRAGMA table_info(addresses_address)")}
                directory_columns = {row[1] for row in database.execute("PRAGMA table_info(addresses_directory)")}
                address_indexes = {
                    row[1]: [column[2] for column in database.execute(f"PRAGMA index_info('{row[1]}')")]
                    for row in database.execute("PRAGMA index_list(addresses_address)")
                }
            finally:
                database.close()
            self.assertEqual(tables - {"sqlite_sequence", "sqlite_stat1"},
                             {"django_migrations", "addresses_address", "addresses_directory"})
            self.assertEqual(address_columns, {
                "id", "generation", "street", "house_number", "postal_code", "locality",
                "street_key", "house_number_key", "locality_key", "egid", "entrance_id",
            })
            self.assertEqual(directory_columns, {
                "id", "generation", "data_date", "imported_at", "checksum", "row_count",
                "has_egid", "import_token", "import_lease_until",
            })
            self.assertEqual(address_indexes, {
                "addr_generation_postal_street": [
                    "generation", "postal_code", "street_key", "street", "house_number_key",
                ],
                "addr_generation_street": [
                    "generation", "street_key", "street", "house_number", "postal_code", "locality",
                ],
                "addr_generation_locality": ["generation", "locality_key", "locality", "postal_code"],
            })
            self.assertEqual({node for node in recorded if node[0] == "addresses"}, {
                ("addresses", "0001_initial"), ("addresses", "0002_importrequest_last_failed_repair_at"),
                ("addresses", "0003_import_progress_and_error"),
                ("addresses", "0004_building_identifier_update_flag_and_lookups"),
                ("addresses", "0005_entrance_identifier"),
                ("addresses", "0006_columns_missing_backoff"),
                ("addresses", "0007_locality_index"),
            })
            # Full-graph migrate records even router-excluded apps as applied. This is not
            # permission-table creation: the exact table set above must still be directory-only.
            self.assertEqual(recorded, set(MigrationLoader(None).graph.nodes))
            self.assertEqual(journal_mode.lower(), "wal")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    # Historical catalogue-row transformations (e.g. 0005's has_egid reset) are not a
    # startup contract: schema changes discard the public file and queue a fresh import.
    # Current-schema migrations and router/write isolation remain tested here.

    def test_real_processes_a_command_that_writes_does_and_everything_else_is_read_only(self):
        # The real settings in child processes (not the test settings, which make the test process
        # a writer): the migrate command and the seed write, `shell` (a process that never declared
        # itself a writer) cannot (D-075, L-1).
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "address-directory.sqlite3"
            environment = {
                **os.environ, "ADDRESS_DIRECTORY_FILE": str(path), "APP_ENV": "local",
                # The real settings: the test settings make the process a writer.
                "DJANGO_SETTINGS_MODULE": "leg_platform.settings",
            }
            manage = [sys.executable, str(Path(settings.BASE_DIR) / "manage.py")]

            def run(*arguments):
                return subprocess.run(
                    [*manage, *arguments], env=environment, capture_output=True, text=True, timeout=300, check=False,
                )

            migrated = run("migrate", "addresses", "--database=addresses", "--noinput")
            self.assertEqual(migrated.returncode, 0, migrated.stderr[-500:])
            seeded = run("seed_local_address_directory")
            self.assertEqual(seeded.returncode, 0, seeded.stderr[-500:])
            self.assertIn("synthetic addresses", seeded.stdout)
            probe = (
                "from django.db import OperationalError\n"
                "from leg_platform.addresses.models import Directory\n"
                "print('rows', Directory.objects.count())\n"
                "try:\n"
                "    Directory.objects.create(pk=2, checksum='x')\n"
                "except OperationalError as error:\n"
                "    print('readonly' in str(error).lower())\n"
                "else:\n"
                "    print('WRITTEN')\n"
            )
            shell = run("shell", "-c", probe)
            self.assertEqual(shell.returncode, 0, shell.stderr[-500:])
            # `shell` prints a line about its automatic imports first.
            self.assertEqual(shell.stdout.split()[-3:], ["rows", "1", "True"])
