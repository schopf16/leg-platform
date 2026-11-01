from django.db import migrations, models


class Migration(migrations.Migration):
    # Only a field of the default database: the address file is not rebuilt for it (see setup.prepare).
    in_place = True

    dependencies = [
        ('addresses', '0022_import_queue_and_published_figures'),
    ]

    operations = [
        migrations.AddField(
            model_name='importrequest',
            name='worker_seen_at',
            field=models.DateTimeField(editable=False, null=True),
        ),
    ]
