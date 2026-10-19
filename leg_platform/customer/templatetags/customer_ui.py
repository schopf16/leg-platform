"""Template tag of the menu of the customer's pages (``templates/_frame_menu.html``, ``base.html``).

The state is read from the name of the resolved view, never from a value of the request: the entries "Participation /
Buildings" and "Information / Articles of association" are active on the pages of ``customer:`` (the contract page is
"Information", every other page of the namespace is "Participation").
"""

from django import template
from django.utils.html import format_html
from django.utils.translation import gettext

register = template.Library()

INFORMATION_VIEWS = frozenset({"customer:contract"})


@register.simple_tag(takes_context=True)
def leg_link(context):
    """The word "LEG" as a link to the instance's website where one is configured (the portal settings, as on the entry
    pages), else the plain word (W1-03). The address is escaped; the link opens in a new tab without the opener."""
    url = str(context.get("instance_website_url") or "")
    word = gettext("LEG")
    # Only an https address becomes a link: never a "javascript:", "data:" or plain "http:" address (the setting is
    # validated already; this is the second check at the place where it becomes a link).
    if not url.lower().startswith("https://"):
        return word
    return format_html(
        '<a href="{}" target="_blank" rel="noopener noreferrer">{}<span class="visually-hidden"> ({})</span></a>',
        url, word, gettext("opens in a new tab"),
    )


@register.simple_tag(takes_context=True)
def customer_menu(context):
    """``{"participation": bool, "information": bool}``: which of the two parent entries holds the current page."""
    match = getattr(context.get("request"), "resolver_match", None)
    name = getattr(match, "view_name", "") or ""
    in_area = name.startswith("customer:")
    return {
        "participation": in_area and name not in INFORMATION_VIEWS,
        "information": name in INFORMATION_VIEWS,
    }
