"""The start page of a customer (owner decision 2026-10-24): the open applications as cards and the confirmed buildings as a table.

* "Open application": an application that is not submitted yet (draft or paused). One card per application; the card pages
  through the buildings of its stage (arrows, "1/2"), with the steps of the wizard and the button of the next step.
* "In review": submitted, not yet registered with the grid operator, with the five steps of the page "submitted".
* "Confirmed": confirmed by the grid operator and waiting for the start. A table, not a card: the buildings with the days
  until the start date and a link to the building on the page "Buildings".

Everything is computed from the stored rows (``community.applications``); nothing is stored here. One application is one
building: the card shows the building of the application.
"""

from dataclasses import dataclass, field

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from leg_platform.community import applications, grid_operator
from leg_platform.community.models import Application
from leg_platform.customer import journey
from leg_platform.customer.steps import STEP_NAMES, post_submit_steps

TO_REVIEW = (Application.State.SUBMITTED, Application.State.IN_REVIEW, Application.State.OPERATOR_STARTED)
CONFIRMED = (Application.State.OPERATOR_CONFIRMED,)


@dataclass(frozen=True)
class Step:
    label: str
    state: str  # done / now / todo
    word: str


@dataclass(frozen=True)
class Slide:
    """One building: the card shows its address, the EGID with the optional name, the steps and the button."""

    address: str
    egid: object
    name: str
    steps: list
    done: int
    total: int
    button: str
    url: str
    # The LEG returned the application for correction: the card says so and gives the reason.
    returned: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Group:
    key: str
    eyebrow: str
    slides: list = field(default_factory=list)


@dataclass(frozen=True)
class ConfirmedRow:
    address: str
    egid: object
    name: str
    days: object  # days until the chosen start date, or None for "as soon as possible"
    url: str


@dataclass(frozen=True)
class StageView:
    groups: list
    confirmed: list

    @property
    def empty(self):
        return not self.groups and not self.confirmed


def _building(application):
    return application.buildings.select_related("location").first()


def _building_facts(building):
    """(address, EGID, name) of a building of an application; a building that is not chosen yet has no address."""
    if building is None:
        return _("Building not chosen yet"), "", ""
    if building.location_id:
        street, house_number, _postal, _town = building.location.current_address()
        return f"{street} {house_number}".strip(), building.location.egid, building.name
    return f"{building.street} {building.house_number}".strip(), building.egid, building.name


def _draft_slide(application):
    progress = applications.progress(application)
    steps = []
    for step in progress.steps:
        if not step.applicable:
            continue
        if step.state != applications.OPEN:
            steps.append(Step(STEP_NAMES[step.key], "done", _("Finished")))
        elif step.key == progress.next_step:
            steps.append(Step(STEP_NAMES[step.key], "now", _("Next up")))
        else:
            steps.append(Step(STEP_NAMES[step.key], "todo", _("Open")))
    if progress.next_step is None and application.returned_at is not None:
        button, url = _("Check your details"), reverse("customer:application_start")
    elif progress.next_step is None:
        button, url = journey.READY[1], reverse(journey.READY[2], args=[application.pk])
    else:
        button, url = journey.NEXT[progress.next_step][1], reverse(journey.NEXT[progress.next_step][2], args=[application.pk])
    address, egid, name = _building_facts(_building(application))
    returned = application.returned_at is not None
    reason = applications.returned_reason(application) if returned else ""
    return Slide(
        address, egid, name, steps, progress.done, progress.total, button, url, returned=returned, reason=reason,
    )


def _review_slide(application, operator):
    rows = post_submit_steps(application, operator, named=False)
    steps = [
        Step(label, "done" if state == applications.DONE else ("now" if state == "next" else "todo"), word)
        for _key, state, label, word in rows
    ]
    address, egid, name = _building_facts(_building(application))
    done = sum(step.state == "done" for step in steps)
    return Slide(address, egid, name, steps, done, len(steps), _("View progress"), reverse("customer:buildings"))


def _started(application, today):
    """The start date has been reached: the building is in operation and leaves the table (an "as soon as possible"
    start has no date to reach; it leaves the table once the LEG completes the application)."""
    return (
        application.start_choice == Application.StartChoice.DATE
        and application.desired_start_date is not None
        and application.desired_start_date <= today
    )


def _confirmed_row(application, today):
    address, egid, name = _building_facts(_building(application))
    days = None
    if application.start_choice == Application.StartChoice.DATE and application.desired_start_date:
        days = (application.desired_start_date - today).days
    return ConfirmedRow(address, egid, name, days, reverse("customer:buildings"))


def for_account(person):
    """The start page's view of the open applications of the account (``StageView``)."""
    rows = list(
        Application.objects.filter(person=person, state__in=Application.OPEN_STATES)
        .prefetch_related("buildings__location")
        .order_by("pk")
    )
    operator = grid_operator.name()
    today = timezone.localdate()
    drafts = [_draft_slide(row) for row in rows if row.state in Application.DRAFT_STATES]
    review = [_review_slide(row, operator) for row in rows if row.state in TO_REVIEW]
    confirmed = [
        _confirmed_row(row, today) for row in rows if row.state in CONFIRMED and not _started(row, today)
    ]
    groups = []
    if drafts:
        groups.append(Group("drafts", _("Open application"), drafts))
    if review:
        groups.append(Group("review", _("In review"), review))
    return StageView(groups=groups, confirmed=confirmed)
