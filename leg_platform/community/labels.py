"""Display labels that name the grid operator (D-086, D-103).

The labels of model choices are static ("... the grid operator ..."): a model label must never read the database,
because the system check, ``migrate`` and ``check --deploy`` evaluate them. Pages and services that show a state or
kind call these helpers, which fill in the operator's name from ``GridOperator`` (``grid_operator.text``) and fall back
to the static label if the database cannot be read.
"""

from django.db import DatabaseError
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator
from leg_platform.community.models import Application, ApplicationNotice

# The msgids with ``%(operator)s``; the static labels of the models are the fallback.
_STATE_TEXTS = {
    Application.State.OPERATOR_STARTED: grid_operator.text(_("Registration at the %(operator)s started")),
    Application.State.OPERATOR_CONFIRMED: grid_operator.text(_("Confirmed by the %(operator)s")),
}
_KIND_TEXTS = {
    ApplicationNotice.Kind.OPERATOR_CONFIRMATION_LAPSED: grid_operator.text(
        _("Confirmation by the %(operator)s did not arrive: withdraw the registration there")
    ),
    ApplicationNotice.Kind.OPERATOR_DATA_CHANGED: grid_operator.text(
        _("Data changed after the registration at the %(operator)s: check the registration again")
    ),
    ApplicationNotice.Kind.ACCOUNT_DELETED: grid_operator.text(
        _("Account deleted: withdraw the registration at the %(operator)s")
    ),
}


def _label(choices, texts, value):
    choice = choices(value)
    text = texts.get(choice)
    if text is None:
        return str(choice.label)
    try:
        return str(text)
    except DatabaseError:
        return str(choice.label)


def application_state_label(state):
    """The label of an ``Application.State`` with the operator's name where the state names it."""
    return _label(Application.State, _STATE_TEXTS, state)


def notice_kind_label(kind):
    """The label of an ``ApplicationNotice.Kind`` with the operator's name where the kind names it."""
    return _label(ApplicationNotice.Kind, _KIND_TEXTS, kind)
