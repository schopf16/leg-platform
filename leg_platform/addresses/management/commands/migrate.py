"""``migrate`` of Django, run as a writer of the address directory file (D-075).

The directory connection is read-only unless the process declares itself a writer
(``leg_platform.addresses.storage``). ``migrate`` creates the tables of the directory and records
its migrations in the file (``migrate --database=addresses``), so it declares itself a writer.
Nothing else differs. Django lets a command of an installed app replace a command of the same name
(https://docs.djangoproject.com/en/stable/howto/custom-management-commands/, "Overriding commands").
"""

from django.core.management.commands.migrate import Command as MigrateCommand

from ...storage import mark_writer


class Command(MigrateCommand):
    def handle(self, *args, **options):
        mark_writer()
        return super().handle(*args, **options)
