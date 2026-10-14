"""Bounded importer for the fixed official Swiss CSV asset.

Sources: D-049 (owner-approved columns/licence), official STAC item, Python
zipfile/csv documentation, Requests streaming documentation. No archive member
is extracted. The temporary download is deleted even on verification failure.
"""

import csv
import hashlib
import hmac
import json
import re
import tempfile
import time
import uuid
import zipfile
from datetime import date, datetime

import requests
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, transaction
from django.utils import timezone

from .models import Address, Directory, WRITE_ALIAS

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


class ImportFailure(Exception):
    """Safe generic failure; do not include upstream responses or CSV data."""


def require_writer():
    if WRITE_ALIAS not in connections.databases:
        raise ImproperlyConfigured("The isolated address_import connection is required.")
    writer = connections.databases[WRITE_ALIAS]
    if writer.get("ENGINE") not in {"django.db.backends.mysql", "django.db.backends.sqlite3"}:
        raise ImproperlyConfigured("The address importer requires a transactional database.")


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


def _publish(archive, checksum, data_date):
    generation = uuid.uuid4()
    # All rows and the pointer commit together. The row lock serializes concurrent
    # operators/workers; existing nonlocking readers continue using the old data.
    # InnoDB is mandatory in deployment. Previous generations are not deleted.
    with transaction.atomic(using=WRITE_ALIAS):
        Directory.objects.using(WRITE_ALIAS).get_or_create(pk=1)
        directory = Directory.objects.using(WRITE_ALIAS).select_for_update().get(pk=1)
        if directory.data_date and data_date < directory.data_date:
            raise ImportFailure("Catalogue data date predates the published directory.")
        if directory.checksum == checksum and directory.imported_at:
            return directory.row_count
        batch = []
        count = 0
        for values in address_rows(archive):
            batch.append(Address(generation=generation, **values))
            count += 1
            if len(batch) >= BATCH_SIZE:
                Address.objects.using(WRITE_ALIAS).bulk_create(batch, batch_size=BATCH_SIZE)
                batch.clear()
        if batch:
            Address.objects.using(WRITE_ALIAS).bulk_create(batch, batch_size=BATCH_SIZE)
        directory.generation = generation
        directory.data_date = data_date
        directory.imported_at = timezone.now()
        directory.checksum = checksum
        directory.row_count = count
        directory.save(using=WRITE_ALIAS)
        return count


def import_directory():
    """Download, verify and publish; callable only in the isolated worker."""
    require_writer()
    try:
        # Ignore netrc/proxy credentials inherited from an operator environment.
        with requests.Session() as session, tempfile.TemporaryFile() as archive:
            session.trust_env = False
            checksum, data_date = _metadata(session)
            digest = hashlib.sha256()
            for chunk in _chunks(session, ASSET_URL, MAX_ARCHIVE_BYTES):
                digest.update(chunk)
                archive.write(chunk)
            if not hmac.compare_digest(digest.hexdigest(), checksum):
                raise ImportFailure("Official archive checksum mismatch.")
            archive.seek(0)
            return _publish(archive, checksum, data_date)
    except ImportFailure:
        raise
    except (requests.RequestException, zipfile.BadZipFile, csv.Error, UnicodeError, OSError) as exc:
        raise ImportFailure("The official directory could not be imported.") from exc
