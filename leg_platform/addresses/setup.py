"""Disposable catalogue schema setup; never transfers historical public address rows (D-075/D-076)."""

import os
import shutil
import stat
from pathlib import Path

from django.core.management import call_command
from django.db import OperationalError, connections, router, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.operations.models import AlterModelManagers, AlterModelOptions
from django.db.migrations.operations.special import RunPython, RunSQL, SeparateDatabaseAndState

from . import jobs, published
from .models import (
    Address, AddressPlace, AddressStreet, Directory, ImportRequest, Plant, PlantRegister, TransformerStation,
)
from .sqlite.base import DatabaseWrapper
from .storage import DIRECTORY_ALIAS, directory_open_guard, rebuilding_directory, reset_marker

# The models of the directory file (``router.DIRECTORY_MODELS`` names the same ones for the router).
DIRECTORY_MODEL_CLASSES = (Address, AddressPlace, AddressStreet, Directory, Plant, PlantRegister, TransformerStation)


def _changes_directory(migration):
    """Use operation routing, not the app label: ImportRequest/BuildingLookup live in MariaDB."""
    def relevant(operation):
        if isinstance(operation, (AlterModelManagers, AlterModelOptions)):
            return False  # State-only operations: their database_forwards() is a no-op.
        if isinstance(operation, SeparateDatabaseAndState):
            return any(relevant(child) for child in operation.database_operations)
        if isinstance(operation, (RunPython, RunSQL)):
            return router.allow_migrate(DIRECTORY_ALIAS, migration.app_label, **operation.hints)
        return migration.app_label == "addresses" and any(
            operation.references_model(model._meta.model_name, "addresses")
            for model in DIRECTORY_MODEL_CLASSES
        )

    return any(relevant(operation) for operation in migration.operations)


def _schema_matches(connection):
    """Also detect missing tables, columns or declared indexes despite recorded migrations."""
    with connection.cursor() as cursor:
        tables = connection.introspection.table_names(cursor)
        for model in DIRECTORY_MODEL_CLASSES:
            table = model._meta.db_table
            if table not in tables:
                return False
            columns = {column.name for column in connection.introspection.get_table_description(cursor, table)}
            if columns != {field.column for field in model._meta.local_fields}:
                return False
            indexes = connection.introspection.get_constraints(cursor, table)
            for index in model._meta.indexes:
                expected = [model._meta.get_field(name.lstrip("-")).column for name in index.fields]
                if index.name not in indexes or indexes[index.name]["columns"] != expected:
                    return False
            # Unique constraints too: without them an INSERT OR IGNORE of the lookup tables would insert duplicates.
            for constraint in model._meta.constraints:
                fields = getattr(constraint, "fields", None)
                if not fields:
                    continue
                expected = [model._meta.get_field(name).column for name in fields]
                found = indexes.get(constraint.name)
                if found is None or not found["unique"] or found["columns"] != expected:
                    return False
    return True


# What an in-place migration needs besides the file itself: the new indexes and tables (about a third of the file), the
# same again for the WAL while it runs, and sorter files in the same folder.
IN_PLACE_FILE_SHARE = 0.35
IN_PLACE_MARGIN = 256 * 1024 * 1024


def _in_place_space_needed(path):
    try:
        return int(path.stat().st_size * IN_PLACE_FILE_SHARE) * 2 + IN_PLACE_MARGIN
    except OSError:
        return 0


def _require_no_live_import():
    """Locks the import claim (call inside a transaction); an import that is alive keeps its claim and file."""
    ImportRequest.objects.get_or_create(pk=1)
    job = ImportRequest.objects.select_for_update().get(pk=1)
    if jobs.is_alive(job):
        raise OperationalError("An address directory import is still running.")


def _apply_in_place(connection, path, stdout, stderr):
    """Apply the additive migrations (``Migration.in_place``) to the existing file: with the exclusive opening mutex,
    only while no import is alive, and only with enough free disk. Without space the migration is SKIPPED with a message
    (setup goes on; the lookups answer "unavailable" until it runs). An incomplete schema afterwards is an ERROR: the
    populated file (stations, plants, addresses) is never wiped by this path."""
    needed = _in_place_space_needed(path)
    free = shutil.disk_usage(path.parent).free
    if free < needed:
        stderr.write(
            f"The address directory migration was skipped: it needs about {needed // 2**20} MB free in {path.parent} "
            f"({free // 2**20} MB are free). Free the space and run setup_instance again.\n"
        )
        return
    try:
        with directory_open_guard(path):
            with transaction.atomic(using="default"):
                _require_no_live_import()
                call_command("migrate", "addresses", database=DIRECTORY_ALIAS, interactive=False,
                             verbosity=0, stdout=stdout, stderr=stderr)
    except OSError as error:
        raise OperationalError("The address directory is busy: the migration was not applied.") from error
    if not _schema_matches(connection):
        raise OperationalError(
            "The address directory schema is incomplete after the in-place migration; the file was left as it is. "
            "Check the migration output, or rebuild the file on purpose (docs/INSTALLATION.md)."
        )


def _remove_catalogue(connection, path):
    """Refuse busy WAL files before unlinking; never follow a link or remove another database."""
    paths = [path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
    for candidate in paths:
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OperationalError("The address directory reset path is not a private regular file.")
    if path.exists():
        # All supported directory connections use WAL. Leaving WAL requires exclusive access;
        # another open reader/writer makes this fail. The marker refuses new application cursors.
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout = 0")
            cursor.execute("PRAGMA journal_mode = DELETE")
            if cursor.fetchone()[0].lower() != "delete":
                raise OperationalError("The address directory is busy.")
            cursor.execute("BEGIN EXCLUSIVE")
            cursor.execute("ROLLBACK")
    connection.close()
    # Windows also refuses unlinking an open SQLite file. On POSIX retained connections are
    # rejected by the backend's file-identity check, not allowed to continue on an old inode.
    for candidate in paths:
        candidate.unlink(missing_ok=True)


def prepare_directory(*, stdout, stderr):
    """Prepare the current schema and durably queue a first/replacement import; no network or thread."""
    connection = connections[DIRECTORY_ALIAS]
    path = Path(connection.settings_dict["NAME"])
    if not path.is_absolute() or connection.vendor != "sqlite":
        raise OperationalError("The address directory requires an absolute SQLite file path.")
    application = connections["default"]
    if application.vendor == "sqlite" and path.resolve() == Path(application.settings_dict["NAME"]).resolve():
        raise OperationalError("The address directory must not be the application database.")
    marker = reset_marker(path)
    fresh = not path.exists()
    with rebuilding_directory():
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes("addresses"))
        # A migration of the directory rebuilds the file and queues a full import, except an ADDITIVE one that keeps the
        # rows (``Migration.in_place = True``: new tables and indexes built from the existing addresses). Those are
        # applied to the existing file; only when the schema is still incomplete afterwards the file is rebuilt.
        pending = [migration for migration, _backwards in plan if _changes_directory(migration)]
        rebuild = not fresh and (
            marker.exists() or any(not getattr(migration, "in_place", False) for migration in pending)
        )
        if not (fresh or rebuild):
            if pending:
                _apply_in_place(connection, path, stdout, stderr)
                return len(plan), False
            call_command("migrate", "addresses", database=DIRECTORY_ALIAS, interactive=False,
                         verbosity=0, stdout=stdout, stderr=stderr)
            if _schema_matches(connection):
                return len(plan), False
            # No pending migration but a schema that does not match the models: the existing rule, rebuild.
            rebuild = True

        # Serialise against every queue claimant, not just other setup commands. A live import
        # keeps its claim and file; stale/failed jobs are requeued through the existing API.
        with directory_open_guard(path):
            with transaction.atomic(using="default"):
                _require_no_live_import()
                DatabaseWrapper._make_folders(path.parent)
                descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600) if not marker.exists() else None
                if descriptor is not None:
                    os.close(descriptor)
                if marker.is_symlink() or not marker.is_file():
                    raise OperationalError("The address directory reset marker is invalid.")
                if rebuild:
                    _remove_catalogue(connection, path)
                    published.clear()  # the snapshot of the published figures belongs to the removed generation (D-132)
                    executor = MigrationExecutor(connection)
                    plan = executor.migration_plan(executor.loader.graph.leaf_nodes("addresses"))
                call_command("migrate", "addresses", database=DIRECTORY_ALIAS, interactive=False,
                             verbosity=0, stdout=stdout, stderr=stderr)
                if not _schema_matches(connection):
                    raise OperationalError("The address directory schema is incomplete.")
                jobs.request_import()
                # An expired holder must not finish late and overwrite the replacement queue.
                # Keep its heartbeat as claim()'s existing stale-lease/takeover marker.
                ImportRequest.objects.filter(pk=1, state=ImportRequest.State.QUEUED).update(claim_token=None)
        # Release the opening mutex before making the committed queue claimable. Workers
        # check the marker under the same job-row lock and leave the request queued meanwhile.
        # A failure leaves the marker for the next setup to recover.
        marker.unlink()
        return len(plan), True
