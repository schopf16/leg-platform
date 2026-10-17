"""Readable names of the rights in the administration.

Django stores "Can change email text"; the lists show "Mailtext: ändern", built from the translated
model name and the kind of right. There are no predefined groups or roles (D-012): this module only
names the rights and explains which one is needed for what.
"""

from django.contrib.auth.models import Permission
from django.utils.text import capfirst
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

ACTIONS = {
    "view": pgettext_lazy("right", "view"),
    "add": pgettext_lazy("right", "add"),
    "change": pgettext_lazy("right", "change"),
    "delete": pgettext_lazy("right", "delete"),
}


def permission_label(permission):
    """The label of a right for a list: model and kind, e.g. "Konto: ansehen"; a right that does not follow Django's pattern keeps its stored name."""
    action, _separator, _model = permission.codename.partition("_")
    model = permission.content_type.model_class()
    if action in ACTIONS and model is not None and permission.codename == f"{action}_{model._meta.model_name}":
        return f"{capfirst(model._meta.verbose_name)}: {ACTIONS[action]}"
    return str(permission)


def _community_tables():
    """The tables of the management data that a role may be allowed to read in the administration (D-091)."""
    from leg_platform.community.models import (
        HistoryEntry,
        Leg,
        LegAssignment,
        Location,
        ManagementSettings,
        MeteringPoint,
        PersonAssignment,
        PersonRecord,
        ShareChange,
        TransformerCircuit,
    )

    return (
        Location, MeteringPoint, Leg, TransformerCircuit, PersonRecord, PersonAssignment, LegAssignment,
        ShareChange, HistoryEntry, ManagementSettings,
    )


def assignable_permissions_filter():
    """The rights a group may hold: exactly the ones the help names (accounts, email texts, portal settings:
    view and change; the tables of the management data: view only).

    Graph, the security log and the other system pages stay with the system administrators; the
    management area itself has no rights to assign (D-086), the "view" of its tables is only for the
    plain lists of the administration (D-091).
    """
    from django.db.models import Q

    from leg_platform.accounts.models import EmailText, PortalSettings, User

    # Joined by model names, not looked up: the form is built without a query.
    condition = Q(pk__in=[])
    for model in (User, EmailText, PortalSettings):
        codenames = [f"{action}_{model._meta.model_name}" for action in ("view", "change")]
        condition |= Q(
            content_type__app_label=model._meta.app_label,
            content_type__model=model._meta.model_name,
            codename__in=codenames,
        )
    for model in _community_tables():
        condition |= Q(
            content_type__app_label=model._meta.app_label,
            content_type__model=model._meta.model_name,
            codename=f"view_{model._meta.model_name}",
        )
    return condition


class PermissionLabelsMixin:
    """For a ModelAdmin: shows the rights of a many-to-many field with their readable names."""

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if field is not None and db_field.related_model is Permission:
            field.queryset = field.queryset.select_related("content_type")
            field.label_from_instance = permission_label
        return field


def rights_help():
    """The rows of "Which rights do I need for what?": what a person wants to do and the rights for it. Text only."""
    from leg_platform.accounts.models import EmailText, PortalSettings, User

    def right(model, action):
        return f"{capfirst(model._meta.verbose_name)}: {ACTIONS[action]}"

    return [
        (_("See accounts"), right(User, "view")),
        (_("Change the details of accounts"), right(User, "change")),
        (_("Read the email texts"), right(EmailText, "view")),
        (_("Change the email texts"), right(EmailText, "change")),
        (_("See the settings of the portal"), right(PortalSettings, "view")),
        (
            _("Change name, logo, colours and addresses of the portal"),
            right(PortalSettings, "change"),
        ),
        *[
            (
                gettext("See the table of %(name)s (read only)") % {"name": model._meta.verbose_name_plural},
                right(model, "view"),
            )
            for model in _community_tables()
        ],
    ]
