"""Notices for the staff (D-103): a customer was refused something without being told why, or an application was rolled
back at the grid operator's side and the registration there must be withdrawn.

A notice holds what the staff needs to act (account, EGID, metering point numbers) and nothing more; the numbers are
deleted when the notice is resolved. The same notice is written once while it is open and at most
``MAX_UNRESOLVED_NOTICES_PER_PERSON`` are kept open per account, so a customer cannot flood the staff.
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from leg_platform.community.application_common import MAX_UNRESOLVED_NOTICES_PER_PERSON, ApplicationError, staff_actor
from leg_platform.community.models import ApplicationNotice

Kind = ApplicationNotice.Kind


def record(person, kind, *, egid=None, numbers=()):
    """Write the notice unless the same one is open or the account has too many open ones; returns it or None.

    The notice ``OPERATOR_CONFIRMATION_LAPSED`` is exempt from the cap and from the deduplication and is always written:
    the application it names is deleted, so the notice is the staff's only way to learn which registration at the grid
    operator must be withdrawn."""
    kind = Kind(kind).value
    numbers = sorted({str(number) for number in numbers})
    user_model = get_user_model()
    must_write = kind == Kind.OPERATOR_CONFIRMATION_LAPSED
    with transaction.atomic():
        # The account row serialises the notices of one account.
        if not user_model._base_manager.select_for_update().filter(pk=person.pk).exists():
            return None
        if not must_write:
            open_rows = list(ApplicationNotice.objects.filter(person_id=person.pk, resolved_at__isnull=True))
            if len(open_rows) >= MAX_UNRESOLVED_NOTICES_PER_PERSON:
                return None
            if any(row.kind == kind and row.egid == egid and sorted(row.numbers) == numbers for row in open_rows):
                return None
        return ApplicationNotice.objects.create(person_id=person.pk, kind=kind, egid=egid, numbers=numbers)


def open_notices():
    """The unresolved notices, oldest first."""
    return ApplicationNotice.objects.filter(resolved_at__isnull=True).select_related("person")


def resolve(notice, actor):
    """The staff has dealt with the notice; its numbers are deleted. Resolving twice does nothing more."""
    staff = staff_actor(actor)
    with transaction.atomic():
        try:
            fresh = ApplicationNotice.objects.select_for_update().get(pk=notice.pk)
        except ApplicationNotice.DoesNotExist:
            raise ApplicationError("not_found") from None
        if fresh.resolved_at is None:
            fresh.resolved_at = timezone.now()
            fresh.resolved_by = staff
            fresh.numbers = []
            fresh.save(update_fields=["resolved_at", "resolved_by", "numbers"])
    return fresh
