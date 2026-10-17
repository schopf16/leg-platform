"""Encrypt the stored person IBANs again with the current ``SECRET_KEY`` (D-087, docs/SECURITY.md section 7).

Run after a key replacement while the old key is in ``SECRET_KEY_FALLBACKS``. Prints numbers only and
exits with code 1 if any IBAN could not be decrypted, so a script does not drop the old key.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from leg_platform.accounts.encryption import DecryptionError, is_current
from leg_platform.community import iban
from leg_platform.community.models import PersonRecord


class Command(BaseCommand):
    help = "Encrypt the IBANs of the persons again with the current SECRET_KEY."

    def handle(self, *args, **options):
        done = skipped = failed = 0
        for pk in list(PersonRecord.objects.exclude(iban_encrypted="").values_list("pk", flat=True)):
            with transaction.atomic():
                record = PersonRecord.objects.select_for_update().filter(pk=pk).first()
                if record is None or not record.iban_encrypted:
                    continue
                try:
                    if is_current(record.iban_encrypted, iban.PERSON_IBAN_PURPOSE) and iban.is_bound(
                        record.iban_encrypted
                    ):
                        skipped += 1
                        continue
                    # Read with any key of the instance and for this account, written with the current key.
                    record.iban = record.iban
                except DecryptionError:
                    failed += 1
                    continue
                record.save(update_fields=["iban_encrypted"])
                done += 1
        self.stdout.write(
            f"{done} IBANs were encrypted again, {skipped} were already current, {failed} could not be decrypted."
        )
        if failed:
            raise CommandError(f"{failed} IBANs could not be decrypted; keep the old key in SECRET_KEY_FALLBACKS.")
