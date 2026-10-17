"""Creating, changing, archiving and deleting a LEG (D-080; ``legs/VORGABEN.md``, ``legs/DETAIL-ENTWURF.md``).

The rules are here, not in the views; every change is one transaction with its entry in the history
(actor and time; the entries hold the old and the new quota and the names of the fields that changed,
never the text of a remark).

* The name is unique (the comparison ignores case and accents, as the column does) and is stored with
  its white space collapsed.
* A quota needs its date "checked on"; without a quota the date stays empty.
* A LEG is deleted only without a history, which means: no metering point was ever assigned to it.
  Otherwise it is archived.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction

from leg_platform.community import addressing, archived, history, legs
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import HistoryEntry, Leg, LegAssignment

Archived = archived.Archived


class DuplicateName(Exception):
    """A LEG with this name exists."""


class NotDeletable(Exception):
    """A metering point was assigned to the LEG at some time: it is archived, not deleted."""


def normalise_name(name):
    return addressing.collapse(name)


def name_in_use(name, exclude_pk=None):
    """Whether another LEG has this name (case and accents ignored, as the unique column compares)."""
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
    """A value of the history detail: the quota without trailing zeros ("8.2"), a day as ISO text."""
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
        # Two requests with the same name at the same moment: the unique column decides.
        if name_in_use(name):
            raise DuplicateName from error
        raise
    return leg


def update_quota(leg, quota_percent, checked_on, actor=None):
    """Quota and date are saved together. Nothing is written (and no entry made) if both are as they were."""
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
    """Name and remark. The entry names the fields that changed, not their text."""
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
    """Without a history: no metering point was ever assigned to the LEG (also none that moved on)."""
    return not LegAssignment.objects.filter(leg=leg).exists()


def delete(leg):
    """Delete a LEG that has no history. Raises ``NotDeletable``. Its entries in the history go with it."""
    with transaction.atomic():
        fresh = Leg.objects.select_for_update().get(pk=leg.pk)
        if not can_delete(fresh):
            raise NotDeletable
        HistoryEntry.objects.filter(subject_type=history.LEG, subject_id=fresh.pk).delete()
        fresh.delete()


__all__ = [
    "Archived", "DuplicateName", "NotDeletable", "can_delete", "create_leg", "delete", "name_in_use",
    "normalise_name", "set_archived", "update_master_data", "update_quota",
]
