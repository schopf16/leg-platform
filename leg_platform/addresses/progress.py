"""Progress of a running import (D-090): plain counters that the importer sets and the heartbeat of
the runner copies into the ``ImportRequest`` row for the status page. No database access here."""

from django.utils import timezone

PHASES = ("starting", "catalogue", "downloading", "importing", "publishing", "finishing")


class Progress:
    """Written by the import thread, read by the heartbeat thread (integers and strings: no lock needed).

    ``check`` (optional, set by the runner) is called at every change of the phase: it raises when the job was taken
    over meanwhile, so an old thread stops before the next expensive step (L-1 of the review of 2026-10-17)."""

    def __init__(self, check=None):
        self.current_phase = "starting"
        self.bytes_done = None
        self.bytes_total = None
        self.rows_done = None
        # Whether a download happened; without it a successful import means "already up to date".
        self.downloaded = False
        self.check = check
        # The time of the last change of any value above: the status shows "last progress N minutes ago" (D-121).
        self.changed_at = None

    def phase(self, name):
        if self.check is not None:
            self.check()
        self.current_phase = name
        self.changed_at = timezone.now()

    def download(self, done, total):
        self.bytes_done = done
        self.bytes_total = total
        self.changed_at = timezone.now()

    def rows(self, count):
        self.rows_done = count
        self.changed_at = timezone.now()
