"""Snapshot of the published figures of the address directory and the plant register (D-132).

The figures live in the directory file (SQLite, alias ``addresses``). While an import writes that file, a read can fail
for a moment; the status page must not show "unavailable" because of that. So ``jobs`` copies the figures into the
application database after every successful run (``refresh``), and the page reads the copy (``get``). A read that fails
keeps the old copy. Public data only; no personal data.
"""

import logging

from django.db import DatabaseError
from django.db.utils import ConnectionDoesNotExist

from .models import Directory, ImportRequest, PlantRegister, PublishedFigures

logger = logging.getLogger(__name__)

_SOURCES = (ImportRequest.Source.ADDRESS, ImportRequest.Source.PLANT)


def _from_file(source):
    """The figures of the published generation as a dict, or None (nothing published, or the file cannot be read)."""
    try:
        if source == ImportRequest.Source.ADDRESS:
            row = Directory.objects.filter(pk=1).first()
            if row is None:
                return None
            return {
                "data_date": row.data_date, "imported_at": row.imported_at, "rows": row.row_count,
                "rejected": None, "skipped": None, "content_changed_at": row.content_changed_at,
            }
        row = PlantRegister.objects.filter(pk=1).first()
        if row is None:
            return None
        return {
            "data_date": row.data_date, "imported_at": row.imported_at, "rows": row.row_count,
            "rejected": row.rejected_count, "skipped": row.skipped_count, "content_changed_at": row.content_changed_at,
        }
    except (DatabaseError, ConnectionDoesNotExist):
        return None


def refresh(source):
    """Copy the published figures of ``source`` into the snapshot (best effort: a failed read keeps the old copy)."""
    if source not in _SOURCES:
        return None
    values = _from_file(source)
    if values is None:
        logger.warning("The published figures of an import source could not be read; the earlier copy stays.")
        return None
    try:
        row, _created = PublishedFigures.objects.update_or_create(source=source, defaults=values)
    except DatabaseError:
        logger.warning("The published figures of an import source could not be stored.")
        return None
    return row


def get(source):
    """The published figures of ``source``: read from the file; when that cannot be read (an import writes the file),
    the stored snapshot. Read only: it never writes (a status GET must not), the snapshot is written by
    ``refresh`` after a successful run. None when nothing was ever published."""
    if source not in _SOURCES:
        return None
    values = _from_file(source)
    if values is None:
        return PublishedFigures.objects.filter(source=source).first()
    return PublishedFigures(source=source, **values)  # not saved


def clear():
    """Drop the snapshots (the directory was reset, ``setup``)."""
    PublishedFigures.objects.all().delete()
