"""Bounded importer for the fixed official Swiss CSV asset (D-049, D-075, D-090).

One writer at a time: the import thread of the web process (``runner``), the worker process or an
operator command (the directory file is read-only for every other thread and process). A new
generation is inserted in chunked transactions and stays invisible until one short final
transaction switches the ``Directory`` row; a failure or crash before that leaves the previous
generation serving.
"""

import csv
import hashlib
import hmac
import io
import json
import logging
import queue
import re
import shutil
import struct
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import requests
from django.db import DatabaseError, connections, transaction
from django.db.models import Q
from django.utils import timezone

from .http_budget import Adapter, Budget
from .models import (
    DIRECTORY_ALIAS, Address, AddressPlace, AddressStreet, Directory, ImportRequest, fill_place_centres,
)
from .progress import count_rows

ITEM_URL = (
    "https://data.geo.admin.ch/api/stac/v0.9/collections/"
    "ch.swisstopo.amtliches-gebaeudeadressverzeichnis/items/"
    "amtliches-gebaeudeadressverzeichnis_ch"
)
# The plant register of the Federal Office of Energy (BFE, D-117): one fixed URL, the only one besides the above.
PLANT_URL = (
    "https://data.geo.admin.ch/ch.bfe.elektrizitaetsproduktionsanlagen/csv/2056/"
    "ch.bfe.elektrizitaetsproduktionsanlagen.zip"
)
# The BKW high-discount map, WMS of the public server of BKW Netz (D-118): the only other host of this process. It is read
# only by ``transformers.py`` (one request at a time, GET, HTTPS, no redirects); the request URL carries the query.
TRANSFORMER_WMS_URL = "https://qgisserver-extern.bkw.ch/ogc/"
APPROVED_WMS_URLS = frozenset({TRANSFORMER_WMS_URL})
ASSET_NAME = "amtliches-gebaeudeadressverzeichnis_ch_2056.csv.zip"
ASSET_URL = (
    "https://data.geo.admin.ch/ch.swisstopo.amtliches-gebaeudeadressverzeichnis/"
    "amtliches-gebaeudeadressverzeichnis_ch/" + ASSET_NAME
)
CSV_NAME = ASSET_NAME.removesuffix(".zip")
# Column of the federal building identifier in the official CSV (verified against the product information of
# swisstopo, table 1, and the header of the published file, 2026-10-19). KONZEPT 3a calls it "EGID".
EGID_COLUMN = "BDG_EGID"
# Column of the entrance identifier (product information table 1: ADR_EDID, SHORT, in the CSV; same header).
ENTRANCE_COLUMN = "ADR_EDID"
# Coordinates of every row (LV95, EPSG:2056; D-049 amendment of 2026-10-25), stored as easting and northing.
EASTING_COLUMN = "ADR_EASTING"
NORTHING_COLUMN = "ADR_NORTHING"
# LV95 bounds of Switzerland, the same as the plant register (D-117).
EASTING_RANGE = (Decimal(2480000), Decimal(2840000))
NORTHING_RANGE = (Decimal(1070000), Decimal(1300000))
MAX_METADATA_BYTES = 128 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_CSV_BYTES = 1024 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024
MAX_ROWS = 5_000_000
# Total time of one official download, from the request to the last byte (D-117; the header phase counts too).
MAX_DOWNLOAD_SECONDS = 1800
CHUNK_SIZE = 64 * 1024
# Messages of the download thread that wait for the caller (bounded: a fast server waits, the memory does not grow).
DOWNLOAD_QUEUE_SIZE = 8
# How often a waiting caller looks at the clock while the download thread is silent (seconds).
DOWNLOAD_POLL_SECONDS = 1.0
BATCH_SIZE = 2000
# One transaction per chunk: the write lock is held briefly.
CHUNK_ROWS = 20_000
# Old generations are deleted in batches of primary keys; at most 900 (SQLite < 3.32 allows 999
# host parameters per statement, D-075).
PRUNE_BATCH_SIZE = 900
# Import refused below this free space on the disk of the directory file: the downloaded archive
# (at most 256 MiB, written next to the file, not to the system temporary folder) and about 1 GB
# per generation, up to three on disk; the disk is shared with the instance database.
MIN_FREE_DISK_BYTES = 4 * 1024**3
# ZIP end records (PKWARE APPNOTE 4.3.16, 4.3.15, 4.3.6), read before ``zipfile`` parses the central directory.
EOCD_SIGNATURE = b"PK\x05\x06"
ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
ZIP64_RECORD_SIGNATURE = b"PK\x06\x06"
MAX_ZIP_COMMENT = 65535
END_RECORD = struct.Struct("<4sHHHHIIH")  # 22 bytes; index 4 entries, index 7 comment length
ZIP64_LOCATOR = struct.Struct("<4sIQI")  # 20 bytes; index 2 offset of the ZIP64 end record
ZIP64_RECORD = struct.Struct("<4sQHHIIQQQQ")  # 56 bytes; index 7 entries
# Lease on the ``Directory`` row against concurrent imports; expires so a killed importer does not
# block forever, renewed with every chunk.
IMPORT_LEASE = timedelta(hours=6)
# After a failed manual repair, the next one is refused for this long (no download per click).
REPAIR_RETRY_INTERVAL = timedelta(hours=6)

logger = logging.getLogger(__name__)


class ImportFailure(Exception):
    """Safe generic failure; do not include upstream responses or CSV data."""


class _Stopped(Exception):
    """The caller gave up (deadline or abandoned download): the download thread stops."""


def _download_body(session, url, limit, content_types, put):
    """The request, the checks of the answer and the body, as messages (``put``) for the caller of ``_chunks``."""
    with session.get(
        url, stream=True, allow_redirects=False, timeout=(10, 60), verify=True,
        headers={"Accept-Encoding": "identity"},
    ) as response:
        if response.status_code == 404:
            raise ImportFailure("Official file not found.")
        if response.status_code != 200:
            raise ImportFailure("Official download failed.")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ImportFailure("Unexpected HTTP content encoding.")
        if content_types is not None:
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type not in content_types:
                raise ImportFailure("Unexpected HTTP content type.")
        length = response.headers.get("Content-Length")
        put("headers", response.headers, length)
        if length is not None and (not length.isdecimal() or not 0 < int(length) <= limit):
            raise ImportFailure("Download exceeds the size limit.")
        size = 0
        for chunk in response.iter_content(CHUNK_SIZE):
            size += len(chunk)
            if size > limit:
                raise ImportFailure("Download exceeds its resource limit.")
            put("chunk", chunk, None)
        if not size or (length is not None and size != int(length)):
            raise ImportFailure("Incomplete download.")


def _download_thread(session, url, limit, content_types, budget, messages):
    """The body of the download thread: every outcome is handed to the caller as one message; nothing is raised here."""

    def put(kind, payload, extra):
        # Waits for room in the queue, but gives up as soon as the caller has expired the budget.
        while not budget.expired:
            try:
                messages.put((kind, payload, extra), timeout=DOWNLOAD_POLL_SECONDS)
                return
            except queue.Full:
                continue
        raise _Stopped

    try:
        _download_body(session, url, limit, content_types, put)
        put("end", None, None)
    except _Stopped:
        pass
    except BaseException as exc:  # noqa: BLE001 - re-raised by the caller; the thread must not fail silently
        try:
            put("error", exc, None)
        except _Stopped:
            pass


def _chunks(
    session, url, limit, on_chunk=None, content_types=None, on_headers=None,
    *, seconds=MAX_DOWNLOAD_SECONDS, clock=time.monotonic,
):
    """Yield the body of an approved URL in chunks; ``on_chunk(size so far, announced length or None)`` reports progress.

    ``content_types`` (media types without parameters) must match the response when given; ``on_headers`` receives the
    response headers before the body is read.

    Hard total deadline (D-117, ``docs/SECURITY.md`` section 6): the request runs in a helper thread and the caller
    waits for its messages until ``seconds`` after the start, connect, headers and body together. The clock is checked
    after every message and at every poll. When the deadline has passed, every socket of the request is shut down
    (``http_budget.Budget``), so that a read which trickles bytes ends at once, and ``ImportFailure`` is raised.
    ``clock`` is injected by the tests.
    """
    if url not in {ITEM_URL, ASSET_URL, PLANT_URL}:
        raise ImportFailure("Unapproved download URL.")
    deadline = clock() + seconds
    budget = Budget()
    session.mount("https://", Adapter(budget))
    messages = queue.Queue(maxsize=DOWNLOAD_QUEUE_SIZE)
    thread = threading.Thread(
        target=_download_thread, args=(session, url, limit, content_types, budget, messages),
        name="official-download", daemon=True,
    )
    thread.start()
    size = 0
    announced = None
    finished = False
    try:
        while True:
            try:
                kind, payload, extra = messages.get(timeout=DOWNLOAD_POLL_SECONDS)
            except queue.Empty:
                if clock() >= deadline:
                    raise ImportFailure("Download exceeds its resource limit.") from None
                continue
            if kind == "error":
                raise payload
            if clock() >= deadline:
                raise ImportFailure("Download exceeds its resource limit.")
            if kind == "headers":
                if on_headers is not None:
                    on_headers(payload)
                announced = int(extra) if extra is not None and extra.isdecimal() else None
            elif kind == "chunk":
                size += len(payload)
                if on_chunk is not None:
                    on_chunk(size, announced)
                yield payload
            else:
                finished = True
                return
    finally:
        if not finished:
            budget.expire()


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
        # The source data date, not the download time.
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


def _egid(value):
    """The federal building identifier of a row: 1 to 999,999,999 (nine digits at most), or None if the file gives none."""
    if not value:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
        raise ImportFailure("Invalid address field.")
    return int(value)


def _coordinate(value, bounds):
    """An LV95 coordinate with at most three decimals inside ``bounds``, or None if the file gives none."""
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{1,8}(\.[0-9]{1,3})?", value):
        raise ImportFailure("Invalid address field.")
    number = Decimal(value)
    if not bounds[0] <= number <= bounds[1]:
        raise ImportFailure("Invalid address field.")
    return number.quantize(Decimal("0.001"))


def _entrance(value):
    """The entrance identifier of a row: 0 to 32767 (a SHORT of the official model), or None if the file gives none."""
    if not value:
        return None
    if not re.fullmatch(r"0|[1-9][0-9]{0,4}", value) or int(value) > 32767:
        raise ImportFailure("Invalid address field.")
    return int(value)


def declared_entry_count(archive):
    """The number of entries the end record of a ZIP file declares, read before ``zipfile`` parses the archive (M1).

    Only the end record, and the ZIP64 locator and record when the count is 0xFFFF, are read from the tail of the file,
    so that an archive with an absurd central directory is refused without parsing it. None if no consistent end record
    is found. The position of ``archive`` is restored.
    """
    start = archive.tell()
    try:
        archive.seek(0, io.SEEK_END)
        size = archive.tell()
        tail_start = max(0, size - (END_RECORD.size + MAX_ZIP_COMMENT))
        archive.seek(tail_start)
        tail = archive.read(size - tail_start)
        position = tail.rfind(EOCD_SIGNATURE)
        # The record must end at the end of the file (its comment length is right); a signature inside a comment does not.
        while position != -1:
            if (
                position + END_RECORD.size <= len(tail)
                and position + END_RECORD.size + END_RECORD.unpack_from(tail, position)[7] == len(tail)
            ):
                break
            position = tail.rfind(EOCD_SIGNATURE, 0, position)
        else:
            return None
        entries = END_RECORD.unpack_from(tail, position)[4]
        if entries != 0xFFFF:
            return entries
        locator_at = tail_start + position - ZIP64_LOCATOR.size
        if locator_at < 0:
            return None
        archive.seek(locator_at)
        locator = archive.read(ZIP64_LOCATOR.size)
        if len(locator) != ZIP64_LOCATOR.size or locator[:4] != ZIP64_LOCATOR_SIGNATURE:
            return None
        archive.seek(ZIP64_LOCATOR.unpack(locator)[2])
        record = archive.read(ZIP64_RECORD.size)
        if len(record) != ZIP64_RECORD.size or record[:4] != ZIP64_RECORD_SIGNATURE:
            return None
        return ZIP64_RECORD.unpack(record)[7]
    finally:
        archive.seek(start)


def _address_rows(archive):
    """Yield only the approved source columns: the locality, the building and entrance identifiers and the coordinates."""
    entries = declared_entry_count(archive)
    if entries is None:
        raise ImportFailure("Invalid official archive format.")
    if entries != 1:
        # Refused before the central directory is parsed (M1); the text is the one of the member check below.
        raise ImportFailure("Expected exactly one CSV member.")
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
            # Official name of the building identifier column (swisstopo, "Amtliches Verzeichnis der
            # Gebaeudeadressen", product information, table 1: BDG_EGID, in the CSV). Without it nothing is imported.
            if EGID_COLUMN not in header:
                raise ImportFailure("Missing EGID column.")
            if ENTRANCE_COLUMN not in header:
                raise ImportFailure("Missing entrance column.")
            if EASTING_COLUMN not in header or NORTHING_COLUMN not in header:
                raise ImportFailure("Missing coordinate column.")
            indexes = [
                header.index(column)
                for column in (*required, EGID_COLUMN, ENTRANCE_COLUMN, EASTING_COLUMN, NORTHING_COLUMN)
            ]
            count = 0
            for row in reader:
                count += 1
                if count > MAX_ROWS or len(row) != len(header):
                    raise ImportFailure("Invalid CSV row count or width.")
                street, house_number, zip_label, egid, entrance, easting, northing = (
                    row[index] for index in indexes
                )
                postal_code, separator, locality = zip_label.partition(" ")
                if not separator or not re.fullmatch(r"[1-9][0-9]{3}", postal_code):
                    raise ImportFailure("Invalid postal label.")
                yield {
                    "street": _text(street, 255),
                    "house_number": _text(house_number, 32, allow_empty=True),
                    "postal_code": postal_code,
                    "locality": _text(locality, 255),
                    "egid": _egid(egid),
                    "entrance_id": _entrance(entrance),
                    "easting": _coordinate(easting, EASTING_RANGE),
                    "northing": _coordinate(northing, NORTHING_RANGE),
                }
            if count == 0:
                raise ImportFailure("Empty directory.")


def address_rows(archive):
    """Parse a verified archive, reporting format errors without source data."""
    try:
        yield from _address_rows(archive)
    except (zipfile.BadZipFile, csv.Error, UnicodeError, OSError) as exc:
        raise ImportFailure("Invalid official archive format.") from exc


def _delete_generation(generation):
    """Delete a generation in short statements, each over a bounded list of primary keys."""
    deleted = 0
    while pks := list(
        Address.objects.filter(generation=generation)
        .order_by().values_list("pk", flat=True)[:PRUNE_BATCH_SIZE]
    ):
        deleted += Address.objects.filter(pk__in=pks).delete()[0]
    # The lookup tables of the generation (a few hundred thousand small rows, one statement each).
    AddressStreet.objects.filter(generation=generation).delete()
    AddressPlace.objects.filter(generation=generation).delete()
    return deleted


def prune_old_generations(keep):
    """Delete every generation not in ``keep`` (after the new one is committed, outside a transaction)."""
    old = list(
        Address.objects.exclude(generation__in=keep)
        .order_by().values_list("generation", flat=True).distinct()
    )
    deleted = sum(_delete_generation(generation) for generation in old)
    # What a crashed import left in the lookup tables.
    AddressStreet.objects.exclude(generation__in=keep).delete()
    AddressPlace.objects.exclude(generation__in=keep).delete()
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


def _load_generation(archive, generation, token, progress=None):
    """Insert every row of the archive as the (still invisible) generation; return the count."""
    chunk = []
    count = 0
    if progress is not None:
        progress.set_rows_total(count_rows(archive, CSV_NAME, MAX_ROWS))
    for values in address_rows(archive):
        chunk.append(Address(generation=generation, **values))
        count += 1
        if len(chunk) >= CHUNK_ROWS:
            _insert_chunk(chunk, token)
            chunk = []
            if progress is not None:
                progress.rows(count)
    if chunk:
        _insert_chunk(chunk, token)
    if progress is not None:
        progress.rows(count)
    return count


def _discard_generation(generation):
    """Delete the rows of an unpublished generation (best effort)."""
    try:
        _delete_generation(generation)
    except DatabaseError:
        # The next successful import deletes every generation that is not kept.
        logger.warning("An unpublished address directory generation could not be deleted.")


def _finish_file():
    """Refresh planner statistics (index choice for postal codes), checkpoint and shrink the WAL (best effort)."""
    try:
        connection = connections[DIRECTORY_ALIAS]
        if connection.in_atomic_block:
            # The checkpoint cannot run inside a transaction.
            return
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA analysis_limit = 2000")
            cursor.execute("ANALYZE")
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            cursor.fetchall()
    except DatabaseError:
        logger.warning("The address directory file could not be analysed or checkpointed.")


def _is_intact(directory):
    """Whether the published generation is undamaged: ``quick_check`` is ok and the row count matches the recorded one."""
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

    With ``repair`` (manual request, D-076) the integrity checks must also pass.
    """
    if (
        directory is None or not directory.imported_at or directory.row_count <= 0
        or directory.checksum != checksum
        # A generation without building identifiers is replaced even if it is the current official file.
        or not directory.has_egid
        # A generation whose lookup tables are empty or missing is not usable (self-healing: it is imported again).
        or not _has_lookup_rows(directory)
    ):
        return False
    return not repair or _is_intact(directory)


def _has_lookup_rows(directory):
    """Whether ``AddressPlace`` and ``AddressStreet`` have rows for the published generation."""
    try:
        return (
            AddressPlace.objects.filter(generation=directory.generation).exists()
            and AddressStreet.objects.filter(generation=directory.generation).exists()
        )
    except DatabaseError:
        return False


def _free_bytes(folder):
    """Free space at ``folder`` (the nearest existing parent if it is missing)."""
    try:
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        return shutil.disk_usage(folder).free
    except OSError:
        raise ImportFailure("The free disk space could not be checked.") from None


def _directory_folder():
    """The folder of the directory file: disk-backed and writable by the service (the system temporary folder can be small and memory-backed)."""
    return Path(str(connections[DIRECTORY_ALIAS].settings_dict["NAME"])).parent


def _require_free_disk():
    """Refuse the import before the download when the disk of the directory file is nearly full (D-075).

    This one disk holds the archive (temporary file next to the directory file), the generations and the
    SQLite temporary files (``SQLITE_TMPDIR``, see ``sqlite.base``); the system temporary folder is not used.
    """
    if _free_bytes(_directory_folder()) < MIN_FREE_DISK_BYTES:
        logger.warning(
            "Address directory import refused: less than %d GB free on the disk of the directory file.",
            MIN_FREE_DISK_BYTES // 1024**3,
        )
        raise ImportFailure("Not enough free disk space for an import.")


def _publish(archive, checksum, data_date, *, repair=False, progress=None):
    """Publish a verified archive as a new generation (skipped if already published, see ``_is_published``)."""
    token = _acquire_lease()
    generation = uuid.uuid4()
    try:
        directory = Directory.objects.get(pk=1)
        if directory.data_date and data_date < directory.data_date:
            raise ImportFailure("Catalogue data date predates the published directory.")
        if _is_published(directory, checksum, repair=repair):
            return directory.row_count
        previous = directory.generation
        # The day the content changed (D-117 hint): today for another file, the old day for the same one.
        content_changed_at = timezone.localdate() if directory.checksum != checksum else directory.content_changed_at
        try:
            if progress is not None:
                progress.phase("importing")
            count = _load_generation(archive, generation, token, progress)
            # The centres of the places (distance ranking of the address check), before the generation is published.
            fill_place_centres(generation)
            if progress is not None:
                progress.phase("publishing")
            with transaction.atomic(using=DIRECTORY_ALIAS):
                switched = Directory.objects.filter(pk=1, import_token=token).update(
                    generation=generation, data_date=data_date, imported_at=timezone.now(),
                    checksum=checksum, row_count=count, has_egid=True, content_changed_at=content_changed_at,
                )
                if not switched:
                    raise ImportFailure("The import lease was lost.")
        except BaseException:
            _discard_generation(generation)
            raise
        # Keep the new and the previous generation only (D-075).
        try:
            if progress is not None:
                progress.phase("finishing")
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
    ``repair``, undamaged); None if an import is needed."""
    try:
        directory = Directory.objects.filter(pk=1).first()
        return directory.row_count if _is_published(directory, checksum, repair=repair) else None
    except DatabaseError:
        return None


def _require_catalogue_not_older(data_date):
    """Refuse a catalogue older than the published directory before the download (also checked under the lease)."""
    try:
        directory = Directory.objects.filter(pk=1).first()
    except DatabaseError:
        return
    if directory is not None and directory.data_date and data_date < directory.data_date:
        raise ImportFailure("Catalogue data date predates the published directory.")


def _repair_is_needed(checksum):
    """True when the published directory is the official file but fails the checks or is unreadable."""
    try:
        directory = Directory.objects.filter(pk=1).first()
    except DatabaseError:
        return True
    return directory is not None and bool(directory.imported_at) and directory.checksum == checksum


def _require_repair_allowed():
    """After a repair that did not end intact (or failed), the next one waits six hours."""
    last = ImportRequest.objects.filter(pk=1).values_list("last_failed_repair_at", flat=True).first()
    if last is not None and timezone.now() < last + REPAIR_RETRY_INTERVAL:
        logger.warning(
            "Address directory repair refused: the last repair did not succeed less than %d hours ago.",
            REPAIR_RETRY_INTERVAL // timedelta(hours=1),
        )
        raise ImportFailure("A repair was tried recently and did not succeed.")


def _record_repair_result(succeeded):
    try:
        ImportRequest.objects.filter(pk=1).update(last_failed_repair_at=None if succeeded else timezone.now())
    except DatabaseError:
        logger.warning("The result of the address directory repair could not be recorded.")


def _published_directory_is_intact():
    try:
        directory = Directory.objects.filter(pk=1).first()
    except DatabaseError:
        return False
    return directory is not None and _is_intact(directory)


def _download_and_publish(session, archive, checksum, data_date, *, repair, progress=None):
    digest = hashlib.sha256()
    if progress is not None:
        progress.phase("downloading")
    on_chunk = progress.download if progress is not None else None
    for chunk in _chunks(session, ASSET_URL, MAX_ARCHIVE_BYTES, on_chunk):
        digest.update(chunk)
        archive.write(chunk)
    if not hmac.compare_digest(digest.hexdigest(), checksum):
        raise ImportFailure("Official archive checksum mismatch.")
    archive.seek(0)
    if progress is not None:
        progress.downloaded = True
    return _publish(archive, checksum, data_date, repair=repair, progress=progress)


def clear_stale_lease():
    """Free the lease of an import whose process is gone (called only after the job was found dead, D-090)."""
    Directory.objects.filter(pk=1).update(import_token=None, import_lease_until=None)


def import_directory(*, repair=False, progress=None):
    """Download, verify and publish; one writer thread or process (D-075, D-090).

    Order: catalogue entry; shortcut if already published (with ``repair`` only when intact);
    refuse an older catalogue; refuse a repair within six hours of a failed one; free-disk check;
    download with checksum and limits; import. A repair is followed by one more integrity check
    whose outcome starts or clears the six hours (D-076). ``progress`` (``progress.Progress``)
    receives the phase and the counters for the status page.
    """
    try:
        # The first connection creates the folder of the directory file, where the archive goes.
        try:
            connections[DIRECTORY_ALIAS].ensure_connection()
        except DatabaseError as exc:
            raise ImportFailure("The official directory could not be imported.") from exc
        # Ignore netrc/proxy credentials inherited from an operator environment. The archive goes next to
        # the directory file (unnamed, removed on close), not to the system temporary folder.
        with requests.Session() as session, tempfile.TemporaryFile(dir=_directory_folder()) as archive:
            session.trust_env = False
            if progress is not None:
                progress.phase("catalogue")
            checksum, data_date = _metadata(session)
            row_count = _published_row_count(checksum, repair=repair)
            if row_count is not None:
                return row_count
            _require_catalogue_not_older(data_date)
            repairing = repair and _repair_is_needed(checksum)
            if repairing:
                _require_repair_allowed()
            _require_free_disk()
            try:
                count = _download_and_publish(
                    session, archive, checksum, data_date, repair=repair, progress=progress
                )
            except Exception:
                if repairing:
                    _record_repair_result(False)
                raise
            if repairing:
                _record_repair_result(_published_directory_is_intact())
            return count
    except ImportFailure:
        raise
    except (requests.RequestException, zipfile.BadZipFile, csv.Error, UnicodeError, OSError) as exc:
        raise ImportFailure("The official directory could not be imported.") from exc
