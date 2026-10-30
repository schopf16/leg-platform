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
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from leg_platform.addresses.models import TransformerStation
from leg_platform.community import applications, buildings, grid_operator, labels, own_iban
from leg_platform.community.models import (
    Application,
    CustomerBuilding,
    Deregistration,
    Direction,
    GridOperator,
    LegAssignment,
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
SEARCH_MIN_CARDS = 8  # from this many cards on, the Buildings page offers a search and expand / collapse all


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
    changeable: bool = False  # submitted and not yet taken into review: the customer may change it (D-107)
    in_review: bool = False  # the LEG is checking it: no change any more (the card says why)
    paused: bool = False
    paused_date: date | None = None  # the day the scheduler deletes a paused draft
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
    # The page that changes the number of this point of a SUBMITTED application ("" = no pencil; set only while the
    # application can be edited in place: state submitted, the LEG has not started the review).
    edit_url: str = ""
    figures: dict = field(default_factory=dict)  # the optional figures of a feed-in point: field name -> text ("" = not given)
    leg_name: str = ""  # the name of the community the LEG administration assigned the point to; "" = not assigned yet
    # The point of a submitted application the customer may still change (``card_edit``): the application and the row id.
    application: object = None
    application_point_id: int | None = None
    form: object = None  # set by ``card_edit`` while the card is in edit mode

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
    name: str = ""  # the name the person gave the building ("" = none)
    leg_name: str = ""  # the community of the building's metering points ("" = not assigned yet)
    feed_in: object = None  # the feed-in PointRow, if the building has one
    transformer: str = ""  # name of the transformer circuit of the building from the address directory ("" = unknown)
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
    change_pk: int | None = None  # a submitted application the LEG has not taken into review yet: "Change application"
    review_locked: bool = False  # the LEG is checking it: the card says that it can no longer be changed here
    deregistrable: bool = False  # an active point that is in no deregistration
    # Edit pages of a SUBMITTED application (D: edit in place), keys ``address`` / ``start`` / ``bank``; a key is present
    # only while the application can be edited (state submitted) and the detail applies (no address for a fixed
    # building, no bank connection without a feed-in). Empty dict = no pencil. The page checks again on the server.
    edit: dict = field(default_factory=dict)
    start_text: str = ""  # the chosen start of a submitted / in-review application ("As soon as possible" / "Desired date: ...")
    iban_masked: str = ""  # the account's IBAN, masked, where the application has a feed-in
    edit_application: object = None  # the submitted application whose address / start the card may change
    can_edit: bool = False  # set by ``card_edit.mark``: something on the card can be changed in place

    @property
    def search_text(self):
        """What the search field of the page matches: only values the card shows anyway (title, address, EGID, own points)."""
        parts = [self.title, self.address, str(self.egid or "")]
        parts.extend(row.number for row in self.rows if row.number)
        return " ".join(part for part in parts if part)
    editing: object = None  # the ``card_edit.EditState`` while the card is shown in edit mode

    @property
    def has_status_blocks(self):
        """Whether a progress block (registration or deregistration) is shown in the status part."""
        return bool(self.extra_blocks) or any(row.block is not None for row in self.rows)

    @property
    def deregistration_query(self):
        """The query of the card's link to the deregistration: always this building (``?building=<id>``). A card without
        a usable building (none, or an archived one that the page would refuse) names its own points instead."""
        if self.location is not None and not self.location.is_archived:
            return f"?building={self.location.pk}"
        ids = [row.point_id for row in self.rows if row.point_id]
        return "?" + urlencode([("point", pk) for pk in ids]) if ids else ""

    @property
    def edit_id(self):
        """The id of the card in ``?edit=<id>``: the kind and the number of its key."""
        return f"{self.key[0][0]}{self.key[1]}"

    @property
    def withdraw_in_footer(self):
        """The card footer offers "Withdraw application": the buttons of the progress block would repeat it (W1-15)."""
        return self.removal == "withdraw"


@dataclass
class Overview:
    cards: list
    operator_name: str
    editing_id: str = ""  # the card shown in edit mode ("" = none); only one at a time
    open_id: str = ""  # the card just saved: shown open although the cards are collapsed

    @property
    def collapsible(self):
        """With two or more cards each one folds (``<details>``); a single card is always shown open."""
        return len(self.cards) > 1

    @property
    def searchable(self):
        """From ``SEARCH_MIN_CARDS`` cards on, the list gets a search field and expand / collapse all buttons."""
        return len(self.cards) >= SEARCH_MIN_CARDS


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
        )
    steps = [
        Step(key, state, label, word) for key, state, label, word in steps_module.post_submit_steps(application, operator)
    ]
    return Block(
        "registration", steps, sum(step.state == STEP_DONE for step in steps), len(steps), application=application,
        withdrawable=application.state in Application.WITHDRAWABLE_STATES,
        changeable=application.state == Application.State.SUBMITTED,
        in_review=application.state == Application.State.IN_REVIEW,
    )


def date_note(deregistration):
    """The words about the date of a deregistration that the LEG has not confirmed yet (N-10): the customer's wish. No
    computed date (owner decision); the LEG confirms the date."""
    if deregistration.wants_earliest or not deregistration.desired_date:
        return _("As early as possible. The LEG confirms the date.")
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
        note=_deregistration_text(deregistration),
    )


def _deregistration_text(deregistration):
    """The one status sentence of the card for a running deregistration. "As early as possible" shows no date (the LEG
    confirms it), like a registration shows no computed date."""
    if deregistration.state != Deregistration.State.SUBMITTED:
        if deregistration.confirmed_date:
            return _("Deregistration on %(date)s.") % {"date": deregistration.confirmed_date.strftime("%d.%m.%Y")}
        return _("Deregistration running")
    if deregistration.wants_earliest or not deregistration.desired_date:
        return _("Your deregistration is submitted. The LEG administration confirms the date.")
    return _("Desired date %(date)s. The LEG administration confirms it.") % {
        "date": deregistration.desired_date.strftime("%d.%m.%Y")
    }


def _figures(point):
    """The optional figures of a point as text per field name; "" where nothing is given."""
    values = {}
    for name in ("pv_power_kwp", "battery_kwh", "wallbox_kw"):
        value = getattr(point, name, None)
        values[name] = "" if value is None else format(value.normalize(), "f")
    return values


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
        label=label, point_id=point.pk, group=("point", point.pk), figures=_figures(point),
    )


def _application_state(application):
    if application.state in Application.DRAFT_STATES:
        if application.state == Application.State.PAUSED:
            return DRAFT, MUTED, labels.application_state_label(Application.State.PAUSED)
        return DRAFT, MUTED, _("Draft")
    if application.state in (Application.State.SUBMITTED, Application.State.IN_REVIEW):
        return IN_REVIEW, WARN, _("In review by the LEG")
    return AT_OPERATOR, WARN, _("Registration running")


def _masked_iban(person):
    """The account's IBAN with the middle hidden (the first and the last four characters show), "" if none."""
    value = own_iban.own_iban(person)
    if len(value) < 12:
        return ""
    return f"{value[:4]} •••• •••• {value[-4:]}"


def _start_text(application):
    if application.start_choice == Application.StartChoice.DATE and application.desired_start_date:
        return _("Desired date") + ": " + application.desired_start_date.strftime("%d.%m.%Y")
    if application.start_choice == Application.StartChoice.ASAP:
        return _("As soon as possible")
    return ""


def _edit_details(card, application, building, editable, iban_text):
    """The facts and edit URLs of a submitted / in-review registration for the card (see ``Card.edit``)."""
    if application.state not in (Application.State.SUBMITTED, Application.State.IN_REVIEW):
        return
    card.start_text = card.start_text or _start_text(application)
    has_feed_in = any(point.direction == Direction.FEED_IN for point in building.metering_points.all())
    if has_feed_in:
        card.iban_masked = card.iban_masked or iban_text()
    if not editable:
        return
    card.edit_application = application
    if not building.preselected:
        card.edit["address"] = reverse("customer:application_edit_address", args=[application.pk, building.pk])
    card.edit["start"] = reverse("customer:application_edit_start", args=[application.pk])
    if has_feed_in:
        card.edit["bank"] = reverse("customer:application_edit_bank", args=[application.pk])


def build(person, today=None):
    """The ``Overview`` of the account."""
    today = today or timezone.localdate()
    iban_cache = []

    def iban_text():
        if not iban_cache:
            iban_cache.append(_masked_iban(person))
        return iban_cache[0]

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
        if not application.buildings.all():
            # A draft whose building is not entered yet is still the account's registration: it gets a card of its own
            # (progress, notes, "Continue"), never the empty state "No building registered yet" (walkthrough 3).
            card = card_for(("draft", application.pk), None)
            card.application_states.append(state)
            card.application_labels.append(label)
            card.extra_blocks.append(block)
            continue
        for building in application.buildings.all():
            key = ("location", building.location_id) if building.location_id else ("application", building.pk)
            card = card_for(key, building.location)
            card.building = card.building or building
            card.application_states.append(state)
            card.application_labels.append(label)
            editable = applications.can_edit_submitted(application)
            rows = [
                PointRow(
                    number=point.number, name=point.name, direction=point.direction, since=started, state=state,
                    tone=tone, label=label, group=("application", application.pk), figures=_figures(point),
                    edit_url=reverse("customer:application_edit_point", args=[application.pk, point.pk]) if editable else "",
                    application=application if editable else None,
                    application_point_id=point.pk if editable else None,
                )
                for point in building.metering_points.all()
            ]
            rows.sort(key=lambda row: row.direction != Direction.CONSUMPTION)
            _edit_details(card, application, building, editable, iban_text)
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

    leg_of = {}
    for leg_assignment in LegAssignment.objects.not_ended(today).filter(
        metering_point__in=[row.point_id for card in cards.values() for row in card.rows if row.point_id]
    ).select_related("leg"):
        leg_of.setdefault(leg_assignment.metering_point_id, leg_assignment.leg.name)
    for card in cards.values():
        for row in card.rows:
            row.leg_name = leg_of.get(row.point_id, "") if row.point_id else ""

    result = [_finish(card, person, own_words, today) for card in cards.values()]
    transformers = dict(
        TransformerStation.objects.filter(egid__in=[card.egid for card in result if card.egid]).values_list("egid", "name")
    )
    for card in result:
        card.transformer = transformers.get(card.egid, "") if card.egid else ""
    # Not yet participating first, participating last; each group by name, otherwise address.
    result.sort(key=lambda card: (
        card.state == "participating",
        card.location is None and card.building is None,
        card.title.casefold(),
    ))
    return Overview(result, operator)


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
        if card.key[0] == "draft":
            card.title, card.address = _("Building not entered yet"), ""
        else:
            card.title, card.address = _("Metering points without a building"), ""
    else:
        card.title, card.address = _lines(address, name)
    card.has_name = bool(name)
    card.leg_name = next((row.leg_name for row in card.rows if row.leg_name), "")
    card.feed_in = next((row for row in card.rows if row.direction == Direction.FEED_IN), None)
    card.name = name

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
        # A draft is a building that is not completely recorded yet: the card says so (walkthrough 3); a paused one
        # keeps its own word.
        card.state, card.tone, card.label = "draft", MUTED, (
            draft_labels[0] if draft_labels and draft_labels[0] != _("Draft") else _("Draft, not completely recorded")
        )
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
    blocks = [row.block for row in card.rows if row.block]
    changeable = next((block.application for block in blocks if block.changeable), None)
    card.change_pk = changeable.pk if changeable is not None else None
    card.review_locked = changeable is None and any(block.in_review for block in blocks)
    if ACTIVE in states:
        card.removal = "link"
    elif withdrawable is not None:
        card.removal, card.withdraw_pk = "withdraw", withdrawable.pk
    elif any(row.state == AT_OPERATOR for row in application_rows):
        card.removal = "blocked"
    return card
