"""Public presentation preferences, independent of authentication."""

import logging

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

logger = logging.getLogger(__name__)
COLOUR_MODES = ("auto", "light", "dark")


def colour_mode(request):
    mode = request.COOKIES.get(settings.COLOUR_MODE_COOKIE_NAME, "auto")
    return mode if mode in COLOUR_MODES else "auto"


@require_POST
def set_colour_mode(request):
    mode = request.POST.get("mode")
    if mode not in COLOUR_MODES:
        logger.warning("Invalid colour mode rejected.")
        return HttpResponse(_("Choose a valid colour mode."), status=400)
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        target, {request.get_host()}, require_https=request.is_secure()
    ):
        target = "/"
    response = redirect(target)
    response.set_cookie(
        settings.COLOUR_MODE_COOKIE_NAME, mode, max_age=31536000, path="/",
        secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Lax",
    )
    return response


def _accent_rules(accent, foreground):
    rgb = ", ".join(str(int(accent[index:index + 2], 16)) for index in (1, 3, 5))
    return (
        f"--tblr-primary:{accent};--tblr-primary-rgb:{rgb};"
        f"--tblr-primary-fg:{foreground};"
        f"--tblr-link-color:{accent};--tblr-link-hover-color:{accent};"
        f"--tblr-link-color-rgb:{rgb};--tblr-link-hover-color-rgb:{rgb};"
        f"--tblr-focus-ring-color:{accent};"
    )


@require_GET
def instance_stylesheet(request):
    branding = settings.INSTANCE_BRANDING
    light = _accent_rules(branding.accent_light, branding.text_light)
    dark = _accent_rules(branding.accent_dark, branding.text_dark)
    highlight = (
        f"--portal-highlight:{branding.highlight};" if branding.highlight
        else "--portal-highlight:transparent;"
    )
    css = (
        f":root{{{light}{highlight}}}"
        f':root[data-bs-theme="dark"]{{{dark}}}'
        '@media(prefers-color-scheme:dark){:root:not([data-bs-theme="light"]){'
        f"{dark}" + "}}"
        ".btn-primary{--tblr-btn-bg:var(--tblr-primary);"
        "--tblr-btn-border-color:var(--tblr-primary);"
        "--tblr-btn-color:var(--tblr-primary-fg);"
        "--tblr-btn-hover-bg:var(--tblr-primary);"
        "--tblr-btn-hover-border-color:var(--tblr-primary);"
        "--tblr-btn-hover-color:var(--tblr-primary-fg);"
        "--tblr-btn-active-bg:var(--tblr-primary);"
        "--tblr-btn-active-border-color:var(--tblr-primary);"
        "--tblr-btn-active-color:var(--tblr-primary-fg);}"
    )
    return HttpResponse(css, content_type="text/css; charset=utf-8")
