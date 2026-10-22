from django.db import migrations, models


class Migration(migrations.Migration):
    """The index for the towns and postal codes of the typing suggestions (D-111); a prefix range on the town key
    over the directory would otherwise read every address."""

    dependencies = [
        ('addresses', '0006_columns_missing_backoff'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='address',
            index=models.Index(
                fields=['generation', 'locality_key', 'locality', 'postal_code'], name='addr_generation_locality'
            ),
        ),
    ]
