"""The HTML frame of the system emails (D-137).

The administrator edits plain text only (D-039); this fixed frame embeds the finally composed text
(after the placeholders are replaced) into one layout: a header with the logo, the text as the white
body and a footer. Inline CSS and tables only (mail clients), no script, no tracking, no button.
The registration code mail has its own layout (``code_mail``, D-136) and does not use this frame.

Everything that comes from the text or from the instance (operator data) is escaped. The only links
are the https addresses that stand in the text (the link text equals the address) and the footer
links of the instance, each re-checked here. The colours come from the portal values and the platform
tokens (docs/DESIGN.md, 3.1); the tint of header and footer is the accent mixed 9 % over the page
background, as on the login page. The dark-mode block is an enhancement, holds only platform tokens
and no operator data. Nothing of it is logged.
"""

import re
from html import escape
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext

from leg_platform.accounts.code_mail import _text, paragraphs
from leg_platform.accounts.portal_settings import portal_values
from leg_platform.branding import COLOUR_DEFAULTS, HEX_COLOUR, MIN_CONTRAST, contrast

FONT = "Arial,Helvetica,sans-serif"
LOGO_HEIGHT = 64
MAX_ADDRESS_LENGTH = 2048

# The platform tokens of the light mode (docs/DESIGN.md, 3.1).
LIGHT = {"bg": "#F6F7F8", "panel": "#FFFFFF", "fg": "#1B1D20", "muted": "#5A6068", "line": "#DEE2E7"}
# The platform tokens of the dark mode; the tint is the platform accent mixed 9 % over the background.
DARK = {
    "bg": "#111315", "panel": "#191C1F", "fg": "#E6E9EC", "muted": "#9AA2AB", "line": "#2A2F35",
    "accent": COLOUR_DEFAULTS["accent_dark"], "warm": "#192027",
}
TINT_PERCENT = 9

# An https address in a text: no blank, no control character and none of < > " ' (so it can never leave an attribute).
_URL = re.compile(r"https://[^\s<>\"'\x00-\x1f\x7f]+")
_TRAILING = ".,;:!?"
_CLOSING = {")": "(", "]": "[", "}": "{"}


def mix(foreground, percent, background):
    """The colour of ``color-mix(in srgb, foreground percent%, background)`` as ``#RRGGBB``."""
    a = [int(foreground[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(background[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(round(x * percent / 100 + y * (100 - percent) / 100) for x, y in zip(a, b))


def https_address(value):
    """``value`` if it is one clean https address with a host, else ""."""
    if not isinstance(value, str) or _URL.fullmatch(value) is None:
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            return ""
        parts.port  # noqa: B018 - raises for an invalid port
    except ValueError:
        return ""
    return value


def _split_trailing(candidate):
    """Cut the punctuation that ends a sentence (and an unbalanced closing bracket) off an address."""
    tail = ""
    while candidate:
        last = candidate[-1]
        if last in _TRAILING or (last in _CLOSING and candidate.count(last) > candidate.count(_CLOSING[last])):
            tail = last + tail
            candidate = candidate[:-1]
        else:
            break
    return candidate, tail


def linkify(text, colour):
    """Escape ``text`` and turn each https address into a link whose text is the address itself."""
    out, position = [], 0
    for match in _URL.finditer(text):
        address, tail = _split_trailing(match.group())
        # A very long candidate stays plain text (a mail never needs such an address).
        valid = https_address(address) if len(match.group()) <= MAX_ADDRESS_LENGTH else ""
        out.append(_text(text[position:match.start()]))
        if valid:
            safe = escape(valid, quote=True)
            out.append(
                f'<a class="link" href="{safe}" rel="noopener noreferrer" style="color:{colour};'
                f'text-decoration:underline;overflow-wrap:anywhere;word-break:break-word">{safe}</a>'
            )
        else:
            out.append(_text(address))
        out.append(_text(tail))
        position = match.end()
    out.append(_text(text[position:]))
    return "".join(out)


def logo_source(portal):
    """The absolute https address of the PNG logo, or "" (then the header shows the name as text).

    Mail clients do not show SVG, so only a PNG logo counts; the address is built from the base
    address of the instance (never from a request) and the address of the public logo, which carries
    no per-mail value.
    """
    path = portal.logo_light
    if portal.logo_light_type != "png" or not path.startswith("/") or path.startswith("//"):
        return ""
    return https_address(f"{settings.INSTANCE_BASE_URL}{path}")


def _colours(portal):
    """The colours of the light mode: the tint, and the text and link colours that reach the contrast on it and on the body."""
    accent = portal.accent_light if HEX_COLOUR.fullmatch(portal.accent_light or "") else COLOUR_DEFAULTS["accent_light"]
    warm = mix(accent, TINT_PERCENT, LIGHT["bg"])
    muted = LIGHT["muted"] if contrast(LIGHT["muted"], warm) >= MIN_CONTRAST else LIGHT["fg"]
    link_on_body = accent if contrast(accent, LIGHT["panel"]) >= MIN_CONTRAST else LIGHT["fg"]
    link_on_tint = accent if contrast(accent, warm) >= MIN_CONTRAST else LIGHT["fg"]
    return {"warm": warm, "muted": muted, "link": link_on_body, "tint_link": link_on_tint}


def _header(portal, source, colours):
    name = escape(portal.name, quote=True)
    if source:
        # The alt text is the name: with blocked images it shows in the colour of the text.
        inner = (
            f'<img src="{escape(source, quote=True)}" alt="{name}" height="{LOGO_HEIGHT}" border="0" '
            f'style="display:block;border:0;outline:none;height:{LOGO_HEIGHT}px;width:auto;max-width:100%;'
            f'font-family:{FONT};font-size:18px;line-height:24px;font-weight:bold;color:{LIGHT["fg"]}">'
        )
    else:
        inner = (
            f'<span class="fg" style="font-family:{FONT};font-size:22px;line-height:28px;font-weight:bold;'
            f'color:{LIGHT["fg"]}">{name}</span>'
        )
    # In the dark mode the positive logo (only the image, never the text fallback) gets a light plate: a mail cannot
    # swap to the negative logo. The text fallback keeps the normal foreground on the dark tint.
    plate = ' class="logo-box"' if source else ""
    return (
        f'<tr><td class="bg-warm" bgcolor="{colours["warm"]}" style="background-color:{colours["warm"]};'
        f'border-radius:8px 8px 0 0;padding:24px 28px"><table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0"><tr><td{plate} style="padding:0">{inner}</td></tr></table></td></tr>'
    )


def _footer(portal, colours):
    labels = (
        (gettext("Website"), https_address(portal.website_url)),
        (gettext("Privacy notice"), https_address(portal.privacy_url)),
        (gettext("Legal notice"), https_address(portal.imprint_url)),
    )
    links = " &nbsp;&middot;&nbsp; ".join(
        f'<a class="link" href="{escape(url, quote=True)}" rel="noopener noreferrer" '
        f'style="color:{colours["tint_link"]};text-decoration:underline">{escape(label)}</a>'
        for label, url in labels
        if url
    )
    name = escape(portal.name, quote=True)
    automatic = escape(gettext("This is an automatic message. Replies are not read."))
    reason = escape(gettext(
        "You are receiving this email because it concerns your account or an address you entered at %(name)s."
    ) % {"name": portal.name}, quote=True)
    link_row = f'<p style="margin:0 0 10px 0">{links}</p>' if links else ""
    return (
        f'<tr><td class="bg-warm fg-muted" bgcolor="{colours["warm"]}" style="background-color:{colours["warm"]};'
        f'border-radius:0 0 8px 8px;padding:20px 28px 24px 28px;font-family:{FONT};font-size:12px;line-height:18px;'
        f'color:{colours["muted"]}"><p style="margin:0 0 6px 0"><b class="fg" style="color:{LIGHT["fg"]}">{name}</b></p>'
        f'{link_row}<p style="margin:0 0 6px 0">{automatic}</p><p style="margin:0">{reason}</p></td></tr>'
    )


def _dark_style():
    return (
        "<style>\n@media (prefers-color-scheme: dark) {\n"
        f"  .bg-page {{ background-color:{DARK['bg']} !important; }}\n"
        f"  .bg-card {{ background-color:{DARK['panel']} !important; border-color:{DARK['line']} !important; }}\n"
        f"  .bg-warm {{ background-color:{DARK['warm']} !important; }}\n"
        f"  .fg {{ color:{DARK['fg']} !important; }}\n"
        f"  .fg-muted {{ color:{DARK['muted']} !important; }}\n"
        f"  .link {{ color:{DARK['accent']} !important; }}\n"
        f"  .logo-box {{ background-color:{LIGHT['panel']} !important; padding:6px 10px !important; border-radius:6px; }}\n"
        "}\n</style>\n"
    )


def build_html(subject, body, portal=None):
    """Return the HTML document of a system email: the logo header, ``body`` (composed plain text) and the footer."""
    portal = portal or portal_values()
    colours = _colours(portal)
    with translation.override(settings.LANGUAGE_CODE):
        header = _header(portal, logo_source(portal), colours)
        footer = _footer(portal, colours)
    rows = "".join(
        f'<tr><td class="fg" style="padding:0 28px 16px 28px;font-family:{FONT};font-size:16px;line-height:24px;'
        f'color:{LIGHT["fg"]}">{linkify(part, colours["link"])}</td></tr>'
        for part in paragraphs(body)
    )
    table = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{escape(settings.LANGUAGE_CODE)}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="x-apple-disable-message-reformatting">\n<meta name="color-scheme" content="light dark">\n<meta name="supported-color-schemes" content="light dark">\n'
        f"<title>{escape(subject, quote=True)}</title>\n{_dark_style()}</head>\n"
        f'<body class="bg-page" bgcolor="{LIGHT["bg"]}" style="margin:0;padding:0;background-color:{LIGHT["bg"]}">\n'
        f'<table class="bg-page" {table} width="100%" bgcolor="{LIGHT["bg"]}" style="background-color:{LIGHT["bg"]}">'
        '<tr><td align="center" style="padding:24px 12px">\n'
        f'<!--[if mso]><table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->\n'
        f'<table class="bg-card" {table} width="100%" bgcolor="{LIGHT["panel"]}" style="max-width:560px;text-align:left;'
        f'background-color:{LIGHT["panel"]};border:1px solid {LIGHT["line"]};border-radius:8px">\n'
        f'{header}\n<tr><td style="padding:24px 0 8px 0"><table {table} width="100%">{rows}</table></td></tr>\n{footer}\n'
        "</table>\n<!--[if mso]></td></tr></table><![endif]-->\n</td></tr></table>\n</body>\n</html>\n"
    )
