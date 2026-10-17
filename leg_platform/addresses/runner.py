"""Runs the address directory import in a background thread of the web process (D-090).

The operator needs no extra service. The thread is started after the job was claimed in the
database (``jobs.claim``: one import at a time over all web workers, a dead one is taken over when
its heartbeat is old). It is a daemon thread: a restart or a recycled worker ends it, the job is then
marked as interrupted (``atexit`` where the interpreter still runs it, otherwise by the next claim),
and the live directory stays as it was (a new generation is invisible until it is complete).

Only this thread writes the directory file (``storage.mark_thread_writer``); the request threads of
the process stay read-only. The thread has its own database connections and closes them at the end.
"""

import atexit
import logging
import threading
import time

from django.conf import settings
from django.db import DatabaseError, connections
from django.utils import timezone

from . import jobs, storage
from .models import ImportRequest
from .progress import Progress

logger = logging.getLogger(__name__)

# How often the heartbeat writes the progress; the page polls about as often.
HEARTBEAT_INTERVAL = 2.0
# The automatic check runs at most this often per web process (one claim attempt, a row lock).
AUTOMATIC_CHECK_INTERVAL = 300.0

_lock = threading.Lock()
_running = {}  # claim token -> Thread, of this process only (tests and ``atexit``)
_next_automatic_check = 0.0


class Heartbeat(threading.Thread):
    """Copies the progress into the job row until stopped; its own connection, only that one row."""

    def __init__(self, token, progress):
        super().__init__(name="address-import-heartbeat", daemon=True)
        self.token = token
        self.progress = progress
        self.stopped = threading.Event()

    def beat(self):
        ImportRequest.objects.filter(
            pk=1, claim_token=self.token, state=ImportRequest.State.RUNNING
        ).update(
            heartbeat_at=timezone.now(), phase=self.progress.current_phase[:16],
            bytes_done=self.progress.bytes_done, bytes_total=self.progress.bytes_total,
            rows_done=self.progress.rows_done,
        )

    def run(self):
        try:
            while not self.stopped.wait(HEARTBEAT_INTERVAL):
                try:
                    self.beat()
                except DatabaseError:
                    logger.warning("The address directory import heartbeat could not be written.")
        finally:
            connections.close_all()

    def stop(self):
        self.stopped.set()
        self.join(timeout=HEARTBEAT_INTERVAL * 3)


def _claim_check(token):
    """A function that raises ``ImportFailure`` when this thread no longer holds the job (taken over after a stale
    heartbeat). A database that does not answer is no reason to stop: the heartbeat copes with that as well."""

    def check():
        from .importer import ImportFailure

        try:
            held = ImportRequest.objects.filter(
                pk=1, claim_token=token, state=ImportRequest.State.RUNNING
            ).exists()
        except DatabaseError:
            return
        if not held:
            raise ImportFailure("The import lease was lost.")

    return check


def _run(claimed):
    """The thread body: run, record the outcome, close the connections. Never raises."""
    storage.mark_thread_writer()
    progress = Progress(check=_claim_check(claimed.token))
    heartbeat = Heartbeat(claimed.token, progress)
    try:
        heartbeat.start()
        if claimed.took_over:
            # The process that held the lease is gone (its heartbeat stopped): free it (D-090).
            from .importer import clear_stale_lease

            clear_stale_lease()
        jobs.execute(claimed, progress)
    except Exception:  # noqa: BLE001 - recorded by ``jobs.execute``; here only the log line with the trace
        logger.exception("The address directory import thread ended with an error.")
    finally:
        heartbeat.stop()
        with _lock:
            _running.pop(claimed.token, None)
        storage.unmark_thread_writer()
        connections.close_all()


def _start(claimed):
    thread = threading.Thread(target=_run, args=(claimed,), name="address-import", daemon=True)
    with _lock:
        _running[claimed.token] = thread
    try:
        thread.start()
    except RuntimeError:
        with _lock:
            _running.pop(claimed.token, None)
        jobs.record_failure(claimed, RuntimeError())
        return False
    return True


def start_manual():
    """Queue and start an import (the button): True if started, False if one is queued or running already."""
    if not jobs.request_import():
        return False
    claimed = jobs.claim(web=True)
    if claimed is None:
        return False
    return _start(claimed)


def maybe_start_automatic():
    """Start the automatic refresh if it is due (D-090); cheap and safe to call on every authenticated page.

    A scheduler is not needed: the first request of a signed-in person after the interval has passed
    starts it (90 days, 24 hours while the directory is missing or empty; the attempt is stored before
    the download, so a failed one is not repeated early). Turned off with ``ADDRESS_IMPORT_AUTOMATIC``.
    """
    global _next_automatic_check
    if not settings.ADDRESS_IMPORT_AUTOMATIC:
        return False
    now = time.monotonic()
    with _lock:
        if now < _next_automatic_check:
            return False
        _next_automatic_check = now + AUTOMATIC_CHECK_INTERVAL
    try:
        claimed = jobs.claim(schedule_automatic=True, web=True)
        if claimed is None:
            return False
        return _start(claimed)
    except Exception:  # noqa: BLE001 - a page must never fail because of the refresh
        logger.warning("The automatic address directory import check failed.", exc_info=True)
        return False


def is_running_here():
    """Whether this process has an import thread (tests)."""
    with _lock:
        return any(thread.is_alive() is True for thread in _running.values())


def wait_for_end(timeout=30.0):
    """Wait until this process has no import thread (tests); True if none is left."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running_here():
            return True
        time.sleep(0.05)
    return not is_running_here()


@atexit.register
def _mark_interrupted():
    """At the end of the process: a job that this process still runs is interrupted (best effort)."""
    with _lock:
        tokens = [token for token, thread in _running.items() if thread.is_alive() is True]
    for token in tokens:
        try:
            ImportRequest.objects.filter(
                pk=1, claim_token=token, state=ImportRequest.State.RUNNING
            ).update(
                state=ImportRequest.State.FAILED, finished_at=timezone.now(), phase="",
                last_error=jobs.INTERRUPTED,
            )
        except Exception:  # noqa: BLE001 - the next claim finds the stale heartbeat anyway
            pass
