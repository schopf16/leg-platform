"""Shared pieces of the onboarding services (D-103): the error type, caps, date arithmetic, the acting accounts.

Every service of the customer's side takes the **account** and finds its objects through it (``Application`` rows are
read with ``person=``), so an id that belongs to another account is "not found" and never reveals anything. Services of
the staff's side take an ``actor`` that is re-read from the database: an active staff account.
"""

import calendar
from datetime import timedelta

from django.contrib.auth import get_user_model

# Caps against a customer who creates many rows (D-103): the numbers are proposals of the developer.
MAX_DRAFTS_PER_PERSON = 3
MAX_OPEN_APPLICATIONS_PER_PERSON = 10
MAX_BUILDINGS_PER_APPLICATION = 20
MAX_UNRESOLVED_NOTICES_PER_PERSON = 20


class ApplicationError(Exception):
    """A step that the rules refuse. ``code`` names the reason; ``details`` carries facts the customer may see.

    Customer codes: ``not_allowed``, ``not_found``, ``not_editable``, ``too_many_applications``, ``too_many_drafts``,
    ``too_many_buildings``, ``already_in_progress``, ``building_complete``, ``building_unavailable`` (neutral: nothing
    about another account), ``directory_unavailable``, ``egid_invalid``, ``building_not_found``, ``ambiguous``,
    ``egid_not_offered``, ``already_added``, ``building_fixed``, ``direction_taken``, ``number_own``,
    ``number_in_application``, ``number_unavailable`` (neutral), ``prefix_not_configured``, ``contract_missing``,
    ``no_contract``, ``agreement_required``, ``version_changed``, ``already_accepted``, ``start_invalid``,
    ``step_not_available``, ``incomplete`` (``details["missing"]`` lists the open items), ``wrong_state``.
    Staff codes add: ``date_in_future``, ``date_before_start``, ``start_too_early``, ``number_exists``,
    ``cannot_terminate``, ``not_eligible``, ``nothing_to_deregister``, ``date_before_earliest``, ``not_month_end``;
    the corrections of D-109 (``application_corrections``): ``changed`` (the row is not the one the dialog showed),
    ``unchanged``, ``building_stored``, ``number_taken``, ``iban_required``, ``draw_required``, ``plant_data_swap``,
    ``direction_invalid``."""

    def __init__(self, code, **details):
        super().__init__(code)
        self.code = code
        self.details = details


def add_days(moment, days):
    """``moment`` plus whole days, or None if the result is beyond the range of dates (the value is "never").

    The settings have no upper limit of their own (owner), so a huge number must not raise."""
    try:
        return moment + timedelta(days=days)
    except OverflowError:
        return None


def subtract_days(moment, days):
    """``moment`` minus whole days, or None if the result is before the range of dates (nothing is that old)."""
    try:
        return moment - timedelta(days=days)
    except OverflowError:
        return None


def add_months(day, months):
    """The day ``months`` later; if that month is shorter the last day of it (31 January + 1 month = 28 February)."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def month_end(day):
    """The last day of the month of ``day``."""
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def staff_actor(actor):
    """The acting account, read again: an active staff account (a customer, a blocked or a deactivated one is refused)."""
    user_model = get_user_model()
    row = None
    if actor is not None and getattr(actor, "pk", None):
        row = user_model.objects.filter(pk=actor.pk, status=user_model.Status.ACTIVE, is_staff=True).first()
    if row is None:
        raise ApplicationError("not_allowed")
    return row


def notify(kind, user):
    """Queue the notification ``kind`` for every address of the account, but only for an ACTIVE account (a blocked or
    deactivated one gets no mail; the state change that goes with it still happens). Returns whether it was queued.
    Call it in the transaction of the change; the account is read again here."""
    from leg_platform.accounts import mail_queue

    user_model = get_user_model()
    fresh = user_model._base_manager.filter(pk=getattr(user, "pk", None)).first()
    if fresh is None or fresh.status != user_model.Status.ACTIVE:
        return False
    mail_queue.enqueue_for_account(kind, fresh)
    return True


def locked_customer(person):
    """The account read again under a lock: active and with a customer number (only such an account takes part)."""
    user_model = get_user_model()
    try:
        row = user_model._base_manager.select_for_update().get(pk=getattr(person, "pk", None))
    except user_model.DoesNotExist:
        raise ApplicationError("not_allowed") from None
    if row.status != user_model.Status.ACTIVE or row.customer_number_id is None:
        raise ApplicationError("not_allowed")
    return row
