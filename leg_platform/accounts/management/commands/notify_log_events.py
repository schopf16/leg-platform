"""Check the database sources of the log menu and queue the notification mail (D-120)."""

from django.core.management.base import BaseCommand

from leg_platform import log_menu


class Command(BaseCommand):
    help = "Check the database sources of the log menu and queue the notification mail (D-120)."

    def handle(self, *args, **options):
        queued = log_menu.notify_log_events()
        self.stdout.write(f"Queued {queued} log notice(s).")
