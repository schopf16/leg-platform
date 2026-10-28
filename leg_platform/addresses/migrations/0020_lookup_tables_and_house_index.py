"""Schema for the lookups of the address suggestions (H, owner decision 2026-10-28): two small tables built from the
addresses (the places of a street, the streets of a postal code or a town, the towns of a postal code) and one covering
index for lookups by street and house number. ADDITIVE and applied IN PLACE to an existing file: no re-download, the
existing generation is read, nothing of it is changed (``setup.prepare_directory`` does not rebuild for this migration,
see ``in_place``). On a file of 3.3 million addresses: one to two minutes (121 s measured on a busy dev machine), about 410 MB more."""

from django.db import migrations, models

# Street rows are built in ranges of the postal code, so that one statement stays short (every Swiss code starts with 1 to 9).
POSTAL_RANGES = ["", "2", "3", "4", "5", "6", "7", "8", "9", "\U0010ffff"]


def fill_tables(apps, schema_editor):
    """Build the place and street rows of the published generation from its addresses (nothing on a new, empty file)."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT generation FROM addresses_directory WHERE id = 1")
        row = cursor.fetchone()
        if row is None:
            return
        generation = row[0]
        cursor.execute(
            "INSERT OR IGNORE INTO addresses_addressplace (generation, postal_code, locality_key, locality) "
            "SELECT DISTINCT generation, postal_code, locality_key, locality FROM addresses_address WHERE generation = %s",
            [generation],
        )
        for low, high in zip(POSTAL_RANGES, POSTAL_RANGES[1:]):
            cursor.execute(
                "INSERT OR IGNORE INTO addresses_addressstreet (generation, street, street_key, postal_code, locality, "
                "locality_key) SELECT DISTINCT generation, street, street_key, postal_code, locality, locality_key "
                "FROM addresses_address WHERE generation = %s AND postal_code >= %s AND postal_code < %s",
                [generation, low, high],
            )


def refresh_statistics(apps, schema_editor):
    """Planner statistics for the new indexes, as the importer does after a generation (best effort)."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("PRAGMA analysis_limit = 2000")
        cursor.execute("ANALYZE")


class Migration(migrations.Migration):
    # Read by ``setup.prepare_directory``: an additive migration of the directory file that keeps the rows.
    in_place = True

    dependencies = [
        ("addresses", "0019_importrequest_progress_units_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="AddressPlace",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("generation", models.UUIDField(editable=False)),
                ("postal_code", models.CharField(max_length=4)),
                ("locality", models.CharField(max_length=255)),
                ("locality_key", models.CharField(max_length=255)),
            ],
            options={"default_permissions": ()},
        ),
        migrations.CreateModel(
            name="AddressStreet",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("generation", models.UUIDField(editable=False)),
                ("street", models.CharField(max_length=255)),
                ("street_key", models.CharField(max_length=255)),
                ("postal_code", models.CharField(max_length=4)),
                ("locality", models.CharField(max_length=255)),
                ("locality_key", models.CharField(max_length=255)),
            ],
            options={"default_permissions": ()},
        ),
        migrations.AddConstraint(
            model_name="addressplace",
            constraint=models.UniqueConstraint(
                fields=["generation", "postal_code", "locality_key", "locality"], name="place_postal_town",
            ),
        ),
        migrations.AddIndex(
            model_name="addressplace",
            index=models.Index(fields=["generation", "locality_key", "postal_code"], name="place_town"),
        ),
        migrations.AddConstraint(
            model_name="addressstreet",
            constraint=models.UniqueConstraint(
                fields=["generation", "street_key", "street", "postal_code", "locality"], name="street_place",
            ),
        ),
        migrations.AddIndex(
            model_name="addressstreet",
            index=models.Index(fields=["generation", "postal_code", "street_key", "street"], name="street_postal"),
        ),
        migrations.AddIndex(
            model_name="addressstreet",
            index=models.Index(fields=["generation", "locality_key", "street_key", "street"], name="street_town"),
        ),
        migrations.AddIndex(
            model_name="address",
            index=models.Index(
                fields=[
                    "generation", "street_key", "house_number_key", "postal_code", "locality_key", "street", "house_number",
                    "locality",
                ],
                name="addr_house",
            ),
        ),
        migrations.RunPython(fill_tables, migrations.RunPython.noop, hints={"model_name": "addressplace"}),
        migrations.RunPython(refresh_statistics, migrations.RunPython.noop, hints={"model_name": "addressplace"}),
    ]
