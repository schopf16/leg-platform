"""Disposable catalogue schema setup; never transfers historical public address rows (D-075/D-076)."""

import os
import stat
from pathlib import Path

from django.core.management import call_command
from django.db import OperationalError, connections, router, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.operations.models import AlterModelManagers, AlterModelOptions
from django.db.migrations.operations.special import RunPython, RunSQL, SeparateDatabaseAndState

from . import jobs
from .models import Address, Directory, ImportRequest
from .sqlite.base import DatabaseWrapper
from .storage import DIRECTORY_ALIAS, directory_open_guard, rebuilding_directory, reset_marker


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
            for model in (Address, Directory)
        )

    return any(relevant(operation) for operation in migration.operations)


def _schema_matches(connection):
    """Also detect missing tables, columns or declared indexes despite recorded migrations."""
    with connection.cursor() as cursor:
        tables = connection.introspection.table_names(cursor)
        for model in (Address, Directory):
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
    return True


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
        rebuild = not fresh and (
            marker.exists() or any(_changes_directory(migration) for migration, _backwards in plan)
            or not _schema_matches(connection)
        )
        if not (fresh or rebuild):
            call_command("migrate", "addresses", database=DIRECTORY_ALIAS, interactive=False,
                         verbosity=0, stdout=stdout, stderr=stderr)
            return len(plan), False

        # Serialise against every queue claimant, not just other setup commands. A live import
        # keeps its claim and file; stale/failed jobs are requeued through the existing API.
        with directory_open_guard(path):
            with transaction.atomic(using="default"):
                ImportRequest.objects.get_or_create(pk=1)
                job = ImportRequest.objects.select_for_update().get(pk=1)
                if jobs.is_alive(job):
                    raise OperationalError("An address directory import is still running.")
                DatabaseWrapper._make_folders(path.parent)
                descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600) if not marker.exists() else None
                if descriptor is not None:
                    os.close(descriptor)
                if marker.is_symlink() or not marker.is_file():
                    raise OperationalError("The address directory reset marker is invalid.")
                if rebuild:
                    _remove_catalogue(connection, path)
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
