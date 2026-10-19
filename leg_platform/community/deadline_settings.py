"""The reminders and deadlines of the applications (D-103): the numbers in ``ManagementSettings`` and the warning before
a change that would delete drafts or applications at the grid operator.

Whole days, at least 1, at most ``MAX_DAYS`` (the column's range; a date beyond year 9999 simply never comes:
``application_common.add_days``). A change is written to the history with the **field names** and the actor, never a
value. ``affected_counts`` gives the two numbers for the warning "this change rolls back N drafts and M applications at
the grid operator"; no names and no addresses.
"""

from datetime import time

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.community import history
from leg_platform.community.application_common import staff_actor, subtract_days
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Application, ManagementSettings

FIELDS = (
    "reminder_after_days",
    "rollback_after_days",
    "contract_deadline_days",
    "operator_confirmation_days",
    "iban_missing_reminder_days",
)
WHOLE_NUMBER_MESSAGE = _("Enter a whole number of days, at least 1.")
# The column is an unsigned 32-bit integer (``PositiveIntegerField`` on MariaDB): the largest value that fits.
MAX_DAYS = 4_294_967_295
MAX_DIGITS = len(str(MAX_DAYS))


def _whole_days(value):
    """A value as the form sends it: an ``int`` (not a bool) or a string of ASCII digits; from 1 up to ``MAX_DAYS``.
    Anything else is a ``ValidationError`` (never another exception: a string is limited before ``int()``, which
    refuses very long digit strings itself)."""
    if isinstance(value, bool):
        raise ValidationError(WHOLE_NUMBER_MESSAGE, code="invalid_days")
    if isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdigit()) or len(text.lstrip("0")) > MAX_DIGITS:
            raise ValidationError(WHOLE_NUMBER_MESSAGE, code="invalid_days")
        value = int(text) if len(text) <= 4000 else 0
    if not isinstance(value, int) or not 1 <= value <= MAX_DAYS:
        raise ValidationError(WHOLE_NUMBER_MESSAGE, code="invalid_days")
    return value


def clean_values(values):
    """The given fields (a subset of ``FIELDS``) as whole days; ``ValidationError`` with the field name as key."""
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise ValueError("Unknown deadline fields.")
    errors = {}
    cleaned = {}
    for name, value in values.items():
        try:
            cleaned[name] = _whole_days(value)
        except ValidationError as error:
            errors[name] = error
    if errors:
        raise ValidationError(errors)
    return cleaned


def current_values():
    row = ManagementSettings.load()
    return {name: getattr(row, name) for name in FIELDS}


def address_run_time():
    """The time of day of the daily address directory run (``addresses.schedule``, KONZEPT 3a.5)."""
    from leg_platform.addresses import schedule

    return schedule.import_run_time()


def _store_run_time(row, new, staff):
    """Write the run time (the caller holds the transaction and the settings row lock); returns whether it changed."""
    from leg_platform.addresses import schedule

    if schedule.import_run_time().replace(second=0, microsecond=0, tzinfo=None) == new:
        return False
    schedule.set_import_run_time(new)
    history.record(row, HistoryEvent.IMPORT_RUN_TIME_CHANGED, staff)
    return True


def set_address_run_time(value, actor):
    """Store the time of the daily address directory run (whole minutes) for an active staff account; returns whether
    it changed. History ``import_run_time_changed`` (no value). ``ValueError`` if ``value`` is not a time of day."""
    staff = staff_actor(actor)
    new = value.replace(second=0, microsecond=0, tzinfo=None) if isinstance(value, time) else None
    if new is None:
        raise ValueError("A time of day is required.")
    with transaction.atomic():
        ManagementSettings.objects.get_or_create(id=1)
        row = ManagementSettings.objects.select_for_update().get(id=1)
        return _store_run_time(row, new, staff)


def _would_delete(now, values):
    """The ids ``(drafts, at_operator)`` that the next run would roll back under ``values`` (a complete set of the
    fields): drafts that lie idle or whose contract deadline lapsed, and applications at the grid operator whose
    confirmation did not arrive in time."""
    ids = set()
    rollback_cut = subtract_days(now, values["rollback_after_days"])
    if rollback_cut is not None:
        ids |= set(
            Application.objects.filter(state=Application.State.PAUSED, reminded_at__lte=rollback_cut)
            .values_list("pk", flat=True)
        )
    contract_cut = subtract_days(now, values["contract_deadline_days"])
    if contract_cut is not None:
        ids |= set(
            Application.objects.filter(
                state__in=Application.DRAFT_STATES, contract_pending_since__isnull=False,
                contract_pending_since__lte=contract_cut,
            ).values_list("pk", flat=True)
        )
    at_operator = set()
    operator_cut = subtract_days(now, values["operator_confirmation_days"])
    if operator_cut is not None:
        at_operator = set(
            Application.objects.filter(
                state=Application.State.OPERATOR_STARTED, operator_started_at__lte=operator_cut
            ).values_list("pk", flat=True)
        )
    return ids, at_operator


class Affected:
    """What a change of the deadlines would roll back: ``drafts`` (not yet submitted) and ``at_operator`` (submitted and
    already at the grid operator). Two numbers only. ``token`` is the short text that the confirmation step carries."""

    def __init__(self, drafts=0, at_operator=0):
        self.drafts, self.at_operator = drafts, at_operator

    def __bool__(self):
        return bool(self.drafts or self.at_operator)

    def __eq__(self, other):
        return isinstance(other, Affected) and (self.drafts, self.at_operator) == (other.drafts, other.at_operator)

    def __hash__(self):
        return hash((self.drafts, self.at_operator))

    def __repr__(self):
        return f"Affected(drafts={self.drafts}, at_operator={self.at_operator})"

    @property
    def token(self):
        return f"{self.drafts}:{self.at_operator}"


class CountChanged(Exception):
    """The numbers of the warning are not the ones the person confirmed (the data moved meanwhile, or none was shown):
    ask again with ``affected``."""

    def __init__(self, affected):
        super().__init__("count_changed")
        self.affected = affected


def affected_counts(new_values, now=None):
    """The ``Affected`` numbers that the next scheduler run would roll back **because of** the new values and not under
    the current ones. Numbers only; a draft that is only reminded is not counted."""
    now = now or timezone.now()
    current = current_values()
    proposed = {**current, **clean_values(new_values)}
    new_drafts, new_operator = _would_delete(now, proposed)
    old_drafts, old_operator = _would_delete(now, current)
    return Affected(len(new_drafts - old_drafts), len(new_operator - old_operator))


def affected_drafts_count(new_values, now=None):
    """The number of drafts only (``affected_counts(...).drafts``)."""
    return affected_counts(new_values, now).drafts


def update(values, actor, *, expected=None, run_time=None, now=None):
    """Save the given fields (a subset of ``FIELDS``) and, if given, the time of the daily address run, in ONE
    transaction; returns the names that changed (the run time is not a field of this list).

    A change that would roll back drafts or applications needs the confirmation: ``expected`` is the ``Affected.token``
    that the person saw. The numbers are computed again AFTER the row lock; if they differ from ``expected`` (or none was
    given) ``CountChanged`` is raised and nothing is saved. History ``deadlines_changed`` with the names only.
    ``ValidationError`` for anything that is not a whole number from 1 up to ``MAX_DAYS``."""
    staff = staff_actor(actor)
    cleaned = clean_values(values)
    new_time = None
    if run_time is not None:
        new_time = run_time.replace(second=0, microsecond=0, tzinfo=None) if isinstance(run_time, time) else None
        if new_time is None:
            raise ValueError("A time of day is required.")
    with transaction.atomic():
        ManagementSettings.objects.get_or_create(id=1)
        row = ManagementSettings.objects.select_for_update().get(id=1)
        changed = [name for name, value in cleaned.items() if getattr(row, name) != value]
        if changed:
            affected = affected_counts(cleaned, now)
            if affected and affected.token != expected:
                raise CountChanged(affected)
            for name in changed:
                setattr(row, name, cleaned[name])
            row.full_clean()
            row.save(update_fields=[*changed, "updated_at"])
            history.record(row, HistoryEvent.DEADLINES_CHANGED, staff, {"fields": changed})
        if new_time is not None:
            _store_run_time(row, new_time, staff)
    return changed
