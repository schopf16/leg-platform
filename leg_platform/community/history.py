"""History entries: who did what to which object, and when (D-077).

One helper writes them (``record``); lists and detail pages read them through ``entries_for``.
The events are English keys; the labels are shown by the pages. An entry holds only small facts in
``detail`` (the old and the new value of a setting, the id of an object), never an address or a
name of a customer.
"""

from django.contrib.auth import get_user_model
from django.db import models
from django.utils.translation import gettext_lazy as _

from leg_platform.community.models import HistoryEntry

LOCATION = "location"
METERING_POINT = "metering_point"
LEG = "leg"
PERSON = "person"
SETTINGS = "settings"
SUBJECT_TYPES = (LOCATION, METERING_POINT, LEG, PERSON, SETTINGS)


class HistoryEvent(models.TextChoices):
    """The events that exist so far; a later page adds its own (no schema change: the column has
    no choices)."""

    CREATED = "created", _("Created")
    ADDRESS_CORRECTION_ACCEPTED = "address_correction_accepted", _("Address correction accepted")
    ADDRESS_CORRECTION_DISMISSED = "address_correction_dismissed", _("Address correction not accepted")
    PERSON_ASSIGNED = "person_assigned", _("Person assigned")
    PERSON_ASSIGNMENT_ENDED = "person_assignment_ended", _("Person assignment ended")
    PERSON_ASSIGNMENT_DELETED = "person_assignment_deleted", _("Person assignment deleted")
    LEG_ASSIGNED = "leg_assigned", _("LEG assigned")
    LEG_ASSIGNMENT_ENDED = "leg_assignment_ended", _("LEG assignment ended")
    LEG_ASSIGNMENT_DELETED = "leg_assignment_deleted", _("LEG assignment deleted")
    SENDER_ADDRESS_CHANGED = "sender_address_changed", _("Sender address changed")
    COUNTRY_CHANGED = "country_changed", _("Country changed")
    IDENTIFIER_CHANGED = "identifier_changed", _("Identifier changed")


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


def record(subject, event, actor=None, detail=None):
    """Write one entry. ``subject`` is a domain object or a ``(type, id)`` pair; ``event`` is one of
    ``HistoryEvent``; ``actor`` the account that did it (None for the system)."""
    subject_type, subject_id = subject if isinstance(subject, tuple) else subject_of(subject)
    if subject_type not in SUBJECT_TYPES:
        raise ValueError(f"Unknown subject type: {subject_type!r}")
    event = HistoryEvent(event).value
    return HistoryEntry.objects.create(
        subject_type=subject_type,
        subject_id=subject_id,
        event=event,
        actor=actor if actor is not None and actor.pk else None,
        detail=detail or {},
    )


def entries_for(subject):
    """The entries of an object, oldest first, with the actor loaded."""
    subject_type, subject_id = subject if isinstance(subject, tuple) else subject_of(subject)
    return HistoryEntry.objects.filter(subject_type=subject_type, subject_id=subject_id).select_related("actor")


def has_history(subject):
    """Whether anything happened besides the creation: the test for "delete or archive"
    (``messpunkte/detail/VORGABEN.md``: at least one entry other than "Created")."""
    subject_type, subject_id = subject if isinstance(subject, tuple) else subject_of(subject)
    return (
        HistoryEntry.objects.filter(subject_type=subject_type, subject_id=subject_id)
        .exclude(event=HistoryEvent.CREATED)
        .exists()
    )
