import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Directory",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("generation", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("data_date", models.DateField(null=True, verbose_name="Data date")),
                ("imported_at", models.DateTimeField(null=True, verbose_name="Imported at")),
                ("checksum", models.CharField(blank=True, max_length=64)),
                ("row_count", models.PositiveIntegerField(default=0, verbose_name="Address count")),
            ],
            options={"default_permissions": (), "verbose_name": "Address directory", "verbose_name_plural": "Address directory"},
        ),
        migrations.CreateModel(
            name="Address",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("generation", models.UUIDField(editable=False)),
                ("street", models.CharField(max_length=255)),
                ("house_number", models.CharField(blank=True, max_length=32)),
                ("postal_code", models.CharField(max_length=4)),
                ("locality", models.CharField(max_length=255)),
            ],
            options={
                "default_permissions": (),
                "indexes": [
                    models.Index(fields=["generation", "postal_code", "street"], name="addr_generation_postal_street"),
                    models.Index(fields=["generation", "street"], name="addr_generation_street"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ImportRequest",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("state", models.CharField(choices=[("idle", "No import requested"), ("queued", "Queued"), ("running", "Running"), ("succeeded", "Completed"), ("failed", "Failed")], default="idle", max_length=12, verbose_name="Status")),
                ("requested_at", models.DateTimeField(null=True, verbose_name="Requested at")),
                ("started_at", models.DateTimeField(null=True, verbose_name="Started at")),
                ("finished_at", models.DateTimeField(null=True, verbose_name="Finished at")),
                ("last_automatic_attempt_at", models.DateTimeField(null=True, verbose_name="Last automatic attempt at")),
                ("is_automatic", models.BooleanField(default=False, editable=False)),
            ],
            options={
                "default_permissions": (),
                "permissions": [("request_directory_import", "Can request an address directory import")],
                "verbose_name": "Address directory import",
                "verbose_name_plural": "Address directory imports",
            },
        ),
    ]
