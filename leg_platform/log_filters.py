"""Log filters (docs/SECURITY.md, sections 6 and 10).

Django's request logging writes the path of every response with status 400 or higher, and the
development server logs every request. Paths of verification links contain a secret token, so
the token is removed from every log message before it is written.
"""

import copy
import logging
import re

# A slash of the path, or its percent-encoded form: the server decodes the path before it routes,
# so "/email-change%2Fstop/<token>/" reaches the view, while the request line of the development
# server shows the raw form (security review of unit A, L-1).
_SLASH = r"(?:/|%2[Ff])"

# The paths of the token views (registration link, password reset link, link to remove the
# second factor, the two links of an address change); a test enumerates the URL configuration
# and requires every route with a ``token`` converter to be redacted. Since D-067 these views
# redirect and render nothing, but the token is still in the request line and in "Not Found"
# lines of the log.
TOKEN_URL_PATTERN = re.compile(
    rf"({_SLASH}(?:(?:register|password-reset|two-factor-reset|email-change){_SLASH}verify"
    rf"|email-change{_SLASH}stop){_SLASH})"
    r"[^/\s\"'?#]+"
)
REDACTED = "<redacted>"

# Personal data that can end up in an exception text (D-092): an email address, an IBAN, and the
# value that MariaDB names in a duplicate-entry or invalid-value error.
# The local part is bounded (RFC 5321: at most 64 octets): without a bound a long run of such characters without an "@"
# was scanned from every start position again (quadratic, L-B of the review of 2026-10-17).
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
IBAN_PATTERN = re.compile(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
DATABASE_VALUE_PATTERN = re.compile(
    r"(Duplicate entry |Incorrect [a-z]+ value: |Data truncated for column [^\s]+ at row )'[^']*'"
)


def redact_tokens(text):
    return TOKEN_URL_PATTERN.sub(r"\g<1>" + REDACTED, text)


def redact_personal_data(text):
    """Replace email addresses, IBANs and the values of database errors."""
    text = EMAIL_PATTERN.sub("<email>", text)
    text = IBAN_PATTERN.sub("<iban>", text)
    return DATABASE_VALUE_PATTERN.sub(r"\g<1>'" + REDACTED + "'", text)


def safe_cut(text, position):
    """The largest position at or before ``position`` that lies in no match of a redaction pattern, so that a text cut
    there can be redacted piece by piece without missing a value that straddles the cut (L-3 of the review of 2026-10-17)."""
    moved = True
    while moved and position > 0:
        moved = False
        for pattern in (TOKEN_URL_PATTERN, EMAIL_PATTERN, IBAN_PATTERN, DATABASE_VALUE_PATTERN):
            for match in pattern.finditer(text):
                if match.start() < position < match.end():
                    position = match.start()
                    moved = True
    return position


# Characters that end or fake a line in a log (M-1): the line breaks of ``str.splitlines`` and the other control characters.
_CONTROL = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029]")


def escape_control(text):
    """Control characters and line separators as visible escapes (``\\n``, ``\\x1b``, ``\\u2028``): a value that a client
    controls (a request path, a header, a name) can never start a new line of the log."""

    def replace(match):
        character = match.group(0)
        return {"\n": "\\n", "\r": "\\r"}.get(character) or (
            f"\\x{ord(character):02x}" if ord(character) < 256 else f"\\u{ord(character):04x}"
        )

    return _CONTROL.sub(replace, text)


def _clean(text):
    return redact_personal_data(redact_tokens(text))


class TokenRedactionFilter(logging.Filter):
    """Replace tokens of verification links and personal data in the message and in an exception text."""

    def filter(self, record):
        message = record.getMessage()
        redacted = _clean(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_info and not record.exc_text:
            # Formatted here, because the formatter would otherwise format it after the filters.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _clean(record.exc_text)
        if record.stack_info:
            record.stack_info = _clean(record.stack_info)
        return True


class SafeFormatter(logging.Formatter):
    """The formatter of every handler: the message is one line (M-1 of the review of 2026-10-17).

    Django logs the request path of every 4xx and 5xx answer, and the server decodes ``%0a`` before it routes, so a
    crafted address could otherwise start a line that looks like a log entry of the application. Control characters
    and line separators of the message become visible escapes; the exception text and the stack, which the program
    writes, keep their lines."""

    def format(self, record):
        message = record.getMessage()
        safe = escape_control(message)
        if safe != message:
            record = copy.copy(record)
            record.msg = safe
            record.args = ()
        return super().format(record)
