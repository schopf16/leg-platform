"""Location and settings of the address directory SQLite file (D-075); imports no model, so ``settings.py`` can use it.

Connections are read-only (``PRAGMA query_only``) unless the process called ``mark_writer`` (the
worker, operator commands, ``migrate``). This guards against code mistakes, not against a
compromised web process (same service user).
"""

import os
import stat
import threading
from ctypes import ArgumentError
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


_windows_api = None
_windows_api_lock = threading.Lock()


def _windows():
    """The ``LockFileEx`` / ``UnlockFileEx`` prototypes, defined ONCE on a private ``WinDLL`` (the shared ``windll`` objects
    are cached per process: setting ``argtypes`` on them per call raced between threads)."""
    global _windows_api
    with _windows_api_lock:
        if _windows_api is None:
            import ctypes
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t), ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.LockFileEx.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped),
            ]
            kernel32.LockFileEx.restype = wintypes.BOOL
            kernel32.UnlockFileEx.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped),
            ]
            kernel32.UnlockFileEx.restype = wintypes.BOOL
            _windows_api = (kernel32, Overlapped)
    return _windows_api


def _lock_shared_windows(handle):
    """A shared, nonblocking lock of the first byte (``LockFileEx``; ``msvcrt.locking`` has no shared mode).
    Conflicts with the exclusive ``msvcrt.locking`` lock of the reset; raises OSError when that is held."""
    import ctypes

    kernel32, overlapped = _windows()
    fail_immediately = 0x1
    if not kernel32.LockFileEx(handle, fail_immediately, 0, 1, 0, ctypes.byref(overlapped())):
        raise OSError("The directory opening lock is held.")


def _unlock_shared_windows(handle):
    import ctypes

    kernel32, overlapped = _windows()
    if not kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped())):
        raise OSError("The directory opening lock could not be released.")


@contextmanager
def directory_open_guard(path, *, shared=False):
    """An OS mutex for connection opening/SQL execution versus reset, not reader lifetime.

    Exclusive by default (the reset, setup). ``shared=True`` is for readers (connection opening, one SQL execution):
    any number of readers hold it at the same time, none while a reset holds it and the reset gets it only when no
    reader holds it. An exclusive lock for every reader made two lookups in parallel fail with "unavailable"
    (owner walkthrough 2026-10-28: the check overlapped the slow locality request).
    Nonblocking: contention makes the optional directory unavailable, never delays startup.
    SQL execution holds it through parameter consumption, including a lazy generator.
    The lock is released explicitly and, after a killed process, by the operating system when the descriptor is closed.
    Keep the empty lock file in place so concurrent callers always lock the same inode.
    Windows has no ``O_NOFOLLOW``: the symbolic-link check before ``os.open`` is not atomic there (the folder of the
    directory file is private to the service account, D-075); POSIX opens with ``O_NOFOLLOW`` and fixes the mode.
    """
    lock = Path(str(path) + ".open-lock")
    if lock.is_symlink():
        raise OSError("The directory opening lock is a symbolic link.")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("The directory opening lock is not a private regular file.")
        if os.name == "nt":
            import msvcrt

            try:
                if shared:
                    _lock_shared_windows(msvcrt.get_osfhandle(descriptor))
                else:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except (ValueError, ArgumentError, ImportError) as error:
                # A failure of the lock call itself is the same refusal as a held lock, never an unhandled 500.
                raise OSError("The directory opening lock could not be taken.") from error
        else:
            import fcntl

            # A lock file that was created with another mode (or by hand) is brought back to the private mode.
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        try:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        if shared:
                            _unlock_shared_windows(msvcrt.get_osfhandle(descriptor))
                        else:
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (OSError, ValueError, ArgumentError) as error:
                    # Like every other failure of the lock: a database error, not a raw OSError out of cursor.execute
                    # (the lookups catch DatabaseError). The operating system drops the lock with the descriptor below.
                    from django.db import OperationalError

                    raise OperationalError("The address directory lock could not be released.") from error
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
