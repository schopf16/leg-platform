"""Public presentation of the instance: the branding stylesheet and the uploaded logos.

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

from leg_platform.accounts.portal_settings import LOGO_VERSION_LENGTH, portal_values

logger = logging.getLogger(__name__)

LOGO_CONTENT_TYPES = {"png": "image/png", "svg": "image/svg+xml"}
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


def _logo_row(variant):
    """The type and the digest of a logo, without the file (up to 1 MB): enough to answer a wrong
    ``?v=`` and a conditional request."""
    from leg_platform.accounts.models import PortalSettings

    columns = (f"logo_{variant}_type", f"logo_{variant}_sha256")
    return PortalSettings.objects.filter(pk=1).values(*columns).first()


def _logo_data(variant):
    """The file of a logo; only read when a body is served."""
    from leg_platform.accounts.models import PortalSettings

    row = PortalSettings.objects.filter(pk=1).values(f"logo_{variant}_data").first()
    return row[f"logo_{variant}_data"] if row else None


@require_safe
def instance_logo(request, variant):
    """An uploaded logo, from the database: a public view without a session, because the logo is
    on every page, also the pages before the login (D-073).

    The file is never chosen by the visitor: only the variant ("light" or "dark") is routed, the
    content comes from the validated column and its type from the stored type. The content type is
    fixed, ``nosniff`` and a sandboxing policy are set, and the digest of the file is checked
    again before a body is served. ``?v=`` must be the start of the digest if it is given (a stale
    address is not found). Without an uploaded logo the answer is 404 (the pages then use the logo
    of the server configuration, which is a static file).

    A wrong ``?v=`` and a conditional request that matches the ETag are answered from the type and
    the digest alone: the file is neither read nor hashed (security review of D-073, L-1).
    Neither the session nor the second-factor middleware touches the session for this view
    (``second_factor.is_session_free``), so the response carries no ``Set-Cookie`` and no
    ``Vary: Cookie`` (L-2).
    """
    try:
        row = _logo_row(variant)
    except Exception:  # noqa: BLE001 - the same fail-safe as the accessor: no logo, no exception
        logger.warning("An uploaded logo could not be read.")
        raise Http404 from None
    kind = row[f"logo_{variant}_type"] if row else ""
    digest = row[f"logo_{variant}_sha256"] if row else ""
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
            data = _logo_data(variant)
        except Exception:  # noqa: BLE001 - as above
            logger.warning("An uploaded logo could not be read.")
            raise Http404 from None
        data = bytes(data) if data else b""
        if not data:
            raise Http404
        if hashlib.sha256(data).hexdigest() != digest:
            logger.warning("The stored logo does not match its digest; it is not served.")
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
