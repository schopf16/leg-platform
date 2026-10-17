"""``migrate`` run as a writer of the address directory file (``migrate --database=addresses``, D-075)."""

from django.core.management.commands.migrate import Command as MigrateCommand

from ...storage import mark_writer


class Command(MigrateCommand):
    def handle(self, *args, **options):
        mark_writer()
        return super().handle(*args, **options)
