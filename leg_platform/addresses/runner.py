"""The heartbeat and the lease check of a running import (D-090, D-133).

An import never runs in the web process (D-133): a recycled or restarted web worker would end it. The import
worker (``worker``, command ``process_import_queue``) and the scheduled tasks (``schedule``) claim a job in the
database (``jobs.claim``) and run it with ``execute_with_heartbeat``: a thread copies the progress into the job row
every few seconds, so that a killed holder is seen as dead after about 90 seconds (``jobs.HEARTBEAT_STALE_AFTER``)
and the next claim takes the job over. The thread has its own database connections and closes them at the end.
"""

import logging
import threading

from django.db import DatabaseError, connections
from django.utils import timezone

from . import jobs, published
from .models import ImportRequest
from .progress import Progress

logger = logging.getLogger(__name__)

# How often the heartbeat writes the progress; the page polls less often.
HEARTBEAT_INTERVAL = 2.0


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
            rows_done=self.progress.rows_done, progress_at=self.progress.changed_at,
            rows_total=self.progress.rows_total, progress_units=self.progress.units(),
            pending_data_date=self.progress.data_date, pending_rejected=self.progress.rejected,
            pending_skipped=self.progress.skipped,
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
    """A function that raises ``ImportFailure`` when this process no longer holds the job (taken over after a stale
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


def execute_with_heartbeat(claimed):
    """Run the claimed job with a heartbeat and record the outcome (``jobs.execute``); a failure is re-raised after it was
    recorded."""
    # The file is readable now and not yet written: keep a copy of the published figures for the page while the import
    # writes the file (``published``; a run before the first success of this version has none yet).
    published.refresh(claimed.source)
    progress = Progress(check=_claim_check(claimed.token))
    heartbeat = Heartbeat(claimed.token, progress)
    heartbeat.start()
    try:
        jobs.execute(claimed, progress)
    finally:
        heartbeat.stop()
