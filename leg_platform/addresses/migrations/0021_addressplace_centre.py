"""The centre of every place (owner decision 2026-10-28, round 4): the address check ranks the offered places by their
distance instead of by the number of the postal code. Additive and IN PLACE like 0020 (``setup.prepare_directory`` does not
rebuild for it): two nullable columns on the small table, filled from a sample of up to 60 addresses per place (4,060 places in
about 2 s on 3.3 million addresses)."""

from django.db import migrations, models

SAMPLE = 60


def fill_centres(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT generation FROM addresses_directory WHERE id = 1")
        row = cursor.fetchone()
        if row is None:
            return
        generation = row[0]
        cursor.execute(
            "SELECT postal_code, locality_key FROM addresses_addressplace WHERE generation = %s", [generation]
        )
        places = cursor.fetchall()
        updates = []
        for postal_code, locality_key in places:
            cursor.execute(
                "SELECT AVG(easting), AVG(northing) FROM (SELECT easting, northing FROM addresses_address "
                "WHERE generation = %s AND postal_code = %s AND locality_key = %s LIMIT %s)",
                [generation, postal_code, locality_key, SAMPLE],
            )
            easting, northing = cursor.fetchone()
            if easting is not None and northing is not None:
                updates.append([float(easting), float(northing), generation, postal_code, locality_key])
        cursor.executemany(
            "UPDATE addresses_addressplace SET easting = %s, northing = %s "
            "WHERE generation = %s AND postal_code = %s AND locality_key = %s",
            updates,
        )


class Migration(migrations.Migration):
    in_place = True

    dependencies = [
        ("addresses", "0020_lookup_tables_and_house_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="addressplace", name="easting", field=models.FloatField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="addressplace", name="northing", field=models.FloatField(editable=False, null=True),
        ),
        migrations.RunPython(fill_centres, migrations.RunPython.noop, hints={"model_name": "addressplace"}),
    ]
