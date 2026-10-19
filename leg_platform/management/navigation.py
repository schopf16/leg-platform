"""The menu group "Management" (D-077, D-086).

Every active staff account sees every entry, except "Mail texts" (admin, own permissions).
To add a page, add one ``MENU`` line; its URL names start with ``key`` (``management:<key>``,
``management:<key>_detail``) to mark the entry active. Note: the key ``processes`` also marks the
detail pages ``processes_application`` and ``processes_deregistration`` active.
"""

from dataclasses import dataclass

from django.contrib import admin
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from leg_platform.management.access import is_management_user

NAMESPACE = "management"


@dataclass(frozen=True)
class MenuEntry:
    key: str
    label: object  # lazy translated text
    icon: str  # id of a symbol in templates/_frame_icons.html, without "#"
    # Owner design (``vertraege/VORGABEN.md``): the entry "Contracts" stands after "Mail texts". That link (a page of
    # the administration with its own rights) exists only for an account that may open it, so such an entry follows it
    # when it is there and the last other entry when it is not.
    after_mail_texts: bool = False

    @property
    def url_name(self):
        return f"{NAMESPACE}:{self.key}"


# Rights on the email texts (admin); either shows the "Mail texts" link.
EMAIL_TEXT_PERMISSIONS = ("accounts.view_emailtext", "accounts.change_emailtext")

MENU = (
    MenuEntry("overview", _("Overview"), "i-overview"),
    MenuEntry("processes", _("Processes"), "i-processes"),
    MenuEntry("persons", _("Persons"), "i-persons"),
    MenuEntry("locations", _("Locations"), "i-building"),
    MenuEntry("legs", _("LEGs"), "i-network"),
    MenuEntry("metering_points", _("Metering points"), "i-metering"),
    MenuEntry("settings", _("Settings"), "i-settings"),
    MenuEntry("contracts", _("Contracts"), "i-document", after_mail_texts=True),
)


def is_active_entry(entry, view_name):
    """Whether the view belongs to the area of the entry."""
    base = entry.url_name
    return view_name == base or (view_name or "").startswith(base + "_")


def system_administration_visible(request):
    """Whether the entry "System administration" (the Django administration) is shown (D-102).

    Rule: an active staff account for which the administration lists **at least one thing it may open**, that is
    ``admin.site.get_app_list(request)`` is not empty. That is Django's own check (a model with any of the rights
    view, add, change or delete of the account, and for a system administrator the log files), so a superuser always
    sees the entry and a staff account without any right of the administration never sees a dead end. No group name is
    looked at. Hiding is not security: the administration checks every request itself (``has_permission``, the rights
    of each model, the second factor). The answer is kept on the request (the menu is drawn twice)."""
    user = getattr(request, "user", None)
    if user is None or not (user.is_authenticated and user.is_active and user.is_staff):
        return False
    if not hasattr(request, "_system_administration_visible"):
        request._system_administration_visible = bool(admin.site.get_app_list(request))
    return request._system_administration_visible


def menu_entries(user, view_name=""):
    """Menu dicts (``key``, ``label``, ``url``, ``icon``, ``active``); empty for a non-staff account.

    The link to the admin email texts is shown for an account with a right on them (the admin checks again, D-082);
    it stands after the entries of ``MENU`` and in front of the entries marked ``after_mail_texts`` (owner design).
    """
    if not is_management_user(user):
        return []
    mail_texts = any(user.has_perm(permission) for permission in EMAIL_TEXT_PERMISSIONS)
    mail_texts_entry = {
        "key": "email_texts",
        "label": _("Mail texts"),
        "url": reverse("admin:accounts_emailtext_changelist"),
        "icon": "i-mail",
        "active": False,
    }
    entries = []
    placed = False
    for entry in MENU:
        if entry.after_mail_texts and mail_texts and not placed:
            entries.append(mail_texts_entry)
            placed = True
        entries.append(
            {
                "key": entry.key,
                "label": entry.label,
                "url": reverse(entry.url_name),
                "icon": entry.icon,
                "active": is_active_entry(entry, view_name),
            }
        )
    if mail_texts and not placed:
        entries.append(mail_texts_entry)
    return entries
