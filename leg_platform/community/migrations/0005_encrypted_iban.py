"""D-087: the IBAN of a person is stored encrypted.

``iban`` (plain text, 34 characters) becomes ``iban_encrypted`` (a Fernet token, a text column). The existing
values are encrypted with the key of the instance (``accounts.encryption``, ``SECRET_KEY``, the label of
``community.iban``): the column is renamed first (the data stays), widened, and then every non-empty value is
encrypted in place. Reversible: backwards decrypts every value into the plain text column again (the operator
needs the same ``SECRET_KEY`` or one of ``SECRET_KEY_FALLBACKS``); a value that cannot be decrypted stops the
migration instead of being lost.
"""

from django.db import migrations, models

# The same label as ``leg_platform.community.iban.PERSON_IBAN_PURPOSE``. Repeated here on purpose: a migration
# must not change when the code later does.
PURPOSE = b"LEG-Platform person IBAN v1"


def encrypt_existing(apps, schema_editor):
    from leg_platform.accounts.encryption import encrypt

    PersonRecord = apps.get_model("community", "PersonRecord")
    for pk, value in list(PersonRecord.objects.exclude(iban_encrypted="").values_list("pk", "iban_encrypted")):
        number = "".join(value.replace("-", " ").split()).upper()
        token = encrypt(number.encode("ascii"), PURPOSE) if number else ""
        PersonRecord.objects.filter(pk=pk).update(iban_encrypted=token)


def decrypt_existing(apps, schema_editor):
    from leg_platform.accounts.encryption import decrypt

    PersonRecord = apps.get_model("community", "PersonRecord")
    for pk, token in list(PersonRecord.objects.exclude(iban_encrypted="").values_list("pk", "iban_encrypted")):
        PersonRecord.objects.filter(pk=pk).update(iban_encrypted=decrypt(token, PURPOSE).decode("ascii"))


class Migration(migrations.Migration):
    dependencies = [
        ("community", "0004_operator_names_share_changes_no_rights"),
    ]

    operations = [
        migrations.RenameField(model_name="personrecord", old_name="iban", new_name="iban_encrypted"),
        migrations.AlterField(
            model_name="personrecord",
            name="iban_encrypted",
            field=models.TextField(blank=True, editable=False, verbose_name="IBAN"),
        ),
        migrations.RunPython(encrypt_existing, decrypt_existing),
    ]
