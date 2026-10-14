import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounts', '0010_graph_configuration'),
    ]

    operations = [
        migrations.AddField(
            model_name='mailjob',
            name='account',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='mail_jobs', to=settings.AUTH_USER_MODEL),
        ),
    ]
