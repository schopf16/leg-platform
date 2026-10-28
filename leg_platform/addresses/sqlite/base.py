"""SQLite backend that creates the directory file (mode 0600) and missing folders (0700) itself,
and refuses a symbolic link at the file path (D-075). Options come from ``storage.directory_database``."""

import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

from django.db import OperationalError
from django.db.backends.sqlite3 import base

from ..storage import directory_open_guard, is_rebuilding, reset_marker


class DirectoryCursorWrapper(base.SQLiteCursorWrapper):
    """Exclude reset through SQL execution, including lazy parameter consumption."""

    def execute(self, query, params=None):
        with self.directory_database.execution_guard():
            return super().execute(query, params)

    def executemany(self, query, param_list):
        with self.directory_database.execution_guard():
            return super().executemany(query, param_list)


class DatabaseWrapper(base.DatabaseWrapper):
    @contextmanager
    def execution_guard(self):
        database = str(self.settings_dict["NAME"])
        with ExitStack() as stack:
            if not database.startswith(("file:", ":memory:")) and not is_rebuilding():
                try:
                    stack.enter_context(directory_open_guard(Path(database), shared=True))
                except OSError:
                    raise OperationalError("The address directory is busy during setup or execution.") from None
            # Check inside the mutex, and keep it until Django and SQLite have consumed all
            # parameters and completed execute/executemany. A pre-check alone misses generators.
            # Setup's thread already owns the reset mutex, so it must not acquire it twice.
            self._check_available()
            yield

    def _check_available(self):
        database = str(self.settings_dict["NAME"])
        if database.startswith(("file:", ":memory:")):
            return
        path = Path(database)
        try:
            if not is_rebuilding() and reset_marker(path).exists():
                raise OSError
            if self.connection is not None and getattr(self, "_file_identity", None) is not None:
                info = path.stat()
                if (info.st_dev, info.st_ino) != self._file_identity:
                    raise OSError
        except OSError:
            raise OperationalError("The address directory is unavailable during a schema rebuild.") from None

    def create_cursor(self, name=None):
        # A retained connection must never query an old, unlinked inode after a rebuild.
        self._check_available()
        cursor = self.connection.cursor(factory=DirectoryCursorWrapper)
        cursor.directory_database = self
        return cursor

    def get_new_connection(self, conn_params):
        database = str(conn_params["database"])
        if database.startswith(("file:", ":memory:")) or is_rebuilding():
            return self._open_connection(conn_params)
        try:
            path = Path(database)
            self._make_folders(path.parent)
            with directory_open_guard(path, shared=True):
                return self._open_connection(conn_params)
        except OSError:
            raise OperationalError("The address directory cannot be opened during setup.") from None

    def _open_connection(self, conn_params):
        database = str(conn_params["database"])
        self._file_identity = None
        self._check_available()
        if not database.startswith(("file:", ":memory:")):
            path = Path(database)
            self._prepare_file(path)
            before = path.stat()
            # SQLite writes large temporary files (sorter, transient indexes, VACUUM) to the first folder of
            # PRAGMA temp_store_directory, SQLITE_TMPDIR, TMPDIR, /var/tmp, /usr/tmp, /tmp. Point them to the disk
            # of the directory file unless the operator set SQLITE_TMPDIR; the variable is read on the first temporary
            # file, so it must be set before the first connection (https://www.sqlite.org/tempfiles.html, section 5;
            # Unix only: Windows uses GetTempPath()).
            os.environ.setdefault("SQLITE_TMPDIR", str(path.parent))
        connection = super().get_new_connection(conn_params)
        try:
            if not database.startswith(("file:", ":memory:")):
                info = path.stat()
                if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                    raise OperationalError("The address directory changed while opening it.")
                self._file_identity = (info.st_dev, info.st_ino)
            # Covers a connection opening concurrently with creation of the reset marker.
            self._check_available()
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _make_folders(folder):
        """Create missing folders, each mode 0700 (``mkdir(parents=True)`` applies ``mode`` to the last only)."""
        missing = []
        while not folder.exists() and folder != folder.parent:
            missing.append(folder)
            folder = folder.parent
        for created in reversed(missing):
            created.mkdir(mode=0o700, exist_ok=True)

    @staticmethod
    def _prepare_file(path):
        try:
            DatabaseWrapper._make_folders(path.parent)
            if path.is_symlink():
                raise OSError("The directory file is a symbolic link.")
            try:
                # O_EXCL: create or fail, never open an existing file (or a link created meanwhile).
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                # Narrow a broader mode; O_NOFOLLOW refuses a link that appeared after the check.
                if os.name == "posix":
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
                            os.fchmod(descriptor, 0o600)
                    finally:
                        os.close(descriptor)
            else:
                os.close(descriptor)
        except OSError:
            # A database error so lookup/status treat it as an unavailable directory; no path in the message.
            raise OperationalError("The address directory file cannot be prepared.") from None
