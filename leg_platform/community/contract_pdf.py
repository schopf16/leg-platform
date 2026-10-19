"""Checks of an uploaded contract PDF (D-103). Pure functions; the type is decided from the content, never from the
name or the content type, and the file is never rewritten.

What is checked: size (at most 10 MiB), the ``%PDF-`` signature at the start, ``%%EOF`` near the end, no encryption
and none of the names of active content (JavaScript, launch and open actions, additional actions, embedded and rich
media files, forms that submit, remote go-to). Names written with ``#xx`` escapes are decoded first, and every
``stream`` that inflates with zlib (FlateDecode, also object streams) is scanned as well, within fixed budgets.

What is **not** checked (so a hostile PDF may pass): streams with other filters (LZW, ASCII85 chains, crypt),
streams beyond the budgets, and the semantics of the document (no PDF parser is used). The upload is staff-only and
the file is served as an attachment or in a sandboxed frame by the page (``X-Content-Type-Options: nosniff``).
"""

import re
import zlib

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MAX_SIZE = 10 * 1024 * 1024
MIN_SIZE = 64
# Inflating a stream is cut at this size, and the whole scan at this total (a decompression bomb).
STREAM_BUDGET = 4 * 1024 * 1024
TOTAL_BUDGET = 40 * 1024 * 1024

SIGNATURE = re.compile(rb"%PDF-[12]\.[0-9]")
# A name is complete when no regular character follows (PDF 32000-1, 7.3.5 delimiters and white space).
_END = rb"(?![^\s/<>\[\](){}%])"
ACTIVE_CONTENT = re.compile(
    rb"/(?:JavaScript|JS|AA|Launch|OpenAction|EmbeddedFile|EmbeddedFiles|RichMedia|XFA|SubmitForm|ImportData|GoToR|GoToE"
    rb"|Encrypt)" + _END
)
_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
# A stream keyword (and the very rare one without end) is found with plain ``bytes.find`` in one linear pass: a regular
# expression with a lazy ``.*?`` is quadratic on input like ``stream\n`` repeated without ``endstream``.
MAX_STREAM_KEYWORDS = 20_000

NOT_A_PDF = _("The file is not a PDF.")
TOO_LARGE = _("The PDF is larger than 10 MiB.")
ACTIVE_OR_ENCRYPTED = _(
    "The PDF is encrypted or contains active content (scripts, launch actions, forms or embedded files) and cannot be used."
)


def _decode_names(data):
    return _NAME_ESCAPE.sub(lambda match: bytes([int(match.group(1), 16)]), data)


def _streams(data):
    """The bytes between ``stream`` and ``endstream``, found by a linear scan. A ``stream`` keyword (followed by a line
    break) without an ``endstream`` after it makes the file invalid (``ValueError``), as do more than
    ``MAX_STREAM_KEYWORDS`` keywords."""
    position = 0
    seen = 0
    while True:
        start = data.find(b"stream", position)
        if start < 0:
            return
        seen += 1
        if seen > MAX_STREAM_KEYWORDS:
            raise ValueError("too many streams")
        position = begin = start + len(b"stream")
        if data[start - 3 : start] == b"end":
            continue  # the end of a stream, not a start
        if data.startswith(b"\r\n", begin):
            begin += 2
        elif data[begin : begin + 1] in (b"\n", b"\r"):
            begin += 1
        else:
            continue  # the word in a text, not a stream keyword
        end = data.find(b"endstream", begin)
        if end < 0:
            raise ValueError("a stream without its end")
        yield data[begin:end]
        position = end + len(b"endstream")


def _has_active_content(data):
    """True if active content is found; ``ValueError`` if the streams cannot be delimited."""
    if ACTIVE_CONTENT.search(_decode_names(data)):
        return True
    scanned = 0
    for chunk in _streams(data):
        if scanned >= TOTAL_BUDGET:
            break
        if len(chunk) < 2:
            continue  # nothing that could be zlib data
        try:
            inflated = zlib.decompressobj().decompress(chunk, STREAM_BUDGET)
        except zlib.error:
            continue
        scanned += len(inflated)
        if ACTIVE_CONTENT.search(_decode_names(inflated)):
            return True
    return False


def validate_pdf(data):
    """Raise ``ValidationError`` (codes ``not_a_pdf``, ``too_large``, ``active_content``) unless the bytes pass."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < MIN_SIZE:
        raise ValidationError(NOT_A_PDF, code="not_a_pdf")
    if len(data) > MAX_SIZE:
        raise ValidationError(TOO_LARGE, code="too_large")
    data = bytes(data)
    if SIGNATURE.match(data) is None or b"%%EOF" not in data[-1024:]:
        raise ValidationError(NOT_A_PDF, code="not_a_pdf")
    try:
        active = _has_active_content(data)
    except ValueError:
        raise ValidationError(NOT_A_PDF, code="not_a_pdf") from None
    if active:
        raise ValidationError(ACTIVE_OR_ENCRYPTED, code="active_content")
