"""The menu group "Management" and the rule for what a person sees in it (D-077).

The group shows only the entries that the account may use: the same permissions as the pages
themselves, so that the menu never offers a link that ends in "no access" (hiding an entry is no
access control; every page checks again, ``leg_platform.management.access``).

To add an entry for a new page, add ONE line to ``MENU`` (in the order of the menu). The URL names
of an area start with its ``key`` (``management:<key>``, ``management:<key>_detail``, ...): that is
how the entry is marked as the active one.
"""

from dataclasses import dataclass

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

NAMESPACE = "management"


@dataclass(frozen=True)
class MenuEntry:
    key: str
    label: object  # a lazy translated text
    icon: str  # the id of a symbol of templates/_frame_icons.html, without "#"
    permissions: tuple = ()  # all required
    any_of: tuple = ()  # or: at least one

    @property
    def url_name(self):
        return f"{NAMESPACE}:{self.key}"


# The view permissions of the areas: whoever holds one of them may open the overview.
AREA_VIEW_PERMISSIONS = (
    "community.view_personrecord",
    "community.view_location",
    "community.view_leg",
    "community.view_meteringpoint",
    "community.view_managementsettings",
)

# One line per entry, in the order of the menu: overview, persons, locations, LEGs, metering
# points, settings. The pages that do not exist yet have no line yet.
MENU = (
    MenuEntry("overview", _("Overview"), "i-overview", any_of=AREA_VIEW_PERMISSIONS),
    MenuEntry("locations", _("Locations"), "i-building", ("community.view_location",)),
    MenuEntry("settings", _("Settings"), "i-settings", ("community.view_managementsettings",)),
)


def may_use(user, entry):
    if not (user.is_authenticated and user.is_active and user.is_staff):
        return False
    if entry.permissions and not user.has_perms(entry.permissions):
        return False
    if entry.any_of and not any(user.has_perm(permission) for permission in entry.any_of):
        return False
    return bool(entry.permissions or entry.any_of)


def is_active_entry(entry, view_name):
    """Whether the view belongs to the area of the entry."""
    base = entry.url_name
    return view_name == base or (view_name or "").startswith(base + "_")


def menu_entries(user, view_name=""):
    """The entries of the group for the account: a list of dicts with ``label``, ``url``,
    ``icon``, ``active`` and ``key``; empty if the account may use none."""
    if not (user.is_authenticated and user.is_staff):
        return []
    return [
        {
            "key": entry.key,
            "label": entry.label,
            "url": reverse(entry.url_name),
            "icon": entry.icon,
            "active": is_active_entry(entry, view_name),
        }
        for entry in MENU
        if may_use(user, entry)
    ]
