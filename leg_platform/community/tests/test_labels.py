"""Labels that name the grid operator (D-086, D-103): model labels are static and never read the database; the helpers fill
in the operator's name at display time."""

from django.apps import apps
from django.db import DatabaseError
from django.test import TestCase, tag
from django.utils import translation
from unittest.mock import patch

from leg_platform.community import labels
from leg_platform.community.models import Application, ApplicationNotice, CustomerMessage, GridOperator


@tag("mariadb")
class ModelLabelTests(TestCase):
    def test_no_label_of_a_community_model_reads_the_database(self):
        with self.assertNumQueries(0):
            for model in apps.get_app_config("community").get_models():
                for field in model._meta.get_fields():
                    for value in (getattr(field, "verbose_name", ""), getattr(field, "help_text", "")):
                        str(value)
                    for _key, label in getattr(field, "choices", None) or ():
                        str(label)
                str(model._meta.verbose_name)
            for choices in (Application.State, Application.StartChoice, ApplicationNotice.Kind, CustomerMessage.Kind):
                for label in choices.labels:
                    str(label)

    def test_the_static_labels_say_grid_operator(self):
        with translation.override("en"):
            self.assertEqual(str(Application.State.OPERATOR_STARTED.label), "Registration at the grid operator started")
            self.assertEqual(str(Application.State.OPERATOR_CONFIRMED.label), "Confirmed by the grid operator")


@tag("mariadb")
class HelperTests(TestCase):
    def test_the_helpers_name_the_operator_once_it_is_entered(self):
        with translation.override("en"):
            self.assertEqual(
                labels.application_state_label(Application.State.OPERATOR_STARTED),
                "Registration at the grid operator started",
            )
            GridOperator.objects.update_or_create(pk=1, defaults={"name": "Example Grid"})
            self.assertEqual(
                labels.application_state_label(Application.State.OPERATOR_STARTED),
                "Registration at the Example Grid started",
            )
            self.assertEqual(labels.application_state_label("operator_confirmed"), "Confirmed by the Example Grid")
            self.assertIn("Example Grid", labels.notice_kind_label(ApplicationNotice.Kind.OPERATOR_CONFIRMATION_LAPSED))
            self.assertEqual(
                labels.notice_kind_label(ApplicationNotice.Kind.OPERATOR_DATA_CHANGED),
                "Data changed after the registration at the Example Grid: check the registration again",
            )

    def test_a_paused_draft_is_labelled_paused_for_the_customer_without_the_operator(self):
        with translation.override("de-ch"):
            self.assertEqual(labels.application_state_label("paused"), "Pausiert")
            self.assertEqual(labels.application_state_label(Application.State.DRAFT), "Entwurf")
        with translation.override("en"):
            self.assertEqual(labels.application_state_label("paused"), "Paused")
        with self.assertNumQueries(0):
            labels.application_state_label("paused")

    def test_other_states_keep_their_plain_label_and_a_database_error_gives_the_static_one(self):
        with translation.override("en"):
            self.assertEqual(labels.application_state_label("draft"), "Draft")
            self.assertEqual(labels.notice_kind_label("building_unavailable"), "Building not available to the customer")
            with patch.object(labels, "_STATE_TEXTS", {Application.State.OPERATOR_STARTED: _Broken()}):
                self.assertEqual(
                    labels.application_state_label("operator_started"), "Registration at the grid operator started"
                )


class _Broken:
    def __str__(self):
        raise DatabaseError("no database")
