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

from leg_platform.community import applications
from leg_platform.community.models import Application, Direction, PersonAssignment

DONE, NOW, TODO = "done", "now", "todo"

STEP_LABELS = {
    applications.BUILDING: _("Record location"),
    applications.METERING_POINTS: _("Record metering points"),
    applications.BANK_CONNECTION: _("Bank or postal connection"),
    applications.CONTRACT: _("Agree to the contract"),
    applications.START: _("Choose the start date"),
}
FINAL_LABEL = _("Registered with the LEG")

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


@dataclass(frozen=True)
class Journey:
    steps: list
    done: int
    total: int
    sentence: object
    button: object
    url: str
    paused: bool = False


def current_application(person):
    """The application the card follows: the newest draft, else the newest one in flight; None if none is open."""
    rows = list(Application.objects.filter(person=person, state__in=Application.OPEN_STATES).order_by("-pk"))
    drafts = [row for row in rows if row.state in Application.DRAFT_STATES]
    return (drafts or rows or [None])[0]


def for_account(person):
    """The ``Journey`` of the account, or None (state B: no application is open)."""
    application = current_application(person)
    if application is None:
        return None
    if application.state in Application.DRAFT_STATES:
        current = applications.progress(application)
        applicable = [step for step in current.steps if step.applicable]
        steps = []
        for step in applicable:
            if step.state != applications.OPEN:
                steps.append(JourneyStep(STEP_LABELS[step.key], DONE))
            else:
                steps.append(JourneyStep(STEP_LABELS[step.key], NOW if step.key == current.next_step else TODO, not step.available))
        steps.append(JourneyStep(FINAL_LABEL, TODO))
        if current.next_step is None:
            sentence, button, name = READY
        else:
            sentence, button, name = NEXT[current.next_step]
        return Journey(
            steps, current.done, current.total + 1, sentence, button,
            reverse(name, args=[application.pk]), paused=application.state == Application.State.PAUSED,
        )
    # Submitted or later: everything the customer does is done; the LEG acts.
    has_feed_in = application.buildings.filter(metering_points__direction=Direction.FEED_IN).exists()
    keys = [
        key for key in applications.STEPS if key != applications.BANK_CONNECTION or has_feed_in
    ]
    steps = [JourneyStep(STEP_LABELS[key], DONE) for key in keys]
    steps.append(JourneyStep(FINAL_LABEL, NOW))
    sentence = {
        Application.State.OPERATOR_STARTED: AT_OPERATOR,
        Application.State.OPERATOR_CONFIRMED: CONFIRMED,
    }.get(application.state, IN_REVIEW)
    return Journey(steps, len(steps) - 1, len(steps), sentence, _("View progress"), reverse("customer:buildings"))


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
