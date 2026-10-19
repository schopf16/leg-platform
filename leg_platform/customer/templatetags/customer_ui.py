"""Template tag of the menu of the customer's pages (``templates/_frame_menu.html``, ``base.html``).

The state is read from the name of the resolved view, never from a value of the request: the entries "Participation /
Buildings" and "Information / Articles of association" are active on the pages of ``customer:`` (the contract page is
"Information", every other page of the namespace is "Participation").
"""

from django import template

register = template.Library()

INFORMATION_VIEWS = frozenset({"customer:contract"})


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
