"""Template tag of the menu of the customer's pages (``templates/_frame_menu.html``, ``base.html``).

The state is read from the name of the resolved view, never from a value of the request: the entries "Participation /
Buildings" and "Information / Articles of association" are active on the pages of ``customer:`` (the contract page is
"Information", every other page of the namespace is "Participation").
"""

from django import template
from django.utils.html import format_html
from django.utils.translation import gettext

from leg_platform import render_as
from leg_platform.customer import access

register = template.Library()

INFORMATION_VIEWS = frozenset({"customer:contract", "customer:contact"})


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
def participates(context):
    """Whether the account takes part as a customer (``customer.access.is_customer``: active, a customer number; staff
    and system administrators too, D-113): ``{% participates as is_participant %}``. Buildings (flat entry, key ``participation``), Information and the
    matching tabs are shown only then, so no link leads to a 404."""
    user = render_as.subject(context.get("request"))
    return bool(user is not None and access.is_customer(user))


@register.simple_tag(takes_context=True)
def render_as_pending(context):
    """In the support view (D-131): the number of unseen messages of the person viewed. They are only counted, never shown
    and never marked as seen."""
    from leg_platform.community import customer_messages

    person = render_as.subject(context.get("request"))
    return len(customer_messages.messages_for(person)) if person is not None else 0


@register.simple_tag(takes_context=True)
def customer_menu(context):
    """``{"participation": bool, "information": bool}``: which holds the current page: ``participation`` (the buildings entry: wizard, deregistration, building edit) or ``information``."""
    match = getattr(context.get("request"), "resolver_match", None)
    name = getattr(match, "view_name", "") or ""
    in_area = name.startswith("customer:")
    return {
        "participation": in_area and name not in INFORMATION_VIEWS,
        "information": name in INFORMATION_VIEWS,
        "contract": name == "customer:contract",
        "contact": name == "customer:contact",
    }
