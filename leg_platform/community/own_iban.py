"""The customer's own IBAN (D-087 amended by D-103): the account itself sees it in full and changes it.

* No password and no second factor (owner decision, KONZEPT 7.2). The checks are the full IBAN validation
  (``community.iban``, python-stdnum), an active account with a customer number, and a limit of changes per hour.
* Every change writes the security event ``iban_changed`` and the history entry ``person_iban_changed`` (neither names the
  value). The mail ``iban_changed`` goes to **every** address of the account on a change of an existing IBAN and on its
  removal, **not** on the first entry from empty (owner decision 2026-10-19). The mail text never holds the IBAN.
* An empty value is refused by ``set_own_iban``; ``remove_own_iban`` removes the bank connection.
* The IBAN stays encrypted at rest and bound to the account (``PersonRecord.iban``).
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from leg_platform.accounts import mail_queue, security_log
from leg_platform.accounts.encryption import DecryptionError
from leg_platform.accounts.models import MailJob, SecurityEvent
from leg_platform.community import history, persons
from leg_platform.community import iban as iban_module
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import PersonRecord

# At most this many changes per account and hour: every change sends mail, so the limit also protects the inbox.
MAX_CHANGES_PER_HOUR = 5


def own_iban(person):
    """The IBAN in normal form ("" if none or if it cannot be decrypted)."""
    record = PersonRecord.objects.filter(person=person).first()
    if record is None:
        return ""
    try:
        return record.iban
    except DecryptionError:
        return ""


def own_iban_display(person):
    """The IBAN in groups of four ("CH93 0076 2011 6238 5295 7"), "" if none. Shown only to the account itself."""
    return iban_module.display(own_iban(person))


def has_iban(person):
    return bool(own_iban(person))


def accounts_with_iban(person_ids):
    """The ids among ``person_ids`` that have an IBAN (one query; an IBAN that cannot be decrypted counts as none, like
    ``own_iban``)."""
    found = set()
    for record in PersonRecord.objects.filter(person_id__in=set(person_ids)):
        try:
            if record.iban:
                found.add(record.person_id)
        except DecryptionError:
            continue
    return found


def _record_change(fresh, record, mail):
    """Security event and history (never the value); the mail to every address only for a change of an existing IBAN
    or its removal, not for the first entry from empty (owner decision 2026-10-19)."""
    history.record(fresh, HistoryEvent.PERSON_IBAN_CHANGED, fresh)
    security_log.record(SecurityEvent.Event.IBAN_CHANGED, account=fresh, actor=fresh)
    if mail:
        mail_queue.enqueue_for_account(MailJob.Kind.IBAN_CHANGED, fresh)


def _check_rate(fresh):
    recent = SecurityEvent.objects.filter(
        event=SecurityEvent.Event.IBAN_CHANGED, account_id=fresh.pk,
        occurred_at__gte=timezone.now() - timedelta(hours=1),
    ).count()
    if recent >= MAX_CHANGES_PER_HOUR:
        raise persons.PersonError("rate_limited")


def set_own_iban(person, value):
    """Store the IBAN of the account itself; returns whether it changed.

    ``ValidationError`` (code ``invalid_iban``) for an invalid IBAN; ``PersonError("iban_required")`` for an empty one
    (``remove_own_iban`` removes), ``PersonError("rate_limited")`` beyond ``MAX_CHANGES_PER_HOUR`` changes in an hour,
    ``PersonError("not_a_person")`` or ``PersonError("not_active")`` for an account that takes no part. The first entry
    from empty writes the security event and the history only; a change of an existing IBAN also mails every address."""
    number = iban_module.validate(value)
    if not number:
        raise persons.PersonError("iban_required")
    with transaction.atomic():
        fresh = persons._lock_person(person)
        if fresh.status != fresh.Status.ACTIVE:
            raise persons.PersonError("not_active")
        record = persons._locked_record(fresh, create=True)
        try:
            current = record.iban
        except DecryptionError:
            current = None
        if current == number:
            return False
        _check_rate(fresh)
        # An IBAN that is stored but cannot be read counts as an existing one.
        had_one = bool(record.iban_encrypted)
        record.iban = number
        record.save(update_fields=["iban_encrypted", "updated_at"])
        _record_change(fresh, record, mail=had_one)
    return True


def remove_own_iban(person):
    """Remove the IBAN of the account itself; returns whether there was one. Event, history and the mail to every
    address, like any change of an existing IBAN."""
    with transaction.atomic():
        fresh = persons._lock_person(person)
        if fresh.status != fresh.Status.ACTIVE:
            raise persons.PersonError("not_active")
        record = persons._locked_record(fresh, create=False)
        if record is None or not record.iban_encrypted:
            return False
        _check_rate(fresh)
        record.iban_encrypted = ""
        record.save(update_fields=["iban_encrypted", "updated_at"])
        _record_change(fresh, record, mail=True)
    return True
