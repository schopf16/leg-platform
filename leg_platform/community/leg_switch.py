"""Changing the LEG of a metering point, together with the ones that belong to it (D-079).

One service for the page of the metering point and the page of the LEG (``messpunkte/VORGABEN.md``,
"LEG wechseln"; ``legs/DETAIL-ENTWURF.md``, "Tausch-Symbol pro Messpunkt"; the mask is
``legs/referenz/leg-messpunkt-wechseln-desktop``). The "move mask" offers the other metering points
of the same person at the same location (they usually move together); none is preselected and only
the ones that the user ticks move. The date applies to all of them. Everything happens in one
transaction: if one assignment is refused, none is made.
"""

from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from leg_platform.community import assignments
from leg_platform.community.models import Leg, MeteringPoint, PersonAssignment


def current_assignment(kind, metering_point, today=None):
    """The assignment that is valid today, else the next one that starts later, or None."""
    today = today or timezone.localdate()
    return (
        assignments.model_of(kind)
        .objects.not_ended(today)
        .filter(metering_point=metering_point)
        .select_related(*("leg",) if kind == assignments.LEG else ("person",))
        .order_by("valid_from")
        .first()
    )


def related_points(metering_point, today=None):
    """The other metering points (not archived) of the same person at the same location, by number;
    none if the metering point has no location or no person."""
    today = today or timezone.localdate()
    if metering_point.location_id is None:
        return []
    person = current_assignment(assignments.PERSON, metering_point, today)
    if person is None:
        return []
    same_person = PersonAssignment.objects.not_ended(today).filter(
        metering_point=OuterRef("pk"), person_id=person.person_id
    )
    return list(
        MeteringPoint.objects.filter(location_id=metering_point.location_id, is_archived=False)
        .exclude(pk=metering_point.pk)
        .filter(Exists(same_person))
        .select_related("location")
        .order_by(F("number").asc(nulls_last=True), "pk")
    )


def switch_leg(metering_point, leg, valid_from, also=(), actor=None, today=None):
    """Assign ``leg`` from ``valid_from`` to the metering point and to the metering points of ``also``
    (each must be one of ``related_points``). The previous LEG of each ends the day before. A point that
    already has this LEG is left alone. Returns the new assignments. Raises ``AssignmentError``."""
    today = today or timezone.localdate()
    with transaction.atomic():
        fresh_leg = Leg.objects.select_for_update().get(pk=leg.pk)
        if fresh_leg.is_archived:
            raise assignments.AssignmentError("leg_archived")
        allowed = {point.pk for point in related_points(metering_point, today)}
        if any(point.pk not in allowed for point in also):
            raise assignments.AssignmentError("not_related")
        made = []
        for point in (metering_point, *also):
            current = current_assignment(assignments.LEG, point, today)
            if current is not None and current.leg_id == fresh_leg.pk and point.pk != metering_point.pk:
                continue
            made.append(assignments.assign(assignments.LEG, point, fresh_leg, valid_from, actor, today))
    return made
