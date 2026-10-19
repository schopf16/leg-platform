"""Run the scheduled rules of the applications and deregistrations once (D-103); part of ``run_scheduled_tasks``.

Pauses and reminds idle drafts, rolls back drafts that lie idle or wait too long for the contract, reminds about a missing
bank connection, rolls back applications whose confirmation of the grid operator did not arrive, and makes confirmed
deregistrations effective. Idempotent: a second run changes nothing. Prints and logs numbers and times only.
"""

from django.core.management.base import BaseCommand, CommandError

from leg_platform.community import application_deadlines


class Command(BaseCommand):
    help = "Process the reminders and deadlines of the applications once."

    def handle(self, *args, **options):
        counts = application_deadlines.process()
        self.stdout.write(
            "Drafts rolled back: {rolled_back}, drafts reminded: {paused}, bank connection reminders: {bank_reminded}, "
            "operator confirmation lapsed: {operator_lapsed}, deregistrations effective: "
            "{deregistrations_effective}, old customer messages deleted: {messages_deleted}, failed rows: {failed}.".format(**counts)
        )
        if counts["failed"]:
            # After all rows were tried: the failures are in the application log (class of the error only).
            raise CommandError(f"{counts['failed']} rows failed; see the application log.")
