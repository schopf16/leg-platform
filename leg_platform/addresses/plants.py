"""The plant register of the Federal Office of Energy (BFE, D-117): one row per plant in the addresses file.

The import runs only in the daily run, after the address import of the day (``schedule.run_daily`` calls
``schedule.run_plant_daily``), with the claim of ``jobs`` (D-090): one writer of the addresses file at a time, also
against the address import. The download comes from the one approved URL (``importer.PLANT_URL``); the ZIP is streamed to
a temporary file next to the directory file and hashed on the way. The SHA-256 is stored and compared only with the
published hash, to see whether the file changed; the BFE publishes no checksum (D-117, residual risk).

A new generation stays invisible until one short transaction switches ``PlantRegister``; a failure before that leaves the
previous generation serving. Rows that fail the checks and rows without EGID are counted and not stored; they never refuse
the import. The import is refused only when no plant is stored or when the row count moves by more than a fixed factor
(D-117). The statistic of the last successful import is stored with the register (``PlantRegister``, D-117).
"""

import csv
import email.utils
import hashlib
import logging
import re
import tempfile
import uuid
import zipfile
from decimal import Decimal
from typing import NamedTuple

import requests
from django.db import DatabaseError, connections, transaction
from django.utils import timezone

from .importer import (
    PLANT_URL,
    PRUNE_BATCH_SIZE,
    ImportFailure,
    _chunks,
    _directory_folder,
    _finish_file,
    _require_free_disk,
    declared_entry_count,
)
from .models import Directory, ImportRequest, Plant, PlantRegister
from .progress import count_rows
from .storage import DIRECTORY_ALIAS

logger = logging.getLogger(__name__)

# The fixed texts of this module; ``jobs.PLANT_FAILURES`` lists them (a test compares the two).
FORMAT_FAILURE = "The plant register file has an unexpected format."
SIZE_FAILURE = "The plant register file is larger than allowed."
CHANGE_FAILURE = "The number of plants changed too much. Nothing was changed."
EMPTY_FAILURE = "The plant register contains no plants."
UNREADABLE_FAILURE = "The plant register could not be downloaded or read."
# The import was taken over (its claim is no longer the job's); the same sentence as ``importer`` and ``runner`` use.
LEASE_FAILURE = "The import lease was lost."

# The one member that is read. The official ZIP (verified 2026-10-24 with a HEAD request and the sample) also holds the
# catalogues and details listed below; they are checked like every member but never read or extracted.
PLANT_CSV_NAME = "ElectricityProductionPlant.csv"
PLANT_OTHER_MEMBERS = frozenset({
    "MainCategoryCatalogue.csv", "OrientationCatalogue.csv", "PlantCategoryCatalogue.csv",
    "PlantDetail.csv", "SubCategoryCatalogue.csv",
})
# Media types of the official file as served (HEAD of the fixed URL, 2026-10-24: application/octet-stream).
PLANT_CONTENT_TYPES = frozenset({"application/zip", "application/octet-stream"})
MAX_PLANT_ARCHIVE_BYTES = 64 * 1024 * 1024  # the ZIP (19.7 MB on 2026-10-24)
MAX_PLANT_CSV_BYTES = 128 * 1024 * 1024  # the uncompressed plant file (43 MB on 2026-10-24, about three times that)
MAX_PLANT_UNPACKED_BYTES = 192 * 1024 * 1024  # all members together, as declared in the ZIP
MAX_PLANT_MEMBERS = 16
MAX_PLANT_COMPRESSION_RATIO = 100
MAX_PLANT_LINE_BYTES = 16 * 1024
MAX_PLANT_ROWS = 1_000_000  # 342,000 rows on 2026-10-24
PLANT_CHUNK_ROWS = 20_000
# SQLite < 3.32 allows 999 host parameters per statement (D-075): 900 divided by the seven inserted columns.
PLANT_BATCH_SIZE = 128
# The row count of the new generation may differ from the published one by at most this factor (up or down).
MAX_ROW_CHANGE_FACTOR = 2
# Power of one plant in kW (TotalPower, Decimal): ten gigawatts is far above any plant of the register (1.5 GW on
# 2026-10-24, the largest).
MAX_PLANT_POWER_KW = Decimal("10000000")
# LV95 (EPSG:2056) range of the coordinates: the range of the file (E 2'486'220 to 2'831'206, N 1'075'619 to 1'294'828
# on 2026-10-24) rounded outwards. Not checked against an official swisstopo extent (unverified).
LV95_EAST = (Decimal("2480000"), Decimal("2840000"))
LV95_NORTH = (Decimal("1070000"), Decimal("1300000"))
REQUIRED_COLUMNS = ("xtf_id", "EGID", "SubCategory", "TotalPower", "_x", "_y")
XTF_ID_PATTERN = r"[0-9A-Za-z_.\-]{1,64}"
EGID_PATTERN = r"[1-9][0-9]{0,8}"
SUBCATEGORY_PATTERN = r"subcat_[0-9]{1,3}"
POWER_PATTERN = r"[0-9]{1,8}(?:\.[0-9]{1,3})?"
COORDINATE_PATTERN = r"[0-9]{6,7}(?:\.[0-9]{1,3})?"


class _Rejected(Exception):
    """A row that fails the checks: counted, its text is not kept."""


class PlantRow(NamedTuple):
    xtf_id: str
    egid: int
    subcategory: str
    total_power_kw: Decimal
    x: Decimal | None
    y: Decimal | None


def _plant_lines(member):
    """Lines of the plant file as text (UTF-8, BOM at the start allowed), at most the size limits."""
    total = 0
    while line := member.readline(MAX_PLANT_LINE_BYTES + 1):
        total += len(line)
        if len(line) > MAX_PLANT_LINE_BYTES or total > MAX_PLANT_CSV_BYTES:
            raise ImportFailure(SIZE_FAILURE)
        yield line.decode("utf-8-sig" if total == len(line) else "utf-8", errors="strict")


def _in_range(value, bounds):
    return bounds[0] <= value <= bounds[1]


def _plant(values, indexes, seen):
    """The plant of one row; None for a valid plant without EGID (skipped, not stored); ``_Rejected`` for a failed row.

    Every field is checked first, also for a row without EGID: such a row is only skipped when it is valid (M3).
    """
    xtf_id = values[indexes["xtf_id"]]
    if not re.fullmatch(XTF_ID_PATTERN, xtf_id) or xtf_id in seen:
        raise _Rejected
    egid_text = values[indexes["EGID"]]
    if egid_text != "" and not re.fullmatch(EGID_PATTERN, egid_text):
        raise _Rejected
    subcategory = values[indexes["SubCategory"]]
    if not re.fullmatch(SUBCATEGORY_PATTERN, subcategory):
        raise _Rejected
    power_text = values[indexes["TotalPower"]]
    if not re.fullmatch(POWER_PATTERN, power_text):
        raise _Rejected
    power = Decimal(power_text)
    if not power.is_finite() or power > MAX_PLANT_POWER_KW:
        raise _Rejected
    x_text = values[indexes["_x"]]
    y_text = values[indexes["_y"]]
    if x_text == "" and y_text == "":
        x = y = None
    elif re.fullmatch(COORDINATE_PATTERN, x_text) and re.fullmatch(COORDINATE_PATTERN, y_text):
        x = Decimal(x_text)
        y = Decimal(y_text)
        if not (_in_range(x, LV95_EAST) and _in_range(y, LV95_NORTH)):
            raise _Rejected
    else:
        raise _Rejected
    seen.add(xtf_id)
    if egid_text == "":
        return None
    return PlantRow(xtf_id, int(egid_text), subcategory, power, x, y)


def _check_members(members):
    names = [member.filename for member in members]
    if len(members) > MAX_PLANT_MEMBERS or len(set(names)) != len(names) or names.count(PLANT_CSV_NAME) != 1:
        raise ImportFailure(FORMAT_FAILURE)
    if any(name != PLANT_CSV_NAME and name not in PLANT_OTHER_MEMBERS for name in names):
        raise ImportFailure(FORMAT_FAILURE)
    for member in members:
        if (
            member.is_dir()
            or member.flag_bits & 1
            or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or member.file_size > max(member.compress_size, 1) * MAX_PLANT_COMPRESSION_RATIO
        ):
            raise ImportFailure(FORMAT_FAILURE)
    if sum(member.file_size for member in members) > MAX_PLANT_UNPACKED_BYTES:
        raise ImportFailure(SIZE_FAILURE)


def _plant_rows(archive, counts):
    """Yield the plants of the archive; ``counts`` collects the rows without EGID ("skipped"), the failed rows ("rejected")
    and, after the last row, the rows of the file ("rows")."""
    # The declared count is read before zipfile parses the central directory (M1).
    entries = declared_entry_count(archive)
    if entries is None or entries > MAX_PLANT_MEMBERS:
        raise ImportFailure(FORMAT_FAILURE)
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        _check_members(members)
        member = next(item for item in members if item.filename == PLANT_CSV_NAME)
        if member.file_size > MAX_PLANT_CSV_BYTES:
            raise ImportFailure(SIZE_FAILURE)
        with zipped.open(member) as stream:
            reader = csv.reader(_plant_lines(stream), delimiter=",", strict=True)
            header = next(reader, [])
            if len(header) != len(set(header)) or not all(column in header for column in REQUIRED_COLUMNS):
                raise ImportFailure(FORMAT_FAILURE)
            indexes = {column: header.index(column) for column in REQUIRED_COLUMNS}
            seen = set()
            count = 0
            for values in reader:
                count += 1
                if count > MAX_PLANT_ROWS:
                    raise ImportFailure(SIZE_FAILURE)
                if len(values) != len(header):
                    counts["rejected"] += 1
                    continue
                try:
                    plant = _plant(values, indexes, seen)
                except _Rejected:
                    counts["rejected"] += 1
                    continue
                if plant is None:
                    counts["skipped"] += 1
                    continue
                yield plant
            counts["rows"] = count


def plant_rows(archive, counts):
    """Parse a verified archive; format errors become ``ImportFailure`` without the file's content."""
    try:
        yield from _plant_rows(archive, counts)
    except (zipfile.BadZipFile, csv.Error, UnicodeError) as exc:
        raise ImportFailure(FORMAT_FAILURE) from exc


def _delete_generation(generation):
    """Delete a generation in short statements, each over a bounded list of primary keys."""
    while pks := list(
        Plant.objects.filter(generation=generation).order_by().values_list("pk", flat=True)[:PRUNE_BATCH_SIZE]
    ):
        Plant.objects.filter(pk__in=pks).delete()


def _insert(rows, progress):
    """Insert one chunk; the claim is checked first (D-090), so a job taken over stops before it writes."""
    if progress is not None and progress.check is not None:
        progress.check()
    Plant.objects.bulk_create(rows, batch_size=PLANT_BATCH_SIZE)
    return len(rows)


def _load_generation(archive, generation, progress):
    """Insert the plants as the (still invisible) generation; return (stored, counts) with the counts of ``plant_rows``."""
    counts = {"rejected": 0, "skipped": 0}
    stored = 0
    if progress is not None:
        progress.set_rows_total(count_rows(archive, PLANT_CSV_NAME, MAX_PLANT_ROWS))
    chunk = []
    for plant in plant_rows(archive, counts):
        chunk.append(Plant(
            generation=generation, xtf_id=plant.xtf_id, egid=plant.egid, subcategory=plant.subcategory,
            total_power_kw=plant.total_power_kw, x=plant.x, y=plant.y,
        ))
        if len(chunk) >= PLANT_CHUNK_ROWS:
            stored += _insert(chunk, progress)
            chunk = []
            if progress is not None:
                progress.rows(stored)
    if chunk:
        stored += _insert(chunk, progress)
    if progress is not None:
        progress.rows(stored)
    return stored, counts


def _require_plausible(stored, previous_count):
    """Refuse the generation when it is empty or its size moved by more than the factor. Rows that fail the checks and
    rows without EGID are left out and counted; they never refuse it (owner decision, D-117)."""
    if stored == 0:
        raise ImportFailure(EMPTY_FAILURE)
    if previous_count and (stored * MAX_ROW_CHANGE_FACTOR < previous_count or stored > previous_count * MAX_ROW_CHANGE_FACTOR):
        raise ImportFailure(CHANGE_FAILURE)


def _discard_generation(generation):
    """Delete the rows of an unpublished generation (best effort)."""
    try:
        _delete_generation(generation)
    except DatabaseError:
        # The next successful import deletes every generation that is not the published one.
        logger.warning("An unpublished plant register generation could not be deleted.")


def _prune_generations(keep):
    """Delete every generation except ``keep``, the published one (as ``importer.prune_old_generations``, M4)."""
    old = list(Plant.objects.exclude(generation=keep).order_by().values_list("generation", flat=True).distinct())
    for generation in old:
        _delete_generation(generation)


def _switch(generation, previous, claim_token, values):
    """The one switch of the published generation (D-117, M4).

    The job row is locked (``select_for_update``, the lock that ``jobs.claim`` takes too) and must still hold this
    claim; the switch of the directory file runs inside that transaction and only if the published generation is still
    ``previous``, the one this import started from. The job row lives in the default database and the plants in the
    directory file (D-075), so no single transaction spans both: the lock keeps a takeover off until the switch is
    committed (on a database with row locks), and the guard on ``previous`` refuses a switch over a newer generation.
    """
    with transaction.atomic():
        held = list(
            ImportRequest.objects.select_for_update()
            .filter(pk=1, claim_token=claim_token, state=ImportRequest.State.RUNNING)
            .values_list("pk", flat=True)
        )
        if not held:
            raise ImportFailure(LEASE_FAILURE)
        with transaction.atomic(using=DIRECTORY_ALIAS):
            switched = PlantRegister.objects.filter(pk=1, generation=previous).update(generation=generation, **values)
        if not switched:
            raise ImportFailure(LEASE_FAILURE)


def _address_count():
    """The address count of the published directory (its row count, 0 when there is none); read only (D-117)."""
    return Directory.objects.filter(pk=1).values_list("row_count", flat=True).first() or 0


def _publish(archive, checksum, data_date, progress, claim_token):
    """Publish a verified archive as a new generation; returns the number of stored plants.

    The same file as the published one (same SHA-256) is not loaded again: its plants and counts stay, and the statistic
    is rewritten with the time of this run and the current address count (D-117).
    """
    register, _created = PlantRegister.objects.get_or_create(pk=1)
    if register.row_count > 0 and register.checksum == checksum:
        values = {"data_date": data_date, "imported_at": timezone.now(), "address_count": _address_count()}
        _switch(register.generation, register.generation, claim_token, values)
        return register.row_count
    previous = register.generation
    previous_count = register.row_count
    # The day the content changed (D-117 hint): today for another file, the old day for the same one.
    content_changed_at = timezone.localdate() if register.checksum != checksum else register.content_changed_at
    generation = uuid.uuid4()
    if progress is not None:
        progress.phase("importing")
    try:
        stored, counts = _load_generation(archive, generation, progress)
        _require_plausible(stored, previous_count)
        # The statistic of this import (D-117): the address count of the published directory and the rows of the file.
        values = {
            "data_date": data_date, "imported_at": timezone.now(), "checksum": checksum,
            "row_count": stored, "rejected_count": counts["rejected"], "skipped_count": counts["skipped"],
            "file_row_count": counts["rows"], "address_count": _address_count(),
            "content_changed_at": content_changed_at,
        }
        if progress is not None:
            progress.phase("publishing")
        _switch(generation, previous, claim_token, values)
    except BaseException:
        _discard_generation(generation)
        raise
    # Only the new generation is kept after the switch: every other one, not just the previous one (M4).
    try:
        if progress is not None:
            progress.phase("finishing")
        _prune_generations(generation)
    except DatabaseError:
        logger.warning("Old plant register generations could not be deleted.")
    # After the claim is still ours: no write follows the checkpoint.
    _finish_file()
    return stored


def _download(session, archive, progress):
    """Stream the ZIP to ``archive`` and hash it on the way; returns (SHA-256 hex, Last-Modified header or None)."""
    digest = hashlib.sha256()
    headers = {}
    on_chunk = progress.download if progress is not None else None

    def remember(response_headers):
        headers["last_modified"] = response_headers.get("Last-Modified")

    for chunk in _chunks(
        session, PLANT_URL, MAX_PLANT_ARCHIVE_BYTES, on_chunk,
        content_types=PLANT_CONTENT_TYPES, on_headers=remember,
    ):
        digest.update(chunk)
        archive.write(chunk)
    archive.seek(0)
    return digest.hexdigest(), headers.get("last_modified")


def _data_date(last_modified):
    """The date of the data (the server's Last-Modified time), or None when the server gives none that can be read."""
    if not last_modified:
        return None
    try:
        moment = email.utils.parsedate_to_datetime(last_modified)
    except (TypeError, ValueError, IndexError):
        return None
    return timezone.localtime(moment).date() if moment.tzinfo else None


def import_plants(*, claim_token, progress=None):
    """Download, check and publish the plant register; returns the number of stored plants (D-117).

    Only the holder of the claim ``claim_token`` calls this (``jobs.execute``). Raises ``ImportFailure`` with a fixed text.
    """
    try:
        try:
            connections[DIRECTORY_ALIAS].ensure_connection()
        except DatabaseError as exc:
            raise ImportFailure(UNREADABLE_FAILURE) from exc
        # Like the address archive: next to the directory file, not in the system temporary folder.
        with requests.Session() as session, tempfile.TemporaryFile(dir=_directory_folder()) as archive:
            # Ignore netrc and proxy settings inherited from the environment of the operator.
            session.trust_env = False
            _require_free_disk()
            if progress is not None:
                progress.phase("downloading")
            checksum, last_modified = _download(session, archive, progress)
            if progress is not None:
                progress.downloaded = True
            return _publish(archive, checksum, _data_date(last_modified), progress, claim_token)
    except ImportFailure:
        raise
    except (requests.RequestException, OSError) as exc:
        raise ImportFailure(UNREADABLE_FAILURE) from exc
