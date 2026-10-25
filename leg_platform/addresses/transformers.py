"""Transformer station of each building, from the public BKW high-discount map (D-118).

The weekly run (``schedule.run_transformer_weekly``, Saturdays after the plant register) and the command
``scan_transformer_stations`` read one WMS layer of the public map server of BKW Netz. Only the map tiles that contain a
building of the postal codes of the service area (``ServicePostalCode``) are requested. Coloured dots are found by their
colour, each dot is matched to the nearest address point (``Address.easting``, ``Address.northing``), and the transformer
name is read from the label next to the dot with OCR. Only the cleaned name is stored, one row per building in
``TransformerStation`` (no raw label, no image, no URL). The method follows ``bkw_leg_scanner.py`` of the project owner.

Politeness and limits (D-118): one request at a time; at least ``TILE_PAUSE_SECONDS`` between two requests; a total time
limit ``MAX_SCAN_SECONDS`` per run; a hard total time limit ``REQUEST_SECONDS`` per tile request (``http_budget``, as in
the downloads of ``importer``); at most ``MAX_TILES_PER_RUN`` tiles per run (a larger area is refused, never cut); a
user agent that names LEG-Platform and the purpose; HTTPS only; no redirects; certificate verification on; no proxy
variables and no ``.netrc``; no credentials.

The image and OCR packages (numpy, scipy, Pillow, OpenCV, rapidocr-onnxruntime; ``requirements/transformers.txt``, server
only) are imported where they are used, so the rest of the application and its tests never load them.
"""

import io
import logging
import re
import threading
import time
from dataclasses import dataclass

import requests
from django.db import transaction
from django.utils import timezone

from . import importer, jobs
from .http_budget import Adapter, Budget
from .importer import ImportFailure
from .models import DIRECTORY_ALIAS, Address, Directory, ImportRequest, ServicePostalCode, TransformerStation

logger = logging.getLogger(__name__)

WMS_URL = importer.TRANSFORMER_WMS_URL
WMS_MAP = "/home/qgisuser/QGIS_Data_base_extern/extern/leg/high_discount.qgz"  # the map on the BKW server
WMS_LAYER = "high_discount"
CRS = "EPSG:2056"  # LV95
USER_AGENT = "LEG-Platform/1.0 (transformer station match of the LEG; read only, one request at a time)"
# Map geometry (the values of bkw_leg_scanner.py): tiles of 200 m, 2048 pixels, about 10 cm per pixel.
TILE_SIZE = 200.0
TILE_PX = 2048
TILE_OVERLAP = 5.0
# Politeness (D-118). The pause is measured from the end of one request to the start of the next.
TILE_PAUSE_SECONDS = 1.0
# At most this many tiles per run. A tile is one request; 300 requests with the pause take about 5 minutes of pauses
# plus the rendering of the server. A municipality of the service area needs well under this; a larger area is refused
# and the owner decides (D-118).
MAX_TILES_PER_RUN = 300
# The whole run, from the first request to the last tile: at most one hour.
MAX_SCAN_SECONDS = 3600
# Per request: connect and read (seconds); each one is also cut at the rest of the run's time.
REQUEST_TIMEOUT = (10, 60)
# Per request, hard: connect, headers and body together (a socket timeout alone lets a trickling server hold the run).
# The request is also cut at the rest of the run's time (D-118, docs/SECURITY.md section 6).
REQUEST_SECONDS = 60
MAX_TILE_BYTES = 16 * 1024 * 1024
# Matching and labels (the values of bkw_leg_scanner.py).
EXACT_RADIUS = 3.0  # metres: a dot this close to an address point is an exact match
FALLBACK_RADIUS = 50.0  # metres: otherwise the nearest dot within this radius
SATURATION_MIN = 50  # max(R, G, B) - min(R, G, B): dots are saturated, labels are grey
MIN_DOT_PIXELS = 6
MAX_DOT_PIXELS = 400
LABEL_MAX_DISTANCE = 150  # pixels from a dot to the centre of its label
OCR_UPSCALE = 2
TRAFO_PATTERN = re.compile(r"([A-Za-zÀ-ſ]*)[_\s]?TRA\s?(\d+)", re.IGNORECASE)
# What is stored: an optional place name, "_TRA" and the number (e.g. "Ittigen_TRA12" or "TRA12"); nothing else.
CLEAN_NAME = re.compile(r"(?:[^\W\d_]{1,40}_)?TRA\d{1,6}")
# Rows per statement that lists primary or EGID values (SQLite < 3.32 allows 999 host parameters, D-075).
IN_CHUNK = 900
BATCH_SIZE = 2000


class ScanFailure(ImportFailure):
    """A fixed, safe reason of a failed scan (``jobs.TRANSFORMER_FAILURES``); never map data or addresses."""


@dataclass(frozen=True)
class Building:
    egid: int
    postal_code: str
    east: float
    north: float


@dataclass
class Dot:
    east: float
    north: float
    col: float = 0.0
    row: float = 0.0
    # The raw text of the label: held in memory for the name only, never stored, logged or shown.
    raw_label: str = ""
    name: str = ""


@dataclass(frozen=True)
class Station:
    egid: int
    postal_code: str
    name: str
    match: str


@dataclass(frozen=True)
class Result:
    """The outcome of a successful scan (the statistic of the last success and the rows to store)."""

    postal_codes: tuple
    buildings: int
    with_name: int
    tiles: int
    stations: tuple


# ---------------------------------------------------------------- buildings


def postal_codes():
    """The postal codes of the service area, sorted (``ServicePostalCode``, entered by staff)."""
    return list(ServicePostalCode.objects.order_by("postal_code").values_list("postal_code", flat=True))


def load_buildings(codes):
    """One building per EGID of the postal codes, from the published address generation (with LV95 coordinates).

    A building with several address rows keeps its first row (the position is the same for its entrances).
    """
    generation = Directory.objects.filter(pk=1).values_list("generation", flat=True).first()
    if generation is None or not codes:
        return []
    rows = (
        Address.objects.filter(
            generation=generation, postal_code__in=codes,
            egid__isnull=False, easting__isnull=False, northing__isnull=False,
        )
        .order_by("egid", "postal_code", "pk")
        .values_list("egid", "postal_code", "easting", "northing")
    )
    seen = {}
    for egid, code, east, north in rows:
        seen.setdefault(egid, Building(egid, code, float(east), float(north)))
    return list(seen.values())


def occupied_cells(buildings, origin):
    """Grid cells (column, row) of ``TILE_SIZE`` that contain at least one building (the tiles to request)."""
    ox, oy = origin
    return sorted({(int((b.east - ox) // TILE_SIZE), int((b.north - oy) // TILE_SIZE)) for b in buildings})


# ---------------------------------------------------------------- map


def make_session():
    """A session without proxy variables, ``.netrc`` or redirects; certificate verification stays on."""
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    return session


def _png(data, px):
    """The tile as an RGBA array; ``ScanFailure`` unless it is a PNG of the requested size."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(io.BytesIO(bytes(data))) as picture:
            if picture.format != "PNG" or picture.size != (px, px):
                raise ScanFailure(jobs.MAP_ANSWER)
            return np.asarray(picture.convert("RGBA"))
    except (OSError, ValueError):
        raise ScanFailure(jobs.MAP_ANSWER) from None


def _render(session, params, px, timeout):
    """One GET of a tile and its checks; returns the RGBA array. Runs in the helper thread of ``fetch_tile``."""
    try:
        response = session.get(
            WMS_URL, params=params, timeout=timeout, allow_redirects=False, stream=True, verify=True,
        )
    except requests.RequestException:
        raise ScanFailure(jobs.MAP_UNREACHABLE) from None
    data = bytearray()
    try:
        with response:
            if response.status_code != 200:
                raise ScanFailure(jobs.MAP_ANSWER)
            media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media != "image/png":
                raise ScanFailure(jobs.MAP_ANSWER)
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_TILE_BYTES:
                    raise ScanFailure(jobs.MAP_ANSWER)
    except requests.RequestException:
        raise ScanFailure(jobs.MAP_UNREACHABLE) from None
    return _png(data, px)


def fetch_tile(session, bbox, px, timeout, seconds=REQUEST_SECONDS):
    """Render one WMS tile (GetMap, PNG) and return it as an RGBA array.

    One GET, no redirect, the answer must be a PNG of at most ``MAX_TILE_BYTES``. Every failure is a fixed ``ScanFailure``.

    Hard total deadline (D-118, ``docs/SECURITY.md`` section 6): the request runs in a helper thread and the caller waits
    at most ``seconds`` (connect, headers and body together). At the deadline every socket of the request is shut down
    (``http_budget.Budget``), so that a read which trickles bytes ends at once, and ``ScanFailure`` is raised. The helper
    thread does not touch the database.
    """
    if WMS_URL not in importer.APPROVED_WMS_URLS:
        raise ScanFailure(jobs.MAP_ANSWER)
    params = {
        "MAP": WMS_MAP, "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": WMS_LAYER, "STYLES": "", "CRS": CRS,
        "BBOX": ",".join(f"{value:.3f}" for value in bbox),
        "WIDTH": px, "HEIGHT": px, "FORMAT": "image/png", "TRANSPARENT": "TRUE",
    }
    budget = Budget()
    adapter = Adapter(budget)
    # The approved address is HTTPS; the plain HTTP prefix is mounted too, so that the budget covers any scheme.
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    outcome = {}

    def work():
        try:
            outcome["png"] = _render(session, params, px, timeout)
        except ScanFailure as failure:
            outcome["failure"] = failure
        except Exception:  # noqa: BLE001 - nothing of the answer may reach the caller or a log
            outcome["failure"] = ScanFailure(jobs.MAP_ANSWER)

    thread = threading.Thread(target=work, name="transformer-tile", daemon=True)
    thread.start()
    try:
        thread.join(seconds)
        if thread.is_alive():
            budget.expire()
            thread.join(1)
            raise ScanFailure(jobs.MAP_TOO_LONG)
    finally:
        adapter.close()
    if "failure" in outcome:
        raise outcome["failure"]
    return outcome["png"]


def find_dots(img):
    """Return (col, row) of every coloured dot of the tile (saturated, of a size between the two limits)."""
    from scipy import ndimage

    import numpy as np

    # int16 holds every channel value (0 to 255) and every difference of two of them exactly; int64 would copy 4x the bytes.
    rgb = img[..., :3].astype(np.int16)
    alpha = img[..., 3]
    saturation = rgb.max(axis=-1) - rgb.min(axis=-1)
    mask = (alpha >= 128) & (saturation >= SATURATION_MIN)
    labels, count = ndimage.label(mask)
    if count == 0:
        return []
    index = range(1, count + 1)
    sizes = ndimage.sum(mask, labels, index)
    centres = ndimage.center_of_mass(mask, labels, index)
    dots = []
    for size, (row, col) in zip(sizes, centres):
        if MIN_DOT_PIXELS <= size <= MAX_DOT_PIXELS:
            dots.append((float(col), float(row)))
    return dots


class LabelReader:
    """Reads the text of a tile with OCR (rapidocr-onnxruntime, imported here only, D-118)."""

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR

        self._ocr = RapidOCR()

    def read_tile(self, img):
        """Return (col, row, text) for every text line of the tile."""
        import numpy as np
        from PIL import Image

        rgb = Image.fromarray(img[..., :3]).convert("RGB")
        big = rgb.resize((rgb.width * OCR_UPSCALE, rgb.height * OCR_UPSCALE), Image.LANCZOS)
        result, _elapsed = self._ocr(np.asarray(big))
        if not result:
            return []
        labels = []
        for box, text, _score in result:
            centre_col = sum(point[0] for point in box) / 4 / OCR_UPSCALE
            centre_row = sum(point[1] for point in box) / 4 / OCR_UPSCALE
            labels.append((centre_col, centre_row, str(text).strip()))
        return labels


def assign_labels(dots, labels):
    """Give each dot the raw text of its nearest label, if one is close enough (in memory only)."""
    import numpy as np

    if not dots or not labels:
        return
    dot_positions = np.array([[d.col, d.row] for d in dots])
    for label_col, label_row, text in labels:
        distances = np.hypot(dot_positions[:, 0] - label_col, dot_positions[:, 1] - label_row)
        nearest = int(np.argmin(distances))
        if distances[nearest] <= LABEL_MAX_DISTANCE and not dots[nearest].raw_label:
            dots[nearest].raw_label = text


def normalise_trafo(raw):
    """Split a raw label into (place name, transformer number), or None."""
    match = TRAFO_PATTERN.search(raw)
    if not match:
        return None
    return match.group(1), match.group(2)


def settle_names(dots):
    """Build the clean names. OCR garbles the place name but the number is reliable: each number takes the most common
    place name among all dots that carry it (``bkw_leg_scanner.settle_trafo_names``). Only names that match
    ``CLEAN_NAME`` are kept; the raw text is not kept."""
    votes = {}
    for dot in dots:
        parsed = normalise_trafo(dot.raw_label)
        if parsed and parsed[0]:
            prefix, number = parsed
            counts = votes.setdefault(number, {})
            counts[prefix] = counts.get(prefix, 0) + 1
    for dot in dots:
        parsed = normalise_trafo(dot.raw_label)
        if not parsed:
            dot.name = ""
            continue
        _prefix, number = parsed
        if number in votes:
            best = max(votes[number].items(), key=lambda item: item[1])[0]
            candidate = f"{best}_TRA{number}"
        else:
            candidate = f"TRA{number}"
        dot.name = candidate if CLEAN_NAME.fullmatch(candidate) and len(candidate) <= 64 else ""


def scan_tiles(buildings, *, session, reader, sleep, clock):
    """Request the tiles that contain the buildings, one at a time, and return (dots, tile count).

    Refused before the first request when the buildings need more than ``MAX_TILES_PER_RUN`` tiles. The pause and the
    time limit are checked here, around every request.
    """
    origin = (min(b.east for b in buildings), min(b.north for b in buildings))
    cells = occupied_cells(buildings, origin)
    if len(cells) > MAX_TILES_PER_RUN:
        raise ScanFailure(jobs.MAP_TOO_MANY_TILES)
    deadline = clock() + MAX_SCAN_SECONDS
    fetch_px = TILE_PX + int(round(2 * TILE_OVERLAP / (TILE_SIZE / TILE_PX)))
    dots = []
    last_end = None
    for i, j in cells:
        if last_end is not None:
            wait = TILE_PAUSE_SECONDS - (clock() - last_end)
            if wait > 0:
                sleep(wait)
        remaining = deadline - clock()
        if remaining <= 0:
            raise ScanFailure(jobs.MAP_TOO_LONG)
        x0 = origin[0] + i * TILE_SIZE
        y0 = origin[1] + j * TILE_SIZE
        x1, y1 = x0 + TILE_SIZE, y0 + TILE_SIZE
        box = (x0 - TILE_OVERLAP, y0 - TILE_OVERLAP, x1 + TILE_OVERLAP, y1 + TILE_OVERLAP)
        timeout = (min(REQUEST_TIMEOUT[0], remaining), min(REQUEST_TIMEOUT[1], remaining))
        img = fetch_tile(session, box, fetch_px, timeout, seconds=min(REQUEST_SECONDS, remaining))
        last_end = clock()
        found = []
        for col, row in find_dots(img):
            east, north = _pixel_to_lv95(col, row, box, fetch_px)
            found.append(Dot(east=east, north=north, col=col, row=row))
        if found and reader is not None:
            assign_labels(found, reader.read_tile(img))
        # The dots of this tile's core only: the overlap of the tiles must not count a dot twice.
        dots.extend(d for d in found if x0 <= d.east < x1 and y0 <= d.north < y1)
    if clock() > deadline:
        raise ScanFailure(jobs.MAP_TOO_LONG)
    settle_names(dots)
    return dots, len(cells)


def _pixel_to_lv95(col, row, box, px):
    xmin, ymin, xmax, ymax = box
    east = xmin + (col + 0.5) * (xmax - xmin) / px
    north = ymax - (row + 0.5) * (ymax - ymin) / px
    return east, north


def match_stations(buildings, dots):
    """Pair every building with a dot: exact within ``EXACT_RADIUS``, nearest within ``FALLBACK_RADIUS``, else none."""
    import numpy as np
    from scipy.spatial import cKDTree

    if not buildings:
        return []
    if not dots:
        return [Station(b.egid, b.postal_code, "", "none") for b in buildings]
    tree = cKDTree(np.array([[d.east, d.north] for d in dots]))
    points = np.array([[b.east, b.north] for b in buildings])
    distances, indices = tree.query(points, distance_upper_bound=FALLBACK_RADIUS)
    stations = []
    for building, distance, index in zip(buildings, distances, indices):
        if not np.isfinite(distance):
            stations.append(Station(building.egid, building.postal_code, "", "none"))
            continue
        dot = dots[int(index)]
        kind = "exact" if float(distance) <= EXACT_RADIUS else "fallback"
        stations.append(Station(building.egid, building.postal_code, dot.name, kind))
    return stations


# ---------------------------------------------------------------- run and store


def run_scan(claim_token, *, progress=None, session=None, reader=None, sleep=time.sleep, clock=time.monotonic):
    """Scan the postal codes, match the buildings and store the result (D-118). Returns the ``Result``.

    Nothing is stored unless every tile was read: a failure raises a ``ScanFailure`` and the stored rows stay as they were.
    ``session``, ``reader``, ``sleep`` and ``clock`` are injected by the tests.
    """
    codes = postal_codes()
    if not codes:
        raise ScanFailure(jobs.SCAN_NO_POSTAL_CODES)
    buildings = load_buildings(codes)
    if not buildings:
        raise ScanFailure(jobs.SCAN_NO_BUILDINGS)
    try:
        import numpy  # noqa: F401  (the server package check; the import error is a fixed text)
        import scipy  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        raise ScanFailure(jobs.SCAN_READER_MISSING) from None
    if reader is None:
        try:
            reader = LabelReader()
        except ImportError:
            raise ScanFailure(jobs.SCAN_READER_MISSING) from None
    if progress is not None:
        progress.phase("downloading")
    dots, tiles = scan_tiles(
        buildings, session=session or make_session(), reader=reader, sleep=sleep, clock=clock,
    )
    stations = match_stations(buildings, dots)
    if progress is not None:
        progress.phase("publishing")
    result = Result(
        postal_codes=tuple(codes),
        buildings=len(stations),
        with_name=sum(1 for station in stations if station.name),
        tiles=tiles,
        stations=tuple(stations),
    )
    store(result, claim_token, timezone.now())
    return result


def _delete_rows(postal_code, egids):
    """Delete the rows of one postal code and the rows of these EGIDs (a building keeps one row), in short statements."""
    TransformerStation.objects.using(DIRECTORY_ALIAS).filter(postal_code=postal_code).delete()
    for start in range(0, len(egids), IN_CHUNK):
        TransformerStation.objects.using(DIRECTORY_ALIAS).filter(egid__in=egids[start:start + IN_CHUNK]).delete()


def store(result, claim_token, scanned_at):
    """Replace the rows of the scanned postal codes in one transaction, after a complete scan (D-118).

    The claim must still be ours. Rows of other postal codes are not touched, nor the rows of the address generation.
    """
    if not ImportRequest.objects.filter(pk=1, claim_token=claim_token).exists():
        raise ImportFailure("The import lease was lost.")
    with transaction.atomic(using=DIRECTORY_ALIAS):
        for code in result.postal_codes:
            rows = [station for station in result.stations if station.postal_code == code]
            _delete_rows(code, [station.egid for station in rows])
            TransformerStation.objects.using(DIRECTORY_ALIAS).bulk_create(
                [
                    TransformerStation(
                        egid=station.egid, postal_code=station.postal_code, name=station.name,
                        match=station.match, scanned_at=scanned_at,
                    )
                    for station in rows
                ],
                batch_size=BATCH_SIZE,
            )


def station_of(egid):
    """The transformer station of a building ({"name", "match"}), if the building is in the current address generation.

    Read only, two plain queries (no join across the databases). A row whose EGID left the generation is not shown.
    """
    generation = Directory.objects.filter(pk=1).values_list("generation", flat=True).first()
    if generation is None or not Address.objects.filter(generation=generation, egid=egid).exists():
        return None
    return TransformerStation.objects.filter(egid=egid).values("name", "match").first()
