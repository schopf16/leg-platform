"""Location and settings of the address directory SQLite file (D-075); imports no model, so ``settings.py`` can use it.

Connections are read-only (``PRAGMA query_only``) unless the process called ``mark_writer`` (the
worker, operator commands, ``migrate``). This guards against code mistakes, not against a
compromised web process (same service user).
"""

import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

from django.db.backends.signals import connection_created

DIRECTORY_ALIAS = "addresses"
ENGINE = "leg_platform.addresses.sqlite"
# Seconds a connection waits for a lock held by another connection.
BUSY_TIMEOUT_SECONDS = 20

_writer = False
# A web process stays read-only, except for the one thread that runs an import (D-090): the flag is
# thread-local, and Django opens one connection per thread, so only that thread's connection writes.
_thread = threading.local()


def mark_writer():
    """Declare that this process may write the directory; call before the first directory connection."""
    global _writer
    _writer = True


def mark_thread_writer():
    """Declare that the calling thread (an import thread of the web process) may write the directory.

    Call it before the thread's first directory connection and ``unmark_thread_writer`` (with a
    close of the thread's connections) when the thread ends.
    """
    _thread.writer = True


def unmark_thread_writer():
    _thread.writer = False


def is_writer():
    return _writer or getattr(_thread, "writer", False) is True


def reset_marker(path):
    """A failed or in-progress schema rebuild makes only the directory unavailable."""
    return Path(str(path) + ".reset")


@contextmanager
def rebuilding_directory():
    """Only setup's thread may open the directory while its reset marker exists."""
    previous = getattr(_thread, "rebuilding", False)
    _thread.rebuilding = True
    try:
        yield
    finally:
        _thread.rebuilding = previous


def is_rebuilding():
    return getattr(_thread, "rebuilding", False)


@contextmanager
def directory_open_guard(path):
    """An OS mutex for connection opening/SQL execution versus reset, not reader lifetime.

    Nonblocking: contention makes the optional directory unavailable, never delays startup.
    SQL execution holds it through parameter consumption, including a lazy generator.
    Closing the descriptor releases the lock, including after a killed process. Keep the
    empty lock file in place so concurrent callers always lock the same inode.
    """
    lock = Path(str(path) + ".open-lock")
    if lock.is_symlink():
        raise OSError("The directory opening lock is a symbolic link.")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("The directory opening lock is not a private regular file.")
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def directory_database(path):
    """The ``DATABASES`` entry of the directory file at ``path`` (an absolute path)."""
    return {
        "ENGINE": ENGINE,
        "NAME": path,
        "OPTIONS": {
            "timeout": BUSY_TIMEOUT_SECONDS,
            # Take the write lock when a write transaction starts instead of failing on upgrade.
            "transaction_mode": "IMMEDIATE",
            # WAL + NORMAL: readers and writer do not block each other; a power loss can lose the
            # last commits but never corrupt the file.
            "init_command": (
                "PRAGMA journal_mode = WAL; "
                "PRAGMA synchronous = NORMAL; "
                f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}"
            ),
        },
    }


def _make_connection_read_only(sender, connection, **kwargs):
    if connection.alias == DIRECTORY_ALIAS and not is_writer():
        connection.connection.execute("PRAGMA query_only = ON")


connection_created.connect(_make_connection_read_only, dispatch_uid="addresses-read-only")
