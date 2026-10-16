"""Readable names of the rights in the administration (design package, acceptance, P1-3).

Django stores the name of a right in English ("Can change email text"). The lists of the
administration show "Mailtext: ändern" instead, built from the name of the model (translated) and
the kind of right. The portal has no predefined groups or roles (D-012): this module only names
the rights and explains which one is needed for what.
"""

from django.contrib.auth.models import Permission
from django.utils.text import capfirst
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

ACTIONS = {
    "view": pgettext_lazy("right", "view"),
    "add": pgettext_lazy("right", "add"),
    "change": pgettext_lazy("right", "change"),
    "delete": pgettext_lazy("right", "delete"),
}

# Rights of the community models that are not view, add, change or delete (D-077).
CUSTOM_RIGHTS = {
    "correct_person_address": pgettext_lazy("right", "correct the address"),
}


def permission_label(permission):
    """The label of a right for a list: the model and the kind of right, e.g. "Konto: ansehen".

    A right that does not follow the pattern of Django (view, add, change, delete of a model) keeps
    its stored name, except the rights of ``CUSTOM_RIGHTS``.
    """
    action, _separator, _model = permission.codename.partition("_")
    model = permission.content_type.model_class()
    if action in ACTIONS and model is not None and permission.codename == f"{action}_{model._meta.model_name}":
        return f"{capfirst(model._meta.verbose_name)}: {ACTIONS[action]}"
    if model is not None and permission.codename in CUSTOM_RIGHTS:
        return f"{capfirst(model._meta.verbose_name)}: {CUSTOM_RIGHTS[permission.codename]}"
    return str(permission)


def community_models():
    """The models of the management area whose rights a role may hold (D-077)."""
    from leg_platform.community.models import (
        Leg,
        Location,
        ManagementSettings,
        MeteringPoint,
        PersonRecord,
        TransformerCircuit,
    )

    return (PersonRecord, Location, TransformerCircuit, Leg, MeteringPoint, ManagementSettings)


def community_codenames(model):
    """The codenames of a model: its default rights (as the model's Meta lists them) and its own."""
    names = [f"{action}_{model._meta.model_name}" for action in model._meta.default_permissions]
    return names + [codename for codename, _name in model._meta.permissions]


def assignable_permissions_filter():
    """The rights that a group may hold (design package, final spot check, 1): exactly the ones that
    the help names. Microsoft Graph, the security log and the other pages of the system stay with
    the system administrators; no right is offered for them. The portal settings are offered since
    D-073 (view and change, assigned at runtime like every right)."""
    from django.db.models import Q

    from leg_platform.accounts.models import EmailText, PortalSettings, User

    # Joined by the names of the model, not looked up: the form is built without a query.
    condition = Q(pk__in=[])
    for model in (User, EmailText, PortalSettings):
        codenames = [f"{action}_{model._meta.model_name}" for action in ("view", "change")]
        condition |= Q(
            content_type__app_label=model._meta.app_label,
            content_type__model=model._meta.model_name,
            codename__in=codenames,
        )
    # The management area (D-077): every right of these models that the pages check, including the
    # right to correct the address of a person.
    for model in community_models():
        condition |= Q(
            content_type__app_label=model._meta.app_label,
            content_type__model=model._meta.model_name,
            codename__in=community_codenames(model),
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
    """The rows of the help "Which rights do I need for what?": what a person wants to do, and
    the rights to give for it. Only text; nothing is created or assigned."""
    from leg_platform.accounts.models import EmailText, PortalSettings, User
    from leg_platform.community.models import (
        Leg,
        Location,
        ManagementSettings,
        MeteringPoint,
        PersonRecord,
        TransformerCircuit,
    )

    def right(model, action):
        return f"{capfirst(model._meta.verbose_name)}: {ACTIONS[action]}"

    def rights(models, action):
        return ", ".join(right(model, action) for model in models)

    areas = (PersonRecord, Location, Leg, MeteringPoint)
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
        # The management area (D-077): the pages check these rights on every request.
        (_("See persons, locations, LEGs and metering points"), rights(areas, "view")),
        (
            _("Change persons, locations, LEGs and metering points, and assign them"),
            rights(areas, "change"),
        ),
        (
            _("Create locations, LEGs, metering points and transformer circuits"),
            rights((Location, Leg, MeteringPoint, TransformerCircuit), "add"),
        ),
        (_("Delete locations, LEGs and metering points"), rights((Location, Leg, MeteringPoint), "delete")),
        (
            _("Correct the address of a person"),
            f"{capfirst(PersonRecord._meta.verbose_name)}: {CUSTOM_RIGHTS['correct_person_address']}",
        ),
        (_("See the settings of the management area"), right(ManagementSettings, "view")),
        (_("Change the settings of the management area"), right(ManagementSettings, "change")),
    ]
