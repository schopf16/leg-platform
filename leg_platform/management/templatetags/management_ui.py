"""Template tags of the management area."""

from django import forms, template
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


def first_field_name(form):
    """The name of the first control of a form that a person types in, in the order of its layout ("" if none):
    not a fact row, hidden field, group of options or check boxes (D-094, focus in the first field)."""
    skipped = (forms.HiddenInput, forms.RadioSelect, forms.CheckboxSelectMultiple)
    already_chosen = getattr(form, "focus_skips", ())
    for row, names in getattr(form, "layout", None) or []:
        if row == "divider":
            continue
        for name in names:
            field = form.fields.get(name)
            if field is None or field.disabled or isinstance(field.widget, skipped) or name in already_chosen:
                continue
            return name
    return ""


@register.filter
def described(field, form=None):
    """The field drawn with ``aria-describedby`` (its message, its hint) and, for the first control of the form,
    ``data-first-field`` (the page script puts the cursor there on a desktop) and the lead sentence."""
    ids = [f"{field.auto_id}_error", f"{field.auto_id}_hint"]
    attrs = {}
    if form is not None and first_field_name(form) == field.name:
        attrs["data-first-field"] = True
        if not getattr(form, "no_lead", False):
            ids.append("dialog-lead")
    attrs["aria-describedby"] = " ".join(ids)
    return field.as_widget(attrs=attrs)


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
