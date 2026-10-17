"""Run every periodic cleanup task once (D-041); schedule it, e.g. every 15 minutes.

The tasks are independent: a failing task does not stop the others, and the command exits with an
error if any failed. ``process_mail_queue --loop`` is not included (a supervised service).
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

# A test checks that every cleanup command of the accounts app is listed here.
TASKS = (
    "delete_expired_registrations",
    "cleanup_mail_requests",
    "cleanup_login_ip_attempts",
    "cleanup_security_log",
    "cleanup_confirmation_failures",
    # Django's own command: removes the expired sessions of the database session store.
    "clearsessions",
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
