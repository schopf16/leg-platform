"""Migration 0043: the sender and the contact address move from the portal settings to the E-Mail chapter (D-116).

The migration runs on empty tables when the test database is created, so the copy is tested here with values.
"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("accounts", "0042_plant_register_and_pv_import")]
AFTER = [("accounts", "0043_mail_addresses_in_graph_configuration")]


def migrate(target):
    """Migrate the default database to ``target`` and return the historical apps of that state."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)
    return executor.loader.project_state(target).apps


class MailAddressMigrationTests(TransactionTestCase):
    databases = {"default"}

    def tearDown(self):
        # Back to the latest state of every app, so that the other tests see the current schema.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_a_stored_contact_address_is_copied_and_a_stored_sender_is_kept_when_the_portal_has_none(self):
        old_apps = migrate(BEFORE)
        old_portal = old_apps.get_model("accounts", "PortalSettings")
        old_graph = old_apps.get_model("accounts", "GraphConfiguration")
        old_portal.objects.update_or_create(
            pk=1,
            defaults={
                "base_url": "https://portal.example.org",
                "sender_address": "",
                "contact_address": "leg@example.org",
            },
        )
        old_graph.objects.update_or_create(pk=1, defaults={"sender_address": "graph@example.org"})

        new_apps = migrate(AFTER)
        graph = new_apps.get_model("accounts", "GraphConfiguration").objects.get(pk=1)

        self.assertEqual(graph.sender_address, "graph@example.org")
        self.assertEqual(graph.contact_address, "leg@example.org")
        portal_fields = {
            field.name for field in new_apps.get_model("accounts", "PortalSettings")._meta.get_fields()
        }
        self.assertFalse({"base_url", "sender_address", "contact_address"} & portal_fields)
