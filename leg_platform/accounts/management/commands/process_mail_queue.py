"""Process the queue of emails that registration and password reset requested (D-037).

Run it continuously (``--loop``, e.g. as a service) or from a scheduler every minute. Several
workers can run at the same time.
"""

import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from leg_platform.accounts.mail_queue import process_pending

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send the emails that requests have queued."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop", action="store_true", help="Keep running and check the queue regularly."
        )
        parser.add_argument(
            "--interval", type=int, default=5, help="Seconds between checks with --loop."
        )

    def handle(self, *args, **options):
        if not options["loop"]:
            self._run_once()
            return
        try:
            while True:
                # A connection that the database closed (restart, timeout) is replaced.
                close_old_connections()
                try:
                    self._run_once()
                except Exception as error:
                    # The worker keeps running; the next round tries again.
                    logger.error("The mail worker round failed (%s).", type(error).__name__)
                time.sleep(max(options["interval"], 1))
        except KeyboardInterrupt:
            return

    def _run_once(self):
        done, failed = process_pending()
        if done or failed:
            self.stdout.write(f"Processed {done} mail jobs, {failed} failed.")
