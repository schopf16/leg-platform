"""The customer's application (D-103): draft services, steps, identity of buildings by EGID, number rules, neutral
refusals, caps, submit/withdraw/cancel, and the safety of a rollback."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, tag

from leg_platform.accounts.models import MailJob
from leg_platform.community import (
    application_common,
    applications,
    buildings,
    contracts,
    history,
    own_iban,
)
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Application,
    ApplicationBuilding,
    ApplicationMeteringPoint,
    ApplicationNotice,
    Direction,
    Location,
    MeteringPoint,
    PersonAssignment,
)
from leg_platform.community.tests.helpers import (
    assign_person,
    make_location,
    make_metering_point,
    number,
    staff_user,
)
from leg_platform.community.tests.onboarding import (
    ADDRESS,
    GOOD_IBAN,
    TODAY,
    candidate,
    configure_numbers,
    customer,
    directory,
    full_number,
    publish_contract,
    ready_draft,
    start_with_building,
)

EGID = 90004269


def code_of(call):
    try:
        call()
    except ApplicationError as error:
        return error.code
    raise AssertionError("ApplicationError was not raised.")


@tag("mariadb")
class StartAndStepsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("steps.customer@example.org")
        cls.staff = staff_user("steps.staff@example.org")

    def setUp(self):
        configure_numbers()

    def test_a_new_draft_has_five_open_steps_and_no_start_date(self):
        application = applications.start_draft(self.person)

        current = applications.progress(application)
        self.assertEqual(application.state, Application.State.DRAFT)
        self.assertEqual((current.done, current.total, current.next_step), (0, 4, applications.BUILDING))
        self.assertFalse(current.step(applications.BANK_CONNECTION).applicable)
        self.assertEqual(application.start_choice, "")
        self.assertIsNone(application.desired_start_date)
        self.assertTrue(history.entries_for(application).filter(event=HistoryEvent.APPLICATION_STARTED).exists())

    def test_the_progress_follows_the_steps_and_the_wizard_state_is_stored(self):
        application, building = start_with_building(self.person)
        application.refresh_from_db()
        self.assertEqual(application.wizard_step, applications.METERING_POINTS)

        applications.add_metering_point(
            self.person, application, building.pk, direction=Direction.CONSUMPTION, entered_number="1"
        )
        application.refresh_from_db()
        current = applications.progress(application)
        self.assertEqual((current.done, current.total, current.next_step), (2, 4, applications.CONTRACT))
        self.assertEqual(application.wizard_step, applications.CONTRACT)
        # The start step exists but is not available before the contract.
        self.assertFalse(current.step(applications.START).available)

    def test_a_feed_in_adds_the_bank_step_and_a_stored_iban_is_already_on_file(self):
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        application, building = start_with_building(self.person)
        applications.add_metering_point(
            self.person, application, building.pk, direction=Direction.CONSUMPTION, entered_number="1"
        )
        self.assertFalse(applications.progress(application).step(applications.BANK_CONNECTION).applicable)
        applications.add_metering_point(
            self.person, application, building.pk, direction=Direction.FEED_IN, entered_number="2"
        )

        current = applications.progress(application)
        self.assertEqual(current.total, 5)
        self.assertEqual(current.step(applications.BANK_CONNECTION).state, applications.KNOWN)
        self.assertEqual(current.step(applications.METERING_POINTS).state, applications.DONE)  # never "known"

    def test_a_contract_accepted_before_the_application_is_already_on_file(self):
        version = publish_contract(self.staff)
        contracts.accept_contract(self.person, version.pk, agrees_contract=True)
        application = applications.start_draft(self.person)

        self.assertEqual(applications.progress(application).step(applications.CONTRACT).state, applications.KNOWN)

    def test_the_wizard_cannot_jump_ahead(self):
        application, _building = start_with_building(self.person)
        applications.go_to_step(self.person, application, applications.BUILDING)  # back
        self.assertEqual(code_of(lambda: applications.go_to_step(self.person, application, applications.CONTRACT)),
                         "step_not_available")
        self.assertEqual(code_of(lambda: applications.go_to_step(self.person, application, "nonsense")),
                         "step_not_available")
        self.assertEqual(code_of(lambda: applications.go_to_step(self.person, application, applications.BANK_CONNECTION)),
                         "step_not_available")

    def test_post_submit_progress_names_five_steps(self):
        application = Application(state=Application.State.IN_REVIEW)
        steps = dict(applications.post_submit_progress(application))
        self.assertEqual(
            steps,
            {"submitted": "done", "review": "done", "operator_registration": "next",
             "operator_confirmation": "open", "start": "open"},
        )


@tag("mariadb")
class BuildingChoiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("building.customer@example.org")
        cls.other = customer("building.other@example.org")
        cls.staff = staff_user("building.staff@example.org")

    def setUp(self):
        configure_numbers()

    def test_a_new_building_is_a_draft_row_and_no_location(self):
        application = applications.start_draft(self.person)
        with directory(candidate(EGID)) as lookup:
            choice = applications.choose_building(self.person, application, **ADDRESS, name="  Wohnhaus  ")

        self.assertFalse(choice.existing)
        self.assertEqual(choice.building.egid, EGID)
        self.assertEqual(choice.building.name, "Wohnhaus")
        self.assertEqual(Location.objects.count(), 0)
        lookup.assert_called_once_with("Ittigenstrasse", "51", "3063", "Ittigen", user=self.person)

    def test_the_egid_must_be_one_the_directory_offers_for_the_address(self):
        application = applications.start_draft(self.person)
        with directory(candidate(EGID)):
            self.assertEqual(
                code_of(lambda: applications.choose_building(self.person, application, **ADDRESS, egid=11111111)),
                "egid_not_offered",
            )
        with directory():
            self.assertEqual(
                code_of(lambda: applications.choose_building(self.person, application, **ADDRESS)), "building_not_found"
            )
        for bad in ("abc", "0", "-5", "1" * 10, True, 1.5):
            with directory(candidate(EGID)), self.subTest(bad=bad):
                self.assertEqual(
                    code_of(lambda: applications.choose_building(self.person, application, **ADDRESS, egid=bad)),
                    "egid_invalid",
                )
        self.assertEqual(ApplicationBuilding.objects.count(), 0)

    def test_several_buildings_for_one_address_need_a_choice(self):
        application = applications.start_draft(self.person)
        with directory(candidate(EGID), candidate(EGID + 1)):
            with self.assertRaises(ApplicationError) as caught:
                applications.choose_building(self.person, application, **ADDRESS)
            self.assertEqual((caught.exception.code, caught.exception.details["egids"]), ("ambiguous", [EGID, EGID + 1]))
            choice = applications.choose_building(self.person, application, **ADDRESS, egid=EGID + 1)
        self.assertEqual(choice.building.egid, EGID + 1)

    def test_without_the_lookup_of_the_directory_nothing_is_chosen(self):
        application = applications.start_draft(self.person)
        with patch.object(buildings, "find_candidates", side_effect=ApplicationError("directory_unavailable")):
            self.assertEqual(
                code_of(lambda: applications.choose_building(self.person, application, **ADDRESS)),
                "directory_unavailable",
            )

    def test_an_invalid_address_or_name_is_a_validation_error(self):
        application = applications.start_draft(self.person)
        with directory(candidate(EGID)):
            with self.assertRaises(ValidationError):
                applications.choose_building(self.person, application, **{**ADDRESS, "postal_code": "30"})
            with self.assertRaises(ValidationError):
                applications.choose_building(self.person, application, **ADDRESS, name="x" * 81)
            with self.assertRaises(ValidationError):
                applications.choose_building(self.person, application, **ADDRESS, name="bad\x00name")

    def test_the_same_egid_attaches_to_the_own_stored_building_with_a_note(self):
        location = make_location(street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen", egid=EGID)
        assign_person(make_metering_point(1, location=location), self.person)
        application = applications.start_draft(self.person)

        with directory(candidate(EGID)):
            choice = applications.choose_building(self.person, application, **ADDRESS)

        self.assertTrue(choice.existing)
        self.assertEqual(choice.building.location, location)
        self.assertEqual(Location.objects.count(), 1)

    def test_a_stored_building_without_any_account_is_attached_too(self):
        location = make_location(street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen", egid=EGID)
        application = applications.start_draft(self.person)
        with directory(candidate(EGID)):
            choice = applications.choose_building(self.person, application, **ADDRESS)
        self.assertEqual((choice.existing, choice.building.location), (True, location))

    def test_a_stored_building_without_egid_at_the_same_address_is_the_same_building(self):
        location = make_location(street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen")
        application = applications.start_draft(self.person)
        with directory(candidate(EGID)):
            choice = applications.choose_building(self.person, application, **ADDRESS)
        self.assertEqual(choice.building.location, location)

    def assert_neutral_refusal(self, call):
        error = None
        try:
            call()
        except ApplicationError as raised:
            error = raised
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "building_unavailable")
        self.assertEqual(error.details, {})
        self.assertEqual(str(error), "building_unavailable")
        return error

    def test_another_accounts_building_is_attached_to_and_only_the_own_data_is_involved(self):
        # A multi-family building is shared: the same EGID always attaches, claimed by another account or not.
        location = make_location(street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen", egid=EGID)
        foreign_point = make_metering_point(1, location=location)
        assign_person(foreign_point, self.other)
        application = applications.start_draft(self.person)

        with directory(candidate(EGID)):
            choice = applications.choose_building(self.person, application, **ADDRESS, name="Meins")

        self.assertEqual((choice.existing, choice.building.location), (True, location))
        self.assertEqual(ApplicationNotice.objects.count(), 0)
        self.assertEqual(Location.objects.count(), 1)
        # The other account's point does not occupy this customer's draw: the customer adds an own one.
        configure_numbers()
        added = applications.add_metering_point(
            self.person, application, choice.building.pk, direction=Direction.CONSUMPTION, entered_number="2"
        )
        self.assertEqual(added.point.number, full_number(2))
        self.assertEqual(applications.progress(application).step(applications.METERING_POINTS).state, applications.DONE)
        # Nothing of the other account was touched.
        self.assertEqual(list(PersonAssignment.objects.filter(metering_point=foreign_point).values_list("person", flat=True)),
                         [self.other.pk])

    def test_two_customers_with_drafts_for_the_same_new_building_do_not_block_each_other(self):
        first, _building = start_with_building(self.other, EGID)
        second, building = start_with_building(self.person, EGID)
        self.assertEqual(building.egid, EGID)
        self.assertEqual(Application.objects.count(), 2)

    def test_the_neutral_refusal_is_the_same_for_an_archived_building_and_a_contradicting_egid(self):
        def at(street):
            return {"street": street, "house_number": "1", "postal_code": "3000", "town": "Bern"}

        archived_location = make_location(**at("Archivweg"), egid=EGID + 20, is_archived=True)
        mismatch = make_location(**at("Vierte"), egid=EGID + 99)
        for egid, address in ((EGID + 20, at("Archivweg")), (EGID + 30, at("Vierte"))):
            application = applications.start_draft(self.person)
            with self.subTest(egid=egid), directory(candidate(egid, **address)):
                self.assert_neutral_refusal(lambda: applications.choose_building(self.person, application, **address))
            applications.cancel(self.person, application)
        self.assertEqual(
            sorted(ApplicationNotice.objects.values_list("kind", "egid")),
            [("building_unavailable", EGID + 20), ("building_unavailable", EGID + 30)],
        )
        self.assertTrue({archived_location.pk, mismatch.pk} <= set(Location.objects.values_list("pk", flat=True)))
        self.assertEqual(ApplicationBuilding.objects.filter(application__person=self.person).count(), 0)

    def test_the_names_of_the_building_are_kept_in_the_draft_per_account(self):
        application = applications.start_draft(self.person)
        with directory(candidate(EGID)):
            choice = applications.choose_building(self.person, application, **ADDRESS, name="Haus", description="Block A")
        applications.set_draft_building_details(self.person, application, choice.building.pk, name="Haus 2", description="B")
        choice.building.refresh_from_db()
        self.assertEqual((choice.building.name, choice.building.description), ("Haus 2", "B"))
        self.assertEqual(
            code_of(lambda: applications.set_draft_building_details(self.other, application, choice.building.pk, name="x")),
            "not_found",
        )

    def test_the_same_building_twice_in_an_application_or_in_two_own_applications(self):
        application, _building = start_with_building(self.person)
        with directory(candidate(EGID)):
            self.assertEqual(
                code_of(lambda: applications.choose_building(self.person, application, **ADDRESS)), "already_added"
            )
            second = applications.start_draft(self.person)
            self.assertEqual(
                code_of(lambda: applications.choose_building(self.person, second, **ADDRESS)), "already_in_progress"
            )

    def test_buildings_per_application_are_capped(self):
        application = applications.start_draft(self.person)
        with patch.object(application_common, "MAX_BUILDINGS_PER_APPLICATION", 2), patch.object(
            applications, "MAX_BUILDINGS_PER_APPLICATION", 2
        ):
            for index in range(2):
                with directory(candidate(EGID + index)):
                    applications.choose_building(self.person, application, **ADDRESS)
            with directory(candidate(EGID + 5)):
                self.assertEqual(
                    code_of(lambda: applications.choose_building(self.person, application, **ADDRESS)),
                    "too_many_buildings",
                )

    def test_a_building_can_be_removed_unless_it_is_fixed(self):
        application, building = start_with_building(self.person)
        applications.remove_building(self.person, application, building.pk)
        self.assertEqual(application.buildings.count(), 0)

        location = make_location(egid=EGID + 40)
        assign_person(make_metering_point(8, location=location), self.person)
        fixed = applications.start_draft(self.person, location=location)
        row = fixed.buildings.get()
        self.assertEqual(code_of(lambda: applications.remove_building(self.person, fixed, row.pk)), "building_fixed")


@tag("mariadb")
class AddMeteringPointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("point.customer@example.org")
        cls.other = customer("point.other@example.org")

    def setUp(self):
        configure_numbers()
        self.application, self.building = start_with_building(self.person)

    def add(self, direction=Direction.CONSUMPTION, last="1", **extra):
        return applications.add_metering_point(
            self.person, self.application, self.building.pk, direction=direction, entered_number=last, **extra
        )

    def test_the_number_is_completed_by_the_rules_of_d_102(self):
        added = self.add(last="23")
        self.assertEqual(added.point.number, full_number("23"))
        self.assertEqual(len(added.point.number), 33)
        self.assertEqual(self.add(Direction.FEED_IN, last="00000000000000000000099").point.number, full_number(99))

    def test_invalid_numbers_are_refused(self):
        for bad in ("", "  ", "ä", "1" * 21, "1.5", "12 3!"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.add(last=bad)
        self.assertEqual(ApplicationMeteringPoint.objects.count(), 0)

    def test_white_space_in_the_number_is_ignored_and_a_pasted_complete_number_loses_its_prefix(self):
        # D-109: the blocks of the display (no-break spaces, or normal ones after a copy) are no part of the number.
        self.assertEqual(self.add(Direction.CONSUMPTION, last="12 34").point.number, full_number(1234))
        pasted = "CH1A2B3C4D5E6\u00a00000\u00a00000\u00a00000\u00a00000\u00a00099"
        self.assertEqual(self.add(Direction.FEED_IN, last=pasted).point.number, full_number(99))

    def test_without_the_identifier_of_the_settings_nothing_is_added(self):
        from leg_platform.community.models import ManagementSettings

        ManagementSettings.objects.filter(id=1).update(identifier="")
        self.assertEqual(code_of(self.add), "prefix_not_configured")

    def test_one_draw_and_at_most_one_feed_in_per_building(self):
        self.add(Direction.CONSUMPTION, "1")
        self.assertEqual(code_of(lambda: self.add(Direction.CONSUMPTION, "2")), "direction_taken")
        self.add(Direction.FEED_IN, "3")
        self.assertEqual(code_of(lambda: self.add(Direction.FEED_IN, "4")), "direction_taken")
        self.assertEqual(code_of(lambda: self.add("sideways", "5")), "direction_invalid")

    def test_the_same_number_twice_in_an_application_is_refused(self):
        self.add(Direction.CONSUMPTION, "1")
        self.assertEqual(code_of(lambda: self.add(Direction.FEED_IN, "1")), "number_in_application")

    def test_a_number_that_is_stored_for_the_account_itself_says_so(self):
        assign_person(make_metering_point(7), self.person)
        self.assertEqual(code_of(lambda: self.add(last="7")), "number_own")
        self.assertEqual(ApplicationNotice.objects.count(), 0)

    def test_a_number_of_another_account_is_the_neutral_refusal_with_a_notice(self):
        assign_person(make_metering_point(7), self.other)
        make_metering_point(8)  # stored without any account: the same answer

        for last in ("7", "8"):
            with self.subTest(last=last):
                error = None
                try:
                    self.add(last=last)
                except ApplicationError as raised:
                    error = raised
                self.assertEqual((error.code, error.details), ("number_unavailable", {}))
        self.assertEqual(
            sorted(ApplicationNotice.objects.values_list("kind", flat=True)), ["number_unavailable"] * 2
        )
        self.assertEqual(ApplicationMeteringPoint.objects.count(), 0)

    def test_a_number_in_an_application_in_flight_of_another_account_is_refused_a_draft_is_not(self):
        other_application, _building = ready_draft(self.other, egid=EGID + 1, number=5)
        # Another account's draft holds the number 5, and it blocks nobody.
        self.assertEqual(self.add(last="5").point.number, full_number(5))
        applications.remove_metering_point(self.person, self.application, ApplicationMeteringPoint.objects.get(
            building=self.building).pk)

        applications.submit(self.other, other_application, today=TODAY)
        self.assertEqual(code_of(lambda: self.add(last="5")), "number_unavailable")

    def test_the_own_other_application_in_flight_says_own(self):
        other_application, _building = ready_draft(self.person, egid=EGID + 1, number=5)
        applications.submit(self.person, other_application, today=TODAY)
        self.assertEqual(code_of(lambda: self.add(last="5")), "number_own")

    def test_plant_figures_belong_to_a_feed_in_and_big_ones_only_warn(self):
        self.assertEqual(code_of(lambda: self.add(Direction.CONSUMPTION, "1", pv_power_kwp=Decimal("5"))),
                         "plant_data_feed_in_only")
        with self.assertRaises(ValidationError):
            self.add(Direction.FEED_IN, "3", pv_power_kwp=Decimal("-1"))
        added = self.add(Direction.FEED_IN, "2", pv_power_kwp=Decimal("5000"), battery_kwh=Decimal("10"), wallbox_kw=None)
        self.assertEqual(added.warnings, (("pv_power_kwp", "unusually_large"),))
        self.assertEqual(added.point.battery_kwh, Decimal("10"))

    def test_a_point_can_be_replaced_and_removed(self):
        point = self.add(last="1").point
        replaced = applications.replace_metering_point(
            self.person, self.application, point.pk, entered_number="2", name="Haus"
        )
        self.assertEqual((replaced.number, replaced.name), (full_number(2), "Haus"))
        # The own number can stay.
        applications.replace_metering_point(self.person, self.application, point.pk, entered_number="2")
        applications.remove_metering_point(self.person, self.application, point.pk)
        self.assertEqual(ApplicationMeteringPoint.objects.count(), 0)
        self.assertEqual(code_of(lambda: applications.remove_metering_point(self.person, self.application, point.pk)),
                         "not_found")

    def test_a_point_replaced_by_a_number_of_another_account_is_refused(self):
        point = self.add(last="1").point
        assign_person(make_metering_point(9), self.other)
        self.assertEqual(
            code_of(lambda: applications.replace_metering_point(self.person, self.application, point.pk, entered_number="9")),
            "number_unavailable",
        )
        point.refresh_from_db()
        self.assertEqual(point.number, full_number(1))

    def test_the_directions_of_a_stored_building_count(self):
        location = make_location(street="Stored", house_number="1", egid=EGID + 50)
        point = make_metering_point(20, location=location)
        assign_person(point, self.person)
        application = applications.start_draft(self.person, location=location)
        row = application.buildings.get()

        self.assertEqual(
            code_of(lambda: applications.add_metering_point(
                self.person, application, row.pk, direction=Direction.CONSUMPTION, entered_number="21")),
            "direction_taken",
        )
        applications.add_metering_point(
            self.person, application, row.pk, direction=Direction.FEED_IN, entered_number="22")
        self.assertEqual(applications.progress(application).step(applications.METERING_POINTS).state, applications.DONE)


    def test_the_points_of_other_customers_of_a_shared_building_do_not_occupy_the_directions(self):
        location = make_location(street="Shared", house_number="3", egid=EGID + 70)
        for last, direction in ((60, Direction.CONSUMPTION), (61, Direction.FEED_IN)):
            assign_person(make_metering_point(last, direction=direction, location=location), self.other)
        application = applications.start_draft(self.person)
        with directory(candidate(EGID + 70, street="Shared", house_number="3", postal_code="3000", town="Bern")):
            choice = applications.choose_building(
                self.person, application, street="Shared", house_number="3", postal_code="3000", town="Bern"
            )
        for last, direction in (("62", Direction.CONSUMPTION), ("63", Direction.FEED_IN)):
            applications.add_metering_point(
                self.person, application, choice.building.pk, direction=direction, entered_number=last
            )
        # One draw and one feed-in per customer: a second draw of the same customer is refused.
        self.assertEqual(
            code_of(lambda: applications.add_metering_point(
                self.person, application, choice.building.pk, direction=Direction.CONSUMPTION, entered_number="64")),
            "direction_taken",
        )


@tag("mariadb")
class AuthorizationTests(TestCase):
    """Every service reads its objects through the account: another account's ids are "not found" and change nothing."""

    @classmethod
    def setUpTestData(cls):
        cls.person = customer("auth.customer@example.org")
        cls.intruder = customer("auth.intruder@example.org")

    def setUp(self):
        configure_numbers()
        publish_contract()
        self.application, self.building = ready_draft(self.person, feed_in=True)
        self.point = ApplicationMeteringPoint.objects.filter(building=self.building).first()

    def test_another_account_cannot_use_the_application_or_its_rows(self):
        intruder, app, building, point = self.intruder, self.application, self.building, self.point
        calls = {
            "choose": lambda: applications.choose_building(intruder, app, **ADDRESS, egid=1),
            "remove_building": lambda: applications.remove_building(intruder, app, building.pk),
            "set_details": lambda: applications.set_draft_building_details(intruder, app, building.pk, name="x"),
            "add_point": lambda: applications.add_metering_point(
                intruder, app, building.pk, direction=Direction.CONSUMPTION, entered_number="77"),
            "replace_point": lambda: applications.replace_metering_point(intruder, app, point.pk, entered_number="77"),
            "remove_point": lambda: applications.remove_metering_point(intruder, app, point.pk),
            "bank": lambda: applications.save_bank_connection(intruder, app, GOOD_IBAN),
            "contract": lambda: applications.accept_contract_step(
                intruder, app, contracts.current_version().pk, agrees_contract=True),
            "start": lambda: applications.set_start(intruder, app, asap=True),
            "submit": lambda: applications.submit(intruder, app),
            "withdraw": lambda: applications.withdraw(intruder, app),
            "cancel": lambda: applications.cancel(intruder, app),
            "go_to_step": lambda: applications.go_to_step(intruder, app, applications.BUILDING),
        }
        with directory(candidate(1)):
            for name, call in calls.items():
                with self.subTest(call=name):
                    self.assertEqual(code_of(call), "not_found")
        with self.assertRaises(Application.DoesNotExist):
            applications.own_application(intruder, app.pk)
        self.assertEqual(self.application.buildings.count(), 1)
        self.assertEqual(Application.objects.filter(person=self.intruder).count(), 0)

    def test_the_intruder_with_own_rows_cannot_reach_a_foreign_row_id_in_an_own_application(self):
        own_application, own_building = start_with_building(self.intruder, egid=EGID + 3)
        # Own application, foreign building id and point id.
        self.assertEqual(
            code_of(lambda: applications.add_metering_point(
                self.intruder, own_application, self.building.pk, direction=Direction.CONSUMPTION, entered_number="5")),
            "not_found",
        )
        self.assertEqual(code_of(lambda: applications.remove_metering_point(self.intruder, own_application, self.point.pk)),
                         "not_found")
        self.assertEqual(code_of(lambda: applications.remove_building(self.intruder, own_application, self.building.pk)),
                         "not_found")

    def test_an_inactive_or_unnumbered_account_cannot_start(self):
        blocked = customer("auth.blocked@example.org")
        blocked.set_status(type(blocked).Status.BLOCKED)
        staff = staff_user("auth.staff@example.org")
        for person in (blocked, None):
            with self.subTest(person=person):
                self.assertEqual(code_of(lambda: applications.start_draft(person)), "not_allowed")
        self.assertIsNotNone(applications.start_draft(staff))  # a staff account is also a customer here

    def test_a_submitted_application_is_not_editable_by_the_customer(self):
        applications.submit(self.person, self.application, today=TODAY)
        with directory(candidate(5)):
            self.assertEqual(code_of(lambda: applications.choose_building(self.person, self.application, **ADDRESS)),
                             "not_editable")
        self.assertEqual(
            code_of(lambda: applications.add_metering_point(
                self.person, self.application, self.building.pk, direction=Direction.CONSUMPTION, entered_number="9")),
            "not_editable",
        )
        self.assertEqual(code_of(lambda: applications.cancel(self.person, self.application)), "not_editable")

    def test_the_start_with_a_foreign_building_id_is_the_same_answer_as_a_missing_one(self):
        mine = make_location(egid=EGID + 60)
        assign_person(make_metering_point(30, location=mine), self.person)
        foreign = make_location(street="Foreign", egid=EGID + 61)
        assign_person(make_metering_point(31, location=foreign), self.intruder)

        self.assertIsNotNone(applications.start_draft(self.person, location=mine))
        for target in (foreign, Location(pk=999_999)):
            with self.subTest(target=target.pk):
                self.assertEqual(code_of(lambda: applications.start_draft(self.person, location=target)), "not_found")


@tag("mariadb")
class StartDraftTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("draft.customer@example.org")

    def test_add_a_metering_point_starts_with_the_own_building_fixed(self):
        location = make_location(egid=EGID + 1)
        assign_person(make_metering_point(1, location=location), self.person)

        application = applications.start_draft(self.person, location=location)

        row = application.buildings.get()
        self.assertEqual((row.location, row.preselected, row.egid), (location, True, EGID + 1))
        current = applications.progress(application)
        self.assertEqual(current.step(applications.BUILDING).state, applications.KNOWN)

    def test_a_complete_building_and_a_building_in_progress_are_refused(self):
        location = make_location(egid=EGID + 2)
        assign_person(make_metering_point(1, location=location), self.person)
        assign_person(make_metering_point(2, direction=Direction.FEED_IN, location=location), self.person)
        self.assertEqual(code_of(lambda: applications.start_draft(self.person, location=location)), "building_complete")

        free = make_location(street="Free", egid=EGID + 3)
        assign_person(make_metering_point(3, location=free), self.person)
        applications.start_draft(self.person, location=free)
        self.assertEqual(code_of(lambda: applications.start_draft(self.person, location=free)), "already_in_progress")

    def test_drafts_and_open_applications_per_account_are_capped(self):
        with patch.object(applications, "MAX_DRAFTS_PER_PERSON", 2):
            applications.start_draft(self.person)
            applications.start_draft(self.person)
            self.assertEqual(code_of(lambda: applications.start_draft(self.person)), "too_many_drafts")
        with patch.object(applications, "MAX_OPEN_APPLICATIONS_PER_PERSON", 2):
            self.assertEqual(code_of(lambda: applications.start_draft(self.person)), "too_many_applications")

    def test_an_archived_location_is_not_found(self):
        location = make_location(egid=EGID + 4, is_archived=True)
        assign_person(make_metering_point(5, location=location), self.person)
        self.assertEqual(code_of(lambda: applications.start_draft(self.person, location=location)), "not_found")


@tag("mariadb")
class SubmitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("submit.customer@example.org")
        cls.staff = staff_user("submit.staff@example.org")

    def setUp(self):
        configure_numbers()

    def missing(self, application):
        with self.assertRaises(ApplicationError) as caught:
            applications.submit(self.person, application, today=TODAY)
        self.assertEqual(caught.exception.code, "incomplete")
        return caught.exception.details["missing"]

    def test_the_submission_lists_what_is_missing(self):
        application = applications.start_draft(self.person)
        self.assertEqual(self.missing(application), ["building", "metering_points", "contract", "start"])
        application, building = start_with_building(self.person, EGID + 9)
        self.assertEqual(self.missing(application), ["metering_points", "contract", "start"])

    def test_a_feed_in_needs_the_iban(self):
        publish_contract(self.staff)
        application, building = ready_draft(self.person, feed_in=True)
        # The bank connection is on the person: removing it (raw edit) makes the step open again.
        from leg_platform.community.models import PersonRecord

        PersonRecord.objects.filter(person=self.person).update(iban_encrypted="")
        self.assertEqual(self.missing(application), ["bank_connection"])

    def test_the_contract_and_the_start_are_needed(self):
        publish_contract(self.staff)
        application, _building = ready_draft(self.person, accept=False, start=False)
        self.assertEqual(self.missing(application), ["contract", "start"])
        self.assertEqual(
            code_of(lambda: applications.set_start(self.person, application, asap=True)), "contract_missing"
        )

    def test_the_start_date_is_never_before_three_months_and_never_prefilled(self):
        publish_contract(self.staff)
        application, _building = ready_draft(self.person, start=False)
        self.assertEqual((application.start_choice, application.desired_start_date), ("", None))
        earliest = applications.earliest_start_date(TODAY)
        self.assertEqual(earliest, date(2027, 1, 19))
        for wish in (date(2027, 1, 18), TODAY, date(2020, 1, 1)):
            with self.subTest(wish=wish), self.assertRaises(ApplicationError) as caught:
                applications.set_start(self.person, application, desired_date=wish, today=TODAY)
            self.assertEqual(caught.exception.code, "start_invalid")
        for kwargs in ({}, {"asap": True, "desired_date": earliest}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(code_of(lambda: applications.set_start(self.person, application, **kwargs)),
                                 "start_invalid")
        applications.set_start(self.person, application, desired_date=earliest, today=TODAY)
        application.refresh_from_db()
        self.assertEqual((application.start_choice, application.desired_start_date), ("date", earliest))

    def test_submit_sets_the_state_clears_the_clocks_and_sends_one_mail_per_address(self):
        publish_contract(self.staff)
        self.person.set_secondary_email("submit.second@example.org")
        application, _building = ready_draft(self.person)

        applications.submit(self.person, application, today=TODAY)

        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.SUBMITTED)
        self.assertIsNotNone(application.submitted_at)
        self.assertIsNone(application.contract_pending_since)
        self.assertEqual(
            sorted(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED).values_list("address", flat=True)),
            ["submit.customer@example.org", "submit.second@example.org"],
        )
        entry = history.entries_for(application).get(event=HistoryEvent.APPLICATION_SUBMITTED)
        self.assertEqual((entry.actor, entry.detail), (self.person, {}))
        self.assertEqual(code_of(lambda: applications.submit(self.person, application, today=TODAY)), "not_editable")

    def test_the_numbers_are_checked_again_at_the_submission(self):
        publish_contract(self.staff)
        application, _building = ready_draft(self.person)
        other = customer("submit.other@example.org")
        # The number was stored for another account after it was entered.
        MeteringPoint.objects.create(number=full_number(1), direction=Direction.CONSUMPTION)
        assign_person(MeteringPoint.objects.get(number=full_number(1)), other)

        self.assertEqual(code_of(lambda: applications.submit(self.person, application, today=TODAY)), "number_unavailable")
        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.DRAFT)
        self.assertEqual(ApplicationNotice.objects.get().kind, "number_unavailable")
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED).count(), 0)

    def test_the_building_is_checked_again_at_the_submission(self):
        publish_contract(self.staff)
        application, building = ready_draft(self.person, egid=EGID + 7)
        # The building was stored and archived after the customer chose it (it is shared, but not an archived one).
        make_location(street="Taken", house_number="1", egid=EGID + 7, is_archived=True)

        self.assertEqual(code_of(lambda: applications.submit(self.person, application, today=TODAY)), "building_unavailable")
        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.DRAFT)
        self.assertEqual(ApplicationNotice.objects.get().kind, "building_unavailable")

    def test_a_building_that_another_account_uses_does_not_stop_the_submission(self):
        publish_contract(self.staff)
        application, building = ready_draft(self.person, egid=EGID + 7)
        other = customer("submit.owner@example.org")
        stored = make_location(street="Taken", house_number="1", egid=EGID + 7)
        assign_person(make_metering_point(40, location=stored), other)

        applications.submit(self.person, application, today=TODAY)

        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.SUBMITTED)

    def test_withdraw_gives_a_draft_with_all_its_data_until_the_registration_at_the_operator_starts(self):
        publish_contract(self.staff)
        application, building = ready_draft(self.person)
        applications.submit(self.person, application, today=TODAY)

        applications.withdraw(self.person, application)

        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.DRAFT)
        self.assertIsNone(application.submitted_at)
        self.assertEqual(ApplicationMeteringPoint.objects.filter(building=building).count(), 1)
        self.assertTrue(history.entries_for(application).filter(event=HistoryEvent.APPLICATION_WITHDRAWN).exists())
        # Nothing to withdraw for a draft, and not after the registration at the operator started.
        self.assertEqual(code_of(lambda: applications.withdraw(self.person, application)), "not_editable")
        applications.submit(self.person, application, today=TODAY)
        Application.objects.filter(pk=application.pk).update(state=Application.State.OPERATOR_STARTED)
        self.assertEqual(code_of(lambda: applications.withdraw(self.person, application)), "not_editable")

    def test_reopen_for_change_only_while_the_leg_has_not_picked_the_application_up(self):
        publish_contract(self.staff)
        application, building = ready_draft(self.person)
        # A draft cannot be reopened: nothing was submitted.
        self.assertEqual(code_of(lambda: applications.reopen_for_change(self.person, application)), "not_editable")
        applications.submit(self.person, application, today=TODAY)

        applications.reopen_for_change(self.person, application)

        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.DRAFT)
        self.assertIsNone(application.submitted_at)
        self.assertEqual(ApplicationMeteringPoint.objects.filter(building=building).count(), 1)
        entry = history.entries_for(application).get(event=HistoryEvent.APPLICATION_WITHDRAWN)
        self.assertEqual(entry.detail, {"purpose": "change"})
        # The details are editable again and the application is submitted again.
        applications.submit(self.person, application, today=TODAY)
        # From the start of the review on the customer cannot change it (the withdrawal stays until the operator).
        Application.objects.filter(pk=application.pk).update(state=Application.State.IN_REVIEW)
        self.assertEqual(code_of(lambda: applications.reopen_for_change(self.person, application)), "review_started")
        Application.objects.filter(pk=application.pk).update(state=Application.State.OPERATOR_STARTED)
        self.assertEqual(code_of(lambda: applications.reopen_for_change(self.person, application)), "not_editable")
        # Another account's application is "not found", never a hint.
        other = customer("reopen.other@example.org")
        self.assertEqual(code_of(lambda: applications.reopen_for_change(other, application)), "not_found")

    def test_there_is_no_cap_of_changes_and_the_submission_mail_is_sent_once_per_application(self):
        """Owner decision 2026-10-22: no cap of change cycles; the mail only at the first submission (D-107)."""
        publish_contract(self.staff)
        application, _building = ready_draft(self.person)
        other_application, _other = ready_draft(self.person, egid=EGID + 31, number=21, address={"house_number": "31"})
        mails = MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED)

        applications.submit(self.person, application, today=TODAY)
        self.assertEqual(mails.count(), 1)
        for _cycle in range(12):
            applications.reopen_for_change(self.person, application)
            applications.submit(self.person, application, today=TODAY)

        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.SUBMITTED)
        # Every submission is in the history (audit trail), only the first one sent a mail.
        self.assertEqual(
            history.entries_for(application).filter(event=HistoryEvent.APPLICATION_SUBMITTED).count(), 13
        )
        self.assertEqual(mails.count(), 1)
        # Another application of the account has its own first submission (and its own mail).
        applications.submit(self.person, other_application, today=TODAY)
        self.assertEqual(mails.count(), 2)

    def test_a_submission_after_a_withdrawal_sends_no_second_mail(self):
        """The application was submitted before: "once per application" holds after a withdrawal too."""
        publish_contract(self.staff)
        application, _building = ready_draft(self.person)
        applications.submit(self.person, application, today=TODAY)
        applications.withdraw(self.person, application)

        applications.submit(self.person, application, today=TODAY)

        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.SUBMITTED)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED).count(), 1)
        self.assertEqual(
            history.entries_for(application).filter(event=HistoryEvent.APPLICATION_SUBMITTED).count(), 2
        )

    def test_a_refused_submission_is_no_submission_and_the_mail_follows_the_first_real_one(self):
        publish_contract(self.staff)
        application, _building = ready_draft(self.person)
        Application.objects.filter(pk=application.pk).update(start_choice="", desired_start_date=None)
        self.assertEqual(code_of(lambda: applications.submit(self.person, application, today=TODAY)), "incomplete")
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED).count(), 0)
        self.assertFalse(history.entries_for(application).filter(event=HistoryEvent.APPLICATION_SUBMITTED).exists())

    def test_a_withdrawal_is_refused_while_the_account_holds_the_most_drafts_it_may_have(self):
        publish_contract(self.staff)
        application, _building = ready_draft(self.person)
        applications.submit(self.person, application, today=TODAY)
        with patch.object(applications, "MAX_DRAFTS_PER_PERSON", 1):
            applications.start_draft(self.person)  # the one draft the cap allows
            self.assertEqual(code_of(lambda: applications.withdraw(self.person, application)), "too_many_drafts")
            application.refresh_from_db()
            self.assertEqual(application.state, Application.State.SUBMITTED)
        # Below the cap it works again.
        applications.withdraw(self.person, application)
        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.DRAFT)


@tag("mariadb")
class RollbackSafetyTests(TestCase):
    """A cancelled draft deletes only its own draft rows: never stored buildings, points, another account's data, the
    account or the IBAN."""

    @classmethod
    def setUpTestData(cls):
        cls.person = customer("rollback.customer@example.org")
        cls.other = customer("rollback.other@example.org")

    def test_cancel_deletes_the_draft_rows_and_nothing_else(self):
        configure_numbers()
        shared = make_location(street="Shared", house_number="1", egid=EGID + 1)
        stored_point = make_metering_point(50, location=shared)
        assignment = assign_person(stored_point, self.other)
        own_location = make_location(street="Mine", house_number="2", egid=EGID + 2)
        own_point = make_metering_point(51, location=own_location)
        assign_person(own_point, self.person)
        own_iban.set_own_iban(self.person, GOOD_IBAN)

        application = applications.start_draft(self.person, location=own_location)
        row = application.buildings.get()
        applications.add_metering_point(
            self.person, application, row.pk, direction=Direction.FEED_IN, entered_number="52"
        )
        application2, building2 = start_with_building(self.person, EGID + 3)
        applications.add_metering_point(
            self.person, application2, building2.pk, direction=Direction.CONSUMPTION, entered_number="53"
        )

        applications.cancel(self.person, application)
        applications.cancel(self.person, application2)

        self.assertEqual(Application.objects.count(), 0)
        self.assertEqual(ApplicationBuilding.objects.count(), 0)
        self.assertEqual(ApplicationMeteringPoint.objects.count(), 0)
        self.assertEqual(
            sorted(Location.objects.values_list("pk", flat=True)), sorted([shared.pk, own_location.pk])
        )
        self.assertEqual(
            sorted(MeteringPoint.objects.values_list("number", flat=True)), [number(50), number(51)]
        )
        self.assertTrue(PersonAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertEqual(own_iban.own_iban(self.person), GOOD_IBAN)
        self.assertTrue(type(self.person).objects.filter(pk=self.person.pk).exists())
        entries = history.entries_for(application).filter(event=HistoryEvent.APPLICATION_ROLLED_BACK)
        self.assertEqual([entry.detail for entry in entries], [{"reason": "cancelled"}])

    def test_a_completed_application_cannot_be_rolled_back(self):
        application = Application.objects.create(person=self.person, state=Application.State.COMPLETED)
        with self.assertRaises(ApplicationError):
            applications.delete_application(application, "cancelled")
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())

    def test_cancel_sends_no_mail(self):
        application = applications.start_draft(self.person)
        applications.cancel(self.person, application)
        self.assertEqual(MailJob.objects.count(), 0)


@tag("mariadb")
class IdleClockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("clock.customer@example.org")

    def setUp(self):
        configure_numbers()

    def test_every_change_restarts_the_idle_clock_and_ends_a_pause(self):
        application, building = start_with_building(self.person)
        Application.objects.filter(pk=application.pk).update(
            state=Application.State.PAUSED, reminded_at=application.created_at, last_activity_at=application.created_at
        )
        before = Application.objects.get(pk=application.pk).last_activity_at

        applications.add_metering_point(
            self.person, application, building.pk, direction=Direction.CONSUMPTION, entered_number="1"
        )

        application.refresh_from_db()
        self.assertEqual(application.state, Application.State.DRAFT)
        self.assertIsNone(application.reminded_at)
        self.assertGreater(application.last_activity_at, before)

    def test_the_contract_clock_runs_only_while_the_contract_is_the_one_open_step(self):
        application, building = start_with_building(self.person)
        self.assertIsNone(Application.objects.get(pk=application.pk).contract_pending_since)
        applications.add_metering_point(
            self.person, application, building.pk, direction=Direction.CONSUMPTION, entered_number="1"
        )
        started = Application.objects.get(pk=application.pk).contract_pending_since
        self.assertIsNotNone(started)

        # A feed-in without a bank connection opens another step: the contract is not the only one missing.
        applications.add_metering_point(
            self.person, application, building.pk, direction=Direction.FEED_IN, entered_number="2"
        )
        self.assertIsNone(Application.objects.get(pk=application.pk).contract_pending_since)
        version = publish_contract()
        applications.save_bank_connection(self.person, application, GOOD_IBAN)
        self.assertIsNotNone(Application.objects.get(pk=application.pk).contract_pending_since)
        applications.accept_contract_step(
            self.person, application, version.pk, agrees_contract=True
        )
        self.assertIsNone(Application.objects.get(pk=application.pk).contract_pending_since)
