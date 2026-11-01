"""The import worker (D-133): runs the jobs of the durable import queue outside the web process.

``manage.py process_import_queue --loop`` is a supervised service like ``process_mail_queue``. The admin page only
enqueues (``jobs.enqueue``) and shows the status; this process claims the oldest entry (``jobs.claim(queued=True)``:
the one claim over all processes, D-090) and runs the jobs one after the other with a heartbeat. A web restart or a
recycled gunicorn worker no longer touches an import.

Stop (SIGTERM, SIGINT): a job that runs is interrupted at once, recorded with its reason (``jobs.STOPPED``) and put at
the front of the queue, so it runs again when the service starts. The directory in use stays (a new generation is
invisible until it is complete). A killed process leaves the job to the next claim, which finds the stale heartbeat
(``jobs.LEASE_EXPIRED``).
"""

import logging
import signal
import threading

from django.db import DatabaseError

from . import jobs, runner

logger = logging.getLogger(__name__)

# How often an idle worker looks at the queue (one cheap read; the claim runs only when something waits).
POLL_SECONDS = 5.0

stop_requested = threading.Event()
# Set only while a job runs: a stop signal then interrupts it. Outside it the signal only ends the loop, so that the
# bookkeeping of a claim or of a finished job is never cut in the middle.
_interruptible = threading.Event()


def request_stop(signum=None, frame=None):
    """Signal handler: ends the loop and interrupts the job that runs (``jobs.WorkerStopped``). A second signal while the
    job is being recorded only repeats the request."""
    stop_requested.set()
    if _interruptible.is_set():
        _interruptible.clear()
        raise jobs.WorkerStopped


def install_signal_handlers():
    """Route SIGTERM and SIGINT to ``request_stop``; only the main thread can do this."""
    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, request_stop)


def _next_queued():
    """The next job of the durable queue (D-132), claimed; None when nothing waits or the claim is refused."""
    try:
        if not jobs.queued_sources():
            return None
        return jobs.claim(web=True, queued=True)
    except DatabaseError:
        logger.warning("The import queue could not be read.")
        return None


def run_job(claimed):
    """Run one claimed job and record its outcome. A failure is recorded by ``jobs.execute`` and does not stop the worker."""
    if stop_requested.is_set():
        # Stopped between the claim and the start: nothing ran, the job goes back to the front of the queue.
        jobs.record_interruption(claimed, jobs.STOPPED, requeue=True)
        return
    _interruptible.set()
    try:
        runner.execute_with_heartbeat(claimed)
    except jobs.WorkerStopped:
        logger.warning("The import worker was stopped while the %s import ran.", claimed.source)
    except Exception:  # noqa: BLE001 - recorded by ``jobs.execute``; here only the log line with the trace
        logger.exception("The %s import ended with an error.", claimed.source)
    finally:
        _interruptible.clear()


def drain():
    """Run the waiting jobs one after the other until the queue is empty or a stop is requested; returns the number of
    jobs that ran."""
    ran = 0
    while not stop_requested.is_set():
        jobs.mark_worker_seen()
        claimed = _next_queued()
        if claimed is None:
            break
        ran += 1
        run_job(claimed)
    return ran


def run_loop(poll_seconds=POLL_SECONDS):
    """Work off the queue, then wait and look again, until a stop is requested."""
    while not stop_requested.is_set():
        drain()
        jobs.mark_worker_seen()
        stop_requested.wait(poll_seconds)
