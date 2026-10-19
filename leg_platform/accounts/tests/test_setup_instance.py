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
import tempfile
import threading
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection, connections
from django.test import Client, SimpleTestCase, TransactionTestCase, tag
from django.urls import reverse

from leg_platform.accounts.management.commands import setup_instance
from leg_platform.accounts.models import SecurityEvent, User
from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.addresses.models import Directory, ImportRequest
from leg_platform.addresses.storage import DIRECTORY_ALIAS
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
WEAK_PASSWORD = "short-sentinel"  # 14 characters: below the minimum of 15
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

    def test_the_minimum_length_of_fifteen_characters_applies(self):
        with self.assertRaisesMessage(CommandError, "at least 15 characters"):
            self.run_command(password="quartz-lantern")  # 14 characters
        self.assertFalse(User.objects.exists())
        self.run_command(password="quartz-lantern1")  # 15 characters
        self.assertTrue(User.objects.get().check_password("quartz-lantern1"))

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
        self.assertEqual(client.get(PROFILE_URL)["Location"], reverse("accounts:two_factor_setup"))
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

    def test_a_first_start_creates_the_file_and_only_the_two_directory_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state" / "address-directory.sqlite3"
            with directory_file(path) as alias:
                self.assertFalse(path.exists())

                output, errors = self.run_command()

                self.assertTrue(path.is_file())
                self.assertEqual(
                    set(alias.introspection.table_names()),
                    {"django_migrations", "addresses_address", "addresses_directory"},
                )
        self.assertEqual(errors, "")
        self.assertIn(f"Address directory: file prepared, {addresses_migration_count()} migration(s) applied.", output)
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)

    def test_a_second_run_changes_nothing_in_the_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "address-directory.sqlite3"
            with directory_file(path):
                self.run_command()
                Directory.objects.create(pk=1, row_count=7, checksum="a" * 64)

                output, errors = self.run_command()

                self.assertEqual(errors, "")
                self.assertIn("Address directory: file ready, none pending.", output)
                self.assertEqual(Directory.objects.get(pk=1).row_count, 7)

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

        with mock.patch.object(setup_instance, "call_command", side_effect=fail_for_the_directory):
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
