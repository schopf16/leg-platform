"""Create the initial system administrator of an instance (docs/DECISIONS.md, D-005).

Run once in a terminal on the server. The email address comes from the environment variable
``BOOTSTRAP_SYSTEM_ADMIN_EMAIL``; the password is asked for interactively with hidden input and
is never stored in a file or logged. The command refuses while any system administrator exists
(also a blocked or deactivated one) and never changes an existing account. At the first login
the password must be replaced and two-factor authentication must be set up before anything
else is possible.
"""

import getpass
import os
import sys
import warnings

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from leg_platform.accounts import security_log
from leg_platform.accounts.models import AccountIntegrityError, SecurityEvent, User

EMAIL_VARIABLE = "BOOTSTRAP_SYSTEM_ADMIN_EMAIL"


class Command(BaseCommand):
    help = (
        "Create the initial system administrator. Only possible while no system administrator "
        "exists; the password is entered interactively."
    )

    def handle(self, *args, **options):
        email = os.environ.get(EMAIL_VARIABLE, "").strip()
        if not email:
            raise CommandError(f"{EMAIL_VARIABLE} is not set.")
        if User._base_manager.filter(is_superuser=True).exists():
            raise CommandError(
                "A system administrator already exists. This command never changes an "
                "existing account."
            )
        if not sys.stdin.isatty():
            raise CommandError("The password is entered interactively; run this in a terminal.")
        try:
            with warnings.catch_warnings():
                # getpass only warns when it cannot hide the input and then echoes it.
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Initial password: ")
                again = getpass.getpass("Initial password (again): ")
        except getpass.GetPassWarning:
            raise CommandError("The terminal cannot hide the input; the password is not read.") from None
        if password != again:
            raise CommandError("The passwords do not match.")
        try:
            user = User.objects.create_superuser(email, password, password_change_required=True)
        except ValidationError as error:
            raise CommandError("The password is not accepted: " + " ".join(error.messages)) from None
        except (ValueError, AccountIntegrityError) as error:
            raise CommandError(str(error)) from None
        security_log.record(SecurityEvent.Event.INITIAL_ADMINISTRATOR_CREATED, account=user)
        self.stdout.write(
            f"The initial system administrator was created (internal number {user.pk}). The "
            "password must be changed and two-factor authentication set up at the first login."
        )
