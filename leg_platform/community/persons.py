"""What the staff may change about a person (D-081, D-087; ``personen/UMSETZUNG.md``, section 1).

A person is an account that has a customer number, or an invited person who has not completed the registration
yet (``person_filter``). What the staff may do:

* keep the staff-only data in the side record ``PersonRecord`` (the note and the optional customer number at the
  grid operator), created when it is first needed, and the history of the cooperative shares (``ShareChange``:
  a number of shares and the day it holds from, D-086);
* correct the address with the suggestion of the address directory, which the staff accept or do not accept
  ("wie beim Standort", ``personen/VORGABEN.md``, "Adresskorrektur bei Personen");
* **create a person and invite them** (D-087, ``create_person``) and send the invitation again
  (``resend_invitation``): the account is created with the data of the staff, without password and customer
  number; the person proves the address with a link and completes the registration (``accounts.invitations``);
* **change the data of a customer for support** (D-087, ``update_customer_data``, ``update_payment_data``,
  ``request_second_email``): salutation, company, names, second person, phone, billing address, IBAN, paper
  invoice and, through the confirmation flow of the second address, the second email address. **Not** the login
  address, the password, the second factor, the status, ``is_staff``, ``is_superuser``, the groups, the retention
  flag and the customer number (D-012 stays valid for these): none of these functions touches them, the save
  names its ``update_fields``, and the guards of the account model (D-034) apply. The data of a staff user or a
  system administrator is changed only by a system administrator, and the data of a deactivated account (the
  archive) not at all.

Every change is one transaction that locks the account and the record, and is written to the history of the
person with the actor and the time, and (the changes of the customer data and the invitations) to the security
log with the actor and the account number. The history and the security log name the changed **fields** and never
a value: the note, the customer number at the grid operator, the IBAN or an address never go there
(``history.record`` refuses these keys). The customer is not notified of a change of the data (owner decision).
Every staff account may call these functions (D-086).
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q

from leg_platform.accounts import email_change, invitations, security_log
from leg_platform.accounts.encryption import DecryptionError
from leg_platform.accounts.models import (
    SWITZERLAND,
    AccountIntegrityError,
    SecurityEvent,
    parse_swiss_postal_code,
    validate_account_email,
)
from leg_platform.accounts.normalization import normalize_email_address
from leg_platform.community import addressing, history
from leg_platform.community import iban as iban_module
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import PersonRecord, ShareChange

NOTE_MAX_LENGTH = 5000
OPERATOR_NUMBER_MAX_LENGTH = PersonRecord._meta.get_field("operator_customer_number").max_length
SHARES_MAX = 100_000

ADDRESS_FIELDS = ("street", "house_number", "town", "swiss_postal_code", "foreign_postal_code", "country")

# The data of a customer that the staff may change, by the card of the detail page (D-087). The login address, the
# password, the status, the rights and the customer number are not in any of these lists.
GROUP_DETAILS = "details"
GROUP_CONTACT = "contact"
GROUP_ADDRESS = "address"
GROUP_FIELDS = {
    GROUP_DETAILS: ("salutation", "organisation_name", "first_name", "last_name", "address_line_1"),
    GROUP_CONTACT: ("phone_number",),
    GROUP_ADDRESS: (*ADDRESS_FIELDS, "post_office_box_number"),
}


class PersonError(Exception):
    """A change that the rules refuse. ``code``: ``not_a_person``, ``note_too_long``, ``number_invalid``,
    ``shares_invalid``, ``date_invalid``, ``no_open_suggestion``, ``not_allowed`` (the actor may not change this
    account), ``email_in_use``, ``email_invalid``, ``rate_limited``, ``not_invited``, ``not_active``,
    ``fields_invalid``."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def person_filter():
    """The accounts that are persons (a ``Q``): an account with a customer number, and an invited person who has
    not completed the registration yet and so has none (D-087). Other unverified accounts are no persons."""
    user_model = get_user_model()
    return Q(customer_number__isnull=False) | Q(status=user_model.Status.UNVERIFIED, invited_at__isnull=False)


def person_queryset():
    return get_user_model().objects.filter(person_filter())


def _lock_person(person):
    """The account, read again under a lock; it must still be a person (see ``person_filter``)."""
    user_model = get_user_model()
    try:
        return person_queryset().select_for_update().get(pk=person.pk)
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


def set_operator_customer_number(person, number, actor=None):
    """Save the optional customer number at the grid operator (empty clears it)."""
    number = " ".join((number or "").split())
    if len(number) > OPERATOR_NUMBER_MAX_LENGTH or not number.isprintable():
        raise PersonError("number_invalid")
    with transaction.atomic():
        fresh = _lock_person(person)
        record = _locked_record(fresh, create=bool(number))
        if record is None or record.operator_customer_number == number:
            return record
        record.operator_customer_number = number
        record.save(update_fields=["operator_customer_number", "updated_at"])
        history.record(fresh, HistoryEvent.PERSON_OPERATOR_NUMBER_CHANGED, actor)
    return record


def add_share_change(person, quantity, valid_from, actor=None):
    """Add an entry to the history of the shares: ``quantity`` shares (a whole number from 0, 0 for "no
    shares any more") hold from ``valid_from`` on. The entry is only added, never changed; a correction is a
    new entry. The current number is the one of the latest entry by date (``current_shares``)."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 0 <= quantity <= SHARES_MAX:
        raise PersonError("shares_invalid")
    if valid_from is None:
        raise PersonError("date_invalid")
    with transaction.atomic():
        fresh = _lock_person(person)
        record = _locked_record(fresh, create=True)
        entry = ShareChange.objects.create(
            record=record, quantity=quantity, valid_from=valid_from,
            actor=actor if actor is not None and actor.pk else None,
        )
        history.record(fresh, HistoryEvent.PERSON_SHARES_CHANGED, actor)
    return entry


def share_history(person):
    """The entries of the history of the shares, the latest date first (the order of the model)."""
    return list(ShareChange.objects.filter(record__person=person))


def current_shares(entries, today):
    """The number of shares of the latest entry by date that holds on ``today`` (``entries`` as
    ``share_history`` gives them), or None while no entry holds yet (D-086)."""
    for entry in entries:
        if entry.valid_from <= today:
            return entry.quantity
    return None


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


# Create a person and invite them (D-087) ------------------------------------------------------------------


def _actor_row(actor):
    """The staff account that acts, read again from the database: an active staff account now."""
    user_model = get_user_model()
    row = None
    if actor is not None and getattr(actor, "pk", None):
        row = user_model.objects.filter(pk=actor.pk, status=user_model.Status.ACTIVE, is_staff=True).first()
    if row is None:
        raise PersonError("not_allowed")
    return row


def may_change_customer_data(actor, person):
    """Whether the actor may change the data of this account (D-087, D-012): an active staff account, never for a
    deactivated account (the archive), and the data of a staff user or a system administrator (also one's own
    account) only for a system administrator. Reads the objects only; the services check again under the lock."""
    user_model = get_user_model()
    if actor is None or not actor.is_active or not actor.is_staff:
        return False
    if person.status == user_model.Status.DEACTIVATED:
        return False
    if (person.is_staff or person.is_superuser) and not actor.is_superuser:
        return False
    return True


def _check_actor(actor, person):
    if not may_change_customer_data(_actor_row(actor), person):
        raise PersonError("not_allowed")


def create_person(actor, email, values):
    """Create a person with the data of the staff and invite them (D-087); return the account.

    ``values`` are the profile fields of the registration (``UserManager.INVITATION_PROFILE_FIELDS``). The account
    is unverified: no password, no customer number (it is issued when the person completes the registration,
    D-021), no privileges, no groups. The email with the personal link is queued (``accounts.invitations``); if a
    limit refuses it, nothing is created (``PersonError("rate_limited")``).

    ``PersonError("email_invalid")`` for an address that cannot be an account's address, ``PersonError("email_in_use")``
    if the address belongs to another account that is not deactivated; the staff are told that and nothing about the
    other account (an unverified self-registration does not hold the address, it is replaced as by a registration,
    D-016). A ``ValidationError`` names the data that does not pass the rules of the account.
    """
    _actor_row(actor)
    email = str(email or "").strip()
    try:
        validate_account_email(email)
    except ValueError:
        raise PersonError("email_invalid") from None
    normalized = normalize_email_address(email)
    user_model = get_user_model()
    with transaction.atomic():
        # Read without a lock like the registration: an address of an account that is not pending is never locked.
        existing = user_model._base_manager.filter(normalized_email=normalized).first()
        if existing is not None:
            pending = (
                user_model._base_manager.select_for_update()
                .filter(pk=existing.pk, status=user_model.Status.UNVERIFIED, invited_at__isnull=True)
                .first()
            )
            if pending is None:
                raise PersonError("email_in_use")
            pending.delete()
        try:
            with transaction.atomic():
                user = user_model.objects.create_invited_user(email, **values)
        except IntegrityError:
            raise PersonError("email_in_use") from None
        if not invitations.queue(user, actor):
            raise PersonError("rate_limited")
        history.record(user, HistoryEvent.CREATED, actor)
        history.record(user, HistoryEvent.PERSON_INVITATION_SENT, actor)
        security_log.record(SecurityEvent.Event.PERSON_INVITED, account=user, actor=actor)
    return user


def resend_invitation(person, actor):
    """Send the invitation of a person who has not completed the registration again (D-087): a new link is made
    by the worker and the earlier one ends. The limits of ``accounts.invitations`` apply
    (``PersonError("rate_limited")``); ``PersonError("not_invited")`` for any other account."""
    _actor_row(actor)
    with transaction.atomic():
        fresh = _lock_person(person)
        if not invitations.is_pending(fresh):
            raise PersonError("not_invited")
        if not invitations.queue(fresh, actor):
            raise PersonError("rate_limited")
        history.record(fresh, HistoryEvent.PERSON_INVITATION_SENT, actor)
        security_log.record(SecurityEvent.Event.INVITATION_RESENT, account=fresh, actor=actor)
    return fresh


def invitation_state(person):
    """What the detail page says about the invitation of a person who has not completed the registration, or None
    for any other account: ``expires_at`` (the end of the valid link, or None), ``sending`` (the email is queued)."""
    if not invitations.is_pending(person):
        return None
    return {"expires_at": invitations.link_expires_at(person), "sending": invitations.is_being_sent(person)}


# The data of a customer, for support (D-087) -------------------------------------------------------------


def _field_messages(error):
    return {field: [message for item in items for message in item.messages] for field, items in error.error_dict.items()}


def group_errors(account, group):
    """The rules of the account model for the fields of one group (the field validators, the postal code rules for
    the address, the rule of the names for the details and of the address for the address): ``{field name:
    [messages]}``, empty if the data passes. The forms of the management area use it as well, so that the dialog and the
    service check the same rules."""
    fields = GROUP_FIELDS[group]
    exclude = {field.name for field in type(account)._meta.fields} - set(fields)
    errors = {}
    try:
        account.clean_fields(exclude=exclude)
    except ValidationError as error:
        errors = _field_messages(error)
    extra = {GROUP_DETAILS: account.name_errors, GROUP_ADDRESS: account.address_errors}.get(group)
    for field, message in (extra() if extra else {}).items():
        errors.setdefault(field, []).append(message)
    return errors


def _validate_group(account, group):
    errors = group_errors(account, group)
    if errors:
        raise ValidationError(errors)


def _record_data_change(account, fields, actor):
    """The history entry and the security log event of a change of customer data: the names of the fields only."""
    history.record(account, HistoryEvent.PERSON_DATA_CHANGED, actor, {"fields": list(fields)})
    security_log.record(
        SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF, account=account, actor=actor,
        detail="fields " + ", ".join(fields),
    )


def update_customer_data(person, group, values, actor):
    """Change the data of the account in one group (``GROUP_FIELDS``) for support; return ``(account, changed
    fields)``.

    Only the fields of the group are written, and only those that changed (``update_fields``): the controlled fields
    of the account (D-034) are never touched, and the model checks them against the locked row again. The data is
    validated with the rules of the model (a ``ValidationError``). ``PersonError("not_allowed")`` if the actor may not
    change this account (``may_change_customer_data``). The postal code goes in as ``swiss_postal_code`` or
    ``foreign_postal_code`` (the form decides by the country, D-036)."""
    fields = GROUP_FIELDS.get(group)
    if fields is None or set(values) - set(fields):
        raise PersonError("fields_invalid")
    with transaction.atomic():
        fresh = _lock_person(person)
        _check_actor(actor, fresh)
        before = {name: getattr(fresh, name) for name in fields}
        for name, value in values.items():
            setattr(fresh, name, value)
        _validate_group(fresh, group)
        changed = [name for name in fields if getattr(fresh, name) != before[name]]
        if not changed:
            return fresh, []
        try:
            fresh.save(update_fields=changed)
        except AccountIntegrityError:
            raise PersonError("not_allowed") from None
        _record_data_change(fresh, changed, actor)
    return fresh, changed


UNSET = object()


def _given(value):
    """Whether a value of ``update_payment_data`` is given and not empty (it needs the side record)."""
    return value is not UNSET and bool(value)


def update_payment_data(person, actor, *, iban=UNSET, paper_invoice=UNSET, operator_customer_number=UNSET):
    """Change the IBAN, the paper invoice wish and the customer number at the grid operator in the side record
    (D-087); a value that is ``UNSET`` stays as it is. Returns the names of the changed fields.

    The number at the grid operator is data of the staff and may be changed for every person; the IBAN and the
    paper invoice are data of the customer and follow ``may_change_customer_data``. The IBAN is validated with the
    package (a ``ValidationError``) and stored encrypted (``community.iban``); neither the history nor the security
    log holds a value."""
    customer_data = iban is not UNSET or paper_invoice is not UNSET
    if iban is not UNSET:
        iban = iban_module.validate(iban)
    if operator_customer_number is not UNSET:
        operator_customer_number = " ".join((operator_customer_number or "").split())
        if len(operator_customer_number) > OPERATOR_NUMBER_MAX_LENGTH or not operator_customer_number.isprintable():
            raise PersonError("number_invalid")
    wanted = _given(iban) or paper_invoice is True or _given(operator_customer_number)
    with transaction.atomic():
        fresh = _lock_person(person)
        _actor_row(actor)
        if customer_data:
            _check_actor(actor, fresh)
        record = _locked_record(fresh, create=wanted)
        if record is None:
            return []
        changed = []
        if iban is not UNSET and iban != _current_iban(record):
            record.iban = iban
            changed.append("iban")
        if paper_invoice is not UNSET and bool(paper_invoice) != record.paper_invoice:
            record.paper_invoice = bool(paper_invoice)
            changed.append("paper_invoice")
        number_changed = (
            operator_customer_number is not UNSET and operator_customer_number != record.operator_customer_number
        )
        if number_changed:
            record.operator_customer_number = operator_customer_number
            changed.append("operator_customer_number")
        if not changed:
            return []
        record.save(update_fields=["iban_encrypted" if name == "iban" else name for name in changed] + ["updated_at"])
        if number_changed:
            history.record(fresh, HistoryEvent.PERSON_OPERATOR_NUMBER_CHANGED, actor)
        customer_changes = [name for name in changed if name != "operator_customer_number"]
        if customer_changes:
            _record_data_change(fresh, customer_changes, actor)
    return changed


def _current_iban(record):
    """The IBAN of the record, or None if it cannot be decrypted (then every value counts as a change, and a new one
    or an empty one replaces it)."""
    try:
        return record.iban
    except DecryptionError:
        return None


def change_second_email(request, person, address, actor):
    """Request a second email address for the account, or remove it with an empty ``address`` (D-087); return
    whether anything was requested or removed.

    The existing mechanism of the second address (D-059, ``accounts.email_change``) is used unchanged: the link goes
    to the **new address only**, the addresses that are valid now get the notice with the link that stops it, nothing
    changes before the owner of the new address confirms it, and the security log names the staff account. A
    removal takes effect at once and is notified to the addresses as always. Only for an active account (the link
    of a blocked account would not work). ``PersonError("rate_limited")`` if a limit of the mail requests refuses it.
    The login address is never changed here."""
    address = normalize_email_address(address) if (address or "").strip() else ""
    if address:
        try:
            validate_account_email(address)
        except ValueError:
            raise PersonError("email_invalid") from None
    with transaction.atomic():
        fresh = _lock_person(person)
        _check_actor(actor, fresh)
        if fresh.status != type(fresh).Status.ACTIVE:
            raise PersonError("not_active")
        if address and address == fresh.email:
            raise PersonError("email_invalid")
        if address == fresh.secondary_email or (not address and not fresh.secondary_email):
            return False
        try:
            if address:
                email_change.request_changes(request, fresh, secondary=address, actor=actor)
            else:
                email_change.request_changes(request, fresh, remove_secondary=True, actor=actor)
        except email_change.RequestRefused:
            raise PersonError("rate_limited") from None
        history.record(fresh, HistoryEvent.PERSON_DATA_CHANGED, actor, {"fields": ["secondary_email"]})
    return True
