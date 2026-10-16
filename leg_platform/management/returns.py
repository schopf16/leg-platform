"""Back to the place you came from (D-077).

A dialog or an action is opened from some page and returns there after saving. The place is the
``next`` value of the request. It is never trusted: only a path of this site is accepted (starts
with a single "/", no scheme, no host, no backslash or control character, and the host check of
Django); anything else falls back to the default page. This also protects against open redirects.
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
    """The page to go back to: ``next`` of the form (POST) or of the link (GET) if it is safe, else
    ``default``."""
    for source in (request.POST, request.GET):
        value = source.get("next")
        if value is not None:
            return value if is_safe_return_url(value, request) else default
    return default


def with_next(url, request):
    """``url`` with the current page as ``next``, for a link to a dialog."""
    return f"{url}?{urlencode({'next': request.get_full_path()})}"
