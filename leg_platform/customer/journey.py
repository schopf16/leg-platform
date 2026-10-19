"""The card "Your way into the LEG" and the tiles about participation on the start page (START-SEITE.md; D-103).

State A: an application of the account is not completed yet: the card shows the steps with a dot and a word, the progress
and the next step with one main button. State B: no application is open: no card (only the tiles). The card cannot be
hidden. Everything is computed from the stored rows (``community.applications.progress``), never stored.

The wording of the sentence and the button of the contract step is the owner's; the other steps follow it (developer's
proposal, marked in ``docs/DESIGN.md``).
"""

from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.community import applications, grid_operator
from leg_platform.community.models import Application, Direction, PersonAssignment
from leg_platform.customer import steps as steps_module
from leg_platform.customer.steps import STEP_NAMES

DONE, NOW, TODO = "done", "now", "todo"

# What the card says about the next step: (sentence, button) and the name of the page of the step.
NEXT = {
    applications.BUILDING: (_("You have to say where your building is."), _("Enter building"), "customer:application_building"),
    applications.METERING_POINTS: (
        _("You have to enter your metering points."), _("Enter metering points"), "customer:application_metering_points",
    ),
    applications.BANK_CONNECTION: (
        _("You have to enter your bank or postal connection."), _("Enter connection"), "customer:application_bank_connection",
    ),
    applications.CONTRACT: (
        _("You have to agree to the articles of association."), _("Read the contract and agree"),
        "customer:application_contract",
    ),
    applications.START: (_("You have to choose the start date."), _("Choose start date"), "customer:application_start_date"),
}
READY = (
    _("Your details are complete. Submit your application now."), _("Submit application"),
    "customer:application_start_date",
)
IN_REVIEW = _("The LEG is checking your details. You do not need to do anything.")
AT_OPERATOR = _("The LEG has registered you at the grid operator. Please confirm the registration there.")
CONFIRMED = _("The registration is confirmed. The LEG completes it.")


@dataclass(frozen=True)
class JourneyStep:
    label: object
    state: str  # done / now / todo
    muted: bool = False  # "not yet selectable": greyed out but visible
    word: str = ""  # a special word for the state (after the submission); empty: Finished / Next up / Open


@dataclass(frozen=True)
class Journey:
    steps: list
    done: int
    total: int
    sentence: object
    button: object
    url: str
    paused: bool = False
    paused_date: object = None  # the day a paused draft is deleted
    reason: str = ""  # why the LEG returned the draft
    returned: bool = False


def current_application(person):
    """The application the card follows: the newest draft, else the newest one in flight; None if none is open."""
    rows = list(Application.objects.filter(person=person, state__in=Application.OPEN_STATES).order_by("-pk"))
    drafts = [row for row in rows if row.state in Application.DRAFT_STATES]
    return (drafts or rows or [None])[0]


def for_account(person):
    """The ``Journey`` of the account, or None (state B: no application is open).

    One list for the whole process (W1-01): before the submission the steps of the wizard with its names and "n of m"
    (no final step: the count is the wizard's); after it the five steps of the page "submitted"."""
    application = current_application(person)
    if application is None:
        return None
    if application.state in Application.DRAFT_STATES:
        current = applications.progress(application)
        applicable = [step for step in current.steps if step.applicable]
        steps = []
        for step in applicable:
            if step.state != applications.OPEN:
                steps.append(JourneyStep(STEP_NAMES[step.key], DONE))
            else:
                steps.append(JourneyStep(STEP_NAMES[step.key], NOW if step.key == current.next_step else TODO, not step.available))
        if current.next_step is None:
            sentence, button, name = READY
        else:
            sentence, button, name = NEXT[current.next_step]
        paused = application.state == Application.State.PAUSED
        return Journey(
            steps, current.done, current.total, sentence, button,
            reverse(name, args=[application.pk]), paused=paused,
            paused_date=applications.rollback_date(application) if paused else None,
            reason=applications.returned_reason(application), returned=application.returned_at is not None,
        )
    # Submitted or later: everything the customer does is done; the LEG acts. The list of the page "submitted".
    steps = [
        JourneyStep(label, {applications.DONE: DONE, "next": NOW}.get(state, TODO), word=word)
        for _key, state, label, word in steps_module.post_submit_steps(application, grid_operator.name())
    ]
    sentence = {
        Application.State.OPERATOR_STARTED: AT_OPERATOR,
        Application.State.OPERATOR_CONFIRMED: CONFIRMED,
    }.get(application.state, IN_REVIEW)
    return Journey(
        steps, sum(step.state == DONE for step in steps), len(steps), sentence, _("View progress"),
        reverse("customer:buildings"),
    )


# ---- Facts for the tiles of the start page ----


def has_participation(person):
    """Whether the account has a building: a stored metering point of its own or an application that is not completed."""
    today = timezone.localdate()
    return (
        PersonAssignment.objects.not_ended(today).filter(person=person, metering_point__is_archived=False).exists()
        or Application.objects.filter(person=person, state__in=Application.OPEN_STATES).exists()
    )


def feed_in_building(person):
    """For the tile "Report feed-in later": ``(show, url)``. Shown only where the account has a draw but no feed-in
    anywhere, and no application is open (then the card above leads the way). The url opens the application with the
    building chosen if there is exactly one such building, else the page Buildings."""
    today = timezone.localdate()
    if Application.objects.filter(person=person, state__in=Application.OPEN_STATES).exists():
        return False, ""
    rows = list(
        PersonAssignment.objects.not_ended(today)
        .filter(person=person, metering_point__is_archived=False, metering_point__terminated_on__isnull=True)
        .values_list("metering_point__direction", "metering_point__location_id")
    )
    if not rows or any(direction == Direction.FEED_IN for direction, _location in rows):
        return False, ""
    locations = {location for direction, location in rows if direction == Direction.CONSUMPTION and location}
    if len(locations) == 1:
        return True, f"{reverse('customer:application_start')}?building={locations.pop()}"
    return bool(locations), reverse("customer:buildings")
