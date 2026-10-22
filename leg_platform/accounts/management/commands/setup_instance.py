"""Set up an instance on every start: migrate the database, create the first administrator (D-076).

``manage.py setup_instance`` is idempotent and safe to run on every start (``leg-setup.service``):

1. Applies pending application migrations (like ``migrate --noinput``); never deletes application data.
2. Only if the account table is completely empty and ``BOOTSTRAP_SYSTEM_ADMIN_EMAIL`` is set, creates
   the initial system administrator with ``BOOTSTRAP_SYSTEM_ADMIN_PASSWORD`` via the locked
   ``User.objects.create_superuser()`` path (validated, never printed or logged, no command line
   argument); it must change the password and set up 2FA at first login (D-041). If accounts exist
   but no system administrator (any superuser, also blocked or deactivated, counts), it creates
   nothing, writes an error to stderr and exits 0: a restored backup or old environment file must
   never create a privileged account later.
3. Once an administrator exists, the password variable is not read; a still-set variable only
   produces a reminder.
4. Creates/rebuilds the disposable address SQLite schema and queues its import. Last and never
   fatal: the directory is optional (D-066) and not backed up (D-075).

A named database lock serialises concurrent runs. Exit status: 0 when set up (also if only the
directory step failed, or accounts exist without an administrator); non-zero when migrating fails or
the configured first administrator cannot be created.
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
from leg_platform.addresses.storage import mark_writer
from leg_platform.addresses.setup import prepare_directory
from leg_platform.environment import (
    BOOTSTRAP_ADMIN_EMAIL_VARIABLE,
    BOOTSTRAP_ADMIN_PASSWORD_VARIABLE,
    parse_bootstrap_administrator,
)

DATABASE = "default"
# Seconds a second run waits for the first one.
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
        # The only writer of the directory file; every other process opens it read-only (D-075).
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
            # Progressive output: a later refusal must not hide what was done.
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
        """Prepare the disposable SQLite schema and queue an asynchronous import (D-075).

        A failure is reported on stderr (exception class only) but never stops the start; the next run tries again.
        """
        try:
            pending, queued = prepare_directory(stdout=self.stdout, stderr=self.stderr)
        except Exception as error:  # noqa: BLE001 - optional step, reported below
            self.stderr.write(
                f"Address directory: NOT set up ({type(error).__name__}). Address suggestions "
                "are unavailable until this is fixed (check that the state folder of the service "
                "is writable and retry after directory readers/importers close); the rest of the "
                "instance is set up. The next start tries again."
            )
            return
        if queued:
            self.stdout.write(f"Address directory: file prepared, {pending} migration(s) applied.")
            self.stdout.write("Address directory: import queued; the worker or scheduled tasks will process it.")
        else:
            self.stdout.write("Address directory: file ready, none pending.")

    def _administrator(self, bootstrap):
        if system_administrator_exists():
            lines = ["System administrator: exists; no account was created."]
            if bootstrap.password:
                lines.append(self._reminder())
            return lines
        if accounts_exist():
            # Not a first start (D-076): the bootstrap account is created only while the account
            # table is empty; a missing administrator must not take the web service down.
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
            # Only the class: a database error's text is never shown. The account may already exist
            # (the log entry is written after the commit, D-076).
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
