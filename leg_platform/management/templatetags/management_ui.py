"""Template tags of the management area."""

from django import template
from django.urls import NoReverseMatch, reverse

from leg_platform.community import grid_operator
from leg_platform.community.models import Direction
from leg_platform.community.status import MeteringPointStatus
from leg_platform.management import navigation
from leg_platform.management.returns import return_url

register = template.Library()


@register.simple_tag(takes_context=True)
def management_menu(context):
    """Menu entries (empty for a non-staff account); see ``navigation``."""
    request = context.get("request")
    if request is None or not hasattr(request, "user"):
        return []
    view_name = getattr(getattr(request, "resolver_match", None), "view_name", "") or ""
    return navigation.menu_entries(request.user, view_name)


@register.simple_tag
def operator_name():
    """The grid operator name: ``{% operator_name as operator %}`` (D-086)."""
    return grid_operator.name()


@register.simple_tag
def optional_url(name, *args):
    """The URL of a page that may not exist yet, or ""."""
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
    """"consumption" or "feed_in" as the CSS suffix "consumption" or "feed-in"."""
    return "consumption" if value == Direction.CONSUMPTION else "feed-in"


@register.filter
def direction_label(value):
    return Direction(value).label


# Dot tone per status (docs/DESIGN.md, section 3.5).
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
