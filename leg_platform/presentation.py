"""Public presentation of the instance: the branding stylesheet, the uploaded logos and the uploaded favicon.

The colour mode (light or dark) is not handled on the server: it is a choice of the person that
is neither stored in a cookie nor sent to the server (docs/DECISIONS.md, D-056). The browser keeps
it for the tab only (``static/js/theme-init.js``).
"""

import hashlib
import logging

from django.http import Http404, HttpResponse
from django.urls import register_converter
from django.utils.cache import get_conditional_response, patch_cache_control
from django.utils.http import quote_etag
from django.views.decorators.http import require_safe

from leg_platform.accounts.portal_settings import IMAGE_CONTENT_TYPES, LOGO_VERSION_LENGTH, portal_values

logger = logging.getLogger(__name__)

LOGO_CONTENT_TYPES = IMAGE_CONTENT_TYPES
# An uploaded file is an image and nothing else: it is shown through <img>, and if someone opens its
# address directly the browser may not run or load anything from it (docs/SECURITY.md, section 6).
LOGO_CONTENT_SECURITY_POLICY = "sandbox; default-src 'none'"
# The address of a logo carries the first characters of the digest of the file (``?v=``), so the
# address changes with the file and a day is a safe lifetime.
LOGO_MAX_AGE = 86400


class LogoVariantConverter:
    """Only the two variants "light" and "dark" are routed; no other value reaches the view."""

    regex = "light|dark"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(LogoVariantConverter, "logo_variant")


def accent_rules(accent, foreground):
    rgb = ", ".join(str(int(accent[index:index + 2], 16)) for index in (1, 3, 5))
    return f"--accent:{accent};--accent-rgb:{rgb};--on-accent:{foreground};"


def stylesheet_text(values):
    """The stylesheet of the validated colours (``PortalValues``) as text."""
    light = accent_rules(values.accent_light, values.text_light)
    dark = accent_rules(values.accent_dark, values.text_dark)
    highlight = f"--yellow:{values.highlight};" if values.highlight else "--yellow:transparent;"
    return f':root{{{light}{highlight}}}:root[data-theme="dark"]{{{dark}}}'


@require_safe
def instance_stylesheet(request):
    """The validated accent and highlight colours of the instance as a stylesheet file.

    The values are the effective ones of the portal settings (the administration, then the server
    configuration, then the defaults; ``portal_values``), checked again when they are read. A file
    is used because the Content Security Policy forbids inline styles. ``portal.css`` maps the
    tokens onto the variables of Tabler; this file is loaded after it and replaces the defaults.
    A change is visible at once: the browser asks again every time (``no-cache``) and the ETag
    answers with "not modified" while nothing changed.
    """
    css = stylesheet_text(portal_values(request))
    etag = quote_etag(hashlib.sha256(css.encode()).hexdigest())
    response = get_conditional_response(request, etag=etag)
    if response is None:
        response = HttpResponse(css, content_type="text/css; charset=utf-8")
    response["ETag"] = etag
    response["Cache-Control"] = "no-cache"
    return response


def _image_row(prefix):
    """The type and the digest of an uploaded image (``logo_light``, ``logo_dark``, ``favicon``), without the
    file (up to 1 MB): enough to answer a wrong ``?v=`` and a conditional request."""
    from leg_platform.accounts.models import PortalSettings

    columns = (f"{prefix}_type", f"{prefix}_sha256")
    return PortalSettings.objects.filter(pk=1).values(*columns).first()


def _image_data(prefix):
    """The file of an uploaded image; only read when a body is served."""
    from leg_platform.accounts.models import PortalSettings

    row = PortalSettings.objects.filter(pk=1).values(f"{prefix}_data").first()
    return row[f"{prefix}_data"] if row else None


def _logo_row(variant):
    return _image_row(f"logo_{variant}")


def _logo_data(variant):
    return _image_data(f"logo_{variant}")


def _favicon_row():
    return _image_row("favicon")


def _favicon_data():
    return _image_data("favicon")


def _serve_image(request, prefix, read_row, read_data, label):
    """The stored image of the column group ``prefix`` as a response, or 404 (the one code of the logos and the favicon).

    The file is never chosen by the visitor: the group is fixed by the route, the content comes from the
    validated column and its type from the stored type. The content type is fixed, ``nosniff`` and a
    sandboxing policy are set, and the digest of the file is checked again before a body is served.
    ``?v=`` must be the start of the digest if it is given (a stale address is not found).

    A wrong ``?v=`` and a conditional request that matches the ETag are answered from the type and
    the digest alone: the file is neither read nor hashed (security review of D-073, L-1).
    """
    try:
        row = read_row()
    except Exception:  # noqa: BLE001 - the same fail-safe as the accessor: no image, no exception
        logger.warning("An uploaded %s could not be read.", label)
        raise Http404 from None
    kind = row[f"{prefix}_type"] if row else ""
    digest = row[f"{prefix}_sha256"] if row else ""
    if kind not in LOGO_CONTENT_TYPES or not digest:
        raise Http404
    version = request.GET.get("v")
    if version is not None and (
        len(version) != LOGO_VERSION_LENGTH or version != digest[:LOGO_VERSION_LENGTH]
    ):
        raise Http404
    etag = quote_etag(digest)
    response = get_conditional_response(request, etag=etag)
    if response is None:
        # A body is needed: only now the file is read and checked against its digest.
        try:
            data = read_data()
        except Exception:  # noqa: BLE001 - as above
            logger.warning("An uploaded %s could not be read.", label)
            raise Http404 from None
        data = bytes(data) if data else b""
        if not data:
            raise Http404
        if hashlib.sha256(data).hexdigest() != digest:
            logger.warning("The stored %s does not match its digest; it is not served.", label)
            raise Http404
        response = HttpResponse(data, content_type=LOGO_CONTENT_TYPES[kind])
    response["ETag"] = etag
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = LOGO_CONTENT_SECURITY_POLICY
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    if version is None:
        patch_cache_control(response, no_cache=True)
    else:
        # Not "public": the cache of the browser is enough, and the address changes with the file.
        patch_cache_control(response, max_age=LOGO_MAX_AGE)
    return response


@require_safe
def instance_logo(request, variant):
    """An uploaded logo, from the database: a public view without a session, because the logo is
    on every page, also the pages before the login (D-073).

    Only the variant ("light" or "dark") is routed (``_serve_image`` has the rules). Without an uploaded logo
    the answer is 404 (the pages then use the logo of the server configuration, which is a static file).
    Neither the session nor the second-factor middleware touches the session for this view
    (``second_factor.is_session_free``), so the response carries no ``Set-Cookie`` and no
    ``Vary: Cookie`` (L-2).
    """
    return _serve_image(
        request, f"logo_{variant}", lambda: _logo_row(variant), lambda: _logo_data(variant), "logo"
    )


@require_safe
def instance_favicon(request):
    """The uploaded favicon (browser tab icon), served like a logo: public, without a session, from the
    database with its content hash (D-073). Without an uploaded favicon the answer is 404 (the pages then
    use the built-in static icon)."""
    return _serve_image(request, "favicon", lambda: _favicon_row(), lambda: _favicon_data(), "favicon")
