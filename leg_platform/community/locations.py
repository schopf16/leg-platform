"""Locations: the directory check, the decision about its suggestion (D-077), and create/change/merge/delete.

The check result is stored on the location so the list can filter "address correction open" without
asking the directory per row. "Not accept" is remembered for the address as it is.
"""

from django.db import transaction

from leg_platform.community import addressing, archived, history, transformer_circuits
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import HistoryEntry, Location, MeteringPoint

Archived = archived.Archived


class SuggestionError(Exception):
    """``code``: ``no_open_suggestion`` or ``address_in_use``."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class MergeNeeded(Exception):
    """The new address belongs to another location: saving needs the explicit (irreversible) merge into ``target``."""

    def __init__(self, target, metering_points):
        super().__init__("merge_needed")
        self.target = target
        self.metering_points = metering_points


class NotDeletable(Exception):
    """The location has a history or metering points: it is archived, not deleted."""


class MergeRefused(Exception):
    """The address belongs to an archived location: nothing is moved to it."""


def _lock_open(location):
    """The location row locked for update; raises ``Archived`` if it is archived."""
    fresh = Location.objects.select_for_update().get(pk=location.pk)
    archived.ensure_open(fresh, archived.LOCATION)
    return fresh


def refresh_suggestion(location):
    """Ask the directory about the address and store the answer (suggestion columns only)."""
    suggestion = addressing.suggest_correction(*location.current_address())
    location.apply_suggestion(suggestion)
    if location.pk:
        location.save(
            update_fields=[
                "suggested_street", "suggested_house_number", "suggested_postal_code",
                "suggested_town", "address_correction_open", "updated_at",
            ]
        )
    return suggestion


def accept_suggestion(location, actor):
    """Replace the address by the suggestion (history entry). Refused without an open suggestion or if
    another location has that address."""
    with transaction.atomic():
        fresh = _lock_open(location)
        suggestion = fresh.suggestion
        if suggestion is None or not fresh.address_correction_open:
            raise SuggestionError("no_open_suggestion")
        other = Location.find_by_address(*suggestion.as_tuple())
        if other is not None and other.pk != fresh.pk:
            raise SuggestionError("address_in_use")
        fresh.street, fresh.house_number, fresh.postal_code, fresh.town = suggestion.as_tuple()
        fresh.clear_suggestion()
        fresh.save()
        history.record(fresh, HistoryEvent.ADDRESS_CORRECTION_ACCEPTED, actor)
    return fresh


def dismiss_suggestion(location, actor):
    """"Do not accept": the address stays, the warning is gone until the address changes."""
    with transaction.atomic():
        fresh = _lock_open(location)
        if fresh.suggestion is None or not fresh.address_correction_open:
            raise SuggestionError("no_open_suggestion")
        fresh.dismiss_suggestion()
        fresh.save(update_fields=["suggestion_dismissed_for", "address_correction_open", "updated_at"])
        history.record(fresh, HistoryEvent.ADDRESS_CORRECTION_DISMISSED, actor)
    return fresh


ADDRESS_FIELDS = ("street", "house_number", "postal_code", "town")


def create_location(
    *, street, house_number, postal_code, town, address_addition="", transformer_circuit=None, new_circuit=None,
    remark="", needs_confirmation=False, actor=None,
):
    """A new location; raises ``ValidationError`` (also for an existing address). The directory check runs once.

    ``new_circuit`` = ``(grid number, name)`` creates a transformer circuit in the same transaction as the location
    (D-095): if either fails nothing is kept; a wrong or existing number raises ``ValidationError`` with the keys
    ``grid_number`` / ``name``. It replaces ``transformer_circuit``."""
    location = Location(
        street=street, house_number=house_number, postal_code=postal_code, town=town,
        address_addition=address_addition, transformer_circuit=transformer_circuit,
        remark=remark.strip(), needs_confirmation=needs_confirmation,
    )
    location.full_clean()
    with transaction.atomic():
        if new_circuit is not None:
            location.transformer_circuit = transformer_circuits.create(*new_circuit)
        location.save()
        history.record(location, HistoryEvent.CREATED, actor)
    refresh_suggestion(location)
    return location


def update_address(
    location, *, street, house_number, postal_code, town, address_addition="", actor=None, merge=False,
    merge_target_id=None,
):
    """Change the address (only the address can be changed).

    If the new address belongs to another location, ``MergeNeeded`` is raised unless ``merge`` is true
    and ``merge_target_id`` is that location's id; then its metering points move to the other
    location, this one is deleted and the other returned. An archived target raises ``MergeRefused``.
    Otherwise the address is saved, a change is logged and the directory asked again. Rows are
    locked in ascending id order."""
    new = addressing.normalise_address(street, house_number, postal_code, town)
    with transaction.atomic():
        probe = Location.find_by_address(*new)
        ids = sorted({location.pk, probe.pk} if probe is not None else {location.pk})
        locked = {row.pk: row for row in Location.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
        fresh = locked[location.pk]
        archived.ensure_open(fresh, archived.LOCATION)
        other = Location.find_by_address(*new)
        if other is not None and other.pk != fresh.pk:
            if other.is_archived:
                raise MergeRefused()
            other = locked.get(other.pk) or Location.objects.select_for_update().get(pk=other.pk)
            count = MeteringPoint.objects.filter(location=fresh).count()
            if not merge or merge_target_id != other.pk:
                raise MergeNeeded(other, count)
            return _merge(fresh, other, actor)
        changed = new != fresh.current_address()
        fresh.street, fresh.house_number, fresh.postal_code, fresh.town = new
        fresh.address_addition = address_addition
        fresh.full_clean()
        fresh.save()
        if changed:
            history.record(fresh, HistoryEvent.ADDRESS_CHANGED, actor)
    refresh_suggestion(fresh)
    return fresh


def _merge(source, target, actor):
    """Move the metering points of ``source`` to ``target`` (history entry each), log the merge, delete ``source``."""
    points = list(MeteringPoint.objects.select_for_update().filter(location=source).order_by("pk"))
    for point in points:
        point.location = target
        point.save(update_fields=["location", "updated_at"])
        history.record(
            point, HistoryEvent.METERING_POINT_LOCATION_CHANGED, actor,
            {"old_location_id": source.pk, "new_location_id": target.pk},
        )
    history.record(
        target, HistoryEvent.LOCATIONS_MERGED, actor,
        {"merged_location_id": source.pk, "metering_points": len(points)},
    )
    source.delete()
    return target


def set_circuit(location, circuit, actor=None, new_circuit=None):
    """Assign the transformer circuit (None removes it); a change is logged. ``new_circuit`` = ``(grid number,
    name)`` creates the circuit in the same transaction (D-095; ``ValidationError`` as for ``create_location``)."""
    with transaction.atomic():
        fresh = Location.objects.select_for_update().select_related("transformer_circuit").get(pk=location.pk)
        archived.ensure_open(fresh, archived.LOCATION)
        if new_circuit is not None:
            circuit = transformer_circuits.create(*new_circuit)
        before = fresh.transformer_circuit
        if before == circuit:
            return fresh
        fresh.transformer_circuit = circuit
        fresh.save(update_fields=["transformer_circuit", "updated_at"])
        history.record(
            fresh, HistoryEvent.TRANSFORMER_CIRCUIT_CHANGED, actor,
            {"old": before.grid_number if before else "", "new": circuit.grid_number if circuit else ""},
        )
    return fresh


def update_remark(location, remark, actor=None):
    """Save the remark (not logged: the text is internal)."""
    with transaction.atomic():
        fresh = _lock_open(location)
        fresh.remark = (remark or "").strip()
        fresh.save(update_fields=["remark", "updated_at"])
    return fresh


def confirm(location, actor=None):
    """Confirm a customer-created location: the mark "new" goes."""
    with transaction.atomic():
        fresh = _lock_open(location)
        if fresh.needs_confirmation:
            fresh.needs_confirmation = False
            fresh.save(update_fields=["needs_confirmation", "updated_at"])
            history.record(fresh, HistoryEvent.LOCATION_CONFIRMED, actor)
    return fresh


def set_archived(location, archived, actor=None):
    with transaction.atomic():
        fresh = Location.objects.select_for_update().get(pk=location.pk)
        if fresh.is_archived != archived:
            fresh.is_archived = archived
            fresh.save(update_fields=["is_archived", "updated_at"])
            history.record(fresh, HistoryEvent.ARCHIVED if archived else HistoryEvent.UNARCHIVED, actor)
    return fresh


def can_delete(location):
    """Deletable only without a history and metering points (those archive instead)."""
    return not history.has_history(location) and not location.metering_points.exists()


def delete(location):
    """Delete a location without history or metering points; raises ``NotDeletable`` (``Archived`` if archived; D-085, D-086)."""
    with transaction.atomic():
        fresh = _lock_open(location)
        if not can_delete(fresh):
            raise NotDeletable
        # Only the entry "Created" can exist; it goes with the location.
        HistoryEntry.objects.filter(subject_type=history.LOCATION, subject_id=fresh.pk).delete()
        fresh.delete()


__all__ = [
    "ADDRESS_FIELDS", "Archived", "MergeNeeded", "MergeRefused", "NotDeletable", "SuggestionError", "accept_suggestion",
    "can_delete", "confirm", "create_location", "delete", "dismiss_suggestion", "refresh_suggestion",
    "set_archived", "set_circuit", "update_address", "update_remark",
]
