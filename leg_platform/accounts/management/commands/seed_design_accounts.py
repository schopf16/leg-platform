"""Create test accounts of every access level for design reviews on a development machine.

Only with APP_ENV=local. The accounts are throw-away accounts for the local database. Their
passwords and authenticator keys are written to a file outside the repository, which only this
command creates (owner read and write on POSIX systems); they are never printed. ``--code``
prints the current authenticator codes of these accounts, so that a reviewer can log in as a
staff user or system administrator, whose second factor is mandatory (docs/DECISIONS.md, D-019).
"""

import json
import os
import secrets
import time
from base64 import b32encode
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.oath import TOTP

from leg_platform.accounts.models import AuthenticatorDevice, User

DEFAULT_OUTPUT = Path.home() / ".leg-platform-design-accounts.json"

# (key, email, is_staff, is_superuser): an authenticated user, a staff user without any role or
# permission, and a system administrator. Reserved test domain (RFC 2606).
ACCOUNTS = (
    ("user", "design.user@example.org", False, False),
    ("staff", "design.staff@example.org", True, False),
    ("administrator", "design.administrator@example.org", True, True),
)


class Command(BaseCommand):
    help = "Create design test accounts of every access level (APP_ENV=local only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=Path,
            default=DEFAULT_OUTPUT,
            help="File for the credentials; must be outside the repository (default: %(default)s).",
        )
        parser.add_argument(
            "--code",
            action="store_true",
            help="Print the current authenticator codes of the design accounts and exit.",
        )

    def handle(self, *args, **options):
        if settings.APP_ENV != "local":
            raise CommandError("This command runs only with APP_ENV=local.")
        if options["code"]:
            self._print_codes()
            return
        output = options["output"].expanduser().resolve()
        base = Path(settings.BASE_DIR).resolve()
        if output == base or base in output.parents:
            raise CommandError("The credentials file must be outside the repository.")
        if output.exists():
            raise CommandError(
                f"{output} exists. Delete it first if the design accounts are to be created again."
            )
        emails = [email for _key, email, _staff, _admin in ACCOUNTS]
        if User.objects.filter(email__in=emails).exists():
            raise CommandError("A design account already exists in this database.")

        credentials = {}
        with transaction.atomic():
            for key, email, is_staff, is_superuser in ACCOUNTS:
                password = secrets.token_urlsafe(24)
                user = User.objects.create_user(
                    email,
                    first_name=key.capitalize(),
                    last_name="Design",
                    street="Examplestrasse",
                    house_number="1",
                    town="Bern",
                    swiss_postal_code=3000,
                )
                user.complete_registration(password)
                entry = {"email": email, "password": password}
                if is_staff or is_superuser:
                    # The second factor first, as the administration requires (D-042).
                    device = AuthenticatorDevice.objects.create_device(user, confirmed=True)
                    user.set_privileges(is_staff=is_staff, is_superuser=is_superuser)
                    entry["authenticator_secret"] = b32encode(device.bin_key).decode("ascii")
                credentials[key] = entry
            # Inside the transaction: if the file cannot be written, no account remains.
            output.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(credentials, file, indent=2)
        self.stdout.write(f"Created {len(ACCOUNTS)} design accounts. Credentials: {output}")

    def _print_codes(self):
        for key, email, is_staff, is_superuser in ACCOUNTS:
            if not (is_staff or is_superuser):
                continue
            device = AuthenticatorDevice.objects.filter(
                user__email=email, confirmed=True
            ).first()
            if device is None:
                raise CommandError(f"No authenticator app is set up for the {key} account.")
            generator = TOTP(device.bin_key, device.STEP, 0, device.DIGITS, device.drift)
            generator.time = time.time()
            self.stdout.write(f"{key}: {str(generator.token()).zfill(device.DIGITS)}")
