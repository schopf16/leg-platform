"""SQLite backend that creates the directory file (mode 0600) and missing folders (0700) itself,
and refuses a symbolic link at the file path (D-075). Options come from ``storage.directory_database``."""

import os
import stat
from pathlib import Path

from django.db import OperationalError
from django.db.backends.sqlite3 import base


class DatabaseWrapper(base.DatabaseWrapper):
    def get_new_connection(self, conn_params):
        database = str(conn_params["database"])
        if not database.startswith(("file:", ":memory:")):
            self._prepare_file(Path(database))
        return super().get_new_connection(conn_params)

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
