"""Log filters (docs/SECURITY.md, sections 6 and 10).

Django's request logging writes the path of every response with status 400 or higher, and the
development server logs every request. Paths of verification links contain a secret token, so
the token is removed from every log message before it is written.
"""

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


def redact_tokens(text):
    return TOKEN_URL_PATTERN.sub(r"\g<1>" + REDACTED, text)


class TokenRedactionFilter(logging.Filter):
    """Replace tokens of verification links in the message and in an exception text."""

    def filter(self, record):
        message = record.getMessage()
        redacted = redact_tokens(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_info and not record.exc_text:
            # Formatted here, because the formatter would otherwise format it after the filters.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_tokens(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_tokens(record.stack_info)
        return True
