import json
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from leg_platform.accounts.models import User
from leg_platform.accounts.second_factor import has_second_factor


def run(*args, **options):
    out = StringIO()
    call_command("seed_design_accounts", *args, stdout=out, **options)
    return out.getvalue()


class SeedDesignAccountsTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "accounts.json"

    def test_it_creates_an_account_of_every_access_level(self):
        run(output=self.output)
        credentials = json.loads(self.output.read_text(encoding="utf-8"))
        user = User.objects.get(email=credentials["user"]["email"])
        staff = User.objects.get(email=credentials["staff"]["email"])
        administrator = User.objects.get(email=credentials["administrator"]["email"])
        self.assertEqual((user.is_staff, user.is_superuser), (False, False))
        self.assertEqual((staff.is_staff, staff.is_superuser), (True, False))
        self.assertEqual((administrator.is_staff, administrator.is_superuser), (True, True))
        self.assertTrue(has_second_factor(staff) and has_second_factor(administrator))
        self.assertFalse(has_second_factor(user))
        self.assertFalse(staff.groups.exists() or staff.user_permissions.exists())
        self.assertTrue(user.check_password(credentials["user"]["password"]))
        self.assertNotIn("authenticator_secret", credentials["user"])

    def test_the_credentials_are_not_printed(self):
        printed = run(output=self.output)
        credentials = self.output.read_text(encoding="utf-8")
        for entry in json.loads(credentials).values():
            self.assertNotIn(entry["password"], printed)

    def test_the_printed_codes_log_in_the_second_factor_step(self):
        run(output=self.output)
        codes = run(code=True)
        self.assertRegex(codes, r"staff: \d{6}")
        self.assertRegex(codes, r"administrator: \d{6}")
        self.assertNotIn("user:", codes)

    def test_it_refuses_outside_local(self):
        with override_settings(APP_ENV="stage"):
            with self.assertRaisesMessage(CommandError, "APP_ENV=local"):
                run(output=self.output)
            with self.assertRaisesMessage(CommandError, "APP_ENV=local"):
                run(code=True)
        self.assertFalse(User.objects.exists())

    def test_it_refuses_a_file_inside_the_repository(self):
        inside = Path(settings.BASE_DIR) / "design-accounts.json"
        with self.assertRaisesMessage(CommandError, "outside the repository"):
            run(output=inside)
        self.assertFalse(inside.exists())
        self.assertFalse(User.objects.exists())

    def test_it_never_overwrites_a_file_or_existing_accounts(self):
        run(output=self.output)
        with self.assertRaisesMessage(CommandError, "exists"):
            run(output=self.output)
        self.output.unlink()
        with self.assertRaisesMessage(CommandError, "already exists"):
            run(output=self.output)
        self.assertEqual(User.objects.count(), 3)

    def test_no_account_remains_if_the_file_cannot_be_written(self):
        blocked = Path(self.output.parent) / "file"
        blocked.write_text("x", encoding="utf-8")
        with self.assertRaises(OSError):
            run(output=blocked / "accounts.json")
        self.assertFalse(User.objects.exists())
