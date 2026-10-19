"""Data for the staff overview and the processing queue of the applications (D-103). Services only, no pages.

Staff see every customer's data in the management area (D-086), so a row may hold the person's label (name and
customer number, never the email address, like the person search). Drafts are never listed: the staff does not work on
drafts (KONZEPT 5.6) and the counts do not include them. Lists are bounded.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count

from leg_platform.community import application_notices, contracts, person_search
from leg_platform.community.application_common import staff_actor
from leg_platform.community.models import Application, Deregistration

MAX_ROWS = 1000
State = Application.State


@dataclass(frozen=True)
class PendingApplication:
    application_id: int
    state: str
    submitted_at: datetime | None
    person_id: int
    person_label: str
    buildings: int
    metering_points: int


@dataclass(frozen=True)
class WaitingDeregistration:
    deregistration_id: int
    person_id: int
    person_label: str
    metering_points: int
    desired_date: object
    earliest_date: object


def queue_counts():
    """The numbers of the overview: applications submitted and in review, at the grid operator, deregistrations waiting
    for their date, ``contract_missing`` (1 while no contract version is current, 0 otherwise) and open ``problems``
    (notices)."""
    by_state = dict(
        Application.objects.exclude(state__in=Application.DRAFT_STATES).values_list("state").annotate(n=Count("pk"))
    )
    return {
        "applications_submitted": by_state.get(State.SUBMITTED, 0),
        "applications_in_review": by_state.get(State.IN_REVIEW, 0),
        "applications_at_operator": by_state.get(State.OPERATOR_STARTED, 0) + by_state.get(State.OPERATOR_CONFIRMED, 0),
        "deregistrations_waiting": Deregistration.objects.filter(state=Deregistration.State.SUBMITTED).count(),
        "contract_missing": 0 if contracts.current_version() is not None else 1,
        "problems": application_notices.open_notices().count(),
    }


def pending_applications(actor, states=(State.SUBMITTED, State.IN_REVIEW)):
    """The applications of the given states (never a draft), oldest submission first, for an active staff account."""
    staff_actor(actor)
    states = [state for state in states if state not in Application.DRAFT_STATES]
    rows = (
        Application.objects.filter(state__in=states)
        .select_related("person")
        .annotate(building_count=Count("buildings", distinct=True), point_count=Count("buildings__metering_points", distinct=True))
        .order_by("submitted_at", "pk")[:MAX_ROWS]
    )
    return [
        PendingApplication(
            row.pk, row.state, row.submitted_at, row.person_id, person_search.label(row.person),
            row.building_count, row.point_count,
        )
        for row in rows
    ]


def applications_at_operator(actor):
    """The applications whose registration at the grid operator started and that are not completed."""
    return pending_applications(actor, (State.OPERATOR_STARTED, State.OPERATOR_CONFIRMED))


def waiting_deregistrations(actor):
    """The deregistrations whose date the staff has not confirmed yet, oldest first."""
    staff_actor(actor)
    rows = (
        Deregistration.objects.filter(state=Deregistration.State.SUBMITTED)
        .select_related("person")
        .annotate(point_count=Count("items"))
        .order_by("submitted_at", "pk")[:MAX_ROWS]
    )
    return [
        WaitingDeregistration(
            row.pk, row.person_id, person_search.label(row.person), row.point_count, row.desired_date, row.earliest_date
        )
        for row in rows
    ]


def problems(actor):
    """The open notices (refused building or number, registration to withdraw at the grid operator), oldest first."""
    staff_actor(actor)
    return list(application_notices.open_notices()[:MAX_ROWS])
