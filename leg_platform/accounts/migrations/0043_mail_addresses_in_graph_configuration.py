"""The sender and the contact address move from the portal settings to the E-Mail chapter (GraphConfiguration).

The address of the portal (base_url) goes back to the environment (INSTANCE_BASE_URL, D-116 amendment) and is removed
from the table without a copy. A sender or contact address stored in the portal settings is copied first, so that a
value already entered is kept.
"""

import leg_platform.accounts.models.common
from django.db import migrations, models


def copy_addresses_to_graph(apps, schema_editor):
    PortalSettings = apps.get_model("accounts", "PortalSettings")
    GraphConfiguration = apps.get_model("accounts", "GraphConfiguration")
    portal = PortalSettings.objects.filter(pk=1).first()
    if portal is None or not (portal.sender_address or portal.contact_address):
        return
    graph, _created = GraphConfiguration.objects.get_or_create(pk=1)
    fields = []
    if portal.sender_address:
        graph.sender_address = portal.sender_address
        fields.append("sender_address")
    if portal.contact_address:
        graph.contact_address = portal.contact_address
        fields.append("contact_address")
    graph.save(update_fields=[*fields, "updated_at"])


def copy_addresses_to_portal(apps, schema_editor):
    PortalSettings = apps.get_model("accounts", "PortalSettings")
    GraphConfiguration = apps.get_model("accounts", "GraphConfiguration")
    graph = GraphConfiguration.objects.filter(pk=1).first()
    if graph is None:
        return
    PortalSettings.objects.update_or_create(
        pk=1,
        defaults={"sender_address": graph.sender_address, "contact_address": graph.contact_address},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0042_plant_register_and_pv_import"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="graphconfiguration",
            options={"default_permissions": (), "verbose_name": "Email", "verbose_name_plural": "Email"},
        ),
        migrations.AddField(
            model_name="graphconfiguration",
            name="contact_address",
            field=models.EmailField(blank=True, help_text="The LEG receives the messages of the contact form here. Without it the contact form is not offered.", max_length=254, validators=[leg_platform.accounts.models.common.validate_sender_address], verbose_name="address for the messages of the contact form"),
        ),
        migrations.AlterField(
            model_name="graphconfiguration",
            name="sender_address",
            field=models.EmailField(blank=True, help_text="The address all emails are sent from. It is the mailbox of the Microsoft Graph application. Without it no email is sent. Use an address of the instance that does not invite a reply, for example noreply@example.org.", max_length=254, validators=[leg_platform.accounts.models.common.validate_sender_address], verbose_name="sender address"),
        ),
        migrations.RunPython(copy_addresses_to_graph, copy_addresses_to_portal),
        migrations.RemoveField(
            model_name="portalsettings",
            name="base_url",
        ),
        migrations.RemoveField(
            model_name="portalsettings",
            name="contact_address",
        ),
        migrations.RemoveField(
            model_name="portalsettings",
            name="sender_address",
        ),
    ]
