"""Progress of a running import (D-090): plain counters that the importer sets and the heartbeat of
the runner copies into the ``ImportRequest`` row for the status page. No database access here."""

import zipfile

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
        # The data rows of the CSV that the run reads, once counted after the download; None when unknown (D-122).
        self.rows_total = None
        # Buildings per postal code: the total (the EGIDs with coordinates) and the done ones (transformer scan, D-122).
        self.buildings_total = {}
        self.buildings_done = {}

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

    def set_rows_total(self, total):
        """The rows of the CSV, counted once after the download; None when the count is not available."""
        self.rows_total = total
        self.changed_at = timezone.now()

    def set_buildings_total(self, totals):
        self.buildings_total = {str(code): int(count) for code, count in dict(totals).items()}
        self.changed_at = timezone.now()

    def add_buildings(self, counts):
        """Buildings of the tile that is done, per postal code."""
        for code, count in dict(counts).items():
            key = str(code)
            self.buildings_done[key] = self.buildings_done.get(key, 0) + int(count)
        self.changed_at = timezone.now()

    def units(self):
        """The building counts for the status row, or None when no total is known."""
        if not self.buildings_total:
            return None
        return {"done": dict(self.buildings_done), "total": dict(self.buildings_total)}


def count_rows(archive, name, limit):
    """The data rows of the member ``name`` of the archive: one bounded pass over its bytes (D-122).

    The header line is not counted. None when the member is missing or unreadable, or has more than ``limit`` rows:
    the caller then shows the rows read without a total."""
    try:
        with zipfile.ZipFile(archive) as zipped, zipped.open(name) as stream:
            lines = 0
            last = b""
            while chunk := stream.read(1024 * 1024):
                lines += chunk.count(b"\n")
                last = chunk[-1:]
                if lines > limit + 2:
                    return None
    except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
        return None
    if last and last != b"\n":
        lines += 1
    rows = max(lines - 1, 0)
    return rows if rows <= limit else None
