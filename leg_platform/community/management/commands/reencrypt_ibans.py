"""Encrypt the stored IBANs of the persons again with the current ``SECRET_KEY`` (D-087; docs/SECURITY.md, section 7).

Run on the server after ``SECRET_KEY`` was replaced and the old key is listed in ``SECRET_KEY_FALLBACKS``, as
``emergency_access reencrypt-second-factor-keys`` does for the second factor keys. Until then the IBANs are readable
with the fallback key; if the old key is removed from the fallbacks before this ran, the IBANs under it cannot be
read any more (the pages then show no IBAN and the log names the account number). Prints the numbers only, never a value.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from leg_platform.accounts.encryption import DecryptionError, is_current, reencrypt
from leg_platform.community.iban import PERSON_IBAN_PURPOSE
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
                if is_current(record.iban_encrypted, PERSON_IBAN_PURPOSE):
                    skipped += 1
                    continue
                try:
                    record.iban_encrypted = reencrypt(record.iban_encrypted, PERSON_IBAN_PURPOSE)
                except DecryptionError:
                    failed += 1
                    continue
                record.save(update_fields=["iban_encrypted"])
                done += 1
        self.stdout.write(
            f"{done} IBANs were encrypted again, {skipped} were already current, {failed} could not be decrypted."
        )
