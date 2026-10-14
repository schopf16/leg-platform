"""The tasks of the start page (D-057; LEG-Design-Vorgaben/navigation/VORGABEN.md, section 4).

A task is open as long as something is missing in the account. It is shown as a tile. The person
can hide a tile for good (``HiddenTask``); a done task disappears by itself. Which tasks exist is
fixed here; there is nothing to configure. The tasks are computed from the state of the account,
never stored.
"""

from dataclasses import dataclass
from typing import Callable

from django.urls import reverse
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import second_factor
from leg_platform.accounts.models import HiddenTask


def address_is_complete(user):
    """A street (or a post office box), a postal code and a town are there."""
    has_street = bool(user.street.strip()) or user.post_office_box_number is not None
    return bool(has_street and user.postal_code_display and user.town.strip())


@dataclass(frozen=True)
class Task:
    key: str
    title: Promise
    description: Promise
    button: Promise
    is_open: Callable
    # How the button leads on: "link" (a page) or "post" (a form that starts something).
    method: str
    url_name: str

    @property
    def url(self):
        return reverse(self.url_name)


TASKS = (
    Task(
        key="address",
        title=_("Add your address"),
        description=_("Your address is still missing."),
        button=_("Add address"),
        is_open=lambda user: not address_is_complete(user),
        method="link",
        url_name="accounts:profile_edit",
    ),
    Task(
        key="two_factor",
        title=_("Two-factor sign-in"),
        description=_("Protects your account additionally with a code from an app."),
        button=_("Set up 2FA"),
        is_open=lambda user: not second_factor.has_second_factor(user),
        method="post",
        url_name="accounts:two_factor_setup",
    ),
)
TASKS_BY_KEY = {task.key: task for task in TASKS}


def hidden_keys(user):
    return set(HiddenTask.objects.filter(user=user).values_list("task_key", flat=True))


def open_tasks(user):
    """The tasks to show: open and not hidden by this person."""
    hidden = hidden_keys(user)
    return [task for task in TASKS if task.key not in hidden and task.is_open(user)]


def hide(user, key):
    """Hide the task of this account for good; an unknown key raises ``KeyError``."""
    TASKS_BY_KEY[key]
    HiddenTask.objects.get_or_create(user=user, task_key=key)
