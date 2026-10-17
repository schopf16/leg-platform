"""Return to the page a dialog was opened from (D-077).

``next`` is never trusted: only a path of this site is accepted (single leading "/", no backslash or
control character, Django host check); anything else falls back to the default (no open redirect).
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse
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


def view_name_of(url):
    """The view name of a safe path of this site (``"management:locations"``), or "" if it names no page."""
    try:
        return resolve(urlsplit(url).path).view_name
    except Resolver404:
        return ""


def after_create(request, list_view, back, detail_url):
    """Where to go after an object was created (D-095): to the page of the new object with the flag ``new`` (it shows
    the next step) if the dialog was opened from the list (or from nowhere); back to the page it was opened from
    if that was another page (a location, a form that waits for the new object). ``back`` is a safe address,
    ``list_view`` the view name of the list."""
    if back and view_name_of(back) != list_view:
        return back
    parameters = {"new": "1"}
    if back:
        parameters["next"] = back
    return f"{detail_url}?{urlencode(parameters)}"


def origin_point_url(request):
    """The page of the metering point whose dialog (for example "Assign person") the person was created from, found from
    ``next``; "" for any other origin. The address is built from the view and its number, never taken from ``next``."""
    back = return_url(request, "")
    if not back:
        return ""
    try:
        match = resolve(urlsplit(back).path)
    except Resolver404:
        return ""
    if match.view_name.startswith("management:metering_points_") and isinstance(match.kwargs.get("pk"), int):
        return reverse("management:metering_points_detail", args=[match.kwargs["pk"]])
    return ""


def created_redirect(request, list_view, back, detail_url, message):
    """The redirect after a create (``after_create``) with the flash message ``message``: when the page of the new object
    opens, its next-step banner says what happened, so the plain message is not queued as well (UX-016); a return to
    another page keeps it."""
    target = after_create(request, list_view, back, detail_url)
    if "new=1" not in urlsplit(target).query.split("&"):
        messages.success(request, message)
    return redirect(target)


def with_parameter(url, name, value):
    """``url`` with the query parameter ``name`` set (the other parameters stay); ``url`` is a safe path of this site."""
    parts = urlsplit(url)
    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True) if key != name]
    query.append((name, str(value)))
    return urlunsplit(("", "", parts.path, urlencode(query), ""))
