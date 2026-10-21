"""The one way a metering point number is shown on a page (D-109).

``{{ number|metering_point }}`` draws the number as ``numbers.format_metering_point_number`` groups it (the first 13
characters as one block, then blocks of four, separated by no-break spaces) in an element that never wraps and has
the tabular figures of a monospace font (class ``mp-number``, ``static/css/frame.css``). ``{{ number|metering_point_text }}``
is the same text without the element, for an attribute or a sentence. A missing number gives an empty string, so a template
keeps its own placeholder (``{% if number %}...{% else %}–{% endif %}``). The value is only escaped, never trusted.
"""

from django import template
from django.utils.html import format_html

from leg_platform.community import numbers

register = template.Library()


@register.filter(is_safe=False)
def metering_point_text(value):
    """The grouped text of a number (plain text; Django escapes it where it is printed)."""
    return numbers.format_metering_point_number(value)


@register.filter
def metering_point(value):
    """The number as a single-line element with its blocks; "" for no number."""
    text = numbers.format_metering_point_number(value)
    if not text:
        return ""
    return format_html('<span class="mp-number">{}</span>', text)
