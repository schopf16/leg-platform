"""Replaces Django's createsuperuser, which would bypass the setup rules of D-005.

The app is listed before django.contrib.auth in INSTALLED_APPS, so this command takes
precedence (django.core.management.get_commands).
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Disabled. The first system administrator is created by 'setup_instance' (from the "
        "environment of the first start) or by 'setup_initial_administrator' (interactive "
        "password); docs/DECISIONS.md, D-005 and D-076."
    )

    def handle(self, *args, **options):
        raise CommandError(
            "createsuperuser is disabled. The first system administrator is created by "
            "'manage.py setup_instance' (from the environment of the first start) or by "
            "'manage.py setup_initial_administrator' (interactive password); see "
            "docs/DECISIONS.md, D-005 and D-076."
        )
