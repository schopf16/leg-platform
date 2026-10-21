"""The list "Processes" (owner design ``verwaltung/vorgaenge``; D-103, D-107): the running applications and deregistrations
of all customers in one list. Pure query code, no pages.

* Listed are the applications that were submitted and are not completed, the drafts and paused drafts of the customers
  (since D-107; the owner wants to see what customers have started; they are marked "Draft / not submitted", the staff
  cannot act on them and they are in no work count of the overview) and the deregistrations that are not yet effective.
* The search finds the name, the customer number, the address and the EGID, never a note or an IBAN. Every word must
  match (AND). The filters are the kind (application / deregistration) and the state.
* Rows are built in Python and sorted there (empty values always last). Both queries are bounded by ``MAX_ROWS``.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Prefetch, Q

from leg_platform.community import applications, deregistrations, person_list
from leg_platform.community.models import (
    EGID_MAX,
    Application,
    ApplicationBuilding,
    Deregistration,
    DeregistrationItem,
)

KIND_ALL = "all"
KIND_APPLICATION = "application"
KIND_DEREGISTRATION = "deregistration"
KINDS = (KIND_ALL, KIND_APPLICATION, KIND_DEREGISTRATION)

STATE_ALL = "all"
# The state of a ROW: the application states are the model's values; the deregistration states carry a prefix (the
# model's values are ``submitted`` and ``confirmed``, which would clash with the application state ``submitted``).
STATE_DEREGISTRATION_SUBMITTED = "deregistration_submitted"
STATE_DEREGISTRATION_CONFIRMED = "deregistration_confirmed"
ROW_STATES = (
    *Application.DRAFT_STATES, *Application.IN_FLIGHT_STATES, STATE_DEREGISTRATION_SUBMITTED,
    STATE_DEREGISTRATION_CONFIRMED,
)
# The state FILTER: the two states at the grid operator are one choice (the overview counts them together), and so are
# the two draft states (a paused draft is a draft that nobody touched for a while).
STATE_AT_OPERATOR = "at_operator"
STATE_DRAFT = Application.State.DRAFT.value
APPLICATION_FILTER = {
    STATE_DRAFT: Application.DRAFT_STATES,
    Application.State.SUBMITTED.value: (Application.State.SUBMITTED,),
    Application.State.IN_REVIEW.value: (Application.State.IN_REVIEW,),
    STATE_AT_OPERATOR: (Application.State.OPERATOR_STARTED, Application.State.OPERATOR_CONFIRMED),
}
DEREGISTRATION_FILTER = {
    STATE_DEREGISTRATION_SUBMITTED: (Deregistration.State.SUBMITTED,),
    STATE_DEREGISTRATION_CONFIRMED: (Deregistration.State.CONFIRMED,),
}
STATES = (STATE_ALL, *APPLICATION_FILTER, *DEREGISTRATION_FILTER)

SORT_KEYS = ("person", "kind", "building", "points", "state", "progress", "date", "changed")
DEFAULT_SORT = "changed"
MAX_ROWS = 2000

STATE_RANK = {str(getattr(state, "value", state)): rank for rank, state in enumerate(ROW_STATES)}
DEREGISTRATION_STEPS = 3
DEREGISTRATION_PROGRESS = {Deregistration.State.SUBMITTED: 1, Deregistration.State.CONFIRMED: 2}


@dataclass(frozen=True)
class ProcessRow:
    """One line of the list. ``state`` is one of ``ROW_STATES``; ``date`` is the start date of an
    application (None while open; ``start_choice`` says "as soon as possible") or the effective date of a deregistration."""

    kind: str
    pk: int
    person_id: int
    person_name: str
    person_contact: str
    person_number: str
    address: str
    more_buildings: int
    egid: int | None
    points: int
    state: str
    progress_done: int
    progress_total: int
    date: object
    start_choice: str
    changed: datetime

    @property
    def sort_name(self):
        return (self.person_name or "").lower()


def _search_person(word, prefix):
    condition = (
        Q(**{f"{prefix}first_name__icontains": word}) | Q(**{f"{prefix}last_name__icontains": word})
        | Q(**{f"{prefix}organisation_name__icontains": word})
    )
    if word.isascii() and word.isdigit():
        condition |= Q(**{f"{prefix}customer_number__number__contains": word})
    return condition


def _egid_of(word):
    return int(word) if word.isascii() and word.isdigit() and len(word) <= len(str(EGID_MAX)) else None


def _application_search(queryset, text):
    for word in (text or "").split():
        condition = _search_person(word, "person__") | (
            Q(buildings__street__icontains=word) | Q(buildings__house_number__icontains=word)
            | Q(buildings__town__icontains=word) | Q(buildings__postal_code__icontains=word)
            | Q(buildings__location__street__icontains=word) | Q(buildings__location__house_number__icontains=word)
            | Q(buildings__location__town__icontains=word) | Q(buildings__location__postal_code__icontains=word)
        )
        egid = _egid_of(word)
        if egid is not None:
            condition |= Q(buildings__egid=egid) | Q(buildings__location__egid=egid)
        queryset = queryset.filter(condition)
    return queryset.distinct()


def _deregistration_search(queryset, text):
    for word in (text or "").split():
        place = "items__metering_point__location__"
        condition = _search_person(word, "person__") | (
            Q(**{f"{place}street__icontains": word}) | Q(**{f"{place}house_number__icontains": word})
            | Q(**{f"{place}town__icontains": word}) | Q(**{f"{place}postal_code__icontains": word})
        )
        egid = _egid_of(word)
        if egid is not None:
            condition |= Q(**{f"{place}egid": egid})
        queryset = queryset.filter(condition)
    return queryset.distinct()


def _address_text(address):
    street, house_number, postal_code, town = address
    return f"{street} {house_number}, {postal_code} {town}".strip(" ,")


def _application_row(application):
    buildings = list(application.buildings.all())
    first = buildings[0] if buildings else None
    if application.state in Application.DRAFT_STATES:
        # A draft has no way after the submission yet: its progress is what the customer completed in the form.
        current = applications.progress(application)
        done, total = current.done, current.total
    else:
        done = sum(1 for _key, step in applications.post_submit_progress(application) if step == applications.DONE)
        total = len(applications.STEPS)
    name, contact = person_list.display_names(application.person)
    egid = None
    if first is not None:
        egid = first.egid or (first.location.egid if first.location_id else None)
    start = application.desired_start_date if application.start_choice == Application.StartChoice.DATE else None
    return ProcessRow(
        kind=KIND_APPLICATION, pk=application.pk, person_id=application.person_id, person_name=name,
        person_contact=contact, person_number=application.person.customer_number_display,
        address=_address_text(first.current_address()) if first is not None else "",
        more_buildings=max(len(buildings) - 1, 0), egid=egid,
        points=sum(len(building.metering_points.all()) for building in buildings),
        state=application.state, progress_done=done, progress_total=total,
        date=start, start_choice=application.start_choice, changed=application.updated_at,
    )


def _deregistration_row(deregistration):
    items = list(deregistration.items.all())
    locations = []
    for item in items:
        location = item.metering_point.location
        if location is not None and location not in locations:
            locations.append(location)
    first = locations[0] if locations else None
    name, contact = person_list.display_names(deregistration.person)
    state = (
        STATE_DEREGISTRATION_CONFIRMED if deregistration.state == Deregistration.State.CONFIRMED
        else STATE_DEREGISTRATION_SUBMITTED
    )
    return ProcessRow(
        kind=KIND_DEREGISTRATION, pk=deregistration.pk, person_id=deregistration.person_id, person_name=name,
        person_contact=contact, person_number=deregistration.person.customer_number_display,
        address=_address_text(first.current_address()) if first is not None else "",
        more_buildings=max(len(locations) - 1, 0), egid=first.egid if first is not None else None,
        points=len(items), state=state, progress_done=DEREGISTRATION_PROGRESS[deregistration.state],
        progress_total=DEREGISTRATION_STEPS, date=deregistration.confirmed_date, start_choice="",
        changed=deregistration.confirmed_at or deregistration.submitted_at,
    )


def application_queryset(text="", state=STATE_ALL):
    """The applications that are not completed (drafts included, D-107), narrowed by the state filter and the search."""
    states = tuple(Application.OPEN_STATES) if state == STATE_ALL else APPLICATION_FILTER[state]
    queryset = (
        Application.objects.filter(state__in=states)
        .select_related("person")
        .prefetch_related(
            Prefetch(
                "buildings",
                queryset=ApplicationBuilding.objects.select_related("location").prefetch_related("metering_points"),
            )
        )
    )
    return _application_search(queryset, text)


def deregistration_queryset(text="", state=STATE_ALL):
    """The running deregistrations, narrowed by the state filter and the search."""
    states = deregistrations.OPEN_STATES if state == STATE_ALL else DEREGISTRATION_FILTER[state]
    queryset = (
        Deregistration.objects.filter(state__in=states)
        .select_related("person")
        .prefetch_related(
            Prefetch("items", queryset=DeregistrationItem.objects.select_related("metering_point__location"))
        )
    )
    return _deregistration_search(queryset, text)


def rows(*, text="", kind=KIND_ALL, state=STATE_ALL, sort=DEFAULT_SORT, descending=False):
    """The rows of the list as the address asks for them (unknown values are the defaults)."""
    kind = kind if kind in KINDS else KIND_ALL
    state = state if state in STATES else STATE_ALL
    found = []
    wants_applications = kind in (KIND_ALL, KIND_APPLICATION) and state not in DEREGISTRATION_FILTER
    wants_deregistrations = kind in (KIND_ALL, KIND_DEREGISTRATION) and (
        state == STATE_ALL or state in DEREGISTRATION_FILTER
    )
    if wants_applications:
        found += [_application_row(row) for row in application_queryset(text, state).order_by("pk")[:MAX_ROWS]]
    if wants_deregistrations:
        found += [_deregistration_row(row) for row in deregistration_queryset(text, state).order_by("pk")[:MAX_ROWS]]
    return sort_rows(found, sort, descending)


def _key(row, sort):
    if sort == "person":
        return (row.sort_name, row.person_number)
    if sort == "kind":
        return row.kind
    if sort == "building":
        return row.address.lower()
    if sort == "points":
        return row.points
    if sort == "state":
        return STATE_RANK[row.state]
    if sort == "progress":
        return row.progress_done / row.progress_total
    if sort == "date":
        return row.date
    return row.changed


def sort_rows(found, sort, descending=False):
    """Sort by a column; an empty value is always last (also descending), the name and the id break ties."""
    sort = sort if sort in SORT_KEYS else DEFAULT_SORT
    ordered = sorted(found, key=lambda row: (row.sort_name, row.person_number, row.kind, row.pk))
    present = [row for row in ordered if _key(row, sort) is not None]
    missing = [row for row in ordered if _key(row, sort) is None]
    present.sort(key=lambda row: _key(row, sort), reverse=descending)
    return present + missing
