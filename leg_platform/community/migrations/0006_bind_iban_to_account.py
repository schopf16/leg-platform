"""D-087, review L-7: the encrypted IBAN is bound to its account.

The payload of a stored IBAN becomes ``v2|<person id>|<IBAN>`` (before: the IBAN alone), so that a stored text copied
to the record of another person is refused when it is read (``community.iban.decrypt_iban``). Every existing token is
decrypted and encrypted again in the new form with the current key. Idempotent (a token that is bound already is
skipped). Reversible: backwards the payload is the IBAN alone again. A value that cannot be decrypted stops the
migration with the number of the record instead of being lost (restore the key, or clear the value). The label and the
prefix are repeated here on purpose: a migration must not change when the code later does.
"""

from django.db import migrations

PURPOSE = b"LEG-Platform person IBAN v1"
PREFIX = "v2|"


def bind(apps, schema_editor):
    from leg_platform.accounts.encryption import DecryptionError, decrypt, encrypt

    PersonRecord = apps.get_model("community", "PersonRecord")
    rows = PersonRecord.objects.exclude(iban_encrypted="").values_list("pk", "person_id", "iban_encrypted")
    for pk, person_id, token in list(rows):
        try:
            payload = decrypt(token, PURPOSE).decode("ascii")
        except DecryptionError:
            raise RuntimeError(
                f"The IBAN of person record {pk} cannot be decrypted with the keys of this instance. Restore the "
                "key (SECRET_KEY or SECRET_KEY_FALLBACKS) or clear the column iban_encrypted of the record."
            ) from None
        if payload.startswith(PREFIX):
            continue
        bound = encrypt(f"{PREFIX}{person_id}|{payload}".encode("ascii"), PURPOSE)
        PersonRecord.objects.filter(pk=pk).update(iban_encrypted=bound)


def unbind(apps, schema_editor):
    from leg_platform.accounts.encryption import decrypt, encrypt

    PersonRecord = apps.get_model("community", "PersonRecord")
    for pk, token in list(PersonRecord.objects.exclude(iban_encrypted="").values_list("pk", "iban_encrypted")):
        payload = decrypt(token, PURPOSE).decode("ascii")
        if payload.startswith(PREFIX):
            number = payload.split("|", 2)[2]
            PersonRecord.objects.filter(pk=pk).update(iban_encrypted=encrypt(number.encode("ascii"), PURPOSE))


class Migration(migrations.Migration):
    dependencies = [
        ("community", "0005_encrypted_iban"),
    ]

    operations = [
        migrations.RunPython(bind, unbind),
    ]
