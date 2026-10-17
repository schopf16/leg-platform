"""Creating, changing, archiving and deleting a LEG (D-080).

Every change is one transaction with its history entry (old/new quota and changed field names, never remark
text). The name is unique ignoring case and accents, stored with collapsed white space; a quota needs its
date; a LEG is deleted only if no metering point was ever assigned to it, otherwise archived.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from leg_platform.community import addressing, archived, assignments, history, legs
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import HistoryEntry, Leg, LegAssignment, MeteringPoint

Archived = archived.Archived


class DuplicateName(Exception):
    """A LEG with this name exists."""


class NotDeletable(Exception):
    """A metering point was assigned to the LEG at some time: it is archived, not deleted."""


def normalise_name(name):
    return addressing.collapse(name)


def name_in_use(name, exclude_pk=None):
    """Whether another LEG has this name (case and accents ignored)."""
    queryset = Leg.objects.filter(name__iexact=normalise_name(name))
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    return queryset.exists()


def _quota_pair(quota_percent, checked_on):
    """No quota, no date (the date belongs to the value)."""
    if quota_percent is None:
        return None, None
    if checked_on is None:
        raise ValueError("A quota needs the day it was checked.")
    return quota_percent, checked_on


def _text(value):
    """A history detail value: quota without trailing zeros, a day as ISO text."""
    if value is None:
        return None
    return legs.quota_number(value) if isinstance(value, Decimal) else str(value)


def create_leg(name, quota_percent=None, checked_on=None, remark="", actor=None):
    quota_percent, checked_on = _quota_pair(quota_percent, checked_on)
    name = normalise_name(name)
    if name_in_use(name):
        raise DuplicateName
    try:
        with transaction.atomic():
            leg = Leg.objects.create(
                name=name, production_quota_percent=quota_percent, quota_checked_on=checked_on,
                remark=(remark or "").strip(),
            )
            history.record(leg, HistoryEvent.CREATED, actor)
    except IntegrityError as error:
        # Concurrent requests with the same name: the unique column decides.
        if name_in_use(name):
            raise DuplicateName from error
        raise
    return leg


def update_quota(leg, quota_percent, checked_on, actor=None):
    """Quota and date are saved together; nothing is written if both are unchanged."""
    quota_percent, checked_on = _quota_pair(quota_percent, checked_on)
    with transaction.atomic():
        fresh = Leg.objects.select_for_update().get(pk=leg.pk)
        archived.ensure_open(fresh, archived.LEG)
        if fresh.production_quota_percent == quota_percent and fresh.quota_checked_on == checked_on:
            return fresh
        detail = {
            "old": _text(fresh.production_quota_percent), "new": _text(quota_percent),
            "old_checked_on": _text(fresh.quota_checked_on), "new_checked_on": _text(checked_on),
        }
        fresh.production_quota_percent, fresh.quota_checked_on = quota_percent, checked_on
        fresh.save(update_fields=["production_quota_percent", "quota_checked_on", "updated_at"])
        history.record(fresh, HistoryEvent.QUOTA_CHANGED, actor, detail)
    return fresh


def update_master_data(leg, name, remark, actor=None):
    """Name and remark; the history entry names the changed fields, not their text."""
    name = normalise_name(name)
    remark = (remark or "").strip()
    if name_in_use(name, exclude_pk=leg.pk):
        raise DuplicateName
    try:
        with transaction.atomic():
            fresh = Leg.objects.select_for_update().get(pk=leg.pk)
            archived.ensure_open(fresh, archived.LEG)
            changed = [field for field, value in (("name", name), ("remark", remark)) if getattr(fresh, field) != value]
            if not changed:
                return fresh
            fresh.name, fresh.remark = name, remark
            fresh.save(update_fields=["name", "remark", "updated_at"])
            history.record(fresh, HistoryEvent.MASTER_DATA_CHANGED, actor, {"fields": changed})
    except IntegrityError as error:
        if name_in_use(name, exclude_pk=leg.pk):
            raise DuplicateName from error
        raise
    return fresh


def set_archived(leg, archived, actor=None):
    with transaction.atomic():
        fresh = Leg.objects.select_for_update().get(pk=leg.pk)
        if fresh.is_archived != archived:
            fresh.is_archived = archived
            fresh.save(update_fields=["is_archived", "updated_at"])
            history.record(fresh, HistoryEvent.ARCHIVED if archived else HistoryEvent.UNARCHIVED, actor)
    return fresh


def can_delete(leg):
    """No metering point was ever assigned to the LEG (also none that moved on)."""
    return not LegAssignment.objects.filter(leg=leg).exists()


def assign_metering_point(leg, metering_point, valid_from, actor=None, today=None):
    """Assign a metering point that has no LEG (none in force and none planned) to the LEG from ``valid_from``
    (the LEG page, D-095). Locks the LEG, then the point (the order of ``assignments.assign``); refuses an archived
    LEG, an archived point and a point that has a LEG meanwhile (``AssignmentError``: ``leg_archived``,
    ``point_archived``, ``already_has_leg``; ``leg_missing`` and ``point_missing`` when one was deleted meanwhile, L-7).
    A move from another LEG is ``leg_switch.switch_leg``."""
    today = today or timezone.localdate()
    with transaction.atomic():
        try:
            if Leg.objects.select_for_update().get(pk=leg.pk).is_archived:
                raise assignments.AssignmentError("leg_archived")
        except Leg.DoesNotExist:
            raise assignments.AssignmentError("leg_missing") from None
        try:
            point = MeteringPoint.objects.select_for_update().get(pk=metering_point.pk)
        except MeteringPoint.DoesNotExist:
            # Deleted between the form and the save: the normal refusal, not an error page (L-7).
            raise assignments.AssignmentError("point_missing") from None
        if point.is_archived:
            raise assignments.AssignmentError("point_archived")
        if LegAssignment.objects.not_ended(today).filter(metering_point=point).exists():
            raise assignments.AssignmentError("already_has_leg")
        return assignments.assign(assignments.LEG, point, leg, valid_from, actor, today)


def delete(leg):
    """Delete a LEG without a history. Raises ``NotDeletable`` or ``Archived`` (D-085, D-086)."""
    with transaction.atomic():
        fresh = Leg.objects.select_for_update().get(pk=leg.pk)
        archived.ensure_open(fresh, archived.LEG)
        if not can_delete(fresh):
            raise NotDeletable
        HistoryEntry.objects.filter(subject_type=history.LEG, subject_id=fresh.pk).delete()
        fresh.delete()


__all__ = [
    "Archived", "DuplicateName", "NotDeletable", "assign_metering_point", "can_delete", "create_leg", "delete",
    "name_in_use", "normalise_name", "set_archived", "update_master_data", "update_quota",
]
