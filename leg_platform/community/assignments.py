"""Rules for the assignments of a metering point to a person or to a LEG (D-077).

* A metering point has a chain of assignments of each kind. A new assignment must start after the
  start of the last one.
* The previous assignment ends the day before the new "valid from" (a "flying change": the metering
  point changes hands without a gap or an overlap). If the previous assignment had ended earlier,
  it is left alone.
* An open assignment is ended with "ends on"; it stays in the history.
* An assignment can be deleted only if it never took effect (it starts in the future) and was never
  ended. Deleting it undoes the change of the previous assignment.
* Every change is one transaction that locks the metering point, and is written to its history.

The rules are here and not in the views; a view only calls them (``messpunkte/detail/VORGABEN.md``,
"Person wechseln", "Zuordnung beenden und löschen").
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import security_log
from leg_platform.community import history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import LegAssignment, MeteringPoint, PersonAssignment

PERSON = "person"
LEG = "leg"

_KINDS = {
    PERSON: {
        "model": PersonAssignment,
        "subject": "person",
        "assigned": HistoryEvent.PERSON_ASSIGNED,
        "ended": HistoryEvent.PERSON_ASSIGNMENT_ENDED,
        "deleted": HistoryEvent.PERSON_ASSIGNMENT_DELETED,
    },
    LEG: {
        "model": LegAssignment,
        "subject": "leg",
        "assigned": HistoryEvent.LEG_ASSIGNED,
        "ended": HistoryEvent.LEG_ASSIGNMENT_ENDED,
        "deleted": HistoryEvent.LEG_ASSIGNMENT_DELETED,
    },
}


class AssignmentError(Exception):
    """A change that the rules refuse. ``code`` is one of: ``starts_not_after_previous``,
    ``end_before_start``, ``already_ended``, ``not_deletable``, ``not_a_person``,
    ``person_not_active``."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _kind(kind):
    try:
        return _KINDS[kind]
    except KeyError:
        raise ValueError(f"Unknown kind of assignment: {kind!r}") from None


def model_of(kind):
    return _kind(kind)["model"]


def in_force(kind, metering_point, on=None):
    """The assignment that is valid on the day, or None."""
    return model_of(kind).objects.in_force(on).filter(metering_point=metering_point).first()


def is_deletable(assignment, today=None):
    """Whether the assignment never took effect and was never ended: only then it may be deleted
    (an entry made by mistake)."""
    today = today or timezone.localdate()
    return assignment.valid_from > today and assignment.valid_to is None


def assign(kind, metering_point, subject, valid_from, actor=None, today=None):
    """Assign a person (an active account with a customer number) or a LEG from ``valid_from``.

    Returns the new assignment. Raises ``AssignmentError``. Assigning a person whose assignment is in
    force now (``valid_from`` is today or earlier) sets the retention obligation of the account
    (D-016, D-077, security review M-1: a future-dated assignment does not, so that deleting one that
    never took effect leaves the account untouched); the account is then also logged in the security
    log (M-2). The person is read again under a lock, so that an account that was blocked or
    deactivated in the meantime is refused (L-1)."""
    spec = _kind(kind)
    model = spec["model"]
    today = today or timezone.localdate()
    with transaction.atomic():
        locked = MeteringPoint.objects.select_for_update().get(pk=metering_point.pk)
        if kind == PERSON:
            subject = _lock_person(subject)
        last = model.objects.select_for_update().filter(metering_point=locked).order_by("-valid_from").first()
        previous = None
        if last is not None:
            if valid_from <= last.valid_from:
                raise AssignmentError("starts_not_after_previous")
            if last.valid_to is None or last.valid_to >= valid_from:
                previous = last
        assignment = model(
            metering_point=locked,
            valid_from=valid_from,
            previous_assignment=previous,
            previous_valid_to=previous.valid_to if previous else None,
            **{spec["subject"]: subject},
        )
        if previous is not None:
            previous.valid_to = valid_from - timedelta(days=1)
            previous.save(update_fields=["valid_to", "updated_at"])
        assignment.save()
        if kind == PERSON and valid_from <= today and not subject.retention_obligation:
            # The person is now part of the accounting: the account is kept, not deleted, when
            # the person closes it (D-016).
            subject.set_retention_obligation()
            security_log.record(security_log.Event.RETENTION_OBLIGATION_SET, account=subject, actor=actor)
        history.record(
            locked, spec["assigned"], actor,
            {f"{spec['subject']}_id": subject.pk, "valid_from": valid_from.isoformat()},
        )
    return assignment


def _lock_person(person):
    """The account of the person, read again under a lock: it must exist, be active and have a
    customer number (a person in the sense of the specification)."""
    user_model = type(person)
    try:
        fresh = user_model._base_manager.select_for_update().get(pk=person.pk)
    except user_model.DoesNotExist:
        raise AssignmentError("not_a_person") from None
    if fresh.customer_number_id is None:
        raise AssignmentError("not_a_person")
    if fresh.status != user_model.Status.ACTIVE:
        raise AssignmentError("person_not_active")
    return fresh


def end(kind, assignment, valid_to, actor=None):
    """End an open assignment on ``valid_to`` (the last day it is valid)."""
    spec = _kind(kind)
    with transaction.atomic():
        MeteringPoint.objects.select_for_update().get(pk=assignment.metering_point_id)
        fresh = spec["model"].objects.select_for_update().get(pk=assignment.pk)
        if fresh.valid_to is not None:
            raise AssignmentError("already_ended")
        if valid_to < fresh.valid_from:
            raise AssignmentError("end_before_start")
        fresh.valid_to = valid_to
        fresh.save(update_fields=["valid_to", "updated_at"])
        history.record(
            fresh.metering_point, spec["ended"], actor,
            {f"{spec['subject']}_id": getattr(fresh, f"{spec['subject']}_id"), "valid_to": valid_to.isoformat()},
        )
    assignment.valid_to = valid_to
    return fresh


def delete(kind, assignment, actor=None, today=None):
    """Delete an assignment that never took effect and undo the change of the previous one."""
    spec = _kind(kind)
    model = spec["model"]
    with transaction.atomic():
        metering_point = MeteringPoint.objects.select_for_update().get(pk=assignment.metering_point_id)
        fresh = model.objects.select_for_update().get(pk=assignment.pk)
        if not is_deletable(fresh, today):
            raise AssignmentError("not_deletable")
        previous = fresh.previous_assignment
        if previous is not None:
            previous = model.objects.select_for_update().get(pk=previous.pk)
            # Only what this assignment changed is undone (the previous one may have been ended
            # on its own in the meantime).
            if previous.valid_to == fresh.valid_from - timedelta(days=1):
                previous.valid_to = fresh.previous_valid_to
                previous.save(update_fields=["valid_to", "updated_at"])
        detail = {f"{spec['subject']}_id": getattr(fresh, f"{spec['subject']}_id"),
                  "valid_from": fresh.valid_from.isoformat()}
        fresh.delete()
        history.record(metering_point, spec["deleted"], actor, detail)
