"""The tasks of the start page (D-057): open while something is missing in the account, shown as tiles.

A person can hide a tile for good (``HiddenTask``); a done task disappears by itself. The tasks are
fixed here and computed from the account's state, never stored. Tasks about the participation (D-103,
START-SEITE.md, LEERE-ZUSTAENDE.md) are for customers only and read ``leg_platform.customer.journey``
(imported when needed: that module reads the community domain).
"""

from dataclasses import dataclass, replace
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
    # "open" (orange dot, "Open") or "optional" (grey dot, "Optional"); "primary" for a button that starts a
    # registration (blue), else "secondary" (START-SEITE.md, section 5).
    status: str = "open"
    button_style: str = "secondary"
    # An address that depends on the account (``url_for(user)``), resolved by ``open_tasks`` into ``fixed_url``.
    url_for: Callable | None = None
    fixed_url: str = ""

    @property
    def url(self):
        return self.fixed_url or reverse(self.url_name)


def _customer(user):
    """The tiles about the participation are for customers (staff accounts take part too, D-113): the one predicate of the
    customer pages, ``customer.access.is_customer`` (imported when needed)."""
    from leg_platform.customer import access

    return access.is_customer(user)


def _has_no_building(user):
    from leg_platform.customer import journey

    return _customer(user) and not journey.has_participation(user)


def _feed_in_missing(user):
    from leg_platform.customer import journey

    return _customer(user) and journey.feed_in_building(user)[0]


def _feed_in_url(user):
    from leg_platform.customer import journey

    return journey.feed_in_building(user)[1]


def _sign_in_method_url(user):
    """The choice "Sign-in method" in the profile card (D-106); an account with nothing to choose lands on the card's
    setup step (``profile_views.two_factor_state``)."""
    return f"{reverse('accounts:profile')}?edit=two_factor&step=choose#card-two-factor"


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
        key="register_building",
        title=_("Register building"),
        description=_("Register your building to take part in the LEG."),
        button=_("Register building"),
        is_open=_has_no_building,
        method="post",  # starts the application (a POST: it may create the draft, W1-05)
        url_name="customer:application_start",
        button_style="primary",
    ),
    Task(
        key="feed_in",
        title=_("Report feed-in later"),
        description=_("Do you have a PV system? Then register the metering point for the feed-in."),
        button=_("Add metering point"),
        is_open=_feed_in_missing,
        method="link",
        url_name="customer:buildings",
        status="optional",
        url_for=_feed_in_url,
    ),
    Task(
        key="two_factor",
        title=_("Choose how you sign in"),
        description=_("Choose a password, password and app, or passkey."),
        button=_("Choose"),
        is_open=lambda user: not second_factor.has_second_factor(user),
        method="link",
        url_name="accounts:profile",
        status="optional",
        url_for=_sign_in_method_url,
    ),
)
TASKS_BY_KEY = {task.key: task for task in TASKS}


def hidden_keys(user):
    return set(HiddenTask.objects.filter(user=user).values_list("task_key", flat=True))


def open_tasks(user):
    """The tasks to show: open and not hidden by this person."""
    hidden = hidden_keys(user)
    return [
        replace(task, fixed_url=task.url_for(user)) if task.url_for else task
        for task in TASKS
        if task.key not in hidden and task.is_open(user)
    ]


def hide(user, key):
    """Hide the task of this account for good; an unknown key raises ``ValueError``."""
    if key not in TASKS_BY_KEY:
        raise ValueError("Unknown task key.")
    HiddenTask.objects.get_or_create(user=user, task_key=key)
