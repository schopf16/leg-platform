"""Validation of uploaded logo and favicon files (D-073). Files are accepted exactly as they are or rejected, never rewritten.

The type is decided from the content. PNG: signature, every chunk's length and CRC-32, header,
palette, data chunks as one zlib stream inflating to exactly the stated size, end chunk last; no
image library. SVG: strict allow-list; the bytes are checked before any XML parser sees them (UTF-8,
no DOCTYPE/ENTITY/CDATA/processing instruction), depth, element count and size are limited, and only
the SVG namespace, fixed elements, attributes and value characters are accepted (no script, style,
image, foreignObject, a, use, animation, filters, editor data, or ``url()`` outside the same file).
This is the first of several layers (docs/SECURITY.md, section 6).
"""

import hashlib
import io
import re
import struct
import xml.etree.ElementTree as ElementTree
import zlib
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

PNG_MAX_BYTES = 1024 * 1024
SVG_MAX_BYTES = 200 * 1024
# The browser tab icon is small: smaller limits than a logo (it also keeps the upload body of the
# portal settings below the limit of ``middleware.BODY_LIMITS``: two logos plus the favicon).
FAVICON_PNG_MAX_BYTES = 128 * 1024
FAVICON_SVG_MAX_BYTES = 64 * 1024
PNG_MIN_SIDE = 64
PNG_MAX_SIDE = 4000
SVG_MAX_DEPTH = 20
SVG_MAX_ELEMENTS = 2000
SVG_MAX_TEXT = 500
# The most that is ever read from an upload.
MAX_UPLOAD_BYTES = PNG_MAX_BYTES + 1
FAVICON_MAX_UPLOAD_BYTES = max(FAVICON_PNG_MAX_BYTES, FAVICON_SVG_MAX_BYTES) + 1

WRONG_TYPE = _("Only PNG and SVG files are accepted.")
TOO_LARGE = _("The file is too large (maximum %(max)s).")
INVALID_PNG = _("The file is not a valid PNG image.")
PNG_SIZE = _("The image must be between 64 and 4000 pixels wide and high.")
PNG_PIXELS = _(
    "The image has too many pixels (at most 4 million in total, for example 2000 by 2000). "
    "Scale it down and upload it again."
)
PNG_METADATA = _(
    "The PNG contains extra data that the portal does not accept (text, a colour profile or "
    "similar). Export it again without metadata."
)
SVG_NO_VIEWBOX = _("The SVG needs a viewBox so that it can be scaled.")
SVG_REJECTED = _(
    "This SVG contains content that the portal does not accept (%(what)s). Save the file as "
    "\"plain SVG\" without scripts, images, styles or editor data and upload it again."
)


class LogoRejected(Exception):
    """A logo file that is not accepted.

    ``message`` is translatable, ``params`` its placeholders, ``reason`` a fixed code for the
    security log; it never contains any part of the file.
    """

    def __init__(self, message, reason, params=None):
        super().__init__(reason)
        self.message = message
        self.reason = reason
        self.params = params or {}


@dataclass(frozen=True)
class Logo:
    """A logo file that passed the checks: ``kind`` is "png" or "svg"."""

    kind: str
    data: bytes
    sha256: str

    @property
    def size(self):
        return len(self.data)


def _size_text(limit):
    """The limit as shown in the message: "1 MB", "200 KB", "128 KB"."""
    return "1 MB" if limit == 1024 * 1024 else f"{limit // 1024} KB"


def validate_logo(data, *, png_max_bytes=PNG_MAX_BYTES, svg_max_bytes=SVG_MAX_BYTES):
    """Return the ``Logo`` for the bytes or raise ``LogoRejected``; the byte limits are those of a logo unless given."""
    if data.startswith(_PNG_SIGNATURE):
        if len(data) > png_max_bytes:
            raise LogoRejected(TOO_LARGE, "png: too large", {"max": _size_text(png_max_bytes)})
        _check_png(data)
        kind = "png"
    elif _looks_like_svg(data):
        if len(data) > svg_max_bytes:
            raise LogoRejected(TOO_LARGE, "svg: too large", {"max": _size_text(svg_max_bytes)})
        _check_svg(data)
        kind = "svg"
    else:
        raise LogoRejected(WRONG_TYPE, "file: not a PNG or SVG file")
    return Logo(kind, bytes(data), hashlib.sha256(data).hexdigest())


def validate_favicon(data):
    """The same checks for the browser tab icon (PNG or SVG, nothing else), with smaller size limits.

    ICO is not accepted: there is no validator for it (a container of several images), and every current
    browser shows a PNG or SVG icon.
    """
    return validate_logo(data, png_max_bytes=FAVICON_PNG_MAX_BYTES, svg_max_bytes=FAVICON_SVG_MAX_BYTES)


# --- PNG ---

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_BIT_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_PNG_CHUNK_TYPE = re.compile(rb"[A-Za-z]{4}")
# Accepted ancillary chunks (with fixed lengths where they have one), each once and only before the
# image data. Every other one (text, iCCP, eXIf, time, private) could hide data or hit browser decoders (D-073).
_PNG_ANCILLARY_LENGTHS = {b"gAMA": 4, b"cHRM": 32, b"sRGB": 1, b"pHYs": 9}
_PNG_ANCILLARY = frozenset(_PNG_ANCILLARY_LENGTHS) | {b"tRNS", b"bKGD"}
# bKGD length by colour type; tRNS length of grey/true colour images (palette: one byte per entry).
_PNG_BACKGROUND_LENGTH = {0: 2, 4: 2, 2: 6, 6: 6, 3: 1}
_PNG_TRANSPARENCY_LENGTH = {0: 2, 2: 6}
# The work of the check and of every browser grows with the pixel count.
PNG_MAX_PIXELS = 4_000_000
# (x start, y start, x step, y step) of the seven passes of Adam7 interlacing.
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
_INFLATE_STEP = 1024 * 1024


def _invalid_png(detail):
    return LogoRejected(INVALID_PNG, f"png: {detail}")


def _passes(width, height, interlaced):
    """The (width, height) of every pass that has pixels: the whole image if not interlaced."""
    if not interlaced:
        yield width, height
        return
    for x0, y0, dx, dy in _ADAM7:
        pass_width, pass_height = -(-(width - x0) // dx), -(-(height - y0) // dy)
        if pass_width > 0 and pass_height > 0:
            yield pass_width, pass_height


def _expected_raw_size(width, height, bits_per_pixel, interlaced):
    """The number of bytes after inflating: a filter byte per row plus the packed pixels, per pass."""
    return sum(
        pass_height * (1 + (pass_width * bits_per_pixel + 7) // 8)
        for pass_width, pass_height in _passes(width, height, interlaced)
    )


def _row_starts(width, height, bits_per_pixel, interlaced):
    """The offsets in the inflated data at which a row (its filter byte) starts."""
    offset = 0
    for pass_width, pass_height in _passes(width, height, interlaced):
        stride = 1 + (pass_width * bits_per_pixel + 7) // 8
        for _row in range(pass_height):
            yield offset
            offset += stride


class _FilterTypes:
    """Checks the filter byte of every row while the data is inflated: it is 0 to 4."""

    def __init__(self, starts):
        self._starts = starts
        self._next = next(starts, None)
        self._seen = 0

    def feed(self, block):
        end = self._seen + len(block)
        while self._next is not None and self._next < end:
            if block[self._next - self._seen] > 4:
                raise _invalid_png("filter type")
            self._next = next(self._starts, None)
        self._seen = end


def _inflate_exactly(chunks, expected, filters):
    """Check the data chunks form one zlib stream of exactly ``expected`` bytes with a valid filter byte per row.

    Never more than ``expected`` plus one byte is produced, in steps of at most 1 MiB (bounded work and memory)."""
    inflater = zlib.decompressobj()
    total = 0

    def take(produced):
        nonlocal total
        total += len(produced)
        if total > expected:
            raise _invalid_png("image data larger than the header says")
        filters.feed(produced)

    try:
        for body in chunks:
            pending = body
            while pending:
                take(inflater.decompress(pending, min(_INFLATE_STEP, expected - total + 1)))
                pending = inflater.unconsumed_tail
        while not inflater.eof:
            produced = inflater.decompress(b"", min(_INFLATE_STEP, expected - total + 1))
            if not produced:
                break
            take(produced)
    except zlib.error:
        raise _invalid_png("image data cannot be inflated") from None
    if not inflater.eof or inflater.unused_data or total != expected:
        raise _invalid_png("image data incomplete")


def _check_png(data):
    position = len(_PNG_SIGNATURE)
    end = len(data)
    header = None
    palette_length = None
    data_chunks = []
    seen = set()
    previous = b""
    finished = False
    while position < end:
        if finished or end - position < 12:
            raise _invalid_png("data after the end chunk or a cut chunk")
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        if length > 0x7FFFFFFF or position + 12 + length > end:
            raise _invalid_png("chunk length")
        body = data[position + 8:position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length:position + 12 + length])
        if _PNG_CHUNK_TYPE.fullmatch(kind) is None or zlib.crc32(kind + body) != crc:
            raise _invalid_png("chunk type or checksum")
        if header is None:
            if kind != b"IHDR" or length != 13:
                raise _invalid_png("first chunk")
            header = _check_png_header(body)
        elif kind == b"IHDR":
            raise _invalid_png("second header")
        elif kind == b"IDAT":
            if data_chunks and previous != b"IDAT":
                raise _invalid_png("data chunks not in one run")
            data_chunks.append(body)
        elif kind == b"PLTE":
            if data_chunks or palette_length is not None or header["colour_type"] in (0, 4):
                raise _invalid_png("palette")
            if length == 0 or length % 3 or length // 3 > 2 ** min(header["bit_depth"], 8):
                raise _invalid_png("palette size")
            palette_length = length
        elif kind == b"IEND":
            if length:
                raise _invalid_png("end chunk")
            finished = True
        elif kind in _PNG_ANCILLARY:
            if kind in seen or data_chunks:
                raise _invalid_png("ancillary chunk position")
            seen.add(kind)
            _check_png_ancillary(kind, length, header["colour_type"], palette_length)
        elif kind[0:1].isupper():
            # A critical chunk that this check does not know.
            raise _invalid_png("unknown critical chunk")
        else:
            raise LogoRejected(PNG_METADATA, "png: chunk not allowed")
        previous = kind
        position += 12 + length
    if header is None or not finished or not data_chunks:
        raise _invalid_png("incomplete")
    if header["colour_type"] == 3 and palette_length is None:
        raise _invalid_png("palette missing")
    bits_per_pixel = _PNG_CHANNELS[header["colour_type"]] * header["bit_depth"]
    width, height, interlaced = header["width"], header["height"], header["interlaced"]
    expected = _expected_raw_size(width, height, bits_per_pixel, interlaced)
    _inflate_exactly(data_chunks, expected, _FilterTypes(_row_starts(width, height, bits_per_pixel, interlaced)))


def _check_png_ancillary(kind, length, colour_type, palette_length):
    """Length (and, for tRNS/bKGD of a palette image, place) of an accepted ancillary chunk per the specification."""
    if kind in _PNG_ANCILLARY_LENGTHS:
        valid = length == _PNG_ANCILLARY_LENGTHS[kind]
    elif kind == b"bKGD":
        valid = length == _PNG_BACKGROUND_LENGTH[colour_type] and (colour_type != 3 or palette_length)
    elif colour_type == 3:
        # tRNS of a palette image: one byte per entry, after the palette.
        valid = palette_length is not None and 1 <= length <= palette_length // 3
    else:
        valid = length == _PNG_TRANSPARENCY_LENGTH.get(colour_type)
    if not valid:
        raise _invalid_png("ancillary chunk length")


def _check_png_header(body):
    width, height, bit_depth, colour_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", body
    )
    if (
        colour_type not in _PNG_BIT_DEPTHS
        or bit_depth not in _PNG_BIT_DEPTHS[colour_type]
        or compression != 0
        or filtering != 0
        or interlace not in (0, 1)
    ):
        raise _invalid_png("header")
    if not (PNG_MIN_SIDE <= width <= PNG_MAX_SIDE and PNG_MIN_SIDE <= height <= PNG_MAX_SIDE):
        raise LogoRejected(PNG_SIZE, "png: size out of range")
    if width * height > PNG_MAX_PIXELS:
        raise LogoRejected(PNG_PIXELS, "png: too many pixels")
    return {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "colour_type": colour_type,
        "interlaced": bool(interlace),
    }


# --- SVG ---

_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_SVG_ELEMENTS = frozenset(
    "svg g path rect circle ellipse line polyline polygon defs linearGradient radialGradient "
    "stop clipPath title desc".split()
)
_TEXT_ELEMENTS = frozenset({"title", "desc"})
_PRESENTATION = frozenset(
    "fill fill-opacity fill-rule stroke stroke-width stroke-linecap stroke-linejoin "
    "stroke-miterlimit stroke-dasharray stroke-dashoffset stroke-opacity opacity clip-path "
    "clip-rule stop-color stop-opacity".split()
)
_SVG_ATTRIBUTES = _PRESENTATION | frozenset(
    "id version x y width height cx cy r rx ry x1 y1 x2 y2 fx fy fr points d transform viewBox "
    "preserveAspectRatio gradientUnits gradientTransform spreadMethod offset clipPathUnits "
    "style".split()
)
# A style attribute may set presentation properties only.
_STYLE_PROPERTIES = _PRESENTATION
# Letters, digits, white space and the signs that numbers, colours, transforms and path data need.
# No quote, "<", "&", "@", ":", ";", "=", "/" or backslash.
_VALUE_CHARACTERS = re.compile(r"[A-Za-z0-9#%.,()+\-_ \t\r\n]*")
_URL_OPENING = re.compile(r"url\s*\(", re.IGNORECASE)
_URL_REFERENCE = re.compile(r"url\(#([A-Za-z_][A-Za-z0-9_.\-]*)\)", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_FORBIDDEN_WORDS = ("javascript", "expression")
_XML_DECLARATION = re.compile(
    r'<\?xml version="1\.0"(?: encoding="[Uu][Tt][Ff]-8")?(?: standalone="(?:yes|no)")?\s*\?>'
)
_WHAT = re.compile(r"[A-Za-z:-]{1,40}")
_VIEW_BOX_NUMBERS = re.compile(r"\s*(-?[0-9.]+)[\s,]+(-?[0-9.]+)[\s,]+([0-9.]+)[\s,]+([0-9.]+)\s*")


def _rejected(reason, what):
    """An SVG rejection; ``what`` is shown to the person, so only a letters-only name passes, else "unknown"."""
    shown = what if _WHAT.fullmatch(what) else "unknown"
    return LogoRejected(SVG_REJECTED, f"svg: {reason}", {"what": shown})


def _looks_like_svg(data):
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<")


def _local_name(name):
    return name.rsplit("}", 1)[1] if name.startswith("{") else name


def _precheck_svg(data):
    """Checks on the bytes before a parser sees them: no DTD, so no entity."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _rejected("not UTF-8", "encoding") from None
    if "\x00" in text:
        raise _rejected("NUL character", "encoding")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise _rejected("doctype or entity", "doctype")
    if "<![cdata[" in lowered:
        raise _rejected("CDATA section", "cdata")
    declaration = _XML_DECLARATION.match(text.lstrip("\ufeff"))
    start = text.find("<?")
    if start != -1:
        # Only the XML declaration at the very start is an allowed processing instruction.
        if declaration is None or start != len(text) - len(text.lstrip("\ufeff")):
            raise _rejected("processing instruction", "processing-instruction")
        if text.find("<?", start + 2) != -1:
            raise _rejected("processing instruction", "processing-instruction")


def _check_value(value, references):
    if not _VALUE_CHARACTERS.fullmatch(value):
        raise _rejected("value not allowed", "value")
    lowered = re.sub(r"\s+", "", value).lower()
    if any(word in lowered for word in _FORBIDDEN_WORDS):
        raise _rejected("value not allowed", "value")
    found = _URL_REFERENCE.findall(value)
    if len(_URL_OPENING.findall(value)) != len(found):
        raise _rejected("url not allowed", "url")
    references.update(found)


def _check_attributes(element, references, identifiers):
    for attribute, value in element.attrib.items():
        if attribute not in _SVG_ATTRIBUTES:
            raise _rejected("attribute not allowed", _local_name(attribute))
        if attribute == "style":
            for declaration in value.split(";"):
                if not declaration.strip():
                    continue
                prop, separator, setting = declaration.partition(":")
                if not separator or prop.strip() not in _STYLE_PROPERTIES:
                    raise _rejected("style not allowed", "style")
                _check_value(setting, references)
            continue
        if attribute == "id":
            if _IDENTIFIER.fullmatch(value) is None:
                raise _rejected("value not allowed", "id")
            identifiers.add(value)
            continue
        _check_value(value, references)


def _check_svg_root(root):
    view_box = _VIEW_BOX_NUMBERS.fullmatch(root.attrib.get("viewBox", ""))
    if view_box is None:
        raise LogoRejected(SVG_NO_VIEWBOX, "svg: no viewBox")
    try:
        numbers = [float(number) for number in view_box.groups()]
    except ValueError:
        raise LogoRejected(SVG_NO_VIEWBOX, "svg: no viewBox") from None
    if numbers[2] <= 0 or numbers[3] <= 0:
        raise LogoRejected(SVG_NO_VIEWBOX, "svg: no viewBox")


def _check_svg(data):
    _precheck_svg(data)
    references = set()
    identifiers = set()
    depth = 0
    count = 0
    root = None
    try:
        for event, element in ElementTree.iterparse(io.BytesIO(data), events=("start", "end")):
            if event == "start":
                depth += 1
                count += 1
                if depth > SVG_MAX_DEPTH:
                    raise _rejected("too deep", "depth")
                if count > SVG_MAX_ELEMENTS:
                    raise _rejected("too many elements", "elements")
                if not element.tag.startswith("{" + _SVG_NAMESPACE + "}"):
                    raise _rejected("namespace not allowed", _local_name(element.tag))
                name = _local_name(element.tag)
                if name not in _SVG_ELEMENTS:
                    raise _rejected("element not allowed", name)
                if root is None:
                    if name != "svg":
                        raise _rejected("root element", name)
                    root = element
                elif name == "svg":
                    raise _rejected("element not allowed", name)
                _check_attributes(element, references, identifiers)
            else:
                depth -= 1
                name = _local_name(element.tag)
                text = element.text or ""
                if name in _TEXT_ELEMENTS:
                    if len(element) or len(text) > SVG_MAX_TEXT:
                        raise _rejected("text not allowed", name)
                elif text.strip():
                    raise _rejected("text not allowed", name)
                if any((child.tail or "").strip() for child in element):
                    raise _rejected("text not allowed", name)
    except ElementTree.ParseError:
        raise _rejected("not well-formed", "xml-error") from None
    except (ValueError, RecursionError):
        raise _rejected("not well-formed", "xml-error") from None
    if root is None:
        raise _rejected("not well-formed", "xml-error")
    _check_svg_root(root)
    if not references <= identifiers:
        raise _rejected("url not allowed", "url")
