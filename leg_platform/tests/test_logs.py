"""The log files and the log pages of the administration (D-092): the handler, the fixed names, reading
with offsets, the redaction, the access for system administrators only and the security events."""

import logging
import os
import stat
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Permission
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform import environment, log_filters, logfiles
from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user

TOKEN_LINE = "WARNING django.request Not Found: /register/verify/SECRET-TOKEN-VALUE/\n"


def write(path, text):
    Path(path).write_text(text, encoding="utf-8", newline="")


class RedactionTests(SimpleTestCase):
    def test_email_addresses_ibans_and_database_values_are_removed(self):
        text = (
            "IntegrityError: (1062, \"Duplicate entry 'anna.person@example.org' for key 'email'\") "
            "iban CH93 0076 2011 6238 5295 7 mail anna.person@example.org Incorrect string value: '\\xF0' for column"
        )
        cleaned = log_filters.redact_personal_data(text)
        self.assertNotIn("anna.person", cleaned)
        self.assertNotIn("CH93", cleaned)
        self.assertNotIn("xF0", cleaned)
        self.assertIn("Duplicate entry '<redacted>' for key", cleaned)
        self.assertIn("<email>", cleaned)
        self.assertIn("<iban>", cleaned)

    def test_the_filter_cleans_message_and_exception_text(self):
        record = logging.LogRecord("x", logging.ERROR, "f", 1, "failed for %s", ("a.b@example.org",), None)
        try:
            raise ValueError("Duplicate entry 'c.d@example.org' for key 'x'")
        except ValueError:
            import sys

            record.exc_info = sys.exc_info()
        log_filters.TokenRedactionFilter().filter(record)
        self.assertNotIn("a.b@example.org", record.getMessage())
        self.assertNotIn("c.d@example.org", record.exc_text)

    def test_a_long_run_of_local_part_characters_is_not_scanned_again_from_every_position(self):
        # L-B: without a bound this took quadratic time; 300 000 characters must finish at once (generous bound: 3 s).
        import time

        for run in ("a" * 300_000, "a." * 150_000, "a-" * 150_000 + "!"):
            started = time.perf_counter()
            cleaned = log_filters.redact_personal_data(run)
            self.assertLess(time.perf_counter() - started, 3.0)
            self.assertEqual(cleaned, run)  # nothing to redact
        # An address next to such a run is still removed; one with a local part of the allowed length too.
        text = "x" * 5000 + " anna.person@example.org " + "y" * 5000
        cleaned = log_filters.redact_personal_data(text)
        self.assertNotIn("anna.person", cleaned)
        self.assertIn("<email>", cleaned)
        self.assertNotIn("a" * 64 + "@", log_filters.redact_personal_data("a" * 64 + "@example.org"))

    def test_tokens_are_still_removed(self):
        self.assertNotIn("SECRET-TOKEN-VALUE", logfiles.redact(TOKEN_LINE))

    def test_ordinary_text_is_untouched(self):
        line = "2026-10-17 10:00:00,000 INFO leg_platform.addresses.jobs Address directory import completed (manual): 3300000 addresses."
        self.assertEqual(logfiles.redact(line), line)


class HandlerAndConfigurationTests(SimpleTestCase):
    def test_without_a_folder_there_are_no_file_handlers(self):
        config = logfiles.logging_config("")
        self.assertEqual(set(config["handlers"]), {"console"})
        self.assertEqual(config["root"]["handlers"], ["console"])
        self.assertEqual(config["loggers"]["leg_platform.security"]["handlers"], ["console"])

    def test_with_a_folder_the_two_logs_go_to_their_own_rotating_files(self):
        with tempfile.TemporaryDirectory() as folder:
            config = logfiles.logging_config(folder)
        self.assertEqual(config["root"]["handlers"], ["console", "application_file"])
        self.assertEqual(config["loggers"]["leg_platform.security"]["handlers"], ["console", "security_file"])
        self.assertEqual(config["loggers"]["django"]["handlers"], ["console", "application_file"])
        # The development server's request log stays on the console.
        self.assertEqual(config["loggers"]["django.server"]["handlers"], ["console"])
        for name in ("application_file", "security_file"):
            handler = config["handlers"][name]
            self.assertEqual(handler["maxBytes"], logfiles.MAX_BYTES)
            self.assertEqual(handler["backupCount"], logfiles.BACKUPS)
            self.assertTrue(handler["delay"])
            self.assertEqual(handler["filters"], ["redact_tokens"])
        self.assertEqual(config["handlers"]["application_file"]["filename"], str(Path(folder) / "application.log"))

    def test_the_test_settings_write_no_files(self):
        from django.conf import settings

        self.assertEqual(settings.LOG_DIRECTORY, "")
        self.assertNotIn("application_file", settings.LOGGING["handlers"])
        self.assertFalse(settings.ADDRESS_IMPORT_AUTOMATIC)

    def test_the_files_are_created_for_the_owner_only_and_stay_bounded(self):
        with tempfile.TemporaryDirectory() as parent:
            folder = Path(parent) / "logs" / "deeper"
            self.assertEqual(logfiles.usable_log_directory(str(folder)), str(folder))
            handler = logfiles.SafeRotatingFileHandler(
                str(folder / "application.log"), maxBytes=300, backupCount=2, encoding="utf-8", delay=True
            )
            logger = logging.getLogger("leg_platform.test_rotation")
            logger.propagate = False
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                for number in range(60):
                    logger.info("line number %d with some padding text", number)
            finally:
                logger.removeHandler(handler)
                handler.close()
            names = sorted(path.name for path in folder.iterdir())
            self.assertEqual(names, ["application.log", "application.log.1", "application.log.2"])
            for path in folder.iterdir():
                self.assertLessEqual(path.stat().st_size, 400)
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

    def test_a_folder_that_cannot_be_used_means_no_file_logging(self):
        with tempfile.TemporaryDirectory() as parent:
            blocker = Path(parent) / "a-file"
            blocker.write_text("x", encoding="utf-8")
            self.assertEqual(logfiles.usable_log_directory(str(blocker / "logs")), "")
            self.assertEqual(logfiles.usable_log_directory(str(blocker)), "")
        self.assertEqual(logfiles.usable_log_directory(""), "")


class EnvironmentTests(SimpleTestCase):
    def parse(self, value=None, app_env="local"):
        env = {} if value is None else {"LOG_DIRECTORY": value}
        return environment.parse_log_directory(env, app_env)

    def test_the_defaults(self):
        self.assertTrue(self.parse().replace("\\", "/").endswith("/.leg-platform-dev/logs"))
        self.assertEqual(self.parse(app_env="production"), "/var/log/leg-platform")
        self.assertEqual(self.parse(app_env="stage"), "/var/log/leg-platform")

    def test_off_turns_the_files_off(self):
        self.assertEqual(self.parse("off"), "")
        self.assertEqual(self.parse("OFF", "production"), "")

    def test_a_configured_folder_must_be_absolute_and_outside_the_repository(self):
        folder = tempfile.gettempdir()
        self.assertEqual(self.parse(folder), folder)
        for bad in ("relative/logs", "logs", str(environment.REPOSITORY_ROOT / "logs"), "/tmp/x\x00y", "/tmp/a\nb"):
            with self.subTest(value=bad), self.assertRaises(ImproperlyConfigured):
                self.parse(bad)

    def test_a_file_is_not_a_folder(self):
        with tempfile.NamedTemporaryFile() as handle, self.assertRaises(ImproperlyConfigured):
            self.parse(handle.name)

    def test_the_automatic_import_switch(self):
        base = {"ADDRESS_IMPORT_AUTOMATIC": "false"}
        with mock.patch.object(environment, "_get", side_effect=lambda env, name: env.get(name, "")):
            self.assertFalse(environment._parse_bool(base, "ADDRESS_IMPORT_AUTOMATIC", True))
            self.assertTrue(environment._parse_bool({}, "ADDRESS_IMPORT_AUTOMATIC", True))
            with self.assertRaises(ImproperlyConfigured):
                environment._parse_bool({"ADDRESS_IMPORT_AUTOMATIC": "maybe"}, "ADDRESS_IMPORT_AUTOMATIC", True)


class LogLineTests(SimpleTestCase):
    """M-1: a value that a client controls can never start a new line of the log."""

    def test_control_characters_and_line_separators_become_visible_escapes(self):
        self.assertEqual(log_filters.escape_control("a\nb\rc\r\nd"), "a\\nb\\rc\\r\\nd")
        self.assertEqual(log_filters.escape_control("a\x00b\x1bc\x7fd\x85e"), "a\\x00b\\x1bc\\x7fd\\x85e")
        self.assertEqual(log_filters.escape_control("x\u2028y\u2029z"), "x\\u2028y\\u2029z")
        self.assertEqual(log_filters.escape_control("tab\tstays and ä ö ü €"), "tab\tstays and ä ö ü €")

    def test_the_formatter_writes_one_line_per_message_and_keeps_the_lines_of_an_exception(self):
        formatter = log_filters.SafeFormatter("{levelname} {name} {message}", style="{")
        record = logging.LogRecord("django.request", logging.WARNING, __file__, 1, "Not Found: %s", ("/x\nINFO leg_platform.security forged",), None)
        text = formatter.format(record)
        self.assertEqual(text, "WARNING django.request Not Found: /x\\nINFO leg_platform.security forged")
        self.assertEqual(record.getMessage(), "Not Found: /x\nINFO leg_platform.security forged")  # the record is not changed
        try:
            raise ValueError("first\nsecond")
        except ValueError:
            import sys

            failing = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed\r\nforged", (), sys.exc_info())
        formatted = formatter.format(failing)
        first, _sep, rest = formatted.partition("\n")
        self.assertEqual(first, "ERROR x failed\\r\\nforged")
        self.assertIn("Traceback", rest)  # the exception text keeps its lines
        self.assertGreater(rest.count("\n"), 2)

    def test_the_configuration_uses_the_formatter_for_every_handler(self):
        config = logfiles.logging_config("")
        self.assertEqual(config["formatters"]["default"]["()"], "leg_platform.log_filters.SafeFormatter")


@tag("mariadb")
class LogInjectionRequestTests(TestCase):
    def test_a_request_to_an_address_with_a_line_break_makes_one_log_line_not_two(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "application.log"
            handler = logfiles.SafeRotatingFileHandler(str(path), maxBytes=100_000, backupCount=1, encoding="utf-8", delay=True)
            handler.setFormatter(log_filters.SafeFormatter("{levelname} {name} {message}", style="{"))
            handler.addFilter(log_filters.TokenRedactionFilter())
            logger = logging.getLogger("django.request")
            logger.addHandler(handler)
            try:
                response = Client().get("/x%0aINFO leg_platform.security FORGED-ENTRY")
            finally:
                logger.removeHandler(handler)
                handler.close()
            self.assertEqual(response.status_code, 404)
            lines = path.read_text(encoding="utf-8").splitlines()
        forged = [line for line in lines if "FORGED-ENTRY" in line]
        self.assertEqual(len(forged), 1)
        self.assertTrue(forged[0].startswith("WARNING django.request Not Found: /x\\nINFO"), forged[0])
        self.assertFalse(any(line.startswith("INFO leg_platform.security") for line in lines))


class ReadingTests(SimpleTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = self.folder.name

    def path(self, name="application.log"):
        return Path(self.directory) / name

    def test_only_the_fixed_names_resolve(self):
        write(self.path(), "x\n")
        write(self.path("application.log.1"), "x\n")
        write(self.path("other.txt"), "x\n")
        self.assertEqual(logfiles.resolve(self.directory, "application.log"), self.path())
        self.assertEqual(logfiles.resolve(self.directory, "application.log.1"), self.path("application.log.1"))
        for bad in (
            "other.txt", "../application.log", "..\\application.log", "application.log/", "/etc/passwd",
            "application.log.0", "application.log.100", "application.log\x00", "APPLICATION.LOG",
            "application.log.1.2", "security.log.", "", None, 5,
        ):
            with self.subTest(name=bad):
                self.assertIsNone(logfiles.resolve(self.directory, bad))
        self.assertIsNone(logfiles.resolve("", "application.log"))
        self.assertIsNone(logfiles.resolve(self.directory, "security.log"))

    def test_a_symbolic_link_is_not_followed(self):
        target = self.path("elsewhere")
        write(target, "secret\n")
        try:
            os.symlink(target, self.path("application.log"))
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available")
        self.assertIsNone(logfiles.resolve(self.directory, "application.log"))
        self.assertEqual(logfiles.list_files(self.directory), [])

    def test_the_list_has_the_log_files_only_current_first(self):
        for name in ("security.log", "application.log.2", "application.log", "application.log.1", "notes.txt", "x.log"):
            write(self.path(name), "line\n")
        names = [item["name"] for item in logfiles.list_files(self.directory)]
        self.assertEqual(names, ["application.log", "application.log.1", "application.log.2", "security.log"])
        self.assertEqual(logfiles.list_files(""), [])
        self.assertEqual(logfiles.list_files(self.directory + "-missing"), [])

    def test_the_tail_returns_the_last_lines_and_the_offset_after_them(self):
        write(self.path(), "".join(f"line {number}\n" for number in range(1, 11)))
        text, offset, file_id = logfiles.tail(self.path(), lines=3)
        self.assertEqual(text, "line 8\nline 9\nline 10\n")
        self.assertEqual(offset, self.path().stat().st_size)
        self.assertTrue(file_id)

    def test_a_half_written_line_is_not_shown_until_it_is_complete(self):
        write(self.path(), "one\ntwo\nthr")
        text, offset, file_id = logfiles.tail(self.path())
        self.assertEqual(text, "one\ntwo\n")
        with open(self.path(), "a", encoding="utf-8", newline="") as handle:
            handle.write("ee\nfour\n")
        more, new_offset, _id, reset = logfiles.read_since(self.path(), offset, file_id)
        self.assertEqual((more, reset), ("three\nfour\n", False))
        self.assertEqual(new_offset, self.path().stat().st_size)
        nothing, same, _id, reset = logfiles.read_since(self.path(), new_offset, file_id)
        self.assertEqual((nothing, same, reset), ("", new_offset, False))

    def test_a_rotated_or_truncated_file_resets_the_view(self):
        write(self.path(), "old 1\nold 2\nold 3\n")
        _text, offset, file_id = logfiles.tail(self.path())
        # Truncated: shorter than the offset.
        write(self.path(), "new\n")
        text, new_offset, _id, reset = logfiles.read_since(self.path(), offset, file_id)
        self.assertEqual((text, reset), ("new\n", True))
        # Replaced by another file (rotation): another identity.
        replacement = self.path("replacement")
        write(replacement, "fresh start long enough\n" * 5)
        os.replace(replacement, self.path())
        text, _offset, _id, reset = logfiles.read_since(self.path(), new_offset, file_id)
        self.assertTrue(reset)
        self.assertIn("fresh start", text)

    def test_every_answer_is_bounded(self):
        write(self.path(), ("y" * 99 + "\n") * 20000)
        text, offset, file_id = logfiles.tail(self.path())
        self.assertLessEqual(len(text.encode()), logfiles.TAIL_MAX_BYTES)
        self.assertLessEqual(len(text.splitlines()), logfiles.TAIL_LINES)
        write(self.path(), ("z" * 99 + "\n") * 5000)
        more, new_offset, _id, reset = logfiles.read_since(self.path(), 0, file_id)
        # A different file identity or not, no answer is larger than the poll limit.
        self.assertLessEqual(len(more.encode()), logfiles.POLL_MAX_BYTES)

    def test_a_negative_or_too_large_offset_is_a_reset_not_an_error(self):
        write(self.path(), "a\nb\n")
        _text, _offset, file_id = logfiles.tail(self.path())
        for offset in (-5, 10**9):
            text, _new, _id, reset = logfiles.read_since(self.path(), offset, file_id)
            self.assertTrue(reset)
            self.assertEqual(text, "a\nb\n")

    def test_what_is_read_is_redacted_again(self):
        write(self.path(), TOKEN_LINE + "mail anna.person@example.org\n")
        text, _offset, _id = logfiles.tail(self.path())
        self.assertNotIn("SECRET-TOKEN-VALUE", text)
        self.assertNotIn("anna.person", text)
        body = b"".join(logfiles.redacted_chunks(self.path())).decode()
        self.assertNotIn("SECRET-TOKEN-VALUE", body)
        self.assertNotIn("anna.person", body)
        self.assertIn("Not Found", body)

    def test_a_download_is_cut_in_pieces(self):
        write(self.path(), ("w" * 1000 + "\n") * 500)
        pieces = list(logfiles.redacted_chunks(self.path()))
        self.assertGreater(len(pieces), 5)
        self.assertEqual(len(b"".join(pieces)), 1001 * 500)
        for piece in pieces:
            self.assertLessEqual(len(piece), logfiles.DOWNLOAD_CHUNK_BYTES + 2 * logfiles.MAX_LINE_BYTES)

    def test_a_very_long_line_is_cut_into_pieces_without_losing_text(self):
        write(self.path(), "q" * 100_000 + "\n")
        body = b"".join(logfiles.redacted_chunks(self.path()))
        self.assertEqual(len(body), 100_001)

    def test_a_value_that_straddles_a_piece_of_a_long_line_is_redacted_whole(self):
        # L-3: a line is read in pieces of MAX_LINE_BYTES; an address or an IBAN at the end of a piece must not survive.
        for value, marker in (("anna.person@example.org", "<email>"), ("CH93 0076 2011 6238 5295 7", "<iban>")):
            for before in (0, 1, 5, 11, 20, 26):
                with self.subTest(value=marker, before=before):
                    line = "x" * (logfiles.MAX_LINE_BYTES - before) + " " + value + " " + "y" * 300
                    write(self.path(), line + "\n")
                    body = b"".join(logfiles.redacted_chunks(self.path())).decode()
                    self.assertNotIn(value[:12], body)
                    self.assertIn(marker, body)
                    self.assertTrue(body.endswith("y" * 300 + "\n"))
        # Several pieces of a download: the same at the boundary of a 64 KiB piece.
        write(self.path(), ("w" * 1000 + "\n") * 63 + "x" * (logfiles.MAX_LINE_BYTES - 5) + " anna.person@example.org\n")
        self.assertNotIn("anna.person", b"".join(logfiles.redacted_chunks(self.path())).decode())

    def test_multibyte_text_cut_by_the_reading_is_not_damaged(self):
        text = "ä" * (logfiles.MAX_LINE_BYTES // 2 + 3) + "\n"  # two bytes each: the piece ends inside a character
        write(self.path(), text)
        self.assertEqual(b"".join(logfiles.redacted_chunks(self.path())).decode(), text)

    def test_a_poll_cuts_an_overlong_line_at_a_blank_not_inside_a_value(self):
        line = ("word " * 4000) + "anna.person@example.org " + "z" * 100  # no line break: longer than the limit
        write(self.path(), line)
        text, offset, _id = logfiles.tail(self.path())
        self.assertNotIn("anna.person", text)
        self.assertLessEqual(offset, len(line))

    def test_a_log_file_is_opened_without_following_a_link_and_only_when_it_is_a_regular_file(self):
        # L-2: the check happens on the open descriptor, not only on the name before it.
        write(self.path("target"), "secret\n")
        with logfiles.open_log(self.path("target")) as handle:
            self.assertEqual(handle.read(), b"secret\n")
        with self.assertRaises(OSError):
            logfiles.open_log(self.directory)  # a folder
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW is not available on this system")
        try:
            os.symlink(self.path("target"), self.path("application.log"))
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available")
        with self.assertRaises(OSError):
            logfiles.open_log(self.path("application.log"))
        with self.assertRaises(OSError):
            logfiles.tail(self.path("application.log"))


def administrator(email="log.administrator@example.org"):
    person = create_active_user(email)
    person.set_privileges(is_staff=True, is_superuser=True)
    client = VerifiedClient()
    client.force_login(person)
    return client, person


@tag("mariadb")
class LogPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_admin, cls.administrator = administrator()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patcher = override_settings(LOG_DIRECTORY=self.folder.name)
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.directory = Path(self.folder.name)
        write(self.directory / "application.log", "first line\nsecond line anna.person@example.org\n" + TOKEN_LINE)
        write(self.directory / "application.log.1", "older line\n")
        write(self.directory / "security.log", "security line\n")

    def url(self, name, log="application", query=""):
        kwargs = {} if name == "logs" else {"log": log}
        return reverse(f"admin:{name}", kwargs=kwargs) + query

    def staff_clients(self):
        """Staff accounts that hold every permission an administrator could hand out, and ordinary ones."""
        person = create_active_user("log.staff@example.org")
        person.set_privileges(is_staff=True, is_superuser=False)
        person.user_permissions.set(Permission.objects.all())
        staff = VerifiedClient()
        staff.force_login(person)
        return staff

    def test_the_list_shows_the_files_with_links(self):
        response = self.client_admin.get(self.url("logs"))
        self.assertEqual(response.status_code, 200)
        for text in ("Protokolldateien", "application.log", "application.log.1", "security.log"):
            self.assertContains(response, text)
        self.assertContains(response, self.url("logs_download", "application"))
        self.assertContains(response, self.url("logs_view", "application", "?n=1"))
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_list_says_what_to_do_without_a_folder(self):
        with override_settings(LOG_DIRECTORY=""):
            response = self.client_admin.get(self.url("logs"))
        self.assertContains(response, "journalctl")
        self.assertContains(response, "LOG_DIRECTORY")

    def test_the_menu_of_the_administration_has_the_entry_for_administrators_only(self):
        self.assertContains(self.client_admin.get(reverse("admin:index")), self.url("logs"))
        self.assertNotContains(self.staff_clients().get(reverse("admin:index")), self.url("logs"))

    def test_every_log_route_is_for_system_administrators_only(self):
        staff = self.staff_clients()
        routes = [self.url("logs")]
        for name in ("logs_view", "logs_tail", "logs_download"):
            routes += [self.url(name, "application"), self.url(name, "security")]
        for route in routes:
            for method in ("get", "post"):
                with self.subTest(route=route, method=method):
                    self.assertEqual(getattr(staff, method)(route).status_code, 403)
                    anonymous = getattr(Client(), method)(route)
                    self.assertEqual(anonymous.status_code, 302)
                    self.assertTrue(anonymous["Location"].startswith(reverse("admin:login")))
        # Nothing was recorded for the refused requests.
        self.assertFalse(SecurityEvent.objects.filter(event__in=("log_viewed", "log_downloaded")).exists())

    def test_a_staff_account_without_a_second_factor_and_an_ordinary_account_get_nothing(self):
        person = create_active_user("log.nosecond@example.org")
        person.set_privileges(is_staff=True, is_superuser=True)
        unverified = Client()
        unverified.force_login(person)
        ordinary = VerifiedClient()
        ordinary.force_login(create_active_user("log.ordinary@example.org"))
        for client in (unverified, ordinary):
            for name in ("logs", "logs_view", "logs_tail", "logs_download"):
                with self.subTest(name=name):
                    response = client.get(self.url(name))
                    self.assertEqual(response.status_code, 302)
                    self.assertNotIn(b"first line", response.content)

    def test_the_download_is_streamed_redacted_and_named_by_the_server(self):
        response = self.client_admin.get(self.url("logs_download"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        body = b"".join(response.streaming_content).decode()
        self.assertIn("first line", body)
        self.assertNotIn("anna.person", body)
        self.assertNotIn("SECRET-TOKEN-VALUE", body)
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="application.log"')
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("no-store", response["Cache-Control"])
        older = self.client_admin.get(self.url("logs_download", query="?n=1"))
        self.assertEqual(b"".join(older.streaming_content), b"older line\n")
        self.assertEqual(older["Content-Disposition"], 'attachment; filename="application.log.1"')

    def test_no_client_value_reaches_the_file_system(self):
        write(self.directory.parent / "outside-secret.log", "SECRET OUTSIDE\n")
        attempts = [
            "/admin/logs/..%2Foutside-secret/download/", "/admin/logs/application%2F..%2F..%2Fx/download/",
            "/admin/logs/passwd/download/", "/admin/logs/../download/",
        ]
        for attempt in attempts:
            with self.subTest(path=attempt):
                self.assertEqual(self.client_admin.get(attempt).status_code, 404)
        for query in ("?n=../outside-secret", "?n=-1", "?n=6", "?n=1.5", "?n=%00", "?n=%E0%A5%A7", "?n=abc"):
            for name in ("logs_download", "logs_tail", "logs_view"):
                with self.subTest(name=name, query=query):
                    self.assertEqual(self.client_admin.get(self.url(name, query=query)).status_code, 404)
        # Names or paths given as other parameters are ignored.
        response = self.client_admin.get(self.url("logs_download", query="?file=../outside-secret.log&name=security.log&path=/etc/passwd"))
        self.assertEqual(b"".join(response.streaming_content).count(b"SECRET OUTSIDE"), 0)
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="application.log"')
        # An absent file is a 404 for the download.
        (self.directory / "security.log").unlink()
        self.assertEqual(self.client_admin.get(self.url("logs_download", "security")).status_code, 404)

    def test_the_tail_gives_the_last_lines_then_only_new_ones(self):
        first = self.client_admin.get(self.url("logs_tail")).json()
        self.assertTrue(first["exists"])
        self.assertIn("first line", first["text"])
        self.assertNotIn("anna.person", first["text"])
        self.assertNotIn("SECRET-TOKEN-VALUE", first["text"])
        query = f"?offset={first['offset']}&file_id={first['file_id']}"
        again = self.client_admin.get(self.url("logs_tail", query=query)).json()
        self.assertEqual((again["text"], again["reset"]), ("", False))
        with open(self.directory / "application.log", "a", encoding="utf-8", newline="") as handle:
            handle.write("a new line\n")
        newer = self.client_admin.get(self.url("logs_tail", query=query)).json()
        self.assertEqual((newer["text"], newer["reset"]), ("a new line\n", False))

    def test_the_tail_is_never_cached_and_get_only_and_tolerates_bad_values(self):
        response = self.client_admin.get(self.url("logs_tail"))
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client_admin.post(self.url("logs_tail")).status_code, 405)
        self.assertEqual(self.client_admin.post(self.url("logs_download")).status_code, 405)
        for offset in ("abc", "-1", "9" * 30, "1.5"):
            self.assertEqual(self.client_admin.get(self.url("logs_tail", query=f"?offset={offset}")).status_code, 404)

    def test_a_missing_file_is_an_empty_answer_not_an_error(self):
        (self.directory / "security.log").unlink()
        body = self.client_admin.get(self.url("logs_tail", "security")).json()
        self.assertEqual((body["exists"], body["text"]), (False, ""))
        page = self.client_admin.get(self.url("logs_view", "security"))
        self.assertEqual(page.status_code, 200)

    def test_without_a_folder_the_tail_is_empty(self):
        with override_settings(LOG_DIRECTORY=""):
            body = self.client_admin.get(self.url("logs_tail")).json()
        self.assertEqual((body["exists"], body["text"]), (False, ""))

    def test_the_viewer_page_has_the_script_and_the_controls_and_no_inline_code(self):
        response = self.client_admin.get(self.url("logs_view"))
        html = response.content.decode()
        self.assertContains(response, 'data-tail-url="%s"' % self.url("logs_tail"))
        self.assertContains(response, "js/log-viewer.js")
        self.assertContains(response, 'id="log-pause"')
        self.assertContains(response, 'id="log-follow"')
        self.assertNotIn("<script>", html)
        self.assertNotIn(" onclick=", html)
        older = self.client_admin.get(self.url("logs_view", query="?n=1"))
        self.assertContains(older, 'data-live="0"')
        self.assertNotContains(older, 'id="log-pause"')

    def test_the_log_pages_follow_the_design_of_the_admin_pages(self):
        # UX-014: the breadcrumbs are the list of every admin page, one pause state, a region for the output.
        viewer = self.client_admin.get(self.url("logs_view")).content.decode()
        index = self.client_admin.get(self.url("logs")).content.decode()
        for html in (viewer, index):
            self.assertIn('<ol class="breadcrumbs">', html)
            self.assertNotIn('<div class="breadcrumbs">', html)
        self.assertIn('<li aria-current="page">', viewer)
        self.assertIn('class="button default" id="log-pause"', viewer)  # the admin button (UX-003)
        self.assertNotIn("aria-pressed", viewer)  # the label changes, not a state as well
        self.assertIn('role="region"', viewer)
        self.assertIn("Automatisch zum Ende scrollen", viewer)
        self.assertNotIn("<h2", viewer)  # the heading is the title of the page, not repeated
        self.assertNotIn("<h2", index)
        self.assertIn('data-label="', index)
        self.assertRegex(index, r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}")  # numeric dates (DESIGN section 8)
        for log, title in (("application", "Anwendungsprotokoll"), ("security", "Sicherheitsprotokoll (Datei)")):
            self.assertIn(title, self.client_admin.get(self.url("logs_view", log)).content.decode())

    def test_the_security_file_has_the_german_line_and_the_page_colours(self):
        # Design review 7 and 9: a German line above the box; the box takes the page colours, not a fixed black.
        html = self.client_admin.get(self.url("logs_view", "security")).content.decode()
        self.assertIn("Die Einträge sind technische Protokollzeilen der Anwendung.", html)
        self.assertLess(html.index("Die Einträge sind technische"), html.index('id="log-output"'))
        css = (Path(settings.BASE_DIR) / "leg_platform" / "static" / "css" / "admin-portal.css").read_text(encoding="utf-8")
        self.assertIn("--log-lines: 24;", css)  # 24 lines of the file viewer, from the one custom property
        self.assertIn("height: calc(var(--log-lines) * 1lh);", css)
        self.assertNotIn("27.3rem", css)
        self.assertIn("background: var(--body-bg, #fff);", css)
        self.assertNotIn("background: #111;", css)

    def test_the_script_shows_a_windows_line_break_once_and_keeps_one_pause_state(self):
        script = (Path(settings.BASE_DIR) / "leg_platform" / "static" / "js" / "log-viewer.js").read_text(encoding="utf-8")
        self.assertIn(r'replace(/\r\n?/g, "\n")', script)
        self.assertNotIn("aria-pressed", script)

    def test_the_pages_and_the_download_are_security_events_the_polls_are_not(self):
        self.client_admin.get(self.url("logs"))
        self.client_admin.get(self.url("logs_view"))
        self.client_admin.get(self.url("logs_tail"))
        self.client_admin.get(self.url("logs_tail"))
        download = self.client_admin.get(self.url("logs_download", "security"))
        b"".join(download.streaming_content)
        viewed = list(SecurityEvent.objects.filter(event="log_viewed"))
        downloaded = list(SecurityEvent.objects.filter(event="log_downloaded"))
        self.assertEqual((len(viewed), len(downloaded)), (2, 1))
        for entry in (*viewed, *downloaded):
            self.assertEqual((entry.actor_id, entry.account_id, entry.long_retention), (self.administrator.pk, None, True))

    def test_a_download_of_a_file_that_cannot_be_opened_is_a_404_not_a_broken_answer(self):
        with mock.patch.object(logfiles, "open_log", side_effect=OSError("gone")):
            response = self.client_admin.get(self.url("logs_download"))
        self.assertEqual(response.status_code, 404)

    def test_the_same_page_again_within_five_minutes_is_not_a_new_event_a_download_always_is(self):
        # L-4: a reload must not fill the security log, which keeps these events for a year.
        for _round in range(3):
            self.client_admin.get(self.url("logs"))
            self.client_admin.get(self.url("logs_view"))
        self.assertEqual(SecurityEvent.objects.filter(event="log_viewed").count(), 2)  # the list and one file
        self.client_admin.get(self.url("logs_view", "security"))  # another file is another event
        self.assertEqual(SecurityEvent.objects.filter(event="log_viewed").count(), 3)
        for _round in range(2):
            b"".join(self.client_admin.get(self.url("logs_download")).streaming_content)
        self.assertEqual(SecurityEvent.objects.filter(event="log_downloaded").count(), 2)
        # After the interval the same page is an event again.
        session = self.client_admin.session
        session["log_views_seen"] = {name: stamp - 301 for name, stamp in session["log_views_seen"].items()}
        session.save()
        self.client_admin.get(self.url("logs_view"))
        self.assertEqual(SecurityEvent.objects.filter(event="log_viewed").count(), 4)

    def test_the_security_event_names_the_file_in_the_log_line(self):
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            self.client_admin.get(self.url("logs_view"))
        self.assertEqual(
            logs.output,
            [f"WARNING:leg_platform.security:A log file was viewed (actor {self.administrator.pk}, application.log)."],
        )

    def test_the_events_have_swiss_german_labels(self):
        from django.utils import translation

        with translation.override("de-ch"):
            self.assertEqual(str(SecurityEvent.Event.LOG_VIEWED.label), "Protokolldatei angesehen")
            self.assertEqual(str(SecurityEvent.Event.LOG_DOWNLOADED.label), "Protokolldatei heruntergeladen")

    def test_the_poll_is_not_an_activity_of_the_session(self):
        from leg_platform.accounts import session_timeout

        self.assertIn("admin:logs_tail", session_timeout.PASSIVE_VIEWS)

    def test_the_converter_matches_the_two_fixed_names_only(self):
        from leg_platform import log_views

        self.assertEqual(log_views.LogKeyConverter.regex, "application|security")
        self.assertEqual(set(log_views.LOGS), {"application", "security"})
        self.assertEqual(log_views.file_name("application", 0), "application.log")
        self.assertEqual(log_views.file_name("security", 3), "security.log.3")
