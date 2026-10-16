"""Create the initial system administrator of an instance with an interactive password.

Run in a terminal on the server (docs/DECISIONS.md, D-005). Since D-076 the first start can also
create the account without a prompt: ``manage.py setup_instance`` does it from the environment
variables of the first start. This command stays for operators who prefer to type the password.
The email address comes from the environment variable ``BOOTSTRAP_SYSTEM_ADMIN_EMAIL``; the
password is asked for interactively with hidden input and is never stored in a file or logged. The
command refuses while any system administrator exists (also a blocked or deactivated one) and never
changes an existing account. At the first login the password must be replaced and two-factor
authentication must be set up before anything else is possible.
"""

import getpass
import os
import sys
import warnings

from django.core.management.base import BaseCommand, CommandError

from leg_platform.accounts.initial_administrator import (
    InitialAdministratorError,
    create_initial_administrator,
    system_administrator_exists,
)
from leg_platform.environment import BOOTSTRAP_ADMIN_EMAIL_VARIABLE, parse_bootstrap_administrator

EMAIL_VARIABLE = BOOTSTRAP_ADMIN_EMAIL_VARIABLE


class Command(BaseCommand):
    help = (
        "Create the initial system administrator. Only possible while no system administrator "
        "exists; the password is entered interactively."
    )

    def handle(self, *args, **options):
        email = parse_bootstrap_administrator(os.environ).email
        if not email:
            raise CommandError(f"{EMAIL_VARIABLE} is not set.")
        if system_administrator_exists():
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
            user = create_initial_administrator(email, password)
        except InitialAdministratorError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(
            f"The initial system administrator was created (internal number {user.pk}). The "
            "password must be changed and two-factor authentication set up at the first login."
        )
