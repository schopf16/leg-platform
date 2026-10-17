"""History entries: who did what to which object, and when (D-077).

``record`` writes, ``entries_for`` reads. ``detail`` holds only small facts (old/new value, ids),
never customer data.
"""

from django.contrib.auth import get_user_model
from django.db import models
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator
from leg_platform.community.models import HistoryEntry

LOCATION = "location"
METERING_POINT = "metering_point"
LEG = "leg"
PERSON = "person"
SETTINGS = "settings"
SUBJECT_TYPES = (LOCATION, METERING_POINT, LEG, PERSON, SETTINGS)


class HistoryEvent(models.TextChoices):
    """The events that exist so far (the column has no choices, so adding one needs no migration)."""

    CREATED = "created", _("Created")
    ADDRESS_CHANGED = "address_changed", _("Address changed")
    TRANSFORMER_CIRCUIT_CHANGED = "transformer_circuit_changed", _("Transformer circuit changed")
    LOCATION_CONFIRMED = "location_confirmed", _("Address confirmed")
    ARCHIVED = "archived", _("Archived")
    UNARCHIVED = "unarchived", _("Restored from the archive")
    QUOTA_CHANGED = "quota_changed", _("Production quota changed")
    MASTER_DATA_CHANGED = "master_data_changed", _("Master data changed")
    LOCATIONS_MERGED = "locations_merged", _("Locations merged")
    METERING_POINT_LOCATION_CHANGED = "metering_point_location_changed", _("Location changed")
    # D-095: names the changed fields in ``detail["fields"]`` (names, never values).
    METERING_POINT_DATA_CHANGED = "metering_point_data_changed", _("Details changed")
    ADDRESS_CORRECTION_ACCEPTED = "address_correction_accepted", _("Address correction accepted")
    ADDRESS_CORRECTION_DISMISSED = "address_correction_dismissed", _("Address correction not accepted")
    PERSON_ASSIGNED = "person_assigned", _("Person assigned")
    PERSON_ASSIGNMENT_ENDED = "person_assignment_ended", _("Person assignment ended")
    PERSON_ASSIGNMENT_DELETED = "person_assignment_deleted", _("Person assignment deleted")
    LEG_ASSIGNED = "leg_assigned", _("LEG assigned")
    LEG_ASSIGNMENT_ENDED = "leg_assignment_ended", _("LEG assignment ended")
    LEG_ASSIGNMENT_DELETED = "leg_assignment_deleted", _("LEG assignment deleted")
    # Steps of a metering point towards the grid operator (D-079).
    REGISTERED_WITH_OPERATOR = "registered_with_operator", _("Marked as notified")
    CONFIRMATION_RECORDED = "confirmation_recorded", _("Confirmation recorded")
    START_DATE_RECORDED = "start_date_recorded", _("Start date recorded")
    TERMINATION_RECORDED = "termination_recorded", _("Termination recorded")
    SENDER_ADDRESS_CHANGED = "sender_address_changed", _("Sender address changed")
    SENDER_SUGGESTION_ACCEPTED = "sender_suggestion_accepted", _("Sender address correction accepted")
    SENDER_SUGGESTION_DISMISSED = "sender_suggestion_dismissed", _("Sender address correction not accepted")
    COUNTRY_CHANGED = "country_changed", _("Country changed")
    IDENTIFIER_CHANGED = "identifier_changed", _("Identifier changed")
    # Person events (D-081) name no value: note, operator customer number and addresses never go into the history.
    PERSON_NOTE_CHANGED = "person_note_changed", _("Note changed")
    PERSON_OPERATOR_NUMBER_CHANGED = "person_operator_number_changed", grid_operator.text(
        _("Customer number at the %(operator)s changed")
    )
    PERSON_SHARES_CHANGED = "person_shares_changed", _("Shares changed")
    # D-087: PERSON_DATA_CHANGED names the changed fields in ``detail["fields"]`` (names, never values).
    PERSON_INVITATION_SENT = "person_invitation_sent", _("Invitation sent")
    PERSON_DATA_CHANGED = "person_data_changed", _("Customer data changed")


def subject_of(obj):
    """``(subject_type, subject_id)`` of a domain object."""
    from leg_platform.community.models import Leg, Location, ManagementSettings, MeteringPoint, PersonRecord

    for model, subject_type in (
        (Location, LOCATION),
        (MeteringPoint, METERING_POINT),
        (Leg, LEG),
        (ManagementSettings, SETTINGS),
    ):
        if isinstance(obj, model):
            return subject_type, obj.pk
    if isinstance(obj, PersonRecord):
        return PERSON, obj.person_id
    # An account is a person.
    if isinstance(obj, get_user_model()):
        return PERSON, obj.pk
    raise TypeError(f"No history for {type(obj).__name__}.")


PROTECTED_DETAIL_KEYS = frozenset({"iban", "staff_note", "note", "operator_customer_number", "password", "email"})


def _resolve(subject):
    return subject if isinstance(subject, tuple) else subject_of(subject)


def _check_detail(detail, path=""):
    """Refuse protected keys (IBAN, notes, operator customer number, credentials) at any depth: staff read the history."""
    if isinstance(detail, dict):
        for key, value in detail.items():
            if str(key).lower() in PROTECTED_DETAIL_KEYS:
                raise ValueError(f"The history detail must not hold {path}{key}.")
            _check_detail(value, f"{path}{key}.")
    elif isinstance(detail, (list, tuple)):
        for item in detail:
            _check_detail(item, path)


def record(subject, event, actor=None, detail=None):
    """Write one entry. ``subject``: domain object or ``(type, id)``; ``actor``: account (None = system)."""
    subject_type, subject_id = _resolve(subject)
    if subject_type not in SUBJECT_TYPES:
        raise ValueError(f"Unknown subject type: {subject_type!r}")
    event = HistoryEvent(event).value
    _check_detail(detail)
    return HistoryEntry.objects.create(
        subject_type=subject_type,
        subject_id=subject_id,
        event=event,
        actor=actor if actor is not None and actor.pk else None,
        detail=detail or {},
    )


def entries_for(subject):
    """The entries of an object, oldest first, with the actor loaded."""
    subject_type, subject_id = _resolve(subject)
    return HistoryEntry.objects.filter(subject_type=subject_type, subject_id=subject_id).select_related("actor")


def has_history(subject):
    """Whether anything besides "Created" happened (the "delete or archive" test)."""
    subject_type, subject_id = _resolve(subject)
    return (
        HistoryEntry.objects.filter(subject_type=subject_type, subject_id=subject_id)
        .exclude(event=HistoryEvent.CREATED)
        .exists()
    )
