"""Run every periodic cleanup task once (D-041); schedule it, e.g. every 15 minutes.

The tasks are independent: a failing task does not stop the others, and the command exits with an
error if any failed. ``process_mail_queue --loop`` is not included (a supervised service).
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

# A test checks that every cleanup command of the accounts app is listed here.
TASKS = (
    "delete_expired_registrations",
    # The reminders and deadlines of the customers' applications (community app, D-103).
    "process_application_deadlines",
    "cleanup_mail_requests",
    "cleanup_login_ip_attempts",
    "cleanup_security_log",
    "cleanup_confirmation_failures",
    # Django's own command: removes the expired sessions of the database session store.
    "clearsessions",
    # Weekly housekeeping of both databases (D-119): Sunday from 04:30, once per ISO week.
    "housekeeping",
    # The log menu: one check of the database sources; at most one mail per source and hour (D-120).
    "notify_log_events",
    # Address app: expired stored building searches, then the daily address directory run (the import itself runs
    # last: it can take minutes and the cleanups must not wait for it).
    # The import error log: rows older than 180 days (D-122, security review).
    "cleanup_import_log",
    "cleanup_address_lookups",
    "scheduled_address_import",
)


class Command(BaseCommand):
    help = "Run all periodic cleanup tasks once; schedule this command, for example every 15 minutes."

    def handle(self, *args, **options):
        failed = []
        for task in TASKS:
            try:
                call_command(task, stdout=self.stdout)
            except Exception as error:  # noqa: BLE001 - only its class is reported
                failed.append(task)
                self.stderr.write(f"{task} failed ({type(error).__name__}).")
        if failed:
            raise CommandError("These tasks failed: " + ", ".join(failed) + ".")
