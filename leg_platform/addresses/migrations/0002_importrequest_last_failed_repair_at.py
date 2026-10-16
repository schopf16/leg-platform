from django.db import migrations, models


class Migration(migrations.Migration):
    # ImportRequest lives in the application database only (leg_platform.addresses.router): on the
    # SQLite file of the directory this migration is recorded and changes nothing. 0001 is not
    # edited, because an application database may already have applied it (D-075, L-10).
    dependencies = [("addresses", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="importrequest",
            name="last_failed_repair_at",
            field=models.DateTimeField(editable=False, null=True),
        ),
    ]
