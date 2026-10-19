"""What the page "Buildings" shows (GEBAEUDE.md; D-103): the account's own buildings, metering points and the progress of a
running registration or deregistration.

Pure reading, through the account only: a building is listed because the account has a person assignment on one of its
metering points or an application of its own names it; **only the account's own metering points** are listed, never
another customer's at the same (shared) building. The name and the description of a building are the account's own words
(``CustomerBuilding``). Nothing here changes data and nothing is logged.

A status is a dot plus a word (``tone`` is the colour of the dot; the word is always shown): participating (ok),
registration running (warn), deregistration running (bad), draft or ended (muted).
"""

from dataclasses import dataclass, field
from datetime import date

from django.utils import timezone
from django.utils.translation import gettext as _

from leg_platform.community import applications, buildings, grid_operator, labels
from leg_platform.community.models import (
    Application,
    CustomerBuilding,
    Deregistration,
    Direction,
    GridOperator,
    PersonAssignment,
)
from leg_platform.community.status import MeteringPointStatus
from leg_platform.customer import steps as steps_module
from leg_platform.customer.steps import STEP_NAMES

OK, WARN, BAD, MUTED = "ok", "warn", "bad", "muted"
STEP_DONE, STEP_NEXT, STEP_OPEN = "done", "next", "open"

# The state of a metering point row (what the person is told, one word each).
ACTIVE = "active"
STARTS = "starts"  # confirmed, the start date is still to come
REGISTERING = "registering"  # stored, not confirmed yet
TERMINATING = "terminating"  # in a deregistration, or terminated for a later day
ENDED = "ended"
DRAFT = "draft"  # a point of an application that is still a draft
IN_REVIEW = "in_review"  # submitted, the LEG has not registered it at the grid operator yet
AT_OPERATOR = "at_operator"  # the registration at the grid operator started


@dataclass
class Step:
    key: str
    state: str  # done / next / open
    label: str = ""
    word: str = ""  # the state in words ("Finished", "Next up", "Open", or a special sentence)


@dataclass
class Block:
    """The progress of one running registration or deregistration, shown under its last metering point."""

    kind: str  # "registration" or "deregistration"
    steps: list
    done: int
    total: int
    application: object = None
    deregistration: object = None
    resume: bool = False  # a draft: "Continue the registration"
    withdrawable: bool = False  # submitted and not yet at the grid operator
    paused: bool = False
    paused_date: date | None = None  # the day the scheduler deletes a paused draft
    reason: str = ""  # why the LEG returned the draft for correction
    returned: bool = False  # the LEG returned the draft (``reason`` may be empty)
    note: str = ""  # one line under the steps (a deregistration whose date the LEG has not confirmed yet)


@dataclass
class PointRow:
    number: str
    name: str
    direction: str
    since: date | None
    state: str
    tone: str
    label: str
    point_id: int | None = None  # a stored metering point of the account: its name can be changed
    group: tuple = ()  # ("point", id) / ("application", id): rows of one process are adjacent
    block: Block | None = None

    @property
    def direction_label(self):
        return Direction(self.direction).label

    @property
    def since_kind(self):
        """Which date line the card shows (W1-16): "since" for a point that runs or ran, "registered" for one that is
        still in registration (the day of the registration), "" where the status already names the start."""
        if self.state in (ACTIVE, TERMINATING, ENDED):
            return "since"
        if self.state == STARTS:
            return ""
        return "registered"


@dataclass
class Card:
    key: tuple
    location: object = None
    building: object = None  # the ApplicationBuilding that names it, if any
    title: str = ""
    address: str = ""
    egid: int | None = None
    description: str = ""
    has_name: bool = False
    state: str = "ended"
    tone: str = MUTED
    label: str = ""
    rows: list = field(default_factory=list)
    application_states: list = field(default_factory=list)
    application_labels: list = field(default_factory=list)
    extra_blocks: list = field(default_factory=list)  # the progress of a draft that has no metering point row yet
    can_add_point: bool = False
    removal: str | None = None  # "link": a point can be deregistered; "withdraw": a submitted registration can be withdrawn
    # (``withdraw_pk``); "blocked": not possible yet
    withdraw_pk: int | None = None
    deregistrable: bool = False  # an active point that is in no deregistration

    @property
    def withdraw_in_footer(self):
        """The card footer offers "Withdraw application": the buttons of the progress block would repeat it (W1-15)."""
        return self.removal == "withdraw"


@dataclass
class Overview:
    cards: list
    operator_name: str
    can_deregister: bool


def _lines(address, name):
    """``(title, line under the title)``: the name as title and the whole address below it, else the street as title
    and the postal code and town below it."""
    street, number, postal_code, town = address
    street_line = f"{street} {number}".strip()
    place = f"{postal_code} {town}".strip()
    if name:
        return name, ", ".join(part for part in (street_line, place) if part)
    return street_line, place


def _word(state):
    return {STEP_DONE: _("Finished"), STEP_NEXT: _("Next up")}.get(state, _("Open"))


def _registration_block(application, operator):
    if application.state in Application.DRAFT_STATES:
        current = applications.progress(application)
        steps = []
        for step in current.steps:
            if not step.applicable:
                continue
            state = STEP_DONE if step.state != applications.OPEN else (
                STEP_NEXT if step.key == current.next_step else STEP_OPEN
            )
            steps.append(Step(step.key, state, str(STEP_NAMES[step.key]), steps_module.state_word(state)))
        paused = application.state == Application.State.PAUSED
        return Block(
            "registration", steps, current.done, current.total, application=application, resume=True,
            paused=paused, paused_date=applications.rollback_date(application) if paused else None,
            reason=applications.returned_reason(application), returned=application.returned_at is not None,
        )
    steps = [
        Step(key, state, label, word) for key, state, label, word in steps_module.post_submit_steps(application, operator)
    ]
    return Block(
        "registration", steps, sum(step.state == STEP_DONE for step in steps), len(steps), application=application,
        withdrawable=application.state in Application.WITHDRAWABLE_STATES,
    )


def date_note(deregistration):
    """The words about the date of a deregistration that the LEG has not confirmed yet (N-10): the earliest month end of
    the notice period, or the customer's wish; the LEG confirms it."""
    if deregistration.wants_earliest:
        return _("Earliest on %(date)s. The LEG confirms the date.") % {
            "date": deregistration.earliest_date.strftime("%d.%m.%Y")
        }
    return _("Desired date %(date)s. The LEG confirms the date.") % {"date": deregistration.desired_date.strftime("%d.%m.%Y")}


def _deregistration_block(deregistration):
    confirmed = deregistration.state != Deregistration.State.SUBMITTED
    states = (STEP_DONE, STEP_DONE if confirmed else STEP_NEXT, STEP_NEXT if confirmed else STEP_OPEN)
    names = (_("Deregistration submitted"), _("Date confirmed by the LEG"), _("Deregistered"))
    steps = [
        Step(key, state, name, _word(state))
        for key, state, name in zip(("submitted", "confirmed", "effective"), states, names)
    ]
    return Block(
        "deregistration", steps, sum(step.state == STEP_DONE for step in steps), len(steps),
        deregistration=deregistration,
        note="" if confirmed else date_note(deregistration),
    )


def _stored_row(assignment, point, deregistration, today):
    status = point.status(today)
    since = point.start_date or assignment.valid_from
    if deregistration is not None:
        state, tone = TERMINATING, BAD
        label = (
            _("Terminated on %(date)s") % {"date": deregistration.confirmed_date.strftime("%d.%m.%Y")}
            if deregistration.confirmed_date else _("Deregistration running")
        )
    elif status == MeteringPointStatus.TERMINATED:
        state, tone, label = ENDED, MUTED, _("Ended")
    elif point.terminated_on is not None:
        state, tone = TERMINATING, BAD
        label = _("Terminated on %(date)s") % {"date": point.terminated_on.strftime("%d.%m.%Y")}
    elif status == MeteringPointStatus.ACTIVE:
        state, tone, label = ACTIVE, OK, _("Active")
    elif status == MeteringPointStatus.CONFIRMED and point.start_date:
        state, tone = STARTS, WARN
        label = _("Starts on %(date)s") % {"date": point.start_date.strftime("%d.%m.%Y")}
    else:
        state, tone, label = REGISTERING, WARN, _("Registration running")
    return PointRow(
        number=point.number or "", name=point.name, direction=point.direction, since=since, state=state, tone=tone,
        label=label, point_id=point.pk, group=("point", point.pk),
    )


def _application_state(application):
    if application.state in Application.DRAFT_STATES:
        if application.state == Application.State.PAUSED:
            return DRAFT, MUTED, labels.application_state_label(Application.State.PAUSED)
        return DRAFT, MUTED, _("Draft")
    if application.state in (Application.State.SUBMITTED, Application.State.IN_REVIEW):
        return IN_REVIEW, WARN, _("In review by the LEG")
    return AT_OPERATOR, WARN, _("Registration running")


def build(person, today=None):
    """The ``Overview`` of the account."""
    today = today or timezone.localdate()
    operator = GridOperator.objects.filter(pk=1).values_list("name", flat=True).first() or ""
    # The word in sentences ("Registration at %(operator)s"): the name, or the neutral word while none is entered.
    operator_text = operator or str(grid_operator.FALLBACK)
    own_words = {row.location_id: row for row in CustomerBuilding.objects.filter(person=person)}
    assignments = list(
        PersonAssignment.objects.not_ended(today).filter(person=person).select_related("metering_point__location")
    )
    deregistrations = Deregistration.objects.filter(
        person=person, state__in=(Deregistration.State.SUBMITTED, Deregistration.State.CONFIRMED)
    ).prefetch_related("items")
    deregistration_of = {item.metering_point_id: row for row in deregistrations for item in row.items.all()}
    running = Application.objects.filter(person=person, state__in=Application.OPEN_STATES).prefetch_related(
        "buildings__metering_points", "buildings__location"
    )

    cards = {}

    def card_for(key, location):
        if key not in cards:
            cards[key] = Card(key=key, location=location)
        return cards[key]

    for assignment in assignments:
        point = assignment.metering_point
        if point.is_archived:
            continue
        location = point.location
        card = card_for(("location", location.pk) if location is not None else ("none", 0), location)
        card.rows.append(_stored_row(assignment, point, deregistration_of.get(point.pk), today))
    for application in running:
        block = _registration_block(application, operator_text)
        state, tone, label = _application_state(application)
        started = timezone.localtime(application.submitted_at or application.created_at).date()
        for building in application.buildings.all():
            key = ("location", building.location_id) if building.location_id else ("application", building.pk)
            card = card_for(key, building.location)
            card.building = card.building or building
            card.application_states.append(state)
            card.application_labels.append(label)
            rows = [
                PointRow(
                    number=point.number, name=point.name, direction=point.direction, since=started, state=state,
                    tone=tone, label=label, group=("application", application.pk),
                )
                for point in building.metering_points.all()
            ]
            rows.sort(key=lambda row: row.direction != Direction.CONSUMPTION)
            if rows:
                rows[-1].block = block
            elif block not in card.extra_blocks:
                # A draft without metering points yet still shows its progress, its notes and "Continue" (N-07).
                card.extra_blocks.append(block)
            card.rows.extend(rows)

    seen_deregistrations = {}
    for card in cards.values():
        for row in card.rows:
            row_dereg = deregistration_of.get(row.point_id) if row.point_id else None
            if row_dereg is not None:
                seen_deregistrations[(card.key, row_dereg.pk)] = (row, row_dereg)
    for row, row_dereg in seen_deregistrations.values():
        row.block = _deregistration_block(row_dereg)

    result = [_finish(card, person, own_words, today) for card in cards.values()]
    result.sort(key=lambda card: (card.location is None and card.building is None, card.title.casefold()))
    return Overview(result, operator, any(card.deregistrable for card in result))


def _finish(card, person, own_words, today):
    """Title, facts, status and buttons of a card."""
    words = own_words.get(card.location.pk) if card.location is not None else None
    name = (words.name if words else "") or (card.building.name if card.building else "")
    card.description = (words.description if words else "") or (card.building.description if card.building else "")
    if card.location is not None:
        address, card.egid = card.location.current_address(), card.location.egid
    elif card.building is not None:
        address, card.egid = card.building.current_address(), card.building.egid
    else:
        address = None
    if address is None:
        card.title, card.address = _("Metering points without a building"), ""
    else:
        card.title, card.address = _lines(address, name)
    card.has_name = bool(name)

    states = {row.state for row in card.rows}
    stored = [row for row in card.rows if row.group and row.group[0] == "point"]
    stored_live = [row for row in stored if row.state != ENDED]
    if stored_live and all(row.state == TERMINATING for row in stored_live):
        card.state, card.tone, card.label = "deregistering", BAD, _("Deregistration running")
    elif ACTIVE in states:
        card.state, card.tone, card.label = "participating", OK, _("Participating")
    elif stored_live or states & {IN_REVIEW, AT_OPERATOR}:
        card.state, card.tone, card.label = "registering", WARN, _("Registration running")
    elif DRAFT in states or DRAFT in card.application_states:
        draft_labels = [label for state, label in zip(card.application_states, card.application_labels) if state == DRAFT]
        card.state, card.tone, card.label = "draft", MUTED, (draft_labels[0] if draft_labels else _("Draft"))
    elif card.application_states:
        card.state, card.tone, card.label = "registering", WARN, _("Registration running")
    else:
        card.state, card.tone, card.label = "ended", MUTED, _("Ended")
    card.deregistrable = ACTIVE in states

    if card.location is not None and not card.location.is_archived:
        taken = buildings.occupied_directions(card.location, person, today)
        # A point is added where the account takes part already and no application of its own names the building.
        card.can_add_point = (
            bool(stored_live) and not card.application_states
            and not {Direction.CONSUMPTION, Direction.FEED_IN} <= taken
        )
    application_rows = [row for row in card.rows if row.state in (DRAFT, IN_REVIEW, AT_OPERATOR)]
    # "Remove metering point" is the deregistration of an active point (notice period). A submitted registration that
    # the LEG has not yet passed to the grid operator is withdrawn (no notice period, no proof: ABMELDUNG.md section 7).
    # After the start of the registration at the grid operator nothing can be withdrawn until the point is active.
    withdrawable = next((row.block.application for row in card.rows if row.block and row.block.withdrawable), None)
    if ACTIVE in states:
        card.removal = "link"
    elif withdrawable is not None:
        card.removal, card.withdraw_pk = "withdraw", withdrawable.pk
    elif any(row.state == AT_OPERATOR for row in application_rows):
        card.removal = "blocked"
    return card
