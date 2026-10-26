"""What the staff may change about a person (D-081, D-087).

A person is an account with a customer number, or an invited person who has not completed the registration
(``person_filter``). Functions here:

* staff-only data in the side record ``PersonRecord`` (note, grid operator customer number, share history);
* the address correction suggested by the address directory;
* ``create_person`` / ``resend_invitation`` (D-087): the account has no password and no customer number until the
  person completes the registration (``accounts.invitations``);
* support changes of customer data (``update_customer_data``, ``update_payment_data``, ``change_second_email``).
  Never the login address, password, second factor, status, ``is_staff``, ``is_superuser``, groups, retention
  flag or customer number (D-012, D-034). Data of a staff user or administrator is changed only by an
  administrator, data of a deactivated account not at all.

Every change is one transaction that locks the account and record, and goes to the person history (and, for
customer data and invitations, to the security log). History and security log name **fields**, never values
(``history.record`` refuses sensitive keys). The customer is not notified (owner decision).
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

# Customer data groups the staff may change, by card of the detail page (D-087); no login, status, rights or number.
GROUP_DETAILS = "details"
GROUP_CONTACT = "contact"
GROUP_ADDRESS = "address"
GROUP_FIELDS = {
    GROUP_DETAILS: ("salutation", "organisation_name", "first_name", "last_name", "address_line_1"),
    GROUP_CONTACT: ("phone_number",),
    GROUP_ADDRESS: (*ADDRESS_FIELDS, "post_office_box_number"),
}


class PersonError(Exception):
    """A change that the rules refuse; ``code``: ``not_a_person``, ``note_too_long``, ``number_invalid``,
    ``shares_invalid``, ``date_invalid``, ``no_open_suggestion``, ``not_allowed``, ``email_in_use``,
    ``email_invalid``, ``rate_limited``, ``not_invited``, ``not_active``, ``fields_invalid``."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def person_filter():
    """The accounts that are persons (a ``Q``): with a customer number, or invited and not yet registered (D-087)."""
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
    """The side record under a lock; created only if ``create``, else None for a person without one."""
    if create:
        return PersonRecord.objects.select_for_update().get_or_create(person=person)[0]
    return PersonRecord.objects.select_for_update().filter(person=person).first()


def record_of(person):
    """The side record of the person, or None (reading never creates it)."""
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


def _clean_operator_number(number):
    number = " ".join((number or "").split())
    if len(number) > OPERATOR_NUMBER_MAX_LENGTH or not number.isprintable():
        raise PersonError("number_invalid")
    return number


def add_share_change(person, quantity, valid_from, actor=None):
    """Add a share history entry: ``quantity`` shares (whole number from 0) hold from ``valid_from``. Entries are
    never changed; a correction is a new entry (see ``current_shares``)."""
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
    """The share entries, latest date first (the model order)."""
    return list(ShareChange.objects.filter(record__person=person))


def current_shares(entries, today):
    """The quantity of the latest entry that holds on ``today`` (``entries`` from ``share_history``), or None
    (D-086)."""
    for entry in entries:
        if entry.valid_from <= today:
            return entry.quantity
    return None


def _address_of(person):
    return (person.street, person.house_number, str(person.swiss_postal_code or ""), person.town)


def _fits(suggestion):
    """Whether the suggestion fits the account columns and has a Swiss postal code."""
    user_model = get_user_model()
    limits = {name: user_model._meta.get_field(name).max_length for name in ("street", "house_number", "town")}
    return (
        len(suggestion.street) <= limits["street"]
        and len(suggestion.house_number) <= limits["house_number"]
        and len(suggestion.town) <= limits["town"]
        and parse_swiss_postal_code(suggestion.postal_code) is not None
    )


def address_suggestion(person):
    """The address directory's suggestion for the person's address, or None.

    Asked on every call. None for an inactive account, a non-Swiss or street-less address, a suggestion that does
    not fit the columns, or after "Do not accept" until street, house number, postal code or town changes."""
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
    """The digest of the shown suggestion: a decision is never made about a suggestion that changed meanwhile."""
    return addressing.address_digest(*suggestion.as_tuple())


def _current_suggestion(fresh, token):
    suggestion = address_suggestion(fresh)
    if suggestion is None or suggestion_token(suggestion) != token:
        raise PersonError("no_open_suggestion")
    return suggestion


def accept_address_suggestion(person, token, actor=None):
    """Replace the address by the suggestion named by ``token`` (re-checked under the lock, else
    ``PersonError("no_open_suggestion")``). The actor must pass ``may_change_customer_data`` (D-012, M-1), else
    ``PersonError("not_allowed")``. The security log names the changed fields."""
    with transaction.atomic():
        fresh = _lock_person(person)
        _check_actor(actor, fresh)
        suggestion = _current_suggestion(fresh, token)
        before = {name: getattr(fresh, name) for name in ADDRESS_FIELDS}
        fresh.street, fresh.house_number, postal_code, fresh.town = suggestion.as_tuple()
        fresh.swiss_postal_code = parse_swiss_postal_code(postal_code)
        fresh.foreign_postal_code = ""
        user_model = type(fresh)
        # Validate and write only the address fields.
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
        changed = [name for name in ADDRESS_FIELDS if getattr(fresh, name) != before[name]]
        security_log.record(
            SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF, account=fresh, actor=actor,
            detail="fields " + ", ".join(changed or ["address (suggestion accepted)"]),
        )
    return fresh


def dismiss_address_suggestion(person, token, actor=None):
    """"Do not accept": the warning is gone until the address changes. Same actor rule (M-1)."""
    with transaction.atomic():
        fresh = _lock_person(person)
        _check_actor(actor, fresh)
        _current_suggestion(fresh, token)
        record = _locked_record(fresh, create=True)
        record.suggestion_dismissed_for = addressing.address_signature(*_address_of(fresh))
        record.save(update_fields=["suggestion_dismissed_for", "updated_at"])
        history.record(fresh, HistoryEvent.ADDRESS_CORRECTION_DISMISSED, actor)
    return fresh


def _actor_row(actor):
    """The acting staff account, re-read: it must be an active staff account now."""
    user_model = get_user_model()
    row = None
    if actor is not None and getattr(actor, "pk", None):
        row = user_model.objects.filter(pk=actor.pk, status=user_model.Status.ACTIVE, is_staff=True).first()
    if row is None:
        raise PersonError("not_allowed")
    return row


def may_change_customer_data(actor, person):
    """Whether the actor may change this account's data (D-087, D-012): an active staff account, never for a
    deactivated account, and for a staff user or administrator (also one's own) only an administrator.
    Reads the objects only; the services check again under the lock."""
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
    """Create a person with the staff's data and invite them (D-087); return the account.

    ``values`` are the registration profile fields (``UserManager.INVITATION_PROFILE_FIELDS``). The account is
    unverified: no password, no customer number (issued at registration, D-021), no privileges. If a limit refuses
    the invitation email, nothing is created (``PersonError("rate_limited")``).

    ``email_invalid`` for an impossible address, ``email_in_use`` if another non-deactivated account holds it
    (an unverified self-registration does not hold it and is replaced, D-016). A ``ValidationError`` names data
    that fails the account rules."""
    _actor_row(actor)
    email = str(email or "").strip()
    try:
        validate_account_email(email)
    except ValueError:
        raise PersonError("email_invalid") from None
    normalized = normalize_email_address(email)
    user_model = get_user_model()
    with transaction.atomic():
        # Read without a lock like the registration: the address of a non-pending account is never locked.
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
    """Send the invitation of a not yet registered person again (D-087): the earlier link ends.
    ``PersonError("rate_limited")`` at a limit; ``PersonError("not_invited")`` for any other account."""
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
    """The invitation state of a not yet registered person (``expires_at``, ``sending``), else None."""
    if not invitations.is_pending(person):
        return None
    return {"expires_at": invitations.link_expires_at(person), "sending": invitations.is_being_sent(person)}


def _field_messages(error):
    return {field: [message for item in items for message in item.messages] for field, items in error.error_dict.items()}


def group_errors(account, group):
    """The account model's rules for the fields of one group: ``{field: [messages]}``, empty if valid.
    Shared with the management forms so dialog and service check the same rules."""
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
    """The history entry and security log event of a customer data change: field names only."""
    history.record(account, HistoryEvent.PERSON_DATA_CHANGED, actor, {"fields": list(fields)})
    security_log.record(
        SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF, account=account, actor=actor,
        detail="fields " + ", ".join(fields),
    )


def update_customer_data(person, group, values, actor):
    """Change the account's data in one group (``GROUP_FIELDS``) for support; return ``(account, changed fields)``.

    Only changed fields of the group are written (``update_fields``); the controlled account fields (D-034) are
    never touched. Validated with the model rules (``ValidationError``); ``PersonError("not_allowed")`` if the actor
    may not change this account. The postal code goes in as ``swiss_postal_code`` or ``foreign_postal_code`` (D-036)."""
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
    """Whether a value of ``update_payment_data`` is given and not empty (it then needs the side record)."""
    return value is not UNSET and bool(value)


def update_payment_data(person, actor, *, iban=UNSET, paper_invoice=UNSET, operator_customer_number=UNSET):
    """Change the IBAN, paper invoice wish and grid operator customer number in the side record (D-087); ``UNSET``
    stays as it is. Returns the changed field names.

    The operator number is staff data (any person); IBAN and paper invoice follow ``may_change_customer_data``.
    The IBAN is validated (``ValidationError``) and stored encrypted; no value reaches history or security log."""
    customer_data = iban is not UNSET or paper_invoice is not UNSET
    if iban is not UNSET:
        iban = iban_module.validate(iban)
    if operator_customer_number is not UNSET:
        operator_customer_number = _clean_operator_number(operator_customer_number)
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
        current = _current_iban(record)
        # An undecryptable IBAN is not touched by an empty value (M-2): the form shows it empty; only a valid IBAN replaces it.
        if iban is not UNSET and iban != current and not (current is None and iban == ""):
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
    """The record's IBAN, or None if it cannot be decrypted."""
    try:
        return record.iban
    except DecryptionError:
        return None


def change_second_email(request, person, address, actor):
    """Request a second email address (D-087), or remove it with an empty ``address``; return whether anything
    was requested or removed.

    Uses the second address flow unchanged (D-059, ``accounts.email_change``): the link goes to the new address
    only, nothing changes before it is confirmed; a removal takes effect at once. Only for an active account.
    ``PersonError("rate_limited")`` if a mail request limit refuses it. The login address is never changed."""
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
        _record_data_change(fresh, ["secondary_email"], actor)
    return True
