"""Where and how the address directory is stored (D-075).

The directory is an own SQLite file, the alias ``addresses``, that both the web process (reads) and
the worker process (writes) open with the same settings. This module has no import of any model, so
that ``settings.py`` can use it.

* ``directory_database`` builds the entry of ``DATABASES`` for a file path.
* Every connection to the directory is read-only (``PRAGMA query_only``) unless the process has
  declared itself a writer with ``mark_writer``. Only the worker (``process_address_imports``),
  the operator commands (``import_address_directory``, ``seed_local_address_directory``,
  ``setup_instance``) and ``migrate`` (the project's override in this app) call it. The web
  processes (``leg_platform.wsgi`` and ``leg_platform.asgi``) never do, so a new entry point or
  command that forgets the call cannot write by accident (D-075, security review, L-1). This is
  a protection against a mistake in the code, not a protection against a compromised web
  process: both processes run as the same service user and the same user may write the file.
"""

from django.db.backends.signals import connection_created

DIRECTORY_ALIAS = "addresses"
ENGINE = "leg_platform.addresses.sqlite"
# Seconds that a connection waits for a lock that another connection holds (the worker's chunks
# are short; a lookup waits at most this long).
BUSY_TIMEOUT_SECONDS = 20

_writer = False


def mark_writer():
    """Declare that this process may write the directory (the importer, an operator command,
    ``migrate``). Call it before the first directory connection of the process is opened; a
    connection that is already open keeps the mode it was opened with."""
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
            # The wait for a lock; the same value as PRAGMA busy_timeout below.
            "timeout": BUSY_TIMEOUT_SECONDS,
            # A write transaction takes the write lock when it starts (and waits for it), instead
            # of failing later when a read transaction tries to become a write transaction.
            "transaction_mode": "IMMEDIATE",
            # WAL: a reader never blocks the writer and the writer never blocks a reader.
            # NORMAL is the usual pair of WAL (a power loss can lose the last commits, never
            # corrupt the file; the import is repeatable).
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
