"""Set up an instance on every start: migrate the database, create the first administrator.

``manage.py setup_instance`` is idempotent and safe to run on every start of the services
(docs/DECISIONS.md, D-076; the systemd unit ``leg-setup.service`` of docs/DEPLOYMENT.md does it):

1. It applies the pending migrations to the default database (the equivalent of
   ``migrate --noinput``). It never flushes or deletes data.
2. Only if the account table is completely empty (a first start; no row of any status) and
   ``BOOTSTRAP_SYSTEM_ADMIN_EMAIL`` is set, it creates the initial system administrator with the
   password of the environment variable ``BOOTSTRAP_SYSTEM_ADMIN_PASSWORD``, through the locked
   ``User.objects.create_superuser()`` path. The password is checked with the project's password
   validators, is never printed or logged, and there is no command line argument for it (it would
   show up in the process list). The account must replace the password at the first login and set
   up two-factor authentication before anything else (D-041). If accounts exist but no system
   administrator (any account with is_superuser, also a blocked or deactivated one, counts as
   one), it creates nothing, writes a clear error to standard error and still exits with 0: a
   restored backup, a manual change or an old environment file must never create a privileged
   account later, and a missing administrator is not a reason to stop the web service.
3. As soon as a system administrator exists, the password variable is not read at all; a still-set
   variable only produces a reminder to remove it from the environment file.
4. It creates the SQLite file of the address directory if it is missing and applies the migrations
   of the directory tables to it (``migrate addresses --database=addresses``). This step runs
   last and is never fatal: the directory is optional (D-066) and not backed up (D-075), so a
   failure (for example a state folder that cannot be written) is reported on standard error and
   the next start tries again; the main database and the administrator are not affected.

A named database lock serialises concurrent runs, so two starts can neither migrate at the same
time nor create two administrators.

Exit status: 0 when the instance is set up (also when only the directory step failed, and when
accounts exist without an administrator, which is reported on standard error); non-zero when
migrating the main database fails or when the first administrator is configured (email variable
set, empty account table) but cannot be created, so that a service that requires the unit does
not start with a half-configured instance.
"""

import hashlib
import os

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections
from django.db.migrations.executor import MigrationExecutor

from leg_platform.accounts.initial_administrator import (
    InitialAdministratorError,
    SystemAdministratorExists,
    accounts_exist,
    create_initial_administrator,
    system_administrator_exists,
)
from leg_platform.addresses.storage import DIRECTORY_ALIAS, mark_writer
from leg_platform.environment import (
    BOOTSTRAP_ADMIN_EMAIL_VARIABLE,
    BOOTSTRAP_ADMIN_PASSWORD_VARIABLE,
    parse_bootstrap_administrator,
)

DATABASE = "default"
# Seconds a second run waits for the first one (migrations of a large update can take a while).
SETUP_LOCK_TIMEOUT = 600


def setup_lock_name(connection):
    """The MariaDB named lock of the setup; MariaDB limits lock names to 64 characters."""
    digest = hashlib.sha256(str(connection.settings_dict["NAME"]).encode()).hexdigest()[:32]
    return f"leg.setup_instance.{digest}"


class Command(BaseCommand):
    help = (
        "Apply pending migrations and, on a first start (the account table is empty), create the "
        f"initial administrator from {BOOTSTRAP_ADMIN_EMAIL_VARIABLE} and "
        f"{BOOTSTRAP_ADMIN_PASSWORD_VARIABLE}. Idempotent; safe to run on every start."
    )

    def handle(self, *args, **options):
        # This command creates the directory file and its tables, so it is a writer of the file;
        # every other process opens it read-only (D-075).
        mark_writer()
        # Read before the lock; nothing of it is printed, logged or stored.
        bootstrap = parse_bootstrap_administrator(os.environ)
        connection = connections[DATABASE]
        lock_name = setup_lock_name(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s)", [lock_name, SETUP_LOCK_TIMEOUT])
            (acquired,) = cursor.fetchone()
        if acquired != 1:
            raise CommandError("Another setup of this instance is running; try again later.")
        try:
            # Progressive output: a refusal later on must not hide what was already done.
            for line in self._migrate(connection):
                self.stdout.write(line)
            for line in self._administrator(bootstrap):
                self.stdout.write(line)
            # Last, and never fatal: the directory is optional (D-066, D-075).
            self._directory()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", [lock_name])

    def _migrate(self, connection):
        executor = MigrationExecutor(connection)
        pending = len(executor.migration_plan(executor.loader.graph.leaf_nodes()))
        call_command("migrate", database=DATABASE, interactive=False, verbosity=0,
                     stdout=self.stdout, stderr=self.stderr)
        if pending:
            return [f"Database migrations: {pending} applied."]
        return ["Database migrations: none pending."]

    def _directory(self):
        """Create the SQLite file of the address directory and its two tables (D-075).

        The file is not part of the backup and is rebuilt by an import, so a failure here is
        reported (standard error, with the exception class only: no path, no detail) but never
        stops the start: the application works without suggestions, and the next run tries again.
        The named lock of the application database also serialises this step between two runs;
        SQLite's own lock and ``busy_timeout`` protect the file against any other writer.
        """
        try:
            connection = connections[DIRECTORY_ALIAS]
            executor = MigrationExecutor(connection)  # opens, and so creates, the file
            pending = len(executor.migration_plan(executor.loader.graph.leaf_nodes("addresses")))
            call_command("migrate", "addresses", database=DIRECTORY_ALIAS, interactive=False,
                         verbosity=0, stdout=self.stdout, stderr=self.stderr)
        except Exception as error:  # noqa: BLE001 - optional step, reported below
            self.stderr.write(
                f"Address directory: NOT set up ({type(error).__name__}). Address suggestions "
                "are unavailable until this is fixed (check that the state folder of the service "
                "is writable); the rest of the instance is set up. The next start tries again."
            )
            return
        if pending:
            self.stdout.write(f"Address directory: file prepared, {pending} migration(s) applied.")
        else:
            self.stdout.write("Address directory: file ready, none pending.")

    def _administrator(self, bootstrap):
        if system_administrator_exists():
            lines = ["System administrator: exists; no account was created."]
            if bootstrap.password:
                lines.append(self._reminder())
            return lines
        if accounts_exist():
            # Not a first start (D-076, security review, M-2): the bootstrap account is created
            # only while the account table is completely empty, so that a restored backup, a
            # manual change or an old environment file can never create a privileged account
            # later. The start is not stopped: a missing administrator is not a reason to take
            # the web service down.
            self.stderr.write(self._not_a_first_start(bool(bootstrap.password)))
            return []
        if not bootstrap.email:
            if bootstrap.password:
                raise CommandError(
                    f"{BOOTSTRAP_ADMIN_PASSWORD_VARIABLE} is set but "
                    f"{BOOTSTRAP_ADMIN_EMAIL_VARIABLE} is not; no administrator was created."
                )
            return [
                "System administrator: none exists and "
                f"{BOOTSTRAP_ADMIN_EMAIL_VARIABLE} is not set; no account was created. Set the "
                f"variables {BOOTSTRAP_ADMIN_EMAIL_VARIABLE} and {BOOTSTRAP_ADMIN_PASSWORD_VARIABLE} "
                "and start again, or run 'manage.py setup_initial_administrator'."
            ]
        if not bootstrap.password:
            raise CommandError(
                f"{BOOTSTRAP_ADMIN_EMAIL_VARIABLE} is set but {BOOTSTRAP_ADMIN_PASSWORD_VARIABLE} "
                "is not; no administrator was created."
            )
        try:
            user = create_initial_administrator(bootstrap.email, bootstrap.password)
        except SystemAdministratorExists:
            return ["System administrator: exists; no account was created."]
        except InitialAdministratorError as error:
            raise CommandError(f"The initial administrator was not created. {error}") from None
        except DatabaseError as error:
            # Only the class: the text of a database error is never shown here. The account may
            # exist already when the failure came after its creation (the security log entry is
            # written after the commit; accepted, D-076), so the text does not claim otherwise.
            raise CommandError(
                "The setup of the initial administrator failed "
                f"(database error: {type(error).__name__}); check whether the account exists."
            ) from None
        return [
            f"System administrator: created (internal number {user.pk}). The password must be "
            "changed and two-factor authentication set up at the first login.",
            self._reminder(),
        ]

    @staticmethod
    def _not_a_first_start(password_is_set):
        message = (
            "System administrator: NONE EXISTS, but accounts exist, so this is not a first "
            "start. Nothing was created; the bootstrap variables "
            f"{BOOTSTRAP_ADMIN_EMAIL_VARIABLE} and {BOOTSTRAP_ADMIN_PASSWORD_VARIABLE} are "
            "ignored (an account is created from them only while the account table is "
            "completely empty). The instance has no administrator until one is restored: "
            "restore a backup that contains the administrator, or, as a deliberate action of "
            "the operator, run 'manage.py setup_initial_administrator' (interactive password). "
            "'manage.py emergency_access' unblocks an account or resets its second factor, but "
            "does not give an account administrator rights."
        )
        if password_is_set:
            message += f" Remove {BOOTSTRAP_ADMIN_PASSWORD_VARIABLE} from the environment file now."
        return message

    @staticmethod
    def _reminder():
        return (
            f"Remove {BOOTSTRAP_ADMIN_PASSWORD_VARIABLE} from the environment file now; it is "
            "not used any more."
        )
