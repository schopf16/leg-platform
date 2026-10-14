"""Public presentation of the instance: the branding stylesheet.

The colour mode (light or dark) is not handled on the server: it is a choice of the person that
is neither stored in a cookie nor sent to the server (docs/DECISIONS.md, D-056). The browser keeps
it for the tab only (``static/js/theme-init.js``).
"""

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.http import require_GET


def _accent_rules(accent, foreground):
    rgb = ", ".join(str(int(accent[index:index + 2], 16)) for index in (1, 3, 5))
    return f"--accent:{accent};--accent-rgb:{rgb};--on-accent:{foreground};"


@require_GET
def instance_stylesheet(request):
    """The validated accent and highlight colours of the instance as a stylesheet file.

    The values come from the validated configuration (``leg_platform.branding``); a file is
    used because the Content Security Policy forbids inline styles. ``portal.css`` maps the
    tokens onto the variables of Tabler; this file is loaded after it and replaces the defaults.
    """
    branding = settings.INSTANCE_BRANDING
    light = _accent_rules(branding.accent_light, branding.text_light)
    dark = _accent_rules(branding.accent_dark, branding.text_dark)
    highlight = (
        f"--yellow:{branding.highlight};" if branding.highlight else "--yellow:transparent;"
    )
    css = f':root{{{light}{highlight}}}:root[data-theme="dark"]{{{dark}}}'
    return HttpResponse(css, content_type="text/css; charset=utf-8")
