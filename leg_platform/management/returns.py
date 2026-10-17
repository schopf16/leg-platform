"""Return to the page a dialog was opened from (D-077).

``next`` is never trusted: only a path of this site is accepted (single leading "/", no backslash or
control character, Django host check); anything else falls back to the default (no open redirect).
"""

from urllib.parse import urlencode

from django.utils.http import url_has_allowed_host_and_scheme

MAX_LENGTH = 2000


def is_safe_return_url(value, request):
    if not isinstance(value, str) or not value or len(value) > MAX_LENGTH:
        return False
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    return url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    )


def return_url(request, default):
    """``next`` of the form (POST) or link (GET) if safe, else ``default``."""
    for source in (request.POST, request.GET):
        value = source.get("next")
        if value is not None:
            return value if is_safe_return_url(value, request) else default
    return default


def with_next(url, request):
    """``url`` with the current page as ``next``, for a link to a dialog."""
    return f"{url}?{urlencode({'next': request.get_full_path()})}"
