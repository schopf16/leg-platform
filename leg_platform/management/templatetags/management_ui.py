"""Template tags of the management area."""

from django import forms, template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext

from leg_platform.community import grid_operator
from leg_platform.community.models import Direction
from leg_platform.community.status import MeteringPointStatus
from leg_platform.management import navigation
from leg_platform.management.access import is_management_user

register = template.Library()


@register.simple_tag(takes_context=True)
def management_menu(context):
    """Menu entries (empty for a non-staff account); see ``navigation``."""
    request = context.get("request")
    if request is None or not hasattr(request, "user"):
        return []
    view_name = getattr(getattr(request, "resolver_match", None), "view_name", "") or ""
    return navigation.menu_entries(request.user, view_name)


@register.simple_tag(takes_context=True)
def system_administration(context):
    """Whether the menu shows "System administration": ``{% system_administration as show_system %}`` (D-102)."""
    request = context.get("request")
    return request is not None and navigation.system_administration_visible(request)


@register.simple_tag(takes_context=True)
def staff_area(context):
    """Whether the account may open the staff area (management and system administration, D-113): the entry "Staff
    area" of the menu of "My area" and the button of the start page. The same test as the management area itself
    (``management.access.is_management_user``); the second factor and every request are checked by the views.
    Never in the support view "View as" (D-131): there the staff entries are not shown."""
    request = context.get("request")
    user = getattr(request, "user", None)
    if getattr(request, "subject", None) is not None:
        return False
    return bool(user is not None and is_management_user(user))


@register.filter
def staff_page_title(title, workspace):
    """The title of a page of the staff area with the prefix "Staff area: ..." (D-113): ``{% filter
    staff_page_title:frame_workspace %}{% block title %}{% endblock %}{% endfilter %}``. The title arrives escaped from
    the template; a raw string is escaped here (``conditional_escape`` leaves safe text alone and escapes any other),
    and so is the text of the catalogue. Any other workspace leaves the title alone."""
    if workspace != "staff":
        return title
    return mark_safe(conditional_escape(gettext("Staff area: %(page)s")) % {"page": conditional_escape(title)})


@register.simple_tag
def operator_name():
    """The grid operator name: ``{% operator_name as operator %}`` (D-086)."""
    return grid_operator.name()


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
