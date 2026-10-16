"""Emails that are sent in the background (outbox; docs/SECURITY.md, section 3; D-037).

The request path of registration and password reset does the same work for every address: it
counts the request against the limits and stores one job. It makes no lookup and sends nothing,
so neither the response nor its time depends on whether an account exists. A worker
(``manage.py process_mail_queue``) then looks up the address, decides and sends. Raw tokens are
created by the worker and sent at once; they are never stored in a job.

The worker takes a job in four steps, so that no database lock is held while an email is sent
and a failing step needs no further database write:

1. claim: under a lock the job's attempt counter is raised and its next due time is moved
   forward (a lease, at the same time the delay of a retry); other workers skip it meanwhile;
2. decide: the handler looks the address up and stores what it needs (a pending account and its
   token) in one transaction, and returns the function that sends the email, or nothing;
3. send, outside any transaction;
4. delete the job.

A crash or error before step 4 leaves the job, which is repeated after the delay; delivery is
at least once. After ``MAX_ATTEMPTS`` the job is given up.
"""

import logging
import time
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.utils import timezone

from leg_platform.accounts import (
    emails,
    greeting,
    password_reset,
    rate_limits,
    registration,
    second_factor_reset,
)
from leg_platform.accounts.login_protection import axes_client_ip
from leg_platform.accounts.models import MailJob
from leg_platform.accounts.normalization import normalize_email_address

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# The delay before an attempt is repeated is this value times the number of attempts so far; it
# also keeps the job away from other workers while it is processed.
RETRY_DELAY = timedelta(minutes=2)
# Jobs that were never processed are deleted after this time; they hold addresses.
STALE_AFTER = timedelta(hours=1)
NOTIFICATION_STALE_AFTER = timedelta(hours=24)


def _notification(sender, greets=True):
    """A handler for an email that tells the owner of an account about a change.

    The handler gets the cleaned name that was stored in the job (D-068). The notice about a
    deleted account passes no name on: it is always neutral.
    """
    if greets:
        return lambda address, greeting_name="": lambda: sender(address, greeting_name)
    return lambda address, greeting_name="": lambda: sender(address)


HANDLERS = {
    MailJob.Kind.REGISTRATION: registration.process_registration,
    MailJob.Kind.PASSWORD_RESET: password_reset.process_reset_request,
    MailJob.Kind.PASSWORD_CHANGED: password_reset.process_password_changed,
    MailJob.Kind.EMAIL_CHANGED: _notification(emails.send_email_changed),
    MailJob.Kind.SECONDARY_EMAIL_CHANGED: _notification(emails.send_secondary_email_changed),
    MailJob.Kind.SESSION_ENDED_WRONG_CODES: _notification(emails.send_session_ended_wrong_codes),
    MailJob.Kind.ALL_SESSIONS_ENDED: _notification(emails.send_all_sessions_ended),
    MailJob.Kind.TWO_FACTOR_ENABLED: _notification(emails.send_two_factor_enabled),
    MailJob.Kind.TWO_FACTOR_DISABLED: _notification(emails.send_two_factor_disabled),
    MailJob.Kind.RECOVERY_CODES_RENEWED: _notification(emails.send_recovery_codes_renewed),
    MailJob.Kind.RECOVERY_CODE_USED: _notification(emails.send_recovery_code_used),
    MailJob.Kind.CODE_ATTEMPTS_WARNING: _notification(emails.send_code_attempts_warning),
    MailJob.Kind.TWO_FACTOR_RESET: _notification(emails.send_two_factor_reset),
    MailJob.Kind.ACCOUNT_DELETED: _notification(emails.send_account_deleted, greets=False),
}

# Handlers of jobs about an account get the account key and the address of the request (D-044).
ACCOUNT_HANDLERS = {
    MailJob.Kind.TWO_FACTOR_RESET_LINK: second_factor_reset.process_reset_link,
}

DONE = "done"
FAILED = "failed"


_REJECTION_LOG_INTERVAL = 60.0
_last_rejection_log = float("-inf")


def _log_rejection():
    """Log that requests are refused, at most once a minute (a flood must not flood the log)."""
    global _last_rejection_log
    now = time.monotonic()
    if now - _last_rejection_log >= _REJECTION_LOG_INTERVAL:
        _last_rejection_log = now
        logger.warning("Requests that send email are refused: the client or instance limit is used up.")


def requests_refused(request, kind):
    """Whether the client IP address or the instance has used up its limit for this kind of request.

    Read only and before anything is stored (security review of phase 3, A1). Only these two
    limits are ever shown to the person (HTTP 429): they do not depend on an account, so no answer
    tells whether an address has an account; the limit of an address stays silent (D-038).
    """
    client_ip = axes_client_ip(request)
    refused = rate_limits.exhausted(
        [
            (rate_limits.SCOPE_MAIL_IP, rate_limits.ip_key(client_ip), rate_limits.MAIL_IP_LIMITS),
            (rate_limits.SCOPE_MAIL_GLOBAL, str(kind), rate_limits.MAIL_GLOBAL_LIMITS),
        ]
    )
    if refused:
        _log_rejection()
    return refused


def submit_mail_request(request, kind, email, *, account=None):
    """Count a request for an email and store the job; the same work for every address.

    Returns False without storing anything if the limit of the client IP address or of the
    instance is used up: the caller shows the refusal. Otherwise the request is counted at the
    address, the client IP address and the whole instance only if all three limits have room, so
    that a client that is refused cannot use up the budget of someone else's address. A request
    above the limit of the address is stored as a suppressed job without the address, so that the
    answer and its time stay the same (D-038).
    """
    if requests_refused(request, kind):
        return False
    address = normalize_email_address(email)
    client_ip = axes_client_ip(request)
    if not client_ip:
        logger.warning("The client IP address could not be determined; requests share one limit.")
    results = rate_limits.consume_all(
        [
            (rate_limits.SCOPE_MAIL_ADDRESS, address, rate_limits.MAIL_ADDRESS_LIMITS),
            (rate_limits.SCOPE_MAIL_IP, rate_limits.ip_key(client_ip), rate_limits.MAIL_IP_LIMITS),
            # One budget per kind, so that requests of one kind cannot use up the other's.
            (rate_limits.SCOPE_MAIL_GLOBAL, str(kind), rate_limits.MAIL_GLOBAL_LIMITS),
        ]
    )
    if not results[2]:
        logger.warning("The instance-wide limit of requests that send email was reached.")
    suppressed = not all(results)
    MailJob.objects.create(
        kind=kind,
        address="" if suppressed else address,
        account=None if suppressed else account,
        suppressed=suppressed,
        next_attempt_at=timezone.now(),
    )
    return True


def enqueue(kind, address, account=None, greeting_name=""):
    """Queue an email that no request limit applies to (e.g. a notification).

    ``greeting_name`` is the cleaned name for the greeting of a notification (D-068); callers
    other than ``enqueue_for_account`` leave it empty, which means the neutral greeting. A name
    for any other kind (a request, which has no account) or for the notice about a deleted
    account is dropped here, so that no caller can store a name that is never used.
    """
    if kind not in MailJob.NOTIFICATION_KINDS or kind == MailJob.Kind.ACCOUNT_DELETED:
        greeting_name = ""
    MailJob.objects.create(
        kind=kind,
        address=address,
        account=account,
        greeting_name=greeting_name,
        next_attempt_at=timezone.now(),
    )


# The jobs that keep the account key, because the worker makes a personal link for them.
ACCOUNT_KEY_KINDS = (MailJob.Kind.EMAIL_CHANGE_NOTICE, MailJob.Kind.TWO_FACTOR_RESET_LINK)


def enqueue_for_account(kind, user, addresses=None):
    """Queue the same notification for every address of the account (D-059).

    The login address and the confirmed second address by default, or the addresses that the
    caller names (e.g. the addresses before a change). One job per address: a failing delivery to
    one address does not hold back the other. A notice about a requested change and the link to
    remove the second factor keep the account key, because the worker makes a personal link.

    This is the single place that captures the name for the greeting (D-068): the cleaned name
    of the account at this time, stored in the job, and only for an address that belongs to the
    account now (so not for the former login address, a removed second address, or any address
    after the account was deleted). The notice about a deleted account never gets a name.
    """
    with_account = user if kind in ACCOUNT_KEY_KINDS else None
    name = ""
    current = set()
    if kind in MailJob.NOTIFICATION_KINDS and kind != MailJob.Kind.ACCOUNT_DELETED:
        name = greeting.name_for(user)
        current = {normalize_email_address(address) for address in user.notification_addresses()}
    for address in addresses if addresses is not None else user.notification_addresses():
        belongs = normalize_email_address(address) in current
        enqueue(
            kind,
            address,
            account=with_account,
            greeting_name=name if belongs else "",
        )


def process_pending(limit=50):
    """Process due jobs; return the numbers of done and failed ones."""
    done = failed = 0
    for _index in range(limit):
        try:
            outcome = _process_next()
        except DatabaseError as error:
            # E.g. a deadlock or a lost connection; the job stays and is repeated.
            logger.error("The mail queue could not be processed (%s).", type(error).__name__)
            failed += 1
            break
        if outcome is None:
            break
        if outcome == DONE:
            done += 1
        else:
            failed += 1
    return done, failed


def _claim():
    """Take the next due job; return (id, kind, address, account id, greeting name), or
    DONE/FAILED if a job was discarded. The name is personal data and is never logged."""
    now = timezone.now()
    with transaction.atomic():
        job = (
            MailJob.objects.select_for_update(skip_locked=True)
            .filter(next_attempt_at__lte=now)
            .order_by("pk")
            .first()
        )
        if job is None:
            return None
        if job.suppressed:
            job.delete()
            return DONE
        if job.attempts >= MAX_ATTEMPTS:
            logger.error("A mail job (%s) was given up after %d attempts.", job.kind, job.attempts)
            job.delete()
            return FAILED
        job.attempts += 1
        job.next_attempt_at = now + RETRY_DELAY * job.attempts
        job.save(update_fields=["attempts", "next_attempt_at"])
        return job.pk, job.kind, job.address, job.account_id, job.greeting_name


def _process_next():
    claimed = _claim()
    if claimed is None or isinstance(claimed, str):
        return claimed
    job_id, kind, address, account_id, greeting_name = claimed
    try:
        with transaction.atomic():
            if kind == MailJob.Kind.EMAIL_CHANGE_REQUEST:
                # Imported only while processing to avoid a module cycle: email_change enqueues
                # the notification through this module after it commits the address change.
                from leg_platform.accounts import email_change

                send = email_change.process_request(account_id, address)
            elif kind == MailJob.Kind.SECONDARY_EMAIL_REQUEST:
                from leg_platform.accounts import email_change

                send = email_change.process_secondary_request(account_id, address)
            elif kind == MailJob.Kind.EMAIL_CHANGE_NOTICE:
                from leg_platform.accounts import email_change

                send = email_change.process_notice(account_id, address, greeting_name)
            elif kind in ACCOUNT_HANDLERS:
                send = ACCOUNT_HANDLERS[kind](account_id, address, greeting_name)
            elif kind in MailJob.NOTIFICATION_KINDS:
                send = HANDLERS[kind](address, greeting_name)
            else:
                # Requests of persons without an account never get a name (D-068).
                send = HANDLERS[kind](address)
        if send is not None:
            send()
    except Exception as error:
        # Only the class is logged: the message could contain the address or a link. The job
        # was already marked at the claim, so nothing more is written here.
        logger.error("A mail job (%s) failed (%s).", kind, type(error).__name__)
        return FAILED
    MailJob.objects.filter(pk=job_id).delete()
    return DONE


def delete_stale_jobs(now=None):
    """Delete jobs that were never processed; requests hold addresses of persons without accounts.

    A notification belongs to a known account and is kept longer, so that a worker that was
    down for some hours does not lose a security notification.
    """
    now = now or timezone.now()
    count, _ = (
        MailJob.objects.exclude(kind__in=MailJob.NOTIFICATION_KINDS)
        .filter(created_at__lt=now - STALE_AFTER)
        .delete()
    )
    notifications, _ = MailJob.objects.filter(
        kind__in=MailJob.NOTIFICATION_KINDS, created_at__lt=now - NOTIFICATION_STALE_AFTER
    ).delete()
    return count + notifications
