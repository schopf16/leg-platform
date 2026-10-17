"""Application log files and their reading (D-092).

The logging configuration writes two bounded, rotating files into one configured folder: the
application log and the security log (the lines of logger ``leg_platform.security``). Files and
folder are created for the service user only (0600 / 0700). The system administrator's log page
reads them through the fixed names of this module: no client-supplied path ever reaches the file
system, every line is redacted again when it is read, and every response is size-bounded.
"""

import codecs
import os
import re
import stat
from logging.handlers import RotatingFileHandler
from pathlib import Path

from leg_platform.log_filters import redact_personal_data, redact_tokens, safe_cut

APPLICATION_LOG = "application.log"
SECURITY_LOG = "security.log"
# The only names the page lists, tails and serves: the two files and their numbered backups.
LOG_NAME_PATTERN = re.compile(r"(?:application|security)\.log(?:\.[1-9][0-9]?)?")
# Rotation: at most (BACKUPS + 1) * MAX_BYTES per file (several processes can rotate at once, D-092).
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
# Bounds of the live view: the first answer and every later one (bytes, not lines).
TAIL_LINES = 200
TAIL_MAX_BYTES = 128 * 1024
POLL_MAX_BYTES = 64 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_LINE_BYTES = 16 * 1024


class SafeRotatingFileHandler(RotatingFileHandler):
    """A rotating file that is created with mode 0600 (the log may name internal numbers and paths).

    The size limit is per file; several processes writing the same file may rotate at the same time
    (the standard library does not coordinate them), which can reorder or drop older lines, never grow the files
    without bound.
    """

    def _open(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        descriptor = os.open(self.baseFilename, flags, 0o600)
        return os.fdopen(descriptor, self.mode, encoding=self.encoding, errors=self.errors)


def usable_log_directory(path):
    """Return ``path`` if the folder exists or can be created (mode 0700) and is writable, else ""."""
    if not path:
        return ""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        if not os.path.isdir(path) or not os.access(path, os.W_OK | os.X_OK):
            return ""
    except OSError:
        return ""
    return str(path)


def logging_config(directory):
    """The ``LOGGING`` setting; with a ``directory`` the application and security logs are also written to files."""
    files = bool(directory)
    handlers = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "filters": ["redact_tokens"],
        },
    }
    application = ["console"]
    security = ["console"]
    if files:
        for name, handler in ((APPLICATION_LOG, "application_file"), (SECURITY_LOG, "security_file")):
            handlers[handler] = {
                "class": "leg_platform.logfiles.SafeRotatingFileHandler",
                "filename": str(Path(directory) / name),
                "maxBytes": MAX_BYTES,
                "backupCount": BACKUPS,
                "encoding": "utf-8",
                # The file is opened by the first line: a process that never logs creates nothing.
                "delay": True,
                "level": "INFO",
                "formatter": "default",
                "filters": ["redact_tokens"],
            }
        application.append("application_file")
        security.append("security_file")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            # Removes tokens of verification links and personal data from the logged text.
            "redact_tokens": {"()": "leg_platform.log_filters.TokenRedactionFilter"},
        },
        "formatters": {
            # One line per message: control characters of a logged value are escaped (M-1).
            "default": {"()": "leg_platform.log_filters.SafeFormatter", "format": "{asctime} {levelname} {name} {message}", "style": "{"},
        },
        "handlers": handlers,
        "root": {"handlers": application, "level": "WARNING"},
        "loggers": {
            "django": {"handlers": application, "level": "WARNING", "propagate": False},
            # The development server's request log names every requested path: console only.
            "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
            # Security events: the internal number of the account and nothing else.
            "leg_platform.security": {"handlers": security, "level": "INFO", "propagate": False},
            # The import of the address directory reports its start, end and failure (D-090).
            "leg_platform.addresses": {"level": "INFO"},
            # MSAL logs a non-JSON token response at ERROR level with the body, which can contain
            # tenant details: only CRITICAL is written for this logger (D-048).
            "msal.oauth2cli.oauth2": {"handlers": ["console"], "level": "CRITICAL", "propagate": False},
            "msal.oauth2cli.assertion": {"handlers": ["console"], "level": "CRITICAL", "propagate": False},
        },
    }


def redact(text):
    """The text of a log line as shown or downloaded: tokens of links and personal data removed."""
    return redact_personal_data(redact_tokens(text))


def list_files(directory):
    """The log files of ``directory`` that exist, as dicts ``name``, ``size``, ``modified`` (a timestamp); current files first."""
    if not directory:
        return []
    found = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    for entry in entries:
        if not LOG_NAME_PATTERN.fullmatch(entry.name):
            continue
        try:
            status = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(status.st_mode):
            found.append({"name": entry.name, "size": status.st_size, "modified": status.st_mtime})
    return sorted(found, key=lambda item: (item["name"].split(".log")[0], len(item["name"]), item["name"]))


def resolve(directory, name):
    """The path of the log file called ``name``, or None.

    ``name`` must match the fixed pattern; the file must be a regular file inside ``directory``
    that is not a symbolic link. Nothing else is ever opened. The check is repeated on the open file
    (``open_log``), so a link put in place between the check and the opening is not followed either.
    """
    if not directory or not isinstance(name, str) or not LOG_NAME_PATTERN.fullmatch(name):
        return None
    path = Path(directory) / name
    try:
        status = path.lstat()
    except OSError:
        return None
    return path if stat.S_ISREG(status.st_mode) else None


def open_log(path):
    """The log file opened for reading without following a symbolic link (L-2 of the review of 2026-10-17).

    ``O_NOFOLLOW`` refuses a link at the opening itself; the open descriptor is checked with ``fstat``: a regular
    file or ``OSError``. Windows has no ``O_NOFOLLOW`` (``resolve`` has checked the name before, which is the
    protection there; a link needs the right to write into the folder of the service user in any case)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{path} is not a regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _decode(data):
    return redact(data.decode("utf-8", errors="replace"))


def _file_id(status):
    """An identifier of the file behind a name that changes when the file is rotated."""
    return f"{status.st_dev}-{status.st_ino}"


def _complete_lines(data):
    """Cut ``data`` after its last line break so that no half line is shown. A line longer than the limit is shown
    as far as it goes, cut at its last blank where there is one (a value that straddles the cut would not be redacted)."""
    cut = data.rfind(b"\n")
    if cut == -1:
        if len(data) < MAX_LINE_BYTES:
            return b""
        blank = data.rfind(b" ")
        return data[: blank + 1] if blank > 0 else data
    return data[: cut + 1]


def tail(path, lines=TAIL_LINES, max_bytes=TAIL_MAX_BYTES):
    """The last complete lines of the file: ``(text, offset, file_id)``; ``offset`` is where the next read starts."""
    with open_log(path) as handle:
        status = os.fstat(handle.fileno())
        size = status.st_size
        start = max(0, size - max_bytes)
        handle.seek(start)
        data = handle.read(size - start)
    if start > 0:
        # The first line is probably cut in the middle: drop it.
        newline = data.find(b"\n")
        start, data = (start + newline + 1, data[newline + 1:]) if newline != -1 else (size, b"")
    data = _complete_lines(data)
    selected = data.splitlines(keepends=True)[-lines:]
    return _decode(b"".join(selected)), start + len(data), _file_id(status)


def read_since(path, offset, file_id, max_bytes=POLL_MAX_BYTES):
    """New complete lines after ``offset``: ``(text, new offset, file_id, reset)``.

    ``reset`` is true when the file was rotated or truncated (another file, or shorter than
    ``offset``): the answer is then the tail of the new file.
    """
    with open_log(path) as handle:
        status = os.fstat(handle.fileno())
        current_id = _file_id(status)
        if current_id != file_id or offset > status.st_size or offset < 0:
            text, new_offset, current_id = tail(path)
            return text, new_offset, current_id, True
        handle.seek(offset)
        data = handle.read(max_bytes)
    complete = _complete_lines(data)
    return _decode(complete), offset + len(complete), current_id, False


# How much of a piece of a download is held back until the next one is read: longer than any value that is redacted
# (an email address has at most 254 characters), so that a value cut by the reading is redacted whole (L-3).
OVERLAP_CHARS = 512


def redacted_chunks(path):
    """The file as redacted bytes in bounded pieces (for a streamed download); it is never read as a whole.

    The file is opened at once (an ``OSError`` is raised here, not in the middle of the answer). A line is read in
    pieces of at most ``MAX_LINE_BYTES``; the end of a piece that does not end a line is carried to the next one
    (cut where no redacted value lies, ``log_filters.safe_cut``), so a very long line is redacted as a whole."""
    handle = open_log(path)
    return _redacted_pieces(handle)


def _redacted_pieces(handle):
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    carry = ""
    with handle:
        while True:
            text = carry
            carry = ""
            size = 0
            last_complete = True
            ended = False
            while size < DOWNLOAD_CHUNK_BYTES:
                line = handle.readline(MAX_LINE_BYTES)
                if not line:
                    ended = True
                    break
                size += len(line)
                text += decoder.decode(line)
                last_complete = line.endswith(b"\n")
            if ended:
                text += decoder.decode(b"", final=True)
                if text:
                    yield redact(text).encode("utf-8")
                return
            if not last_complete and len(text) > OVERLAP_CHARS:
                # The piece ends inside a line: hold its end back, cut where no value to redact lies.
                cut = safe_cut(text, len(text) - OVERLAP_CHARS)
                text, carry = text[:cut], text[cut:]
            if text:
                yield redact(text).encode("utf-8")
