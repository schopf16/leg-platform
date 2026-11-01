"""The HTML variant of the registration email (D-136).

The administrator edits plain text only (D-039); this fixed wrapper turns the composed text into the
mail layout: the name of the instance small at the top, the code alone in one large box, everything
else as small print, and the footer with the privacy notice. Inline CSS only, no image, no script, no
tracking and no link but the privacy notice. Every value is escaped; instance values are operator data.
"""

import re
from html import escape

_PARAGRAPHS = re.compile(r"\n[ \t]*\n")
_FONT = "Arial, Helvetica, sans-serif"
_MONO = "Consolas, 'Courier New', monospace"


def paragraphs(text):
    """Split a plain text into its paragraphs (separated by empty lines); empty ones are dropped."""
    return [part.strip() for part in _PARAGRAPHS.split(text.replace("\r\n", "\n")) if part.strip()]


def footer_lines(privacy_url, contact_url, privacy_label, contact_label):
    """The fixed footer of the plain text variant: the privacy notice and the optional contact line."""
    lines = []
    if privacy_url:
        lines.append(f"{privacy_label} {privacy_url}")
    if contact_url:
        lines.append(f"{contact_label} {contact_url}")
    return lines


def _text(value):
    return escape(value, quote=True).replace("\n", "<br>")


def build_html(*, language, instance_name, code, body, privacy_url, contact_url, privacy_label, contact_label):
    """Return the HTML document of the email. ``body`` is the composed plain text with the code in a paragraph of its own."""
    before, after, seen = [], [], False
    for part in paragraphs(body):
        if not seen and part == code:
            seen = True
        elif seen:
            after.append(part)
        else:
            before.append(part)
    cells = []
    if instance_name:
        cells.append(
            f'<tr><td style="padding:0 0 16px 0;font-family:{_FONT};font-size:13px;color:#6b7280;">'
            f"{_text(instance_name)}</td></tr>"
        )
    for part in before:
        cells.append(
            f'<tr><td style="padding:0 0 16px 0;font-family:{_FONT};font-size:15px;color:#1f2937;">{_text(part)}</td></tr>'
        )
    cells.append(
        '<tr><td style="padding:0;">'
        f'<div style="background-color:#e5e7eb;border:2px solid #9ca3af;color:#111111;font-family:{_MONO};font-size:40px;'
        f'font-weight:bold;line-height:48px;text-align:center;padding:20px 8px;">{escape(code)}</div></td></tr>'
    )
    for index, part in enumerate(after):
        padding = "10px 0 0 0" if index == 0 else "14px 0 0 0"
        cells.append(
            f'<tr><td style="padding:{padding};font-family:{_FONT};font-size:13px;line-height:19px;color:#4b5563;">'
            f"{_text(part)}</td></tr>"
        )
    footer = []
    if privacy_url:
        footer.append(
            f'<a href="{escape(privacy_url, quote=True)}" style="color:#4b5563;">{_text(privacy_label)}</a>'
        )
    if contact_url:
        footer.append(f"{_text(contact_label)} {_text(contact_url)}")
    if footer:
        cells.append(
            f'<tr><td style="padding:24px 0 0 0;font-family:{_FONT};font-size:12px;line-height:18px;color:#6b7280;">'
            f'{"<br>".join(footer)}</td></tr>'
        )
    rows = "".join(cells)
    return (
        "<!DOCTYPE html>"
        f'<html lang="{escape(language, quote=True)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{_text(instance_name)}</title></head>"
        '<body style="margin:0;padding:0;background-color:#ffffff;">'
        # The hidden preheader: the preview of a mail list starts with the code (not visible in the mail itself).
        '<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;'
        f'color:#ffffff;opacity:0;">{escape(code)} {_text(before[0]) if before else ""}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background-color:#ffffff;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:520px;">{rows}</table></td></tr></table></body></html>'
    )
