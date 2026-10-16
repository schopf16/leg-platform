"""Template tags of the management area."""

from django import template
from django.urls import NoReverseMatch, reverse

from leg_platform.community.models import Direction
from leg_platform.community.status import MeteringPointStatus
from leg_platform.management import navigation
from leg_platform.management.returns import return_url

register = template.Library()


@register.simple_tag(takes_context=True)
def management_menu(context):
    """The entries of the menu group "Management" that the account may use (empty for most
    accounts); see ``leg_platform.management.navigation``."""
    request = context.get("request")
    if request is None or not hasattr(request, "user"):
        return []
    view_name = getattr(getattr(request, "resolver_match", None), "view_name", "") or ""
    return navigation.menu_entries(request.user, view_name)


@register.simple_tag
def optional_url(name, *args):
    """The address of a page that may not exist yet, or "": the links of a list to the pages of
    another area (LEG, person) appear as soon as that area has its pages."""
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


@register.simple_tag(takes_context=True)
def management_return_url(context, default):
    """The safe address to go back to (``next``), else ``default``."""
    return return_url(context["request"], default)


@register.filter
def bound(form, name):
    """The bound field of a form by its name (for a layout that is a list of names)."""
    return form[name]


@register.filter
def direction_class(value):
    """"consumption" or "feed_in" as the CSS class suffix ("consumption" or "feed-in")."""
    return "consumption" if value == Direction.CONSUMPTION else "feed-in"


@register.filter
def direction_label(value):
    return Direction(value).label


# The tone of the dot of each status of a metering point (docs/DESIGN.md, section 3.5; the
# specification: registered yellow-brown, notified blue, confirmed a green ring around a dark centre,
# active green, terminated grey).
STATUS_TONES = {
    MeteringPointStatus.REGISTERED: "warn",
    MeteringPointStatus.NOTIFIED: "accent",
    MeteringPointStatus.CONFIRMED: "ring",
    MeteringPointStatus.ACTIVE: "ok",
    MeteringPointStatus.TERMINATED: "muted",
}


@register.filter
def status_tone(value):
    return STATUS_TONES[MeteringPointStatus(value)]


@register.filter
def status_label(value):
    return MeteringPointStatus(value).label
