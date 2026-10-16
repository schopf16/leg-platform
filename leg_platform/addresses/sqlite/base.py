"""Django's SQLite backend, with a directory file that is created with mode 0600 (D-075).

Nothing else differs: the options (WAL, ``busy_timeout``, ``synchronous``, the transaction mode)
are given in ``DATABASES`` (``leg_platform.addresses.storage.directory_database``). The
backend creates the file itself before SQLite does, so that the file is readable and writable only
for the service user whatever the umask is (SQLite gives the ``-wal`` and ``-shm`` files the
permissions of the database file). It also creates every missing folder of the path, each as mode
0700 (on the servers the folder comes from ``StateDirectory=`` of the systemd unit). It refuses a
symbolic link at the file path (security review of D-075, L-4): a link could point the importer at
another file, and narrowing the mode would then change that file.
"""

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
        """Create the missing folders of ``folder``, every one of them as mode 0700.

        ``Path.mkdir(parents=True)`` applies ``mode`` only to the last folder; the others get the
        default mode (https://docs.python.org/3/library/pathlib.html#pathlib.Path.mkdir).
        """
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
                # O_EXCL: the file is created or the call fails, never an existing file (or a link
                # created in between) opened for creation.
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                # An existing file keeps its content; a broader mode is narrowed. Windows has no
                # such modes. O_NOFOLLOW: a link that appeared after the check above is refused
                # instead of followed.
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
            # A database error, so that the lookup and the status page treat it like any other
            # unavailable directory instead of failing the request. No path in the message.
            raise OperationalError("The address directory file cannot be prepared.") from None
