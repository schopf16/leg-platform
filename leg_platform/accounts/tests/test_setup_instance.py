"""The command setup_instance: migrations, the initial administrator of the first start and the
address directory file (docs/DECISIONS.md, D-075, D-076; docs/DEPLOYMENT.md, "The setup unit").

The first-login behaviour of the created account (new password, then second factor, before
anything else) is the same account path as the interactive command and is tested in detail in
``test_second_factor_reset.InitialAdministratorTests``; here it is checked once for the account
created from the environment.
"""

import contextlib
import logging
import os
import sqlite3
import tempfile
import uuid
import threading
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection, connections
from django.db import migrations, models
from django.test import Client, SimpleTestCase, TransactionTestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.management.commands import setup_instance
from leg_platform.accounts.models import SecurityEvent, User
from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.addresses.models import Directory, ImportRequest, TransformerStation
from leg_platform.addresses import setup as directory_setup
from leg_platform.addresses.storage import DIRECTORY_ALIAS, directory_open_guard, reset_marker
from leg_platform.environment import parse_bootstrap_administrator


def addresses_migration_count():
    """The migrations of the addresses app in the migration graph (the number the setup command reports)."""
    from django.db.migrations.loader import MigrationLoader

    return len([node for node in MigrationLoader(None, ignore_no_migrations=True).graph.nodes if node[0] == "addresses"])

EMAIL_VARIABLE = "BOOTSTRAP_SYSTEM_ADMIN_EMAIL"
PASSWORD_VARIABLE = "BOOTSTRAP_SYSTEM_ADMIN_PASSWORD"
EMAIL = "admin@example.org"
# Test values only; not secrets of any environment.
INITIAL_PASSWORD = "initial passphrase of the first start"
NEW_PASSWORD = "paper lantern rests under silver rain"
WEAK_PASSWORD = "sentinl"  # 7 characters: below the minimum of 8
LOGIN_URL = reverse("accounts:login")
PROFILE_URL = reverse("accounts:profile")


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage() + " " + str(record.args))


class EnvironmentValuesTests(SimpleTestCase):
    """The variables are read from the environment only; no setting holds them."""

    def test_the_values_are_parsed_and_the_password_is_not_in_the_representation(self):
        values = parse_bootstrap_administrator(
            {EMAIL_VARIABLE: "  " + EMAIL + " ", PASSWORD_VARIABLE: INITIAL_PASSWORD}
        )
        self.assertEqual((values.email, values.password), (EMAIL, INITIAL_PASSWORD))
        self.assertNotIn(INITIAL_PASSWORD, repr(values))
        self.assertNotIn(EMAIL, repr(values))
        nothing = parse_bootstrap_administrator({PASSWORD_VARIABLE: "   "})
        self.assertEqual((nothing.email, nothing.password), ("", ""))

    def test_no_django_setting_holds_the_password(self):
        with mock.patch.dict(os.environ, {PASSWORD_VARIABLE: INITIAL_PASSWORD}):
            for name in dir(settings):
                if name.isupper():
                    self.assertNotIn(INITIAL_PASSWORD, repr(getattr(settings, name)), name)


@tag("mariadb")
class SetupInstanceTests(TransactionTestCase):
    # create_superuser() refuses to run inside a transaction, so TestCase cannot be used. The
    # command also prepares the address directory file (the alias "addresses", D-075).
    databases = {"default", "addresses"}

    def run_command(self, email=EMAIL, password=INITIAL_PASSWORD, **options):
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ):
            for name, value in ((EMAIL_VARIABLE, email), (PASSWORD_VARIABLE, password)):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            call_command("setup_instance", stdout=out, stderr=err, **options)
        return out.getvalue() + err.getvalue()

    def test_it_applies_missing_migrations_and_creates_the_administrator(self):
        # Prove the migrations on a real schema change: the independent address app is rolled
        # back in the test database, and the command must create its tables again.
        call_command("migrate", "addresses", "zero", verbosity=0)
        self.assertNotIn("addresses_importrequest", connection.introspection.table_names())

        output = self.run_command()

        # The queue table of the address app is in the application database; the directory tables are
        # in the SQLite file and are created by `migrate addresses --database=addresses` (D-075).
        self.assertIn("addresses_importrequest", connection.introspection.table_names())
        self.assertNotIn("addresses_directory", connection.introspection.table_names())
        self.assertIn(f"Database migrations: {addresses_migration_count()} applied.", output)
        user = User.objects.get()
        self.assertTrue(user.is_superuser and user.is_staff and user.is_active)
        self.assertTrue(user.password_change_required)
        self.assertTrue(user.check_password(INITIAL_PASSWORD))
        self.assertTrue(user.customer_number_id)
        self.assertEqual(user.email, EMAIL)
        entry = SecurityEvent.objects.get()
        self.assertEqual(
            (entry.event, entry.account_id, entry.long_retention),
            ("initial_administrator_created", user.pk, True),
        )
        self.assertIn("System administrator: created", output)
        self.assertIn(f"Remove {PASSWORD_VARIABLE} from the environment file", output)

    def test_a_second_run_changes_nothing_and_reminds_to_remove_the_password(self):
        self.run_command()
        before = User.objects.get()
        events = SecurityEvent.objects.count()

        output = self.run_command()

        self.assertIn("Database migrations: none pending.", output)
        self.assertIn("System administrator: exists; no account was created.", output)
        self.assertIn(f"Remove {PASSWORD_VARIABLE} from the environment file", output)
        self.assertEqual(User.objects.count(), 1)
        after = User.objects.get()
        self.assertEqual((after.pk, after.password, after.password_change_required),
                         (before.pk, before.password, before.password_change_required))
        self.assertEqual(SecurityEvent.objects.count(), events)

    def test_no_reminder_without_the_variable_once_the_administrator_exists(self):
        self.run_command()
        output = self.run_command(email=None, password=None)
        self.assertIn("exists; no account was created", output)
        self.assertNotIn("Remove", output)

    def test_the_password_is_not_read_while_a_system_administrator_exists(self):
        self.run_command()
        # A weak value would be refused if it were validated; another email would be refused too.
        with mock.patch.object(setup_instance, "create_initial_administrator") as create:
            output = self.run_command(email="second@example.org", password=WEAK_PASSWORD)
        create.assert_not_called()
        self.assertIn("exists; no account was created", output)
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(User.objects.get().check_password(INITIAL_PASSWORD))

    def test_a_blocked_or_deactivated_administrator_counts_as_existing(self):
        self.run_command()
        for status in ("blocked", "deactivated"):
            with self.subTest(status=status), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounts_user SET status = %s, is_active = 0 WHERE is_superuser = 1",
                    [status],
                )
                output = self.run_command(email="second@example.org")
                self.assertIn("exists; no account was created", output)
        self.assertEqual(User.objects.count(), 1)

    def test_it_only_migrates_when_nothing_is_configured(self):
        output = self.run_command(email=None, password=None)
        self.assertIn("none exists", output)
        self.assertNotIn("Remove", output)
        self.assertFalse(User.objects.exists())
        self.assertFalse(SecurityEvent.objects.exists())

    def test_a_missing_password_or_email_is_refused_with_a_clear_message(self):
        with self.assertRaisesMessage(CommandError, f"{PASSWORD_VARIABLE} is not"):
            self.run_command(password=None)
        with self.assertRaisesMessage(CommandError, f"{EMAIL_VARIABLE} is not"):
            self.run_command(email=None)
        with self.assertRaisesMessage(CommandError, f"{PASSWORD_VARIABLE} is not"):
            self.run_command(password="   ")
        self.assertFalse(User.objects.exists())
        self.assertFalse(SecurityEvent.objects.exists())

    def test_a_weak_password_is_refused_without_showing_it(self):
        for password in (WEAK_PASSWORD, "12345678901234567890", EMAIL):
            with self.subTest(length=len(password)):
                with self.assertRaises(CommandError) as raised:
                    self.run_command(password=password)
                self.assertIn("not accepted", str(raised.exception))
                self.assertNotIn(password, str(raised.exception))
        self.assertFalse(User.objects.exists())
        self.assertFalse(SecurityEvent.objects.exists())

    def test_the_minimum_length_of_eight_characters_applies(self):
        with self.assertRaisesMessage(CommandError, "at least 8 characters"):
            self.run_command(password="quartz-")  # 7 characters
        self.assertFalse(User.objects.exists())
        self.run_command(password="quartzla")  # 8 characters, letters only
        self.assertTrue(User.objects.get().check_password("quartzla"))

    def test_a_database_that_ran_the_old_directory_migration_needs_nothing_and_gets_no_directory_tables(self):
        # The old router kept the directory tables out of the application database: it ran
        # addresses 0001 there and created addresses_importrequest only. The new 0001 differs in the
        # directory tables only (they are created in the SQLite file, from scratch), so a database
        # with the old record has nothing to apply for them (D-075, security review, L-10). The
        # column of the repair block is added to the queue table by its own migration 0002 (N-2),
        # which is why 0001 is never edited again.
        from django.db.migrations.recorder import MigrationRecorder

        self.assertTrue(MigrationRecorder(connection).migration_qs.filter(
            app="addresses", name="0001_initial").exists())
        tables = connection.introspection.table_names()
        self.assertIn("addresses_importrequest", tables)
        self.assertFalse({"addresses_directory", "addresses_address"} & set(tables))
        columns = {column.name for column in connection.introspection.get_table_description(
            connection.cursor(), "addresses_importrequest")}
        # Every column of the model: the migrations 0002 to 0006 added the later ones to the queue table. The
        # columns of the first versions are named so that a dropped one is noticed.
        self.assertEqual(columns, {field.column for field in ImportRequest._meta.concrete_fields})
        self.assertLessEqual({
            "id", "state", "requested_at", "started_at", "finished_at",
            "last_automatic_attempt_at", "is_automatic", "last_failed_repair_at",
            # Progress and last error of the import in the web process (migration 0003, D-090).
            "claim_token", "heartbeat_at", "phase", "bytes_done", "bytes_total", "rows_done", "last_error",
        }, columns)

        output = self.run_command()

        self.assertIn("Database migrations: none pending.", output)
        self.assertFalse({"addresses_directory", "addresses_address"} & set(connection.introspection.table_names()))
        self.assertIn(
            "addresses_directory", connections[DIRECTORY_ALIAS].introspection.table_names(),
        )

    def test_an_invalid_email_address_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command(email="not an address")
        self.assertFalse(User.objects.exists())

    def run_separately(self, email=EMAIL):
        """Run the command; return what it wrote to standard output and standard error."""
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ, {EMAIL_VARIABLE: email, PASSWORD_VARIABLE: INITIAL_PASSWORD}):
            call_command("setup_instance", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def assert_no_administrator_was_created(self, users):
        self.assertEqual(User.objects.count(), users)
        self.assertFalse(User.objects.filter(is_superuser=True).exists())
        self.assertFalse(SecurityEvent.objects.filter(event="initial_administrator_created").exists())

    def test_accounts_without_an_administrator_create_nothing_and_are_reported_loudly(self):
        # A restored backup, a manual change or an old environment file must never create a
        # privileged account later (D-076, security review, M-2). The start is not stopped.
        create_active_user("person@example.org")
        for _run in range(2):
            out, err = self.run_separately()
            self.assertIn("NONE EXISTS, but accounts exist", err)
            self.assertIn("not a first start", err)
            self.assertIn("emergency_access", err)
            self.assertIn("setup_initial_administrator", err)
            self.assertIn(f"Remove {PASSWORD_VARIABLE} from the environment file now.", err)
            self.assertNotIn("created", out)
            self.assertNotIn(INITIAL_PASSWORD, out + err)
            # Nothing else of the setup is skipped: the start goes on (exit status 0).
            self.assertIn("Database migrations: none pending.", out)
            self.assertIn("Address directory:", out)
            self.assert_no_administrator_was_created(users=1)

    def test_an_email_that_belongs_to_an_account_does_not_fail_the_start_or_change_the_account(self):
        person = create_active_user(EMAIL)
        before = (person.pk, person.password, person.is_superuser, person.is_staff, person.status)
        out, err = self.run_separately()
        self.assertIn("NONE EXISTS, but accounts exist", err)
        self.assertNotIn(INITIAL_PASSWORD, out + err)
        person.refresh_from_db()
        self.assertEqual((person.pk, person.password, person.is_superuser, person.is_staff, person.status), before)
        self.assertFalse(person.check_password(INITIAL_PASSWORD))
        self.assert_no_administrator_was_created(users=1)

    def test_every_kind_of_existing_account_counts(self):
        person = create_active_user("person@example.org")
        for status in ("blocked", "deactivated"):
            with self.subTest(status=status), connection.cursor() as cursor:
                cursor.execute("UPDATE accounts_user SET status = %s, is_active = 0", [status])
                out, err = self.run_separately()
                self.assertIn("NONE EXISTS, but accounts exist", err)
                self.assert_no_administrator_was_created(users=1)
        self.assertEqual(User.objects.get().pk, person.pk)

    def test_accounts_without_an_administrator_and_without_variables_are_reported_too(self):
        create_active_user("person@example.org")
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ):
            os.environ.pop(EMAIL_VARIABLE, None)
            os.environ.pop(PASSWORD_VARIABLE, None)
            call_command("setup_instance", stdout=out, stderr=err)
        self.assertIn("NONE EXISTS, but accounts exist", err.getvalue())
        self.assertNotIn("Remove", err.getvalue())
        # The advice of an empty database ("set the variables and start again") would be wrong here.
        self.assertNotIn("start again", out.getvalue() + err.getvalue())
        self.assert_no_administrator_was_created(users=1)

    def test_a_database_error_while_creating_the_administrator_shows_only_its_class(self):
        handler = RecordingHandler()
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        leaking = OperationalError(f"server said: {INITIAL_PASSWORD} {EMAIL}")
        out, err = StringIO(), StringIO()
        try:
            with mock.patch.object(setup_instance, "create_initial_administrator", side_effect=leaking):
                with mock.patch.dict(os.environ, {EMAIL_VARIABLE: EMAIL, PASSWORD_VARIABLE: INITIAL_PASSWORD}):
                    with self.assertRaises(CommandError) as raised:
                        call_command("setup_instance", stdout=out, stderr=err)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        self.assertIn("OperationalError", str(raised.exception))
        # The original exception is neither chained nor shown in a traceback.
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        for text in (str(raised.exception), out.getvalue(), err.getvalue(), *handler.messages):
            self.assertNotIn(INITIAL_PASSWORD, text)
            self.assertNotIn(EMAIL, text)
        self.assertEqual(self.lock_is_free(), 1)
        self.assertFalse(User.objects.exists())

    def test_nothing_secret_is_printed_or_logged(self):
        handler = RecordingHandler()
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with self.assertRaises(CommandError) as raised:
                self.run_command(password=WEAK_PASSWORD)
            outputs = [str(raised.exception), self.run_command(), self.run_command()]
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        for text in (*outputs, *handler.messages):
            self.assertNotIn(INITIAL_PASSWORD, text)
            self.assertNotIn(WEAK_PASSWORD, text)
        user = User.objects.get()
        self.assertNotIn(INITIAL_PASSWORD, user.password)
        for entry in SecurityEvent.objects.all():
            self.assertNotIn(INITIAL_PASSWORD, repr(entry.__dict__))

    def test_there_is_no_command_line_argument_for_the_password(self):
        parser = setup_instance.Command().create_parser("manage.py", "setup_instance")
        options = {option for action in parser._actions for option in action.option_strings}
        self.assertFalse([option for option in options if "pass" in option or "email" in option])
        # Django's parser reports an unknown argument as a CommandError (CommandParser.error).
        with self.assertRaisesMessage(CommandError, "unrecognized arguments"):
            parser.parse_args(["--password", INITIAL_PASSWORD])
        with self.assertRaisesMessage(CommandError, "unrecognized arguments"):
            parser.parse_args([INITIAL_PASSWORD])
        with self.assertRaisesMessage(CommandError, "unrecognized arguments"):
            call_command("setup_instance", "--password", INITIAL_PASSWORD, stdout=StringIO())
        self.assertFalse(User.objects.exists())

    def test_a_failing_migration_stops_before_the_administrator(self):
        with mock.patch.object(setup_instance, "call_command", side_effect=CommandError("broken")):
            with self.assertRaisesMessage(CommandError, "broken"):
                self.run_command()
        self.assertFalse(User.objects.exists())

    def test_the_lock_is_released_after_a_failure(self):
        with self.assertRaises(CommandError):
            self.run_command(password=WEAK_PASSWORD)
        self.assertEqual(self.lock_is_free(), 1)
        self.run_command()
        self.assertEqual(self.lock_is_free(), 1)

    @staticmethod
    def lock_is_free():
        with connection.cursor() as cursor:
            cursor.execute("SELECT IS_FREE_LOCK(%s)", [setup_instance.setup_lock_name(connection)])
            return cursor.fetchone()[0]

    def test_a_run_waits_for_a_running_setup_and_gives_up_after_the_timeout(self):
        name = setup_instance.setup_lock_name(connection)
        holding, release, done = threading.Event(), threading.Event(), threading.Event()

        def hold():
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT GET_LOCK(%s, 10)", [name])
                    holding.set()
                    release.wait(timeout=30)
                    cursor.execute("SELECT RELEASE_LOCK(%s)", [name])
            finally:
                connections.close_all()
                done.set()

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(holding.wait(timeout=10))
            with mock.patch.object(setup_instance, "SETUP_LOCK_TIMEOUT", 1):
                with self.assertRaisesMessage(CommandError, "Another setup"):
                    self.run_command()
            self.assertFalse(User.objects.exists())
        finally:
            release.set()
            thread.join(timeout=30)
        self.assertTrue(done.is_set())
        self.run_command()
        self.assertEqual(User.objects.count(), 1)

    def test_parallel_starts_create_exactly_one_administrator(self):
        count = 4
        barrier = threading.Barrier(count)
        results = [None] * count

        def start(index):
            try:
                barrier.wait(timeout=10)
                out = StringIO()
                call_command("setup_instance", stdout=out, stderr=StringIO())
                results[index] = out.getvalue()
            except Exception as error:  # reported by the assertions below
                results[index] = error
            finally:
                connections.close_all()

        with mock.patch.dict(os.environ, {EMAIL_VARIABLE: EMAIL, PASSWORD_VARIABLE: INITIAL_PASSWORD}):
            threads = [threading.Thread(target=start, args=(index,)) for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)

        for result in results:
            self.assertIsInstance(result, str, result)
            self.assertNotIn(INITIAL_PASSWORD, result)
        self.assertEqual(sum("System administrator: created" in result for result in results), 1)
        self.assertEqual(sum("exists; no account was created" in result for result in results), count - 1)
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(SecurityEvent.objects.filter(event="initial_administrator_created").count(), 1)

    def test_the_first_login_requires_a_new_password_and_then_the_second_factor(self):
        self.run_command()
        client = Client()
        client.post(LOGIN_URL, {"username": EMAIL, "password": INITIAL_PASSWORD})
        password_change = reverse("accounts:password_change")
        for url in (PROFILE_URL, reverse("admin:index"), reverse("accounts:two_factor_setup")):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    response["Location"] == password_change or response["Location"].startswith(LOGIN_URL),
                    response["Location"],
                )
        changed = client.post(
            password_change,
            {
                "current_password": INITIAL_PASSWORD,
                "new_password1": NEW_PASSWORD,
                "new_password2": NEW_PASSWORD,
            },
        )
        self.assertEqual(changed.status_code, 302)
        user = User.objects.get()
        self.assertFalse(user.password_change_required)
        self.assertEqual(client.get(PROFILE_URL)["Location"], reverse("accounts:login_method"))
        self.assertEqual(client.get(reverse("admin:index")).status_code, 302)


@contextlib.contextmanager
def directory_file(path):
    """Point the alias of the address directory at ``path`` (a file that may not exist yet)."""
    alias = connections[DIRECTORY_ALIAS]
    alias.close()
    with mock.patch.dict(alias.settings_dict, {"NAME": str(path)}):
        try:
            yield alias
        finally:
            alias.close()


@tag("mariadb")
class SetupInstanceDirectoryTests(TransactionTestCase):
    """The SQLite file of the address directory (D-075) is prepared by the same command, last and
    never fatal: the main database and the administrator do not depend on it."""

    databases = {"default", "addresses"}

    def run_command(self):
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ, {EMAIL_VARIABLE: EMAIL, PASSWORD_VARIABLE: INITIAL_PASSWORD}):
            call_command("setup_instance", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_a_first_start_creates_the_file_and_only_the_directory_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state" / "address-directory.sqlite3"
            with directory_file(path) as alias:
                self.assertFalse(path.exists())

                output, errors = self.run_command()

                self.assertTrue(path.is_file())
                self.assertEqual(
                    # sqlite_stat1: the planner statistics that migration 0020 (and every import) writes with ANALYZE.
                    set(alias.introspection.table_names()) - {"sqlite_stat1"},
                    {"django_migrations", "addresses_address", "addresses_directory", "addresses_plant",
                     "addresses_plantregister", "addresses_transformerstation",
                     # The lookup tables of the address suggestions (migration 0020, H).
                     "addresses_addressplace", "addresses_addressstreet"},
                )
                job = ImportRequest.objects.get(pk=1)
                self.assertEqual(job.state, ImportRequest.State.QUEUED)
                self.assertIsNotNone(job.requested_at)
        self.assertEqual(errors, "")
        self.assertIn(f"Address directory: file prepared, {addresses_migration_count()} migration(s) applied.", output)
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)

    def test_a_second_run_changes_nothing_in_the_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "address-directory.sqlite3"
            with directory_file(path):
                self.run_command()
                Directory.objects.create(pk=1, row_count=7, checksum="a" * 64)
                # Scanned data lives in the same file and must survive every start without a schema change.
                TransformerStation.objects.create(
                    egid=1, postal_code="3063", name="Station 1", match="exact", scanned_at=timezone.now(),
                )
                ImportRequest.objects.filter(pk=1).update(state=ImportRequest.State.SUCCEEDED)
                before = ImportRequest.objects.values().get(pk=1)

                with mock.patch.object(directory_setup, "_remove_catalogue") as remove:
                    output, errors = self.run_command()

                remove.assert_not_called()
                self.assertEqual(TransformerStation.objects.get(egid=1).name, "Station 1")
                self.assertEqual(errors, "")
                self.assertIn("Address directory: file ready, none pending.", output)
                self.assertEqual(Directory.objects.get(pk=1).row_count, 7)
                self.assertEqual(ImportRequest.objects.values().get(pk=1), before)

    def test_missing_tables_of_an_existing_file_are_created_again(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "address-directory.sqlite3"
            with directory_file(path) as alias:
                self.run_command()
                call_command("migrate", "addresses", "zero", database=DIRECTORY_ALIAS, verbosity=0)
                self.assertNotIn("addresses_directory", alias.introspection.table_names())

                output, _errors = self.run_command()

                self.assertIn(f"file prepared, {addresses_migration_count()} migration(s) applied", output)
                self.assertIn("addresses_directory", alias.introspection.table_names())

    def test_an_empty_current_file_does_not_requeue_a_failed_import_on_each_start(self):
        with tempfile.TemporaryDirectory() as folder, directory_file(Path(folder) / "directory.sqlite3"):
            self.run_command()
            ImportRequest.objects.filter(pk=1).update(
                state=ImportRequest.State.FAILED, last_error="Official download failed.",
            )
            before = ImportRequest.objects.values().get(pk=1)
            output, errors = self.run_command()
            self.assertEqual(errors, "")
            self.assertNotIn("import queued", output)
            self.assertEqual(ImportRequest.objects.values().get(pk=1), before)

    def test_a_directory_path_that_cannot_be_used_is_reported_and_does_not_stop_the_administrator(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "blocker"
            blocker.write_text("a file, not a folder")
            with directory_file(blocker / "address-directory.sqlite3"):
                output, errors = self.run_command()
        self.assertIn("Address directory: NOT set up (OperationalError)", errors)
        self.assertNotIn(folder, output + errors)
        self.assertIn("Database migrations:", output)
        self.assertIn("System administrator: created", output)
        user = User.objects.get()
        self.assertTrue(user.is_superuser and user.password_change_required)

    def test_a_failing_directory_migration_is_reported_without_detail_and_the_run_succeeds(self):
        real = call_command

        def fail_for_the_directory(*args, **options):
            if options.get("database") == DIRECTORY_ALIAS:
                raise RuntimeError("detail that must not be shown /secret/path")
            return real(*args, **options)

        with tempfile.TemporaryDirectory() as folder, directory_file(Path(folder) / "directory.sqlite3"):
            with mock.patch.object(directory_setup, "call_command", side_effect=fail_for_the_directory):
                output, errors = self.run_command()
        self.assertIn("Address directory: NOT set up (RuntimeError)", errors)
        self.assertNotIn("detail that must not be shown", output + errors)
        self.assertNotIn("/secret/path", output + errors)
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)

    def test_a_refused_administrator_still_stops_the_run(self):
        with mock.patch.dict(os.environ, {EMAIL_VARIABLE: EMAIL, PASSWORD_VARIABLE: WEAK_PASSWORD}):
            with self.assertRaisesMessage(CommandError, "not accepted"):
                call_command("setup_instance", stdout=StringIO(), stderr=StringIO())
        self.assertFalse(User.objects.exists())

    def test_pending_catalogue_migration_discards_rows_and_queues_once_without_changing_application_data(self):
        with tempfile.TemporaryDirectory() as folder, directory_file(Path(folder) / "directory.sqlite3") as alias:
            self.run_command()
            call_command("migrate", "addresses", "0006", database=DIRECTORY_ALIAS, verbosity=0)
            # Raw insert: the model has columns (content_changed_at) that the table at migration 0006 lacks.
            with alias.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO addresses_directory (id, generation, row_count, checksum, has_egid) VALUES (1, %s, 7, %s, 0)",
                    [uuid.uuid4().hex, "a" * 64],
                )
            ImportRequest.objects.filter(pk=1).update(
                state=ImportRequest.State.FAILED, last_error="Official download failed.",
                bytes_done=123, rows_done=12,
            )
            user = User.objects.values().get()
            events = list(SecurityEvent.objects.values())
            tables = connection.introspection.table_names()
            with mock.patch("leg_platform.addresses.importer.import_directory") as download:
                output, errors = self.run_command()
                requested_at = ImportRequest.objects.get(pk=1).requested_at
                self.run_command()
            download.assert_not_called()
            self.assertEqual(errors, "")
            self.assertIn("import queued", output)
            self.assertFalse(Directory.objects.exists())
            with alias.cursor() as cursor:
                self.assertIn("addr_generation_locality", alias.introspection.get_constraints(
                    cursor, "addresses_address"))
            job = ImportRequest.objects.get(pk=1)
            self.assertEqual((job.state, job.requested_at), (ImportRequest.State.QUEUED, requested_at))
            self.assertEqual(User.objects.values().get(), user)
            self.assertEqual(list(SecurityEvent.objects.values()), events)
            self.assertEqual(connection.introspection.table_names(), tables)

    def test_missing_index_with_current_migration_records_also_rebuilds(self):
        with tempfile.TemporaryDirectory() as folder, directory_file(Path(folder) / "directory.sqlite3") as alias:
            self.run_command()
            Directory.objects.create(pk=1, row_count=7)
            with alias.cursor() as cursor:
                cursor.execute("DROP INDEX addr_generation_locality")
            output, errors = self.run_command()
            self.assertEqual(errors, "")
            self.assertIn("import queued", output)
            self.assertFalse(Directory.objects.exists())

    def test_only_application_model_operations_do_not_trigger_a_reset(self):
        migration = migrations.Migration("application_only", "addresses")
        migration.operations = [
            migrations.AddField("importrequest", "test_field", models.IntegerField(null=True)),
            migrations.CreateModel("BuildingLookup", []),
        ]
        self.assertFalse(directory_setup._changes_directory(migration))
        migration.operations = [migrations.AddField("address", "test_field", models.IntegerField(null=True))]
        self.assertTrue(directory_setup._changes_directory(migration))
        with tempfile.TemporaryDirectory() as folder, directory_file(Path(folder) / "directory.sqlite3"):
            self.run_command()
            Directory.objects.create(pk=1, row_count=7)
            migration.operations = [migrations.AddField("importrequest", "test_field", models.IntegerField(null=True))]
            real_executor = directory_setup.MigrationExecutor(connections[DIRECTORY_ALIAS])
            with mock.patch.object(directory_setup, "MigrationExecutor", return_value=real_executor):
                with mock.patch.object(real_executor, "migration_plan", return_value=[(migration, False)]):
                    with mock.patch.object(directory_setup, "_remove_catalogue") as remove:
                        output, errors = self.run_command()
            remove.assert_not_called()
            self.assertEqual(errors, "")
            self.assertNotIn("import queued", output)
            self.assertEqual(Directory.objects.get(pk=1).row_count, 7)

    def test_live_job_is_not_reset_or_replaced_but_an_expired_job_is_requeued(self):
        from datetime import timedelta
        from django.utils import timezone
        with tempfile.TemporaryDirectory() as folder, directory_file(Path(folder) / "directory.sqlite3"):
            self.run_command()
            call_command("migrate", "addresses", "0006", database=DIRECTORY_ALIAS, verbosity=0)
            old_token = uuid.uuid4()
            ImportRequest.objects.filter(pk=1).update(
                state=ImportRequest.State.RUNNING, started_at=timezone.now(), claim_token=old_token,
            )
            before = ImportRequest.objects.values().get(pk=1)
            with mock.patch.object(directory_setup, "_remove_catalogue") as remove:
                _output, errors = self.run_command()
            self.assertIn("NOT set up (OperationalError)", errors)
            remove.assert_not_called()
            self.assertEqual(ImportRequest.objects.values().get(pk=1), before)
            ImportRequest.objects.filter(pk=1).update(started_at=timezone.now() - timedelta(hours=7))
            _output, errors = self.run_command()
            self.assertEqual(errors, "")
            self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.QUEUED)
            from leg_platform.addresses import jobs
            jobs.record_failure(jobs.Claim(old_token, False, False), RuntimeError("late old holder"))
            self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.QUEUED)
    def test_unlink_failure_is_nonfatal_and_leaves_directory_unavailable_until_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            with directory_file(path) as alias:
                self.run_command()
                call_command("migrate", "addresses", "0006", database=DIRECTORY_ALIAS, verbosity=0)
                before = ImportRequest.objects.values().get(pk=1)
                with mock.patch.object(Path, "unlink", side_effect=PermissionError("private detail")):
                    output, errors = self.run_command()
                self.assertIn("NOT set up (PermissionError)", errors)
                self.assertNotIn("private detail", output + errors)
                self.assertTrue(path.exists())
                self.assertTrue(reset_marker(path).exists())
                self.assertEqual(ImportRequest.objects.values().get(pk=1), before)
                with self.assertRaises(OperationalError):
                    Directory.objects.exists()
                _output, errors = self.run_command()
                self.assertEqual(errors, "")
                self.assertFalse(reset_marker(path).exists())
                self.assertIn("addresses_directory", alias.introspection.table_names())

    def test_open_reader_refuses_reset_nonfatally_then_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            with directory_file(path):
                self.run_command()
                call_command("migrate", "addresses", "0006", database=DIRECTORY_ALIAS, verbosity=0)
                reader = sqlite3.connect(path)
                reader.execute("SELECT * FROM addresses_directory").fetchall()
                try:
                    output, errors = self.run_command()
                    self.assertIn("NOT set up (OperationalError)", errors)
                    self.assertNotIn("import queued", output)
                    self.assertTrue(path.exists())
                finally:
                    reader.close()
                _output, errors = self.run_command()
                self.assertEqual(errors, "")

    def test_queue_failure_does_not_publish_partial_setup_and_retry_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            with directory_file(path):
                with mock.patch.object(directory_setup.jobs, "request_import", side_effect=OperationalError("private")):
                    _output, errors = self.run_command()
                self.assertIn("NOT set up (OperationalError)", errors)
                self.assertTrue(reset_marker(path).exists())
                self.assertFalse(ImportRequest.objects.exists())
                self.assertTrue(User.objects.filter(is_superuser=True).exists())
                _output, errors = self.run_command()
                self.assertEqual(errors, "")
                self.assertFalse(reset_marker(path).exists())
                self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.QUEUED)

    def test_reset_marker_blocks_retained_and_new_connections_but_not_application_queries(self):
        from leg_platform.addresses import jobs
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            with directory_file(path) as alias:
                self.run_command()
                reset_marker(path).touch()
                self.assertIsNone(jobs.claim())
                self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.QUEUED)
                with self.assertRaises(OperationalError):
                    Directory.objects.exists()
                alias.close()
                with self.assertRaises(OperationalError):
                    Directory.objects.exists()
                self.assertTrue(User.objects.filter(is_superuser=True).exists())
                reset_marker(path).unlink()
                self.assertFalse(Directory.objects.exists())

    def test_the_committed_queue_waits_until_schema_publication_before_becoming_claimable(self):
        from leg_platform.addresses import jobs
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            real_unlink = Path.unlink
            checked = []

            def unlink(candidate, **kwargs):
                if candidate == reset_marker(path):
                    # The transaction has committed, but consumers must leave the queue alone.
                    self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.QUEUED)
                    self.assertIsNone(jobs.claim())
                    checked.append(True)
                return real_unlink(candidate, **kwargs)

            with directory_file(path), mock.patch.object(Path, "unlink", unlink):
                _output, errors = self.run_command()
                self.assertEqual(errors, "")
                self.assertEqual(checked, [True])
                self.assertIsNotNone(jobs.claim())

    def test_opening_a_connection_cannot_overlap_the_reset_opening_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            with directory_file(path) as alias:
                self.run_command()
                alias.close()
                with directory_open_guard(path):
                    with self.assertRaises(OperationalError):
                        Directory.objects.exists()
                self.assertFalse(Directory.objects.exists())

    def _assert_paused_cursor_refuses_reset(self, *, replace_file):
        """Pause after cursor creation, then resume SQL only after reset begins/replacement."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            with directory_file(path) as alias:
                self.run_command()
                options = {**alias.settings_dict, "NAME": str(path)}
                alias.close()
                ready, resume = threading.Event(), threading.Event()
                outcomes = []

                def use_retained_cursor():
                    database = directory_setup.DatabaseWrapper(options, alias=DIRECTORY_ALIAS)
                    try:
                        with database.cursor() as cursor:
                            # Also prove Django's mapping and generator parameter conversion
                            # still works through the subclass before the reset starts.
                            cursor.execute("SELECT %(value)s", {"value": 7})
                            outcomes.append(cursor.fetchone())
                            cursor.execute("CREATE TABLE cursor_probe (value integer)")
                            cursor.executemany(
                                "INSERT INTO cursor_probe VALUES (%s)", ((value,) for value in (1, 2)),
                            )
                            ready.set()
                            if not resume.wait(timeout=15):
                                raise RuntimeError("The reset test did not resume the cursor.")
                            for query, params, many in (
                                ("SELECT * FROM cursor_probe", None, False),
                                ("INSERT INTO cursor_probe VALUES (%s)", [3], False),
                                ("INSERT INTO cursor_probe VALUES (%s)", [(4,), (5,)], True),
                            ):
                                try:
                                    if many:
                                        cursor.executemany(query, params)
                                    else:
                                        cursor.execute(query, params)
                                except OperationalError:
                                    outcomes.append("refused")
                                else:
                                    outcomes.append("unexpected execution")
                    except Exception as error:
                        outcomes.append(error)
                    finally:
                        ready.set()
                        database.close()

                thread = threading.Thread(target=use_retained_cursor)
                thread.start()
                try:
                    self.assertTrue(ready.wait(timeout=15))
                    self.assertEqual(outcomes, [(7,)])
                    if replace_file:
                        # The supported reset refuses a live WAL reader. Simulate a POSIX
                        # replacement anyway to exercise the retained cursor's identity guard.
                        replacement = Path(folder) / "replacement.sqlite3"
                        with contextlib.closing(sqlite3.connect(replacement)) as database:
                            with database:
                                database.execute("CREATE TABLE cursor_probe (value integer)")
                                database.execute("INSERT INTO cursor_probe VALUES (99)")
                        replacement.replace(path)
                    else:
                        reset_marker(path).touch()
                finally:
                    resume.set()
                    thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcomes, [(7,), "refused", "refused", "refused"])
                if replace_file:
                    # This bypasses setup's WAL coordination. The old connection is closed
                    # now; discard only its orphaned fixture sidecars before opening the
                    # replacement (https://www.sqlite.org/howtocorrupt.html, section 2.5).
                    for suffix in ("-wal", "-shm"):
                        Path(str(path) + suffix).unlink(missing_ok=True)
                    with contextlib.closing(sqlite3.connect(path)) as database:
                        self.assertEqual(database.execute("SELECT * FROM cursor_probe").fetchall(), [(99,)])
                else:
                    reset_marker(path).unlink()
                    with contextlib.closing(sqlite3.connect(path)) as database:
                        self.assertEqual(database.execute("SELECT * FROM cursor_probe").fetchall(), [(1,), (2,)])

    def test_cursor_created_before_reset_refuses_execution_after_marker_appears(self):
        self._assert_paused_cursor_refuses_reset(replace_file=False)

    def test_cursor_created_before_replacement_refuses_stale_reads_and_writes(self):
        if os.name != "posix":
            self.skipTest("Windows refuses replacing an open SQLite file; marker regression runs on both platforms.")
        self._assert_paused_cursor_refuses_reset(replace_file=True)

    def _assert_parameter_consumption_excludes_reset(self, *, many):
        """Block inside SQL parameter consumption, after the execution guard was entered."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            with directory_file(path) as alias:
                self.run_command()
                # Require a real schema rebuild, rather than a no-op setup.
                with alias.cursor() as cursor:
                    cursor.execute("DROP INDEX addr_generation_locality")
                    cursor.execute("CREATE TABLE cursor_probe (value integer)")
                options = {**alias.settings_dict, "NAME": str(path)}
                alias.close()
                consuming, resume = threading.Event(), threading.Event()
                outcomes = []

                def pause():
                    consuming.set()
                    if not resume.wait(timeout=15):
                        raise RuntimeError("The parameter test did not resume SQL execution.")

                def parameters():
                    yield (1,)
                    pause()
                    yield (2,)

                class DelayedSequence:
                    def __len__(self):
                        return 1

                    def __getitem__(self, index):
                        if index != 0:
                            raise IndexError(index)
                        pause()
                        return 1

                def execute():
                    database = directory_setup.DatabaseWrapper(options, alias=DIRECTORY_ALIAS)
                    try:
                        with database.cursor() as cursor:
                            if many:
                                cursor.executemany("INSERT INTO cursor_probe VALUES (%s)", parameters())
                            else:
                                cursor.execute("INSERT INTO cursor_probe VALUES (%s)", DelayedSequence())
                        outcomes.append("executed")
                    except Exception as error:
                        outcomes.append(error)
                    finally:
                        consuming.set()
                        database.close()

                thread = threading.Thread(target=execute)
                thread.start()
                try:
                    self.assertTrue(consuming.wait(timeout=15))
                    self.assertEqual(outcomes, [])
                    # Try the real reset entry point while the generator/sequence is paused.
                    # It must fail nonblockingly before any marker, unlink or replacement.
                    with mock.patch.object(directory_setup, "_remove_catalogue") as remove:
                        with self.assertRaises(OSError):
                            directory_setup.prepare_directory(stdout=StringIO(), stderr=StringIO())
                    remove.assert_not_called()
                    self.assertFalse(reset_marker(path).exists())
                    self.assertTrue(path.exists())
                finally:
                    resume.set()
                    thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcomes, ["executed"])
                with alias.cursor() as cursor:
                    cursor.execute("SELECT value FROM cursor_probe ORDER BY value")
                    self.assertEqual(cursor.fetchall(), [(1,), (2,)] if many else [(1,)])
                # With parameter consumption finished and the writer closed, retry really
                # rebuilds the catalogue and cannot inherit any writes into the old file.
                _pending, queued = directory_setup.prepare_directory(stdout=StringIO(), stderr=StringIO())
                self.assertTrue(queued)
                self.assertNotIn("cursor_probe", alias.introspection.table_names())
                self.assertFalse(reset_marker(path).exists())

    def test_blocking_executemany_generator_prevents_reset_until_execution_finishes(self):
        self._assert_parameter_consumption_excludes_reset(many=True)

    def test_blocking_execute_parameter_sequence_prevents_reset_until_execution_finishes(self):
        self._assert_parameter_consumption_excludes_reset(many=False)

    def test_retained_posix_connection_refuses_a_replaced_file(self):
        if os.name != "posix":
            self.skipTest("Windows refuses unlinking an open database; inode replacement is a POSIX case.")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            with directory_file(path):
                self.run_command()
                # Simulate replacement outside the backend, on disposable test files only.
                replacement = Path(folder) / "replacement.sqlite3"
                replacement.touch()
                replacement.replace(path)
                with self.assertRaises(OperationalError):
                    Directory.objects.exists()


class CatalogueRemovalTests(SimpleTestCase):
    """File removal is narrowly scoped; the MariaDB job/account data is never a target."""

    def test_unrelated_and_state_only_migrations_do_not_require_catalogue_removal(self):
        migration = migrations.Migration("metadata_only", "addresses")
        migration.operations = [
            migrations.AlterModelOptions("address", {"verbose_name": "Address"}),
            migrations.AlterModelManagers("directory", []),
            migrations.RunPython(migrations.RunPython.noop, hints={"model_name": "importrequest"}),
        ]
        self.assertFalse(directory_setup._changes_directory(migration))
        unrelated = migrations.Migration("application_only", "accounts")
        unrelated.operations = [migrations.AddField("user", "test_field", models.IntegerField(null=True))]
        self.assertFalse(directory_setup._changes_directory(unrelated))

    def test_only_the_configured_file_and_exact_sqlite_sidecars_are_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            removed = [path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
            unrelated = [Path(folder) / "application.sqlite3", Path(str(path) + "-wal.backup")]
            for candidate in [*removed, *unrelated]:
                candidate.write_bytes(b"disposable test data")
            alias = mock.MagicMock()
            alias.cursor.return_value.__enter__.return_value.fetchone.return_value = ("delete",)
            directory_setup._remove_catalogue(alias, path)
            self.assertTrue(all(not candidate.exists() for candidate in removed))
            self.assertTrue(all(candidate.exists() for candidate in unrelated))
            alias.close.assert_called_once()

    def test_a_symbolic_link_is_never_unlinked_as_a_catalogue(self):
        if os.name != "posix":
            self.skipTest("Creating symlinks needs extra privileges on Windows.")
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "application.sqlite3"
            original.write_bytes(b"protected")
            path = Path(folder) / "directory.sqlite3"
            path.symlink_to(original)
            with self.assertRaises(OperationalError):
                directory_setup._remove_catalogue(mock.MagicMock(), path)
            self.assertEqual(original.read_bytes(), b"protected")

    def test_the_opening_guard_is_nonblocking_and_is_released_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "directory.sqlite3"
            with directory_open_guard(path):
                with self.assertRaises(OSError):
                    with directory_open_guard(path):
                        self.fail("A second connection/reset entered the same opening guard.")
            with self.assertRaises(RuntimeError):
                with directory_open_guard(path):
                    raise RuntimeError("test failure")
            with directory_open_guard(path):
                pass
