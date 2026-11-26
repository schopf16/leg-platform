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
    """The number as a single-line element with its blocks; "" for no number. The grid operator's part and the leading
    zeros are muted (class ``mp-number-head``), so that the eye lands on the digits that differ between the numbers;
    the text stays the whole number (copy, search)."""
    text = numbers.format_metering_point_number(value)
    if not text:
        return ""
    head_length = len(text) - len(text[numbers.HEAD_LENGTH:].lstrip("0  ")) if len(text) > numbers.HEAD_LENGTH else 0
    if not head_length:
        return format_html('<span class="mp-number">{}</span>', text)
    return format_html(
        '<span class="mp-number"><span class="mp-number-head">{}</span>{}</span>', text[:head_length], text[head_length:]
    )


SHORT_HEAD = 4  # "CH" and the first two characters of the identifier
SHORT_TAIL = 4  # at least the last four characters


@register.filter
def metering_point_short(value):
    """The number cut to its ends for a narrow column: "CH1A…0007" (the head, an ellipsis, then the characters that
    differ: from the first non-zero of the grid operator's assigned part, at least ``SHORT_TAIL``). The whole number is
    in the ``title`` (and on the detail page). A number not longer than head and tail is shown whole."""
    text = numbers.compact_number(value)
    if not text:
        return ""
    assigned = text[numbers.HEAD_LENGTH:]
    tail = max(len(assigned.lstrip("0")), SHORT_TAIL)
    if len(text) <= SHORT_HEAD + tail + 1:
        return format_html('<span class="mp-number">{}</span>', numbers.format_metering_point_number(text))
    return format_html(
        '<span class="mp-number" title="{}">{}…{}</span>',
        numbers.format_metering_point_number(text), text[:SHORT_HEAD], text[-tail:],
    )
