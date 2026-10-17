"""The menu group "Management" (D-077, D-086).

Every active staff account sees every entry (the pages are open to all staff, ``leg_platform.management.access``).
The one exception is the link "Mail texts" at the end: it leads to the Django administration, which keeps its own
permission model, so it is shown only to an account that holds a right on the email texts.

To add an entry for a new page, add ONE line to ``MENU`` (in the order of the menu). The URL names
of an area start with its ``key`` (``management:<key>``, ``management:<key>_detail``, ...): that is
how the entry is marked as the active one.
"""

from dataclasses import dataclass

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from leg_platform.management.access import is_management_user

NAMESPACE = "management"


@dataclass(frozen=True)
class MenuEntry:
    key: str
    label: object  # a lazy translated text
    icon: str  # the id of a symbol of templates/_frame_icons.html, without "#"

    @property
    def url_name(self):
        return f"{NAMESPACE}:{self.key}"


# The rights on the email texts of the system administration (the link "Mail texts" at the end of the group).
EMAIL_TEXT_PERMISSIONS = ("accounts.view_emailtext", "accounts.change_emailtext")

# One line per entry, in the order of the menu: overview, persons, locations, LEGs, metering
# points, settings.
MENU = (
    MenuEntry("overview", _("Overview"), "i-overview"),
    MenuEntry("persons", _("Persons"), "i-persons"),
    MenuEntry("locations", _("Locations"), "i-building"),
    MenuEntry("legs", _("LEGs"), "i-network"),
    MenuEntry("metering_points", _("Metering points"), "i-metering"),
    MenuEntry("settings", _("Settings"), "i-settings"),
)


def is_active_entry(entry, view_name):
    """Whether the view belongs to the area of the entry."""
    base = entry.url_name
    return view_name == base or (view_name or "").startswith(base + "_")


def menu_entries(user, view_name=""):
    """The entries of the group for the account: a list of dicts with ``label``, ``url``,
    ``icon``, ``active`` and ``key``; empty for an account that is no active staff account. The last entry is
    the link to the email texts of the system administration (the page that exists; nothing new is built for it,
    D-082) for an account that holds a right on them; the administration checks the right again."""
    if not is_management_user(user):
        return []
    entries = [
        {
            "key": entry.key,
            "label": entry.label,
            "url": reverse(entry.url_name),
            "icon": entry.icon,
            "active": is_active_entry(entry, view_name),
        }
        for entry in MENU
    ]
    if any(user.has_perm(permission) for permission in EMAIL_TEXT_PERMISSIONS):
        entries.append(
            {
                "key": "email_texts",
                "label": _("Mail texts"),
                "url": reverse("admin:accounts_emailtext_changelist"),
                "icon": "i-mail",
                "active": False,
            }
        )
    return entries
