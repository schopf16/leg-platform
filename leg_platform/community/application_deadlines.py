"""The scheduled rules of the applications and deregistrations (D-103; KONZEPT 5.2, 5.9). ``process`` is run by
``manage.py process_application_deadlines`` (part of ``run_scheduled_tasks``, every 15 minutes).

Rules, each row in its own transaction with the application row locked and its condition checked again:

1. **Rollback of idle drafts.** A paused draft whose reminder is older than ``rollback_after_days`` is rolled back
   (rows deleted, mail "rolled back"). A draft that has been waiting for the contract for ``contract_deadline_days``
   is rolled back too (mail "contract: rolled back"). Only drafts: never a submitted or completed application, never the
   account, the IBAN or a stored building.
2. **Pause and remind.** A draft without a change for ``reminder_after_days`` is paused and reminded once (mail
   "reminder", or "contract reminder" while only the contract is missing). Any change of the customer ends the pause and
   the next idle period gets its own reminder; one run never pauses and rolls back the same draft.
3. **Bank connection missing.** A submitted application with a feed-in whose account has no readable IBAN after
   ``iban_missing_reminder_days`` gets one reminder per period of missing.
4. **Confirmation of the grid operator.** An application at the grid operator without a recorded confirmation after
   ``operator_confirmation_days`` is rolled back; the active staff accounts get the mail to withdraw the registration and
   a notice (never capped, never deduplicated) holds the numbers, because the application rows are gone.
5. **Deregistrations** whose confirmed date is reached become effective, with one mail each.
6. **Customer messages** (``customer_messages.cleanup``): seen ones are deleted after 30 days, unseen ones after 180.

Mails go only to ACTIVE accounts (``application_common.notify``); the state change happens anyway. Every state change and
its mail job are written in one transaction, so a second run changes nothing. A row that fails (a database error, a
rule that raises) is counted and logged by the class of the error only, and the run goes on with the next row; the command
exits with an error afterwards if any row failed. The log lines hold counts and times only.
"""

import logging
from collections import Counter

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from leg_platform.accounts.models import MailJob
from leg_platform.community import application_notices, customer_messages, deregistrations
from leg_platform.community.application_common import notify, subtract_days
from leg_platform.community.applications import delete_application
from leg_platform.community.models import Application, ApplicationMeteringPoint, Direction, ManagementSettings

logger = logging.getLogger(__name__)
State = Application.State


def _locked(pk, **conditions):
    # No join: the account row is never locked after the application row (customers lock the account first).
    return Application.objects.select_for_update().filter(pk=pk, **conditions).first()


def _ids(queryset):
    return list(queryset.values_list("pk", flat=True))


def _each(rule, pks, counts, handler):
    """Run ``handler(pk)`` in its own transaction for every id; a failing row is counted and logged by the class of its
    error and does not stop the others."""
    for pk in pks:
        try:
            with transaction.atomic():
                handler(pk)
        except Exception as error:  # noqa: BLE001 - only its class is logged
            counts["failed"] += 1
            logger.error("An application deadline row failed in rule %s (%s).", rule, type(error).__name__)


def _roll_back_draft(application, mail_kind, reason):
    delete_application(application, reason)
    notify(mail_kind, application.person)


def _rollbacks(now, values, counts):
    cut = subtract_days(now, values.rollback_after_days)
    if cut is not None:
        def paused(pk):
            row = _locked(pk, state=State.PAUSED, reminded_at__lte=cut)
            if row is None:
                return
            contract = row.contract_pending_since is not None
            _roll_back_draft(
                row, MailJob.Kind.CONTRACT_ROLLED_BACK if contract else MailJob.Kind.APPLICATION_ROLLED_BACK, "idle"
            )
            counts["rolled_back"] += 1

        _each("idle_rollback", _ids(Application.objects.filter(state=State.PAUSED, reminded_at__lte=cut)), counts, paused)
    cut = subtract_days(now, values.contract_deadline_days)
    if cut is not None:
        conditions = {
            "state__in": Application.DRAFT_STATES, "contract_pending_since__isnull": False,
            "contract_pending_since__lte": cut,
        }

        def contract(pk):
            row = _locked(pk, **conditions)
            if row is None:
                return
            _roll_back_draft(row, MailJob.Kind.CONTRACT_ROLLED_BACK, "contract_deadline")
            counts["rolled_back"] += 1

        _each("contract_deadline", _ids(Application.objects.filter(**conditions)), counts, contract)


def _reminders(now, values, counts):
    cut = subtract_days(now, values.reminder_after_days)
    if cut is None:
        return
    conditions = {"state": State.DRAFT, "reminded_at__isnull": True, "last_activity_at__lte": cut}

    def remind(pk):
        row = _locked(pk, **conditions)
        if row is None:
            return
        row.state = State.PAUSED
        row.reminded_at = now
        row.save(update_fields=["state", "reminded_at", "updated_at"])
        kind = (
            MailJob.Kind.CONTRACT_REMINDER if row.contract_pending_since is not None
            else MailJob.Kind.APPLICATION_REMINDER
        )
        notify(kind, row.person)
        counts["paused"] += 1

    _each("reminder", _ids(Application.objects.filter(**conditions)), counts, remind)


def _bank_connection(now, values, counts):
    from leg_platform.community import own_iban

    cut = subtract_days(now, values.iban_missing_reminder_days)
    open_states = Application.IN_FLIGHT_STATES
    feed_in = ApplicationMeteringPoint.objects.filter(
        direction=Direction.FEED_IN, building__application__state__in=open_states
    ).values("building__application_id")

    def check(pk):
        row = _locked(pk, state__in=open_states)
        if row is None:
            return
        if own_iban.has_iban(row.person):
            if row.iban_reminded_at is not None:
                row.iban_reminded_at = None
                row.save(update_fields=["iban_reminded_at", "updated_at"])
            return
        if cut is None or row.iban_reminded_at is not None or row.submitted_at is None or row.submitted_at > cut:
            return
        row.iban_reminded_at = now
        row.save(update_fields=["iban_reminded_at", "updated_at"])
        notify(MailJob.Kind.BANK_CONNECTION_MISSING, row.person)
        counts["bank_reminded"] += 1

    _each("bank_connection", _ids(Application.objects.filter(state__in=open_states, pk__in=feed_in)), counts, check)


def _operator_lapses(now, values, counts):
    cut = subtract_days(now, values.operator_confirmation_days)
    if cut is None:
        return
    conditions = {"state": State.OPERATOR_STARTED, "operator_started_at__lte": cut}
    user_model = get_user_model()
    staff = list(user_model.objects.filter(is_staff=True, status=user_model.Status.ACTIVE))

    def lapse(pk):
        person_id = Application.objects.filter(pk=pk).values_list("person_id", flat=True).first()
        if person_id is None:
            return
        # The account first, then the application (the order of the customer's services).
        person = user_model._base_manager.select_for_update().get(pk=person_id)
        row = _locked(pk, **conditions)
        if row is None:
            return
        numbers = list(
            ApplicationMeteringPoint.objects.filter(building__application=row).values_list("number", flat=True)
        )
        notice = application_notices.record(person, application_notices.Kind.OPERATOR_CONFIRMATION_LAPSED, numbers=numbers)
        if notice is None:
            # The notice is the only trace of the registration to withdraw: without it nothing is deleted.
            raise RuntimeError("The notice could not be written.")
        delete_application(row, "operator_not_confirmed")
        customer_messages.record(person, customer_messages.Kind.OPERATOR_NOT_CONFIRMED)
        for account in staff:
            notify(MailJob.Kind.OPERATOR_CONFIRMATION_LAPSED, account)
        counts["operator_lapsed"] += 1

    _each("operator_confirmation", _ids(Application.objects.filter(**conditions)), counts, lapse)


def process(now=None, today=None):
    """Run every rule once; returns the counts (``rolled_back``, ``paused``, ``bank_reminded``, ``operator_lapsed``,
    ``deregistrations_effective``, ``failed``). A second run right after the first changes nothing."""
    now = now or timezone.now()
    today = today or timezone.localdate(now)
    values = ManagementSettings.load()
    counts = Counter(
        rolled_back=0, paused=0, bank_reminded=0, operator_lapsed=0, deregistrations_effective=0, messages_deleted=0,
        failed=0,
    )
    _rollbacks(now, values, counts)
    _reminders(now, values, counts)
    _bank_connection(now, values, counts)
    _operator_lapses(now, values, counts)
    failures = []
    counts["deregistrations_effective"] = deregistrations.finish_due(today, failures=failures)
    counts["failed"] += len(failures)
    try:
        counts["messages_deleted"] = customer_messages.cleanup(now)
    except Exception as error:  # noqa: BLE001 - only its class is logged
        counts["failed"] += 1
        logger.error("Old customer messages could not be deleted (%s).", type(error).__name__)
    logger.info(
        "Application deadlines at %s: rolled back %d, reminded %d, bank connection reminders %d, "
        "operator confirmation lapsed %d, deregistrations effective %d, messages deleted %d, failed rows %d.",
        now.isoformat(timespec="seconds"), counts["rolled_back"], counts["paused"], counts["bank_reminded"],
        counts["operator_lapsed"], counts["deregistrations_effective"], counts["messages_deleted"], counts["failed"],
    )
    return dict(counts)
