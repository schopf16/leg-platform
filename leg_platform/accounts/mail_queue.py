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
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.utils import timezone

from leg_platform.accounts import (
    emails,
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


def _notification(sender):
    """A handler for an email that tells the owner of an account about a change."""
    return lambda address: lambda: sender(address)


HANDLERS = {
    MailJob.Kind.REGISTRATION: registration.process_registration,
    MailJob.Kind.PASSWORD_RESET: password_reset.process_reset_request,
    MailJob.Kind.PASSWORD_CHANGED: password_reset.process_password_changed,
    MailJob.Kind.EMAIL_CHANGED: _notification(emails.send_email_changed),
    MailJob.Kind.TWO_FACTOR_ENABLED: _notification(emails.send_two_factor_enabled),
    MailJob.Kind.TWO_FACTOR_DISABLED: _notification(emails.send_two_factor_disabled),
    MailJob.Kind.RECOVERY_CODES_RENEWED: _notification(emails.send_recovery_codes_renewed),
    MailJob.Kind.RECOVERY_CODE_USED: _notification(emails.send_recovery_code_used),
    MailJob.Kind.CODE_ATTEMPTS_WARNING: _notification(emails.send_code_attempts_warning),
    MailJob.Kind.TWO_FACTOR_RESET: _notification(emails.send_two_factor_reset),
}

# Handlers of jobs about an account get the account key and the address of the request (D-044).
ACCOUNT_HANDLERS = {
    MailJob.Kind.TWO_FACTOR_RESET_LINK: second_factor_reset.process_reset_link,
}

DONE = "done"
FAILED = "failed"


def submit_mail_request(request, kind, email, *, account=None):
    """Count a request for an email and store the job; the same work for every address.

    The request is counted at the address, the client IP address and the whole instance only if
    all three limits have room, so that a client that is refused cannot use up the budget of
    someone else's address. A refused request is stored as a suppressed job without the address.
    """
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


def enqueue(kind, address, account=None):
    """Queue an email that no request limit applies to (e.g. a notification)."""
    MailJob.objects.create(
        kind=kind, address=address, account=account, next_attempt_at=timezone.now()
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
    """Take the next due job; return (id, kind, address, account id), DONE/FAILED if discarded."""
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
        return job.pk, job.kind, job.address, job.account_id


def _process_next():
    claimed = _claim()
    if claimed is None or isinstance(claimed, str):
        return claimed
    job_id, kind, address, account_id = claimed
    try:
        with transaction.atomic():
            if kind == MailJob.Kind.EMAIL_CHANGE_REQUEST:
                # Imported only while processing to avoid a module cycle: email_change enqueues
                # the notification through this module after it commits the address change.
                from leg_platform.accounts import email_change

                send = email_change.process_request(account_id, address)
            elif kind in ACCOUNT_HANDLERS:
                send = ACCOUNT_HANDLERS[kind](account_id, address)
            else:
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
