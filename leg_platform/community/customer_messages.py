"""One-time messages for a customer (D-103): shown once on the next visit after an event that removed or changed the
application (a rejection, a missing confirmation of the grid operator).

A message holds a kind key, the optional short note the staff wrote and the times, nothing else (no address, number or
name). Rows are read and marked through the account, so an id of another account is never found. At most
``MAX_PER_PERSON`` rows are kept per account (the oldest go first). Seen messages are deleted after 30 days, unseen ones
after 180 (``cleanup``, run by the scheduler of the deadlines).
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from leg_platform.community.models import CustomerMessage, REJECTION_NOTE_MAX_LENGTH

Kind = CustomerMessage.Kind
MAX_PER_PERSON = 20
SEEN_KEPT_FOR = timedelta(days=30)
UNSEEN_KEPT_FOR = timedelta(days=180)


def record(person, kind, text="", now=None):
    """Write a message for the account; the caller holds the transaction of the event. ``kind`` must be a ``Kind``; the
    text is cut to the limit (it was checked by the staff service already). The oldest rows beyond the cap are deleted.
    Returns the message."""
    kind = Kind(kind).value
    now = now or timezone.now()
    message = CustomerMessage.objects.create(
        person_id=person.pk, kind=kind, text=(text or "")[:REJECTION_NOTE_MAX_LENGTH], created_at=now
    )
    surplus = CustomerMessage.objects.filter(person_id=person.pk).order_by("-created_at", "-pk").values_list(
        "pk", flat=True
    )[MAX_PER_PERSON:]
    stale = list(surplus)
    if stale:
        CustomerMessage.objects.filter(pk__in=stale).delete()
    return message


def messages_for(person):
    """The account's unseen messages, oldest first (a list of ``CustomerMessage``: ``kind``, ``text``,
    ``created_at``). Reading does not mark them: ``mark_seen`` does, after the page showed them."""
    return list(CustomerMessage.objects.filter(person_id=person.pk, seen_at__isnull=True))


def mark_seen(person, message_ids=None):
    """Mark the account's unseen messages as seen: the given ids only (ids of other accounts are ignored) or all of
    them. Returns how many were marked."""
    rows = CustomerMessage.objects.filter(person_id=person.pk, seen_at__isnull=True)
    if message_ids is not None:
        rows = rows.filter(pk__in=list(message_ids))
    with transaction.atomic():
        return rows.update(seen_at=timezone.now())


def purge(person):
    """The account is only deactivated (retention): delete its one-time messages, so that no free text of the staff
    outlives the account's use of the portal. Returns the number of messages deleted. Idempotent; the caller holds the
    transaction."""
    count, _by_model = CustomerMessage.objects.filter(person_id=person.pk).delete()
    return count


def cleanup(now=None):
    """Delete seen messages after 30 days and unseen ones after 180; returns the number deleted. Idempotent."""
    now = now or timezone.now()
    deleted = 0
    for rows in (
        CustomerMessage.objects.filter(seen_at__isnull=False, seen_at__lte=now - SEEN_KEPT_FOR),
        CustomerMessage.objects.filter(seen_at__isnull=True, created_at__lte=now - UNSEEN_KEPT_FOR),
    ):
        count, _by_model = rows.delete()
        deleted += count
    return deleted
