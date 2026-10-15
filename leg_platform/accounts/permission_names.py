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


def permission_label(permission):
    """The label of a right for a list: the model and the kind of right, e.g. "Konto: ansehen".

    A right that does not follow the pattern of Django (view, add, change, delete of a model) keeps
    its stored name.
    """
    action, _separator, _model = permission.codename.partition("_")
    model = permission.content_type.model_class()
    if action in ACTIONS and model is not None and permission.codename == f"{action}_{model._meta.model_name}":
        return f"{capfirst(model._meta.verbose_name)}: {ACTIONS[action]}"
    return str(permission)


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
    from leg_platform.accounts.models import EmailText, User

    def right(model, action):
        return f"{capfirst(model._meta.verbose_name)}: {ACTIONS[action]}"

    return [
        (_("See accounts"), right(User, "view")),
        (_("Change the details of accounts"), right(User, "change")),
        (_("Read the email texts"), right(EmailText, "view")),
        (_("Change the email texts"), right(EmailText, "change")),
    ]
