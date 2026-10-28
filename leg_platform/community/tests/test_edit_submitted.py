"""Single details of a SUBMITTED application ("edit in place") and the figures of a stored feed-in point: the services
``applications.edit_submitted_*`` and ``buildings.set_metering_point_name``. Same rules as the wizard; allowed in state
``submitted`` only; another account never reaches the rows."""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, tag

from leg_platform.accounts.models import MailJob
from leg_platform.community import application_staff, applications, buildings, own_iban
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.history import HistoryEvent, entries_for
from leg_platform.community.models import Application, ApplicationBuilding, ApplicationMeteringPoint, Direction
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import (
    GOOD_IBAN,
    OTHER_IBAN,
    TODAY,
    candidate,
    configure_numbers,
    customer,
    directory,
    full_number,
    publish_contract,
    ready_draft,
)

LONG_AGO = date.today() - timedelta(days=400)


def code_of(call):
    try:
        call()
    except ApplicationError as error:
        return error.code
    return None


@tag("mariadb")
class EditSubmittedTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        publish_contract()
        cls.person = customer("edit.submitted@example.org")
        cls.other = customer("edit.other@example.org")
        cls.application, cls.building = ready_draft(cls.person, feed_in=True, number=10)
        applications.submit(cls.person, cls.application, today=TODAY)
        cls.application.refresh_from_db()
        cls.draw = ApplicationMeteringPoint.objects.get(building=cls.building, direction=Direction.CONSUMPTION)
        cls.feed_in = ApplicationMeteringPoint.objects.get(building=cls.building, direction=Direction.FEED_IN)

    def setUp(self):
        configure_numbers()

    # --- state -------------------------------------------------------------------------------------------------

    def test_the_application_is_submitted_and_editable(self):
        self.assertEqual(self.application.state, Application.State.SUBMITTED)
        self.assertTrue(applications.can_edit_submitted(self.application))

    def test_a_draft_and_an_in_review_application_are_not_editable_here(self):
        draft, _building = ready_draft(self.other, egid=90004270, number=30)
        self.assertEqual(
            code_of(lambda: applications.edit_submitted_start(self.other, draft, asap=True)), "wrong_state"
        )
        self.assertFalse(applications.can_edit_submitted(draft))

    def test_every_service_refuses_with_review_started_once_the_leg_checks(self):
        person = customer("edit.review@example.org")
        application, building = ready_draft(person, feed_in=True, egid=90004271, number=40)
        applications.submit(person, application, today=TODAY)
        draw = ApplicationMeteringPoint.objects.get(building=building, direction=Direction.CONSUMPTION)
        application_staff.start_review(application, staff_user("edit.staff@example.org"))
        application.refresh_from_db()
        self.assertFalse(applications.can_edit_submitted(application))
        calls = {
            "point": lambda: applications.edit_submitted_metering_point(person, application, draw.pk, entered_number="41"),
            "start": lambda: applications.edit_submitted_start(person, application, asap=True),
            "bank": lambda: applications.edit_submitted_bank_connection(person, application, OTHER_IBAN),
            "building": lambda: applications.edit_submitted_building(
                person, application, building.pk, street="Neuweg", house_number="1", postal_code="3000", town="Bern"
            ),
        }
        for name, call in calls.items():
            with self.subTest(name), directory(candidate(90009999, street="Neuweg", house_number="1", postal_code="3000", town="Bern")):
                self.assertEqual(code_of(call), "review_started")
        draw.refresh_from_db()
        self.assertEqual(draw.number, full_number(40))

    # --- another account ---------------------------------------------------------------------------------------

    def test_another_account_gets_not_found_for_every_service(self):
        calls = {
            "point": lambda: applications.edit_submitted_metering_point(
                self.other, self.application, self.draw.pk, entered_number="99"
            ),
            "start": lambda: applications.edit_submitted_start(self.other, self.application, asap=True),
            "bank": lambda: applications.edit_submitted_bank_connection(self.other, self.application, OTHER_IBAN),
            "building": lambda: applications.edit_submitted_building(
                self.other, self.application, self.building.pk, street="Neuweg", house_number="1", postal_code="3000",
                town="Bern",
            ),
        }
        for name, call in calls.items():
            with self.subTest(name), directory(candidate(90009999, street="Neuweg", house_number="1", postal_code="3000", town="Bern")):
                self.assertIn(code_of(call), ("not_found", "not_allowed"))
        self.draw.refresh_from_db()
        self.assertEqual(self.draw.number, full_number(10))

    def test_a_row_of_another_application_is_not_found(self):
        mine = customer("edit.second@example.org")
        application, _building = ready_draft(mine, egid=90004272, number=50)
        applications.submit(mine, application, today=TODAY)
        self.assertEqual(
            code_of(lambda: applications.edit_submitted_metering_point(
                mine, application, self.draw.pk, entered_number="51"
            )),
            "not_found",
        )

    # --- a metering point --------------------------------------------------------------------------------------

    def test_the_number_the_name_and_the_figures_of_a_point_change(self):
        applications.edit_submitted_metering_point(
            self.person, self.application, self.feed_in.pk, entered_number="12", name="Dach", pv_power_kwp=Decimal("9.5"),
            battery_kwh=Decimal("5"), wallbox_kw=None,
        )
        row = ApplicationMeteringPoint.objects.get(pk=self.feed_in.pk)
        self.assertEqual((row.number, row.name), (full_number(12), "Dach"))
        self.assertEqual((row.pv_power_kwp, row.battery_kwh, row.wallbox_kw), (Decimal("9.5"), Decimal("5"), None))
        self.application.refresh_from_db()
        self.assertEqual(self.application.state, Application.State.SUBMITTED)

    def test_a_number_of_another_point_of_the_application_is_refused(self):
        self.assertEqual(
            code_of(lambda: applications.edit_submitted_metering_point(
                self.person, self.application, self.draw.pk, entered_number="11"
            )),
            "number_in_application",
        )
        self.draw.refresh_from_db()
        self.assertEqual(self.draw.number, full_number(10))

    def test_the_same_number_again_is_fine(self):
        applications.edit_submitted_metering_point(
            self.person, self.application, self.draw.pk, entered_number="10", name="Haus"
        )
        self.draw.refresh_from_db()
        self.assertEqual((self.draw.number, self.draw.name), (full_number(10), "Haus"))

    def test_figures_are_for_a_feed_in_and_a_negative_figure_is_refused(self):
        with self.assertRaises(ValidationError):
            applications.edit_submitted_metering_point(
                self.person, self.application, self.feed_in.pk, entered_number="11", pv_power_kwp=Decimal("-1")
            )
        with self.assertRaises((ApplicationError, ValidationError)):
            applications.edit_submitted_metering_point(
                self.person, self.application, self.draw.pk, entered_number="10", pv_power_kwp=Decimal("3")
            )
        self.feed_in.refresh_from_db()
        self.assertNotEqual(self.feed_in.pv_power_kwp, Decimal("-1"))

    # --- the start ---------------------------------------------------------------------------------------------

    def test_the_start_changes_between_asap_and_a_date(self):
        later = applications.earliest_start_date(TODAY) + timedelta(days=10)
        applications.edit_submitted_start(self.person, self.application, desired_date=later, today=TODAY)
        self.application.refresh_from_db()
        self.assertEqual(
            (self.application.start_choice, self.application.desired_start_date),
            (Application.StartChoice.DATE, later),
        )
        applications.edit_submitted_start(self.person, self.application, asap=True, today=TODAY)
        self.application.refresh_from_db()
        self.assertEqual(
            (self.application.start_choice, self.application.desired_start_date), (Application.StartChoice.ASAP, None)
        )

    def test_a_start_that_is_too_early_or_not_one_of_the_two_is_refused(self):
        early = applications.earliest_start_date(TODAY) - timedelta(days=1)
        error = None
        try:
            applications.edit_submitted_start(self.person, self.application, desired_date=early, today=TODAY)
        except ApplicationError as caught:
            error = caught
        self.assertEqual(error.code, "start_invalid")
        self.assertEqual(error.details["earliest"], applications.earliest_start_date(TODAY))
        self.assertEqual(
            code_of(lambda: applications.edit_submitted_start(self.person, self.application, today=TODAY)),
            "start_invalid",
        )
        self.assertEqual(
            code_of(lambda: applications.edit_submitted_start(
                self.person, self.application, asap=True, desired_date=early + timedelta(days=30), today=TODAY
            )),
            "start_invalid",
        )

    # --- the bank connection -----------------------------------------------------------------------------------

    def test_the_iban_changes_through_the_account_service(self):
        self.assertTrue(applications.edit_submitted_bank_connection(self.person, self.application, OTHER_IBAN))
        self.assertEqual(own_iban.own_iban(self.person), OTHER_IBAN)
        self.assertFalse(applications.edit_submitted_bank_connection(self.person, self.application, OTHER_IBAN))
        with self.assertRaises(ValidationError):
            applications.edit_submitted_bank_connection(self.person, self.application, "CH00 not an iban")
        self.assertEqual(own_iban.own_iban(self.person), OTHER_IBAN)

    def test_without_a_feed_in_there_is_no_bank_connection_to_change(self):
        person = customer("edit.nofeed@example.org")
        application, _building = ready_draft(person, egid=90004273, number=60)
        applications.submit(person, application, today=TODAY)
        self.assertEqual(
            code_of(lambda: applications.edit_submitted_bank_connection(person, application, GOOD_IBAN)),
            "step_not_available",
        )

    # --- the address of a building -----------------------------------------------------------------------------

    def test_the_address_of_a_building_is_replaced_and_its_points_stay(self):
        with directory(candidate(90005001, street="Neuweg", house_number="3", postal_code="3000", town="Bern")):
            choice = applications.edit_submitted_building(
                self.person, self.application, self.building.pk, street="Neuweg", house_number="3",
                postal_code="3000", town="Bern", name="Scheune",
            )
        row = ApplicationBuilding.objects.get(pk=self.building.pk)
        self.assertEqual((row.egid, row.name), (90005001, "Scheune"))
        self.assertEqual(choice.building.pk, self.building.pk)
        self.assertEqual(row.metering_points.count(), 2)
        self.application.refresh_from_db()
        self.assertEqual(self.application.state, Application.State.SUBMITTED)
        self.assertTrue(
            any(entry.event == HistoryEvent.APPLICATION_BUILDING_SET for entry in entries_for(self.application))
        )

    def test_an_address_that_is_already_part_of_the_application_is_refused(self):
        second = customer("edit.twobuildings@example.org")
        application, _first = ready_draft(second, egid=90004274, number=70)
        with directory(candidate(90005002, street="Zweitweg", house_number="1")):
            added = applications.choose_building(
                second, application, street="Zweitweg", house_number="1", postal_code="3063", town="Ittigen"
            )
        applications.add_metering_point(
            second, application, added.building.pk, direction=Direction.CONSUMPTION, entered_number="71"
        )
        applications.submit(second, application, today=TODAY)
        # The second building cannot take the address of the first.
        with directory(candidate(90004274)):
            self.assertEqual(
                code_of(lambda: applications.edit_submitted_building(
                    second, application, added.building.pk, street="Ittigenstrasse", house_number="51",
                    postal_code="3063", town="Ittigen",
                )),
                "already_added",
            )

    def test_a_fixed_building_keeps_its_address(self):
        ApplicationBuilding.objects.filter(pk=self.building.pk).update(preselected=True)
        with directory(candidate(90005003, street="Neuweg", house_number="9", postal_code="3000", town="Bern")):
            self.assertEqual(
                code_of(lambda: applications.edit_submitted_building(
                    self.person, self.application, self.building.pk, street="Neuweg", house_number="9",
                    postal_code="3000", town="Bern",
                )),
                "building_fixed",
            )
        ApplicationBuilding.objects.filter(pk=self.building.pk).update(preselected=False)

    def test_a_bad_address_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            applications.edit_submitted_building(
                self.person, self.application, self.building.pk, street="", house_number="1", postal_code="3000",
                town="Bern",
            )

    def test_an_edit_sends_no_mail_and_keeps_the_queue_position(self):
        before = (MailJob.objects.count(), self.application.submitted_at)
        applications.edit_submitted_start(self.person, self.application, asap=True, today=TODAY)
        applications.edit_submitted_metering_point(
            self.person, self.application, self.draw.pk, entered_number="10", name="Neu"
        )
        self.application.refresh_from_db()
        self.assertEqual((MailJob.objects.count(), self.application.submitted_at), before)

    def test_the_number_and_the_building_keep_the_queue_position(self):
        """The customer registers the own meter and the grid operator checks the number (no race, owner 2026-10-28)."""
        before = Application.objects.get(pk=self.application.pk).submitted_at
        applications.edit_submitted_metering_point(
            self.person, self.application, self.feed_in.pk, entered_number="12"
        )
        with directory(candidate(90005009, street="Neuweg", house_number="5", postal_code="3000", town="Bern")):
            applications.edit_submitted_building(
                self.person, self.application, self.building.pk, street="Neuweg", house_number="5",
                postal_code="3000", town="Bern",
            )
        self.assertEqual(Application.objects.get(pk=self.application.pk).submitted_at, before)

    def test_a_number_that_belongs_to_another_open_application_is_still_refused(self):
        second = customer("edit.swap@example.org")
        application, building = ready_draft(second, feed_in=True, egid=90004280, number=90)
        applications.submit(second, application, today=TODAY)
        point = ApplicationMeteringPoint.objects.get(building=building, direction=Direction.CONSUMPTION)
        self.assertEqual(
            code_of(lambda: applications.edit_submitted_metering_point(second, application, point.pk, entered_number="10")),
            "number_unavailable",
        )
        self.assertEqual(
            code_of(lambda: applications.edit_submitted_metering_point(
                self.person, self.application, self.draw.pk, entered_number="90"
            )),
            "number_unavailable",
        )
        point.refresh_from_db()
        self.draw.refresh_from_db()
        self.assertEqual((point.number, self.draw.number), (full_number(90), full_number(10)))
    def test_a_number_of_a_stored_point_of_another_account_is_refused_neutrally(self):
        assign_person(make_metering_point(80), self.other)
        make_metering_point(81)
        for typed in ("80", "81"):
            with self.subTest(typed):
                self.assertEqual(
                    code_of(lambda: applications.edit_submitted_metering_point(
                        self.person, self.application, self.draw.pk, entered_number=typed
                    )),
                    "number_unavailable",
                )
        self.draw.refresh_from_db()
        self.assertEqual(self.draw.number, full_number(10))

    def test_a_number_in_an_open_application_of_another_account_is_refused_neutrally(self):
        application, _building = ready_draft(self.other, egid=90004281, number=82)
        applications.submit(self.other, application, today=TODAY)
        self.assertEqual(
            code_of(lambda: applications.edit_submitted_metering_point(
                self.person, self.application, self.draw.pk, entered_number="82"
            )),
            "number_unavailable",
        )

    # --- the history (security review M-2) -------------------------------------------------------------------

    def test_a_number_and_a_start_edit_are_in_the_history_with_field_names_only(self):
        applications.edit_submitted_metering_point(
            self.person, self.application, self.draw.pk, entered_number="13", name="Haus"
        )
        applications.edit_submitted_start(
            self.person, self.application, desired_date=applications.earliest_start_date(TODAY) + timedelta(days=9),
            today=TODAY,
        )
        entries = [e for e in entries_for(self.application) if e.event == HistoryEvent.METERING_POINT_DATA_CHANGED]
        self.assertEqual([e.detail["fields"] for e in entries].count(["start"]), 1)
        self.assertTrue(any(set(e.detail["fields"]) >= {"number", "name"} for e in entries))
        self.assertTrue(all(e.detail.get("after_submission") for e in entries))
        text = " ".join(str(e.detail) for e in entries)
        self.assertNotIn(full_number(13), text)
        self.assertNotIn("Haus", text)

    def test_an_edit_without_a_change_writes_no_history(self):
        count = len(list(entries_for(self.application)))
        applications.edit_submitted_metering_point(
            self.person, self.application, self.draw.pk, entered_number="10"
        )
        applications.edit_submitted_start(
            self.person, self.application, asap=self.application.start_choice == Application.StartChoice.ASAP,
            desired_date=self.application.desired_start_date, today=TODAY,
        )
        self.assertEqual(len(list(entries_for(self.application))), count)


@tag("mariadb")
class StoredPointFiguresTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("figures.mine@example.org")
        cls.other = customer("figures.other@example.org")
        cls.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        cls.feed_in = make_metering_point(
            1, Direction.FEED_IN, cls.location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO,
            start_date=LONG_AGO,
        )
        assign_person(cls.feed_in, cls.mine, valid_from=LONG_AGO)
        cls.draw = make_metering_point(
            2, Direction.CONSUMPTION, cls.location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO,
            start_date=LONG_AGO,
        )
        assign_person(cls.draw, cls.mine, valid_from=LONG_AGO)

    def test_the_name_and_the_figures_change_and_the_history_lists_field_names_only(self):
        changed = buildings.set_metering_point_name(
            self.mine, self.feed_in, "Dach", pv_power_kwp=Decimal("8.4"), battery_kwh=Decimal("6"), wallbox_kw=None
        )
        self.assertTrue(changed)
        self.feed_in.refresh_from_db()
        self.assertEqual((self.feed_in.name, self.feed_in.pv_power_kwp, self.feed_in.battery_kwh), ("Dach", Decimal("8.4"), Decimal("6")))
        entry = [e for e in entries_for(self.feed_in) if e.event == HistoryEvent.METERING_POINT_DATA_CHANGED][-1]
        self.assertEqual(sorted(entry.detail["fields"]), ["battery_kwh", "name", "pv_power_kwp"])
        self.assertNotIn("8.4", str(entry.detail))

    def test_nothing_changed_returns_false_and_writes_no_history(self):
        buildings.set_metering_point_name(self.mine, self.feed_in, "Dach", pv_power_kwp=Decimal("8.4"))
        count = len(list(entries_for(self.feed_in)))
        self.assertFalse(buildings.set_metering_point_name(self.mine, self.feed_in, "Dach", pv_power_kwp=Decimal("8.4")))
        self.assertEqual(len(list(entries_for(self.feed_in))), count)

    def test_the_name_alone_still_works_for_a_draw(self):
        self.assertTrue(buildings.set_metering_point_name(self.mine, self.draw, "Wohnhaus"))
        self.draw.refresh_from_db()
        self.assertEqual(self.draw.name, "Wohnhaus")

    def test_figures_on_a_draw_and_unknown_figures_are_refused(self):
        self.assertEqual(
            code_of(lambda: buildings.set_metering_point_name(self.mine, self.draw, "X", pv_power_kwp=Decimal("1"))),
            "plant_data_feed_in_only",
        )
        self.assertEqual(
            code_of(lambda: buildings.set_metering_point_name(self.mine, self.feed_in, "X", number="CH0")),
            "not_allowed",
        )

    def test_a_negative_figure_is_refused_and_nothing_is_saved(self):
        with self.assertRaises(ValidationError):
            buildings.set_metering_point_name(self.mine, self.feed_in, "Neu", pv_power_kwp=Decimal("-3"))
        self.feed_in.refresh_from_db()
        self.assertNotEqual(self.feed_in.name, "Neu")

    def test_another_account_cannot_change_the_point(self):
        self.assertEqual(
            code_of(lambda: buildings.set_metering_point_name(self.other, self.feed_in, "Fremd", pv_power_kwp=Decimal("1"))),
            "not_found",
        )
        self.feed_in.refresh_from_db()
        self.assertNotEqual(self.feed_in.name, "Fremd")
