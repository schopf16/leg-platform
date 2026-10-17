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

from .models import DIRECTORY_ALIAS, Address, Directory, ImportRequest

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
# One transaction per chunk: the write lock is held briefly.
CHUNK_ROWS = 20_000
# Old generations are deleted in batches of primary keys; at most 900 (SQLite < 3.32 allows 999
# host parameters per statement, D-075).
PRUNE_BATCH_SIZE = 900
# Import refused below this free space (about 1 GB per generation, up to three on disk; the disk
# is shared with the instance database).
MIN_FREE_DISK_BYTES = 4 * 1024**3
# Lease on the ``Directory`` row against concurrent imports; expires so a killed importer does not
# block forever, renewed with every chunk.
IMPORT_LEASE = timedelta(hours=6)
# After a failed manual repair, the next one is refused for this long (no download per click).
REPAIR_RETRY_INTERVAL = timedelta(hours=6)

logger = logging.getLogger(__name__)


class ImportFailure(Exception):
    """Safe generic failure; do not include upstream responses or CSV data."""


def _chunks(session, url, limit, on_chunk=None):
    """Yield the body of an approved URL in chunks; ``on_chunk(size so far, announced length or None)`` reports progress."""
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
            if on_chunk is not None:
                on_chunk(size, int(length) if length is not None else None)
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


def _delete_generation(generation):
    """Delete a generation in short statements, each over a bounded list of primary keys."""
    deleted = 0
    while pks := list(
        Address.objects.filter(generation=generation)
        .order_by().values_list("pk", flat=True)[:PRUNE_BATCH_SIZE]
    ):
        deleted += Address.objects.filter(pk__in=pks).delete()[0]
    return deleted


def prune_old_generations(keep):
    """Delete every generation not in ``keep`` (after the new one is committed, outside a transaction)."""
    old = list(
        Address.objects.exclude(generation__in=keep)
        .order_by().values_list("generation", flat=True).distinct()
    )
    return sum(_delete_generation(generation) for generation in old)


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
    ):
        return False
    return not repair or _is_intact(directory)


def _free_bytes(folder):
    """Free space at ``folder`` (the nearest existing parent if it is missing)."""
    try:
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        return shutil.disk_usage(folder).free
    except OSError:
        raise ImportFailure("The free disk space could not be checked.") from None


def _require_free_disk():
    """Refuse the import before the download when the directory or temporary disk is nearly full (D-075)."""
    directory_folder = Path(str(connections[DIRECTORY_ALIAS].settings_dict["NAME"])).parent
    try:
        temporary_folder = Path(tempfile.gettempdir())
    except OSError:
        raise ImportFailure("The free disk space could not be checked.") from None
    for folder, purpose in ((directory_folder, "the directory file"), (temporary_folder, "the temporary download")):
        if _free_bytes(folder) < MIN_FREE_DISK_BYTES:
            logger.warning(
                "Address directory import refused: less than %d GB free on the disk of %s.",
                MIN_FREE_DISK_BYTES // 1024**3, purpose,
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
        try:
            if progress is not None:
                progress.phase("importing")
            count = _load_generation(archive, generation, token, progress)
            if progress is not None:
                progress.phase("publishing")
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
        # Ignore netrc/proxy credentials inherited from an operator environment.
        with requests.Session() as session, tempfile.TemporaryFile() as archive:
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
