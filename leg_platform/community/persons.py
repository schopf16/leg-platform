"""What the staff may change about a person (D-081; ``personen/UMSETZUNG.md``, section 1).

The master data of a customer is the customer's own: the staff do not edit it. The exceptions are

* the staff-only data in the side record ``PersonRecord`` (the note, the optional BKW customer number and
  the shares of the cooperative with their period), created when it is first needed, and
* the correction of the address with the suggestion of the address directory, which the staff accept or do
  not accept ("wie beim Standort", ``personen/VORGABEN.md``, "Adresskorrektur bei Personen").

Every change is one transaction that locks the account and the record, and is written to the history of the
person with the actor and the time. The history never holds the note, the BKW customer number, the IBAN or
an address (``history.record`` refuses these keys); an entry names the event only. The address is written
through the same save of the account as the profile of the customer (``update_fields``: only the address
fields; the controlled fields of the model, D-034, are never touched). Whoever calls these functions has
checked the right of the account (``community.change_personrecord``, ``community.correct_person_address``).
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction

from leg_platform.accounts.models import SWITZERLAND, parse_swiss_postal_code
from leg_platform.community import addressing, history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import PersonRecord

NOTE_MAX_LENGTH = 5000
BKW_NUMBER_MAX_LENGTH = PersonRecord._meta.get_field("bkw_customer_number").max_length
SHARES_MAX = 100_000

ADDRESS_FIELDS = ("street", "house_number", "town", "swiss_postal_code", "foreign_postal_code", "country")


class PersonError(Exception):
    """A change that the rules refuse. ``code``: ``not_a_person``, ``note_too_long``, ``number_invalid``,
    ``shares_invalid``, ``period_invalid``, ``no_open_suggestion``."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _lock_person(person):
    """The account, read again under a lock; it must still be a person (have a customer number)."""
    user_model = get_user_model()
    try:
        return user_model.objects.select_for_update().get(pk=person.pk, customer_number__isnull=False)
    except user_model.DoesNotExist:
        raise PersonError("not_a_person") from None


def _locked_record(person, *, create):
    """The side record under a lock. It is created only when ``create`` is true (the first value that needs
    it); otherwise a person without a record gives None."""
    if create:
        return PersonRecord.objects.select_for_update().get_or_create(person=person)[0]
    return PersonRecord.objects.select_for_update().filter(person=person).first()


def record_of(person):
    """The side record of the person, or None if none was needed so far (it is not created by reading)."""
    return PersonRecord.objects.filter(person=person).first()


def set_note(person, note, actor=None):
    """Save the staff-only note (empty clears it)."""
    note = (note or "").strip()
    if len(note) > NOTE_MAX_LENGTH:
        raise PersonError("note_too_long")
    with transaction.atomic():
        fresh = _lock_person(person)
        record = _locked_record(fresh, create=bool(note))
        if record is None or record.staff_note == note:
            return record
        record.staff_note = note
        record.save(update_fields=["staff_note", "updated_at"])
        history.record(fresh, HistoryEvent.PERSON_NOTE_CHANGED, actor)
    return record


def set_bkw_customer_number(person, number, actor=None):
    """Save the optional BKW customer number (empty clears it)."""
    number = " ".join((number or "").split())
    if len(number) > BKW_NUMBER_MAX_LENGTH or not number.isprintable():
        raise PersonError("number_invalid")
    with transaction.atomic():
        fresh = _lock_person(person)
        record = _locked_record(fresh, create=bool(number))
        if record is None or record.bkw_customer_number == number:
            return record
        record.bkw_customer_number = number
        record.save(update_fields=["bkw_customer_number", "updated_at"])
        history.record(fresh, HistoryEvent.PERSON_BKW_NUMBER_CHANGED, actor)
    return record


def set_shares(person, shares, shares_from, shares_to, actor=None):
    """Save the shares of the cooperative with their period; no number of shares clears all three values.

    Shares are a whole number from 1; the period starts on ``shares_from`` (required with shares) and ends on
    ``shares_to`` (empty: open); the end is not before the start. "Ending" shares is setting the end date."""
    if shares is None:
        shares_from = shares_to = None
    else:
        if isinstance(shares, bool) or not isinstance(shares, int) or not 1 <= shares <= SHARES_MAX:
            raise PersonError("shares_invalid")
        if shares_from is None or (shares_to is not None and shares_to < shares_from):
            raise PersonError("period_invalid")
    with transaction.atomic():
        fresh = _lock_person(person)
        record = _locked_record(fresh, create=shares is not None)
        if record is None:
            return None
        if (record.shares, record.shares_from, record.shares_to) == (shares, shares_from, shares_to):
            return record
        record.shares, record.shares_from, record.shares_to = shares, shares_from, shares_to
        record.save(update_fields=["shares", "shares_from", "shares_to", "updated_at"])
        history.record(fresh, HistoryEvent.PERSON_SHARES_CHANGED, actor)
    return record


# The address correction -----------------------------------------------------------------------------


def _address_of(person):
    return (person.street, person.house_number, str(person.swiss_postal_code or ""), person.town)


def _fits(suggestion):
    """Whether the suggestion can be written to the account (the lengths of the columns, a Swiss postal code)."""
    user_model = get_user_model()
    limits = {name: user_model._meta.get_field(name).max_length for name in ("street", "house_number", "town")}
    return (
        len(suggestion.street) <= limits["street"]
        and len(suggestion.house_number) <= limits["house_number"]
        and len(suggestion.town) <= limits["town"]
        and parse_swiss_postal_code(suggestion.postal_code) is not None
    )


def address_suggestion(person):
    """The suggestion of the address directory for the address of the person, or None.

    Asked for every call (the directory is a local file; nothing is stored for a page that only looks).
    There is none for an inactive account, for an address outside Switzerland or without a street, when the
    suggestion would not fit the columns of the account, or after "Do not accept" for the address as it is
    now; the warning returns only when street, house number, postal code or town changes (``addressing``)."""
    user_model = get_user_model()
    if (
        person.customer_number_id is None
        or person.status == user_model.Status.DEACTIVATED
        or not person.country
        or person.country.code != SWITZERLAND
        or person.swiss_postal_code is None
        or not person.street.strip()
    ):
        return None
    suggestion = addressing.suggest_correction(*_address_of(person))
    if suggestion is None or not _fits(suggestion):
        return None
    record = record_of(person)
    if record is not None and record.suggestion_dismissed_for == addressing.address_signature(*_address_of(person)):
        return None
    return suggestion


def suggestion_token(suggestion):
    """What the buttons of the banner carry back: the digest of the suggestion that was shown, so that a
    decision is never made about a suggestion that changed in the meantime."""
    return addressing.address_digest(*suggestion.as_tuple())


def _current_suggestion(fresh, token):
    suggestion = address_suggestion(fresh)
    if suggestion is None or suggestion_token(suggestion) != token:
        raise PersonError("no_open_suggestion")
    return suggestion


def accept_address_suggestion(person, token, actor=None):
    """Replace the address of the account by the suggestion that the ``token`` names; the entry goes to the
    history with the actor and the time. The suggestion is asked again under the lock; if it is not the one
    that was shown, ``PersonError("no_open_suggestion")``."""
    with transaction.atomic():
        fresh = _lock_person(person)
        suggestion = _current_suggestion(fresh, token)
        fresh.street, fresh.house_number, postal_code, fresh.town = suggestion.as_tuple()
        fresh.swiss_postal_code = parse_swiss_postal_code(postal_code)
        fresh.foreign_postal_code = ""
        user_model = type(fresh)
        # Only the address fields are validated and written; the rest of the account stays as it is.
        try:
            fresh.clean_fields(exclude={field.name for field in user_model._meta.fields} - set(ADDRESS_FIELDS))
        except ValidationError:
            raise PersonError("no_open_suggestion") from None
        fresh.save(update_fields=list(ADDRESS_FIELDS))
        PersonRecord.objects.filter(person=fresh).update(
            suggestion_dismissed_for="", suggested_street="", suggested_house_number="",
            suggested_postal_code="", suggested_town="", address_correction_open=False,
        )
        history.record(fresh, HistoryEvent.ADDRESS_CORRECTION_ACCEPTED, actor)
    return fresh


def dismiss_address_suggestion(person, token, actor=None):
    """"Do not accept": the address stays and the warning is gone until the address changes."""
    with transaction.atomic():
        fresh = _lock_person(person)
        _current_suggestion(fresh, token)
        record = _locked_record(fresh, create=True)
        record.suggestion_dismissed_for = addressing.address_signature(*_address_of(fresh))
        record.save(update_fields=["suggestion_dismissed_for", "updated_at"])
        history.record(fresh, HistoryEvent.ADDRESS_CORRECTION_DISMISSED, actor)
    return fresh
