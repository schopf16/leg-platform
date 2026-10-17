"""The data functions of the community migration 0004 (D-086) on the real models: the renamed history events and the
deleted rights. The conversion of the share periods into entries needs the old columns, which no longer exist on the
real model; it is covered by the migration run on the real database, not here."""

import importlib

from django.apps import apps
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, tag

from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import HistoryEntry

migration = importlib.import_module("leg_platform.community.migrations.0004_operator_names_share_changes_no_rights")


@tag("mariadb")
class MigrationDataFunctionTests(TestCase):
    def events(self):
        return list(HistoryEntry.objects.order_by("pk").values_list("event", flat=True))

    def test_the_renamed_events_are_the_ones_of_the_history_choices(self):
        self.assertEqual(
            dict(migration.HISTORY_EVENTS),
            {
                "registered_with_bkw": HistoryEvent.REGISTERED_WITH_OPERATOR.value,
                "person_bkw_number_changed": HistoryEvent.PERSON_OPERATOR_NUMBER_CHANGED.value,
            },
        )

    def test_the_stored_events_are_renamed_in_both_directions_and_others_are_left_alone(self):
        old = ["registered_with_bkw", "person_bkw_number_changed", HistoryEvent.CREATED.value]
        for event in old:
            HistoryEntry.objects.create(subject_type="metering_point", subject_id=1, event=event)

        migration.rename_events(apps, None)
        self.assertEqual(
            self.events(), ["registered_with_operator", "person_operator_number_changed", HistoryEvent.CREATED.value]
        )

        migration.rename_events_back(apps, None)
        self.assertEqual(self.events(), old)

    def test_only_the_rights_of_the_community_are_deleted(self):
        leg = ContentType.objects.get(app_label="community", model="leg")
        user = ContentType.objects.get(app_label="accounts", model="user")
        Permission.objects.create(content_type=leg, codename="view_leg", name="Can view LEG")

        migration.delete_rights(apps, None)

        self.assertFalse(Permission.objects.filter(content_type__app_label="community").exists())
        self.assertTrue(Permission.objects.filter(content_type=user, codename="view_user").exists())
