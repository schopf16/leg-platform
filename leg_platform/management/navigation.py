"""The menu group "Management" (D-077, D-086).

Every active staff account sees every entry, except "Mail texts" (admin, own permissions).
To add a page, add one ``MENU`` line; its URL names start with ``key`` (``management:<key>``,
``management:<key>_detail``) to mark the entry active.
"""

from dataclasses import dataclass

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from leg_platform.management.access import is_management_user

NAMESPACE = "management"


@dataclass(frozen=True)
class MenuEntry:
    key: str
    label: object  # lazy translated text
    icon: str  # id of a symbol in templates/_frame_icons.html, without "#"

    @property
    def url_name(self):
        return f"{NAMESPACE}:{self.key}"


# Rights on the email texts (admin); either shows the "Mail texts" link.
EMAIL_TEXT_PERMISSIONS = ("accounts.view_emailtext", "accounts.change_emailtext")

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
    """Menu dicts (``key``, ``label``, ``url``, ``icon``, ``active``); empty for a non-staff account.

    The last entry links to the admin email texts for an account with a right on them (the admin checks again, D-082).
    """
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
