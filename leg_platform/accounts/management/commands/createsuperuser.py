"""Replaces Django's createsuperuser, which would bypass the setup rules of D-005.

The app is listed before django.contrib.auth in INSTALLED_APPS, so this command takes
precedence (django.core.management.get_commands).
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Disabled. The first system administrator is created with the setup command of the "
        "instance (docs/DECISIONS.md, D-005)."
    )

    def handle(self, *args, **options):
        raise CommandError(
            "createsuperuser is disabled. The first system administrator is created with the "
            "setup command of the instance (docs/DECISIONS.md, D-005), which is not "
            "implemented yet."
        )
