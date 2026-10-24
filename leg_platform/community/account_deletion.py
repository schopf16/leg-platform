"""What the deletion of an account does to its running work (owner decision 2026-10-24, amends D-105).

The customer can always delete the account. Deleting it does implicitly what the customer would otherwise do by hand:

* the running applications are withdrawn: a draft or a submitted application is deleted with the account's other work;
* an application that the LEG already registered at the grid operator gets a notice for the staff (``ACCOUNT_DELETED``,
  with the EGID and the numbers) before it is deleted: the LEG withdraws the registration there, the same as for a
  deregistration. The notice is the only record of it, as for ``OPERATOR_CONFIRMATION_LAPSED``;
* the running metering points are deregistered (``deregistrations.request_deregistration``): the staff sees them in the
  processes like any deregistration of a customer, with the earliest date.

The functions are called inside the transaction that holds the lock of the account (``accounts.profile_views``).
"""

from leg_platform.community import application_notices, applications, deregistrations
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import Application, ApplicationNotice, ApplicationMeteringPoint

# Registered at the grid operator: the staff is told (a notice) before the application is deleted.
REGISTERED_STATES = (Application.State.OPERATOR_STARTED, Application.State.OPERATOR_CONFIRMED)


class DeletionBlocked(Exception):
    """A deregistration of the account could not be started (for example too many open ones)."""


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
        try:
            deregistrations.request_deregistration(person, metering_point_ids=[point.pk for point in points])
        except ApplicationError as error:
            raise DeletionBlocked from error
