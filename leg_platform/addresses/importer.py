"""Bounded importer for the fixed official Swiss CSV asset.

Sources: D-049 (owner-approved columns/licence), official STAC item, Python
zipfile/csv documentation, Requests streaming documentation. No archive member
is extracted. The temporary download is deleted even on verification failure.

The importer runs only in the worker process (``process_address_imports`` or the operator command
``import_address_directory``) with the normal settings; it writes the directory file through the
alias ``addresses`` (D-075). No web request reaches this module (``leg_platform.addresses.jobs``
imports it inside ``process_one`` only), and the web processes open the file read-only
(``leg_platform.addresses.storage``).

A new generation is inserted in chunked transactions and stays invisible: the lookup reads the
generation of the row ``Directory`` only. One short final transaction switches that row. A
crash or a failure before that leaves the previous generation serving, and the half-loaded rows
are deleted (or, after a crash, by the next import).
"""

import csv
import hashlib
import hmac
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from django.db import DatabaseError, connections, transaction
from django.db.models import Q
from django.utils import timezone

from .models import DIRECTORY_ALIAS, Address, Directory

ITEM_URL = (
    "https://data.geo.admin.ch/api/stac/v0.9/collections/"
    "ch.swisstopo.amtliches-gebaeudeadressverzeichnis/items/"
    "amtliches-gebaeudeadressverzeichnis_ch"
)
ASSET_NAME = "amtliches-gebaeudeadressverzeichnis_ch_2056.csv.zip"
ASSET_URL = (
    "https://data.geo.admin.ch/ch.swisstopo.amtliches-gebaeudeadressverzeichnis/"
    "amtliches-gebaeudeadressverzeichnis_ch/" + ASSET_NAME
)
CSV_NAME = ASSET_NAME.removesuffix(".zip")
MAX_METADATA_BYTES = 128 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_CSV_BYTES = 1024 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024
MAX_ROWS = 5_000_000
MAX_DOWNLOAD_SECONDS = 1800
CHUNK_SIZE = 64 * 1024
BATCH_SIZE = 2000
# One transaction inserts this many rows. The write lock is held for one chunk only, so a lookup
# or the status page never waits long.
CHUNK_ROWS = 20_000
# Retention (D-075): after a successful import the current and the previous generation are kept,
# every older one is deleted in batches of this many rows. Each batch is its own short statement
# (autocommit), so the table is never locked for long and the import never holds one large delete.
# At most 900: a statement with a list of primary keys must stay below the limit of 999 variables
# that SQLite has before 3.32.0 (https://www.sqlite.org/limits.html, "Maximum Number Of Host
# Parameters"); Django 6.1 accepts SQLite 3.31 (D-075).
PRUNE_BATCH_SIZE = 900
# The import is refused when the disk of the directory file has less free space than this (security
# review of D-075, M-4): an import writes about 1 GB per generation, keeps up to three generations
# on the disk for a moment and the disk is shared with the database of the instance, which must
# never run out of space because of an optional address directory.
MIN_FREE_DISK_BYTES = 4 * 1024**3
# Against two imports at the same time (the worker and the operator command): the importer holds a
# lease on the row of the directory that expires, so that a killed importer does not block the next
# import for ever. It is renewed with every chunk.
IMPORT_LEASE = timedelta(hours=6)

logger = logging.getLogger(__name__)


class ImportFailure(Exception):
    """Safe generic failure; do not include upstream responses or CSV data."""


def _chunks(session, url, limit):
    if url not in {ITEM_URL, ASSET_URL}:
        raise ImportFailure("Unapproved download URL.")
    started = time.monotonic()
    with session.get(
        url, stream=True, allow_redirects=False, timeout=(10, 60),
        headers={"Accept-Encoding": "identity"},
    ) as response:
        if response.status_code != 200:
            raise ImportFailure("Official download failed.")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ImportFailure("Unexpected HTTP content encoding.")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or not 0 < int(length) <= limit):
            raise ImportFailure("Download exceeds the size limit.")
        size = 0
        for chunk in response.iter_content(CHUNK_SIZE):
            size += len(chunk)
            if size > limit or time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                raise ImportFailure("Download exceeds its resource limit.")
            yield chunk
        if not size or (length is not None and size != int(length)):
            raise ImportFailure("Incomplete download.")


def _metadata(session):
    content = b"".join(_chunks(session, ITEM_URL, MAX_METADATA_BYTES))
    try:
        item = json.loads(content)
        if item["id"] != "amtliches-gebaeudeadressverzeichnis_ch":
            raise ValueError
        asset = item["assets"][ASSET_NAME]
        if asset["href"] != ASSET_URL:
            raise ValueError
        multihash = asset["checksum:multihash"]
        if not isinstance(multihash, str) or not re.fullmatch(r"1220[0-9a-fA-F]{64}", multihash):
            raise ValueError
        # The source data date, not the time this instance downloaded the file.
        data_date = datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00")).date()
        if data_date > timezone.localdate() or data_date < date(2000, 1, 1):
            raise ValueError
        return multihash[4:].lower(), data_date
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ImportFailure("Invalid official catalogue metadata.") from exc


def _lines(member):
    total = 0
    while line := member.readline(MAX_LINE_BYTES + 1):
        total += len(line)
        if len(line) > MAX_LINE_BYTES or total > MAX_CSV_BYTES:
            raise ImportFailure("CSV exceeds its resource limit.")
        yield line.decode("utf-8-sig" if total == len(line) else "utf-8", errors="strict")


def _text(value, limit, *, allow_empty=False):
    if (not value and not allow_empty) or len(value) > limit:
        raise ImportFailure("Invalid address field length.")
    if value != value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ImportFailure("Invalid address field.")
    return value


def _address_rows(archive):
    """Yield only the three approved source columns, split into four values."""
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        if len(members) != 1:
            raise ImportFailure("Expected exactly one CSV member.")
        member = members[0]
        if (
            member.filename != CSV_NAME
            or member.is_dir()
            or member.flag_bits & 1
            or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or not 0 < member.file_size <= MAX_CSV_BYTES
            or member.file_size > max(member.compress_size, 1) * 100
        ):
            raise ImportFailure("Unsafe ZIP member.")
        with zipped.open(member) as stream:
            reader = csv.reader(_lines(stream), delimiter=";", strict=True)
            header = next(reader, [])
            if len(header) > 32 or len(header) != len(set(header)):
                raise ImportFailure("Invalid CSV header.")
            required = ("STN_LABEL", "ADR_NUMBER", "ZIP_LABEL")
            if not all(column in header for column in required):
                raise ImportFailure("Missing approved CSV columns.")
            indexes = [header.index(column) for column in required]
            count = 0
            for row in reader:
                count += 1
                if count > MAX_ROWS or len(row) != len(header):
                    raise ImportFailure("Invalid CSV row count or width.")
                street, house_number, zip_label = (row[index] for index in indexes)
                postal_code, separator, locality = zip_label.partition(" ")
                if not separator or not re.fullmatch(r"[1-9][0-9]{3}", postal_code):
                    raise ImportFailure("Invalid postal label.")
                yield {
                    "street": _text(street, 255),
                    "house_number": _text(house_number, 32, allow_empty=True),
                    "postal_code": postal_code,
                    "locality": _text(locality, 255),
                }
            if count == 0:
                raise ImportFailure("Empty directory.")


def address_rows(archive):
    """Parse a verified archive, reporting format errors without source data."""
    try:
        yield from _address_rows(archive)
    except (zipfile.BadZipFile, csv.Error, UnicodeError, OSError) as exc:
        raise ImportFailure("Invalid official archive format.") from exc


def prune_old_generations(keep):
    """Delete the rows of every generation that is not in ``keep``, in short batches.

    Called only after the new generation is committed and outside any transaction of its own, so
    that every batch is one short statement. A failure leaves old rows behind (bounded: the next
    successful import removes them); it never touches the kept generations.
    """
    deleted = 0
    old = list(
        Address.objects.exclude(generation__in=keep)
        .order_by().values_list("generation", flat=True).distinct()
    )
    for generation in old:
        while True:
            # The primary keys of one batch are read first, so that every delete is a bounded list.
            pks = list(
                Address.objects.filter(generation=generation)
                .order_by().values_list("pk", flat=True)[:PRUNE_BATCH_SIZE]
            )
            if not pks:
                break
            deleted += Address.objects.filter(pk__in=pks).delete()[0]
    return deleted


def _acquire_lease():
    """Take the lease against a second import; ``ImportFailure`` if another import holds it."""
    token = uuid.uuid4()
    now = timezone.now()
    with transaction.atomic(using=DIRECTORY_ALIAS):
        Directory.objects.get_or_create(pk=1)
        claimed = (
            Directory.objects.filter(pk=1)
            .filter(Q(import_lease_until__isnull=True) | Q(import_lease_until__lt=now))
            .update(import_token=token, import_lease_until=now + IMPORT_LEASE)
        )
    if not claimed:
        raise ImportFailure("Another import is running.")
    return token


def _release_lease(token):
    try:
        Directory.objects.filter(pk=1, import_token=token).update(
            import_token=None, import_lease_until=None,
        )
    except DatabaseError:
        # The lease expires by itself.
        logger.warning("The address directory import lease could not be released.")


def _insert_chunk(rows, token):
    """Insert one chunk in one short transaction and renew the lease."""
    with transaction.atomic(using=DIRECTORY_ALIAS):
        renewed = Directory.objects.filter(pk=1, import_token=token).update(
            import_lease_until=timezone.now() + IMPORT_LEASE,
        )
        if not renewed:
            raise ImportFailure("The import lease was lost.")
        Address.objects.bulk_create(rows, batch_size=BATCH_SIZE)


def _load_generation(archive, generation, token):
    """Insert every row of the archive as the (still invisible) generation; return the count."""
    chunk = []
    count = 0
    for values in address_rows(archive):
        chunk.append(Address(generation=generation, **values))
        count += 1
        if len(chunk) >= CHUNK_ROWS:
            _insert_chunk(chunk, token)
            chunk = []
    if chunk:
        _insert_chunk(chunk, token)
    return count


def _discard_generation(generation):
    """Delete the rows of a generation that was not published (best effort)."""
    try:
        while True:
            pks = list(
                Address.objects.filter(generation=generation)
                .order_by().values_list("pk", flat=True)[:PRUNE_BATCH_SIZE]
            )
            if not pks:
                return
            Address.objects.filter(pk__in=pks).delete()
    except DatabaseError:
        # The next successful import deletes every generation that is not kept.
        logger.warning("An unpublished address directory generation could not be deleted.")


def _finish_file():
    """After an import: refresh the statistics of the query planner, fold the write-ahead log into
    the file and shrink it (best effort).

    The statistics matter: with them SQLite picks the index of the postal code for a query with a
    complete postal code. ``analysis_limit`` keeps ANALYZE to a fraction of a second.
    """
    try:
        connection = connections[DIRECTORY_ALIAS]
        if connection.in_atomic_block:
            # Not inside an open transaction (only a test or a caller that wraps the import): the
            # checkpoint cannot run there.
            return
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA analysis_limit = 2000")
            cursor.execute("ANALYZE")
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            cursor.fetchall()
    except DatabaseError:
        logger.warning("The address directory file could not be analysed or checkpointed.")


def _is_intact(directory):
    """Whether the published generation is undamaged (cheap checks, no download).

    ``PRAGMA quick_check`` of the file reports ``ok`` AND the number of rows of the published
    generation equals the count that the import recorded (``Directory.row_count``). A deleted,
    truncated or half-written generation fails the second check; a damaged file structure fails
    the first (https://www.sqlite.org/pragma.html#pragma_quick_check).
    """
    try:
        with connections[DIRECTORY_ALIAS].cursor() as cursor:
            cursor.execute("PRAGMA quick_check")
            if [row[0] for row in cursor.fetchall()] != ["ok"]:
                return False
        return (
            directory.row_count > 0
            and Address.objects.filter(generation=directory.generation).count() == directory.row_count
        )
    except DatabaseError:
        return False


def _is_published(directory, checksum, *, repair):
    """Whether the published directory is the official file ``checksum`` and needs no import.

    The automatic attempt trusts the recorded checksum. A manual request (``repair``, D-076) also
    needs the integrity checks to pass, so that it repairs a damaged directory but ends early,
    without a download and without an import, when everything is intact (security review of
    D-076, M-3: a repeated request must stay cheap).
    """
    if directory is None or not directory.imported_at or directory.checksum != checksum:
        return False
    return not repair or _is_intact(directory)


def _require_free_disk():
    """Refuse the import when the disk of the directory file is nearly full (M-4).

    Checked before the download. Nothing is written and the published generation is not touched.
    The log line names no path.
    """
    folder = Path(str(connections[DIRECTORY_ALIAS].settings_dict["NAME"])).parent
    try:
        # The folder is created by the first connection; before that its nearest parent counts.
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        free = shutil.disk_usage(folder).free
    except OSError:
        raise ImportFailure("The free disk space could not be checked.") from None
    if free < MIN_FREE_DISK_BYTES:
        logger.warning(
            "Address directory import refused: less than %d GB free on the disk of the directory file.",
            MIN_FREE_DISK_BYTES // 1024**3,
        )
        raise ImportFailure("Not enough free disk space for an import.")


def _publish(archive, checksum, data_date, *, repair=False):
    """Publish a verified archive as a new generation.

    An archive whose checksum equals the published one is not loaded again (the automatic
    attempt). With ``repair`` (a manual request, D-076) it is loaded anyway when the published
    generation fails the integrity check (``_is_intact``), so that a damaged directory is
    replaced by a new complete generation.

    The rows are inserted in chunked transactions as a generation that no reader sees; one short
    final transaction switches the pointer. Until then, and after any failure or crash, the
    previous generation serves.
    """
    token = _acquire_lease()
    generation = uuid.uuid4()
    try:
        directory = Directory.objects.get(pk=1)
        if directory.data_date and data_date < directory.data_date:
            raise ImportFailure("Catalogue data date predates the published directory.")
        if _is_published(directory, checksum, repair=repair):
            return directory.row_count
        previous = directory.generation
        try:
            count = _load_generation(archive, generation, token)
            with transaction.atomic(using=DIRECTORY_ALIAS):
                switched = Directory.objects.filter(pk=1, import_token=token).update(
                    generation=generation, data_date=data_date, imported_at=timezone.now(),
                    checksum=checksum, row_count=count,
                )
                if not switched:
                    raise ImportFailure("The import lease was lost.")
        except BaseException:
            _discard_generation(generation)
            raise
        # Retention (D-075): the new generation is published; keep it and the previous one only.
        try:
            prune_old_generations({generation, previous})
        except DatabaseError:
            # The import itself succeeded; the next successful import removes what is left.
            logger.warning("Old address directory generations could not be deleted.")
    finally:
        _release_lease(token)
    # After the lease is released, so that no write follows the checkpoint.
    _finish_file()
    return count


def _published_row_count(checksum, *, repair):
    """The row count of the published directory if it is the official file ``checksum`` (and, with
    ``repair``, undamaged); None if an import is needed. A file that cannot be read needs one."""
    try:
        directory = Directory.objects.filter(pk=1).first()
        return directory.row_count if _is_published(directory, checksum, repair=repair) else None
    except DatabaseError:
        return None


def import_directory(*, repair=False):
    """Download, verify and publish; called only by the worker process (D-075).

    The order: the official catalogue entry (a few KB); if the published directory already is that
    file, the import ends here (with ``repair`` only if the integrity checks pass); the check of
    the free disk space; the download, the checksum and every limit; the import. ``repair`` (a
    manual request, D-076) only adds the integrity checks to the first shortcut; it never skips a
    check of the download.
    """
    try:
        # Ignore netrc/proxy credentials inherited from an operator environment.
        with requests.Session() as session, tempfile.TemporaryFile() as archive:
            session.trust_env = False
            checksum, data_date = _metadata(session)
            row_count = _published_row_count(checksum, repair=repair)
            if row_count is not None:
                return row_count
            _require_free_disk()
            digest = hashlib.sha256()
            for chunk in _chunks(session, ASSET_URL, MAX_ARCHIVE_BYTES):
                digest.update(chunk)
                archive.write(chunk)
            if not hmac.compare_digest(digest.hexdigest(), checksum):
                raise ImportFailure("Official archive checksum mismatch.")
            archive.seek(0)
            return _publish(archive, checksum, data_date, repair=repair)
    except ImportFailure:
        raise
    except (requests.RequestException, zipfile.BadZipFile, csv.Error, UnicodeError, OSError) as exc:
        raise ImportFailure("The official directory could not be imported.") from exc
