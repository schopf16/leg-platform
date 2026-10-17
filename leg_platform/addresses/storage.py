"""Location and settings of the address directory SQLite file (D-075); imports no model, so ``settings.py`` can use it.

Connections are read-only (``PRAGMA query_only``) unless the process called ``mark_writer`` (the
worker, operator commands, ``migrate``). This guards against code mistakes, not against a
compromised web process (same service user).
"""

from django.db.backends.signals import connection_created

DIRECTORY_ALIAS = "addresses"
ENGINE = "leg_platform.addresses.sqlite"
# Seconds a connection waits for a lock held by another connection.
BUSY_TIMEOUT_SECONDS = 20

_writer = False


def mark_writer():
    """Declare that this process may write the directory; call before the first directory connection."""
    global _writer
    _writer = True


def is_writer():
    return _writer


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
    if connection.alias == DIRECTORY_ALIAS and not _writer:
        connection.connection.execute("PRAGMA query_only = ON")


connection_created.connect(_make_connection_read_only, dispatch_uid="addresses-read-only")
