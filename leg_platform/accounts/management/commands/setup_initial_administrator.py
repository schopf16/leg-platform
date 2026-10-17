"""Create the initial system administrator with an interactive password (D-005).

Run in a terminal on the server. (``setup_instance`` can also create it from the first start's
environment, D-076.) The email comes from ``BOOTSTRAP_SYSTEM_ADMIN_EMAIL``; the password is read with
hidden input and never stored or logged. Refuses while any system administrator exists (also a
blocked or deactivated one) and never changes an existing account.
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
