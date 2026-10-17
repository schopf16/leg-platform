"""Changing the management settings, with history for sender address, country and identifier (D-077)."""

from django.db import transaction

from leg_platform.community import addressing, history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import ManagementSettings

SENDER_FIELDS = (
    "sender_street",
    "sender_house_number",
    "sender_address_addition",
    "sender_postal_code",
    "sender_town",
)
EDITABLE_FIELDS = ("country_code", "identifier", "warning_threshold_percent", *SENDER_FIELDS)


def sender_signature(values_or_row):
    """The memory key of the sender address (the address addition is not part of it)."""
    get = values_or_row.get if isinstance(values_or_row, dict) else lambda name: getattr(values_or_row, name)
    return addressing.address_signature(
        get("sender_street"), get("sender_house_number"), get("sender_postal_code"), get("sender_town")
    )


def update(values, actor):
    """Save ``values`` (exactly ``EDITABLE_FIELDS``) and write the history; returns the recorded events."""
    if set(values) != set(EDITABLE_FIELDS):
        raise ValueError("The settings need exactly the editable fields.")
    events = []
    with transaction.atomic():
        ManagementSettings.objects.get_or_create(id=1)
        row = ManagementSettings.objects.select_for_update().get(id=1)
        before = {name: getattr(row, name) for name in EDITABLE_FIELDS}
        for name in EDITABLE_FIELDS:
            setattr(row, name, values[name])
        row.full_clean()
        row.save()
        if any(before[name] != values[name] for name in SENDER_FIELDS):
            history.record(
                row, HistoryEvent.SENDER_ADDRESS_CHANGED, actor,
                {"old": {n: before[n] for n in SENDER_FIELDS}, "new": {n: values[n] for n in SENDER_FIELDS}},
            )
            events.append(HistoryEvent.SENDER_ADDRESS_CHANGED)
        for name, event in (
            ("country_code", HistoryEvent.COUNTRY_CHANGED),
            ("identifier", HistoryEvent.IDENTIFIER_CHANGED),
        ):
            if before[name] != values[name]:
                history.record(row, event, actor, {"old": before[name], "new": values[name]})
                events.append(event)
    return events


def accept_sender_suggestion(suggestion, actor):
    """Record the acceptance only; the fields are saved with "Save", which writes ``SENDER_ADDRESS_CHANGED``."""
    with transaction.atomic():
        ManagementSettings.objects.get_or_create(id=1)
        row = ManagementSettings.objects.select_for_update().get(id=1)
        history.record(
            row, HistoryEvent.SENDER_SUGGESTION_ACCEPTED, actor, {"suggested": list(suggestion.as_tuple())}
        )


def dismiss_sender_suggestion(signature, actor=None):
    """Remember the dismissal for the sender address as it is, and record it."""
    with transaction.atomic():
        ManagementSettings.objects.get_or_create(id=1)
        row = ManagementSettings.objects.select_for_update().get(id=1)
        row.sender_suggestion_dismissed_for = signature
        row.save(update_fields=["sender_suggestion_dismissed_for", "updated_at"])
        history.record(row, HistoryEvent.SENDER_SUGGESTION_DISMISSED, actor)
