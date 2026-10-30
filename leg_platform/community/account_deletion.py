"""What the deletion of an account does to its running work (owner decision 2026-10-24, amends D-105).

The customer can always delete the account. Deleting it does implicitly what the customer would otherwise do by hand:

* the running applications are withdrawn: a draft or a submitted application is deleted with the account's other work;
* an application that the LEG already registered at the grid operator gets a notice for the staff (``ACCOUNT_DELETED``,
  with the EGID and the numbers) before it is deleted: the LEG withdraws the registration there, the same as for a
  deregistration. The notice is the only record of it, as for ``OPERATOR_CONFIRMATION_LAPSED``;
* the running metering points and those that are assigned but not active yet (registered, notified, confirmed with a
  later start) are deregistered in one deregistration (``deregistrations.request_deregistration``): the staff sees it in
  the processes like any deregistration of a customer, with the earliest date, and a note on the points that are not
  active (owner decision 2026-10-29: the customer can always cancel, the deregistration is never refused).

The functions are called inside the transaction that holds the lock of the account (``accounts.profile_views``).
"""

from leg_platform.community import application_notices, applications, deregistrations
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import (
    Application,
    ApplicationMeteringPoint,
    ApplicationNotice,
    DeregistrationItem,
)

# Registered at the grid operator: the staff is told (a notice) before the application is deleted.
REGISTERED_STATES = (Application.State.OPERATOR_STARTED, Application.State.OPERATOR_CONFIRMED)


class DeletionBlocked(Exception):
    """A deregistration of the account could not be started (for example too many open ones)."""


def has_participation(person):
    """Whether the account takes part in the LEG now: a running metering point (also one in a deregistration already) or
    a registration in progress. The profile then offers the fork "only deregister" / "delete the account"; everyone
    else goes straight to the deletion page."""
    return bool(
        deregistrations.eligible_points(person)
        or Application.objects.filter(person=person, state__in=Application.IN_FLIGHT_STATES).exists()
        or DeregistrationItem.objects.filter(
            deregistration__person=person, deregistration__state__in=deregistrations.OPEN_STATES
        ).exists()
    )


def _notice_for(person, application):
    """The notice for the staff about a registration at the grid operator, with the EGID and the numbers."""
    building = application.buildings.first()
    egid = building.egid if building is not None else None
    numbers = ApplicationMeteringPoint.objects.filter(building__application=application).values_list("number", flat=True)
    application_notices.record(person, ApplicationNotice.Kind.ACCOUNT_DELETED, egid=egid, numbers=list(numbers))


def prepare(person, actor=None):
    """Withdraw the running applications, tell the staff about the registered ones, deregister the running points."""
    for application in Application.objects.select_for_update().filter(person=person, state__in=Application.OPEN_STATES):
        if application.state in REGISTERED_STATES:
            _notice_for(person, application)
        applications.delete_application(application, "account_deleted", actor)
    points = deregistrations.eligible_points(person)
    if points:
        # Running and not active yet alike: one deregistration, the staff sees a note on the points that are not active.
        try:
            deregistrations.request_deregistration(person, metering_point_ids=[point.pk for point in points])
        except ApplicationError as error:
            raise DeletionBlocked from error
