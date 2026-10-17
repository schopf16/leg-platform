"""D-086: the management area is open to every staff account, the grid operator is a model of the instance, and the
shares of the cooperative are a history of changes.

* ``bkw_number`` of the transformer circuit becomes ``grid_number``, ``registered_with_bkw_on`` of the metering point
  becomes ``registered_with_operator_on`` and ``bkw_customer_number`` of the person record becomes
  ``operator_customer_number`` (renamed columns, the data stays); the two history events that carry the old name
  are renamed in the stored entries.
* ``GridOperator`` (one row, the name) and ``ShareChange`` (the entries of the history of the shares) are new. Each
  existing period of shares becomes entries: the number from its first day and, if the period has an end, the
  number 0 from the day after it. The three columns of the period are removed.
* The Django rights of the community models are not used by the management area any more (every staff account may
  use it): the models keep no rights (``default_permissions = ()``) and the rows of the existing rights of the app
  are deleted (with them the assignments to groups and accounts; the rights of nothing else are touched).
"""

import django.db.models.deletion
import django.utils.timezone
from datetime import timedelta

from django.conf import settings
from django.db import migrations, models

import leg_platform.community.numbers

HISTORY_EVENTS = (
    ("registered_with_bkw", "registered_with_operator"),
    ("person_bkw_number_changed", "person_operator_number_changed"),
)


def shares_to_history(apps, schema_editor):
    """Every period of shares becomes entries of the history: the number from its first day and, for a period with an
    end, 0 from the day after the end (the end day itself was the last day of the shares)."""
    PersonRecord = apps.get_model("community", "PersonRecord")
    ShareChange = apps.get_model("community", "ShareChange")
    for record in PersonRecord.objects.exclude(shares__isnull=True):
        start = record.shares_from or record.created_at.date()
        ShareChange.objects.create(record=record, quantity=record.shares, valid_from=start)
        if record.shares_to:
            ShareChange.objects.create(record=record, quantity=0, valid_from=record.shares_to + timedelta(days=1))


def history_to_periods(apps, schema_editor):
    """Backwards: the latest entry with a number above 0 becomes the period again (the older entries are dropped)."""
    PersonRecord = apps.get_model("community", "PersonRecord")
    ShareChange = apps.get_model("community", "ShareChange")
    for record in PersonRecord.objects.all():
        entries = list(ShareChange.objects.filter(record=record).order_by("valid_from", "pk"))
        positive = [entry for entry in entries if entry.quantity > 0]
        if not positive:
            continue
        last = positive[-1]
        record.shares, record.shares_from = last.quantity, last.valid_from
        following = [entry for entry in entries if entry.quantity == 0 and entry.valid_from > last.valid_from]
        if following:
            record.shares_to = following[0].valid_from - timedelta(days=1)
        record.save(update_fields=["shares", "shares_from", "shares_to"])


def rename_events(apps, schema_editor, reverse=False):
    HistoryEntry = apps.get_model("community", "HistoryEntry")
    for old, new in HISTORY_EVENTS:
        source, target = (new, old) if reverse else (old, new)
        HistoryEntry.objects.filter(event=source).update(event=target)


def rename_events_back(apps, schema_editor):
    rename_events(apps, schema_editor, reverse=True)


def delete_rights(apps, schema_editor):
    """The rows of the rights of the community models; the links of groups and accounts go with them."""
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(content_type__app_label="community").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("community", "0003_alter_leg_remark_alter_location_remark_and_more"),
    ]

    operations = [
        # The names that carried the operator's name.
        migrations.RenameField(model_name="transformercircuit", old_name="bkw_number", new_name="grid_number"),
        migrations.RenameField(
            model_name="meteringpoint", old_name="registered_with_bkw_on", new_name="registered_with_operator_on"
        ),
        migrations.RenameField(
            model_name="personrecord", old_name="bkw_customer_number", new_name="operator_customer_number"
        ),
        migrations.AlterField(
            model_name="transformercircuit",
            name="grid_number",
            field=models.CharField(
                db_collation="utf8mb4_bin", max_length=20, unique=True,
                validators=[leg_platform.community.numbers.validate_transformer_circuit_number],
                verbose_name="grid number",
            ),
        ),
        migrations.AlterField(
            model_name="meteringpoint",
            name="registered_with_operator_on",
            field=models.DateField(blank=True, null=True, verbose_name="notified on"),
        ),
        migrations.AlterField(
            model_name="personrecord",
            name="operator_customer_number",
            field=models.CharField(blank=True, max_length=30, verbose_name="customer number at the grid operator"),
        ),
        migrations.RunPython(rename_events, rename_events_back),
        # The grid operator and the history of the shares.
        migrations.CreateModel(
            name="GridOperator",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(blank=True, max_length=120, verbose_name="name")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "grid operator",
                "verbose_name_plural": "grid operators",
                "default_permissions": (),
                "constraints": [models.CheckConstraint(condition=models.Q(("id", 1)), name="community_grid_operator_singleton")],
            },
        ),
        migrations.CreateModel(
            name="ShareChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.PositiveIntegerField(verbose_name="number of shares")),
                ("valid_from", models.DateField(verbose_name="valid from")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False, verbose_name="time")),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "record",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="share_changes",
                        to="community.personrecord",
                    ),
                ),
            ],
            options={
                "verbose_name": "share change",
                "verbose_name_plural": "share changes",
                "ordering": ("-valid_from", "-pk"),
                "default_permissions": (),
                "indexes": [models.Index(fields=["record", "valid_from"], name="community_sharechange_record")],
            },
        ),
        migrations.RunPython(shares_to_history, history_to_periods),
        migrations.RemoveConstraint(model_name="personrecord", name="community_personrecord_shares_period"),
        migrations.RemoveField(model_name="personrecord", name="shares"),
        migrations.RemoveField(model_name="personrecord", name="shares_from"),
        migrations.RemoveField(model_name="personrecord", name="shares_to"),
        # No rights of the Django permission system for the management area.
        migrations.AlterModelOptions(
            name="leg",
            options={"default_permissions": (), "ordering": ("name",), "verbose_name": "LEG", "verbose_name_plural": "LEGs"},
        ),
        migrations.AlterModelOptions(
            name="location",
            options={
                "default_permissions": (), "ordering": ("street", "house_number"),
                "verbose_name": "location", "verbose_name_plural": "locations",
            },
        ),
        migrations.AlterModelOptions(
            name="managementsettings",
            options={
                "default_permissions": (), "verbose_name": "management settings",
                "verbose_name_plural": "management settings",
            },
        ),
        migrations.AlterModelOptions(
            name="meteringpoint",
            options={
                "default_permissions": (), "ordering": ("number",),
                "verbose_name": "metering point", "verbose_name_plural": "metering points",
            },
        ),
        migrations.AlterModelOptions(
            name="personrecord",
            options={"default_permissions": (), "verbose_name": "person", "verbose_name_plural": "persons"},
        ),
        migrations.AlterModelOptions(
            name="transformercircuit",
            options={
                "default_permissions": (), "ordering": ("grid_number",),
                "verbose_name": "transformer circuit", "verbose_name_plural": "transformer circuits",
            },
        ),
        migrations.RunPython(delete_rights, migrations.RunPython.noop),
    ]
