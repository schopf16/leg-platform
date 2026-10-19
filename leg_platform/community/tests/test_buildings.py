"""Own buildings and metering points (D-103): the names and the description that the customer chooses, and the lock of the
address once the staff confirmed it (``locations.confirm``)."""

from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase, tag

from leg_platform.community import applications, buildings, history, locations
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import ApplicationNotice, CustomerBuilding, Location, MeteringPoint
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import ADDRESS, candidate, customer, directory

EGID = 60_001


def code_of(call):
    try:
        call()
    except ApplicationError as error:
        return error.code
    raise AssertionError("ApplicationError was not raised.")


@tag("mariadb")
class NamesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("names.customer@example.org")
        cls.other = customer("names.other@example.org")

    def setUp(self):
        self.location = make_location(egid=EGID)
        self.point = make_metering_point(1, location=self.location)
        assign_person(self.point, self.person)

    def test_the_name_and_the_description_are_set_and_cleaned_per_account(self):
        changed = buildings.set_building_details(self.person, self.location, name="  Wohn   haus ", description="Block A")

        self.assertEqual(changed, ["name", "description"])
        self.assertEqual(buildings.building_details(self.person, self.location), ("Wohn haus", "Block A"))
        entry = history.entries_for(self.location).get(event=HistoryEvent.LOCATION_DETAILS_CHANGED)
        self.assertEqual((entry.actor, entry.detail), (self.person, {"fields": ["name", "description"]}))
        self.assertEqual(buildings.set_building_details(self.person, self.location, name="Wohn haus", description="Block A"), [])

    def test_two_customers_of_one_building_never_see_or_change_each_others_words(self):
        assign_person(make_metering_point(2, location=self.location), self.other)

        buildings.set_building_details(self.person, self.location, name="Meins", description="oben")
        self.assertEqual(buildings.building_details(self.other, self.location), ("", ""))
        buildings.set_building_details(self.other, self.location, name="Deins", description="unten")
        buildings.set_building_details(self.other, self.location, name="Deins 2", description="unten")

        self.assertEqual(buildings.building_details(self.person, self.location), ("Meins", "oben"))
        self.assertEqual(buildings.building_details(self.other, self.location), ("Deins 2", "unten"))
        self.assertEqual(CustomerBuilding.objects.filter(location=self.location).count(), 2)

    def test_they_stay_editable_after_the_address_was_confirmed(self):
        locations.confirm(self.location, staff_user("names.staff@example.org"))
        buildings.set_building_details(self.person, self.location, name="Neu", description="")
        self.assertEqual(buildings.building_details(self.person, self.location), ("Neu", ""))

    def test_the_limits_and_control_characters_are_refused(self):
        for kwargs in ({"name": "x" * 81, "description": ""}, {"name": "", "description": "x" * 201},
                       {"name": "a\x00b", "description": ""}, {"name": "", "description": "a\u202eb"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                buildings.set_building_details(self.person, self.location, **kwargs)
        self.assertEqual(CustomerBuilding.objects.count(), 0)

    def test_only_a_customer_of_the_building_changes_it_or_a_point(self):
        self.assertEqual(code_of(lambda: buildings.set_building_details(self.other, self.location, name="x", description="")),
                         "not_found")
        self.assertEqual(code_of(lambda: buildings.set_metering_point_name(self.other, self.point, "x")), "not_found")
        stranger = make_location(street="Stranger", egid=EGID + 1)
        self.assertEqual(code_of(lambda: buildings.set_building_details(self.person, stranger, name="x", description="")),
                         "not_found")
        self.point.refresh_from_db()
        self.assertEqual(self.point.name, "")
        self.assertEqual(CustomerBuilding.objects.count(), 0)

    def test_an_archived_building_or_point_is_read_only(self):
        Location.objects.filter(pk=self.location.pk).update(is_archived=True)
        with self.assertRaises(buildings.archived.Archived):
            buildings.set_building_details(self.person, self.location, name="x", description="")
        MeteringPoint.objects.filter(pk=self.point.pk).update(is_archived=True)
        self.assertEqual(code_of(lambda: buildings.set_metering_point_name(self.person, self.point, "x")), "not_editable")

    def test_the_name_of_a_metering_point_is_set_with_the_field_name_in_the_history(self):
        self.assertTrue(buildings.set_metering_point_name(self.person, self.point, " PV-Anlage "))
        self.point.refresh_from_db()
        self.assertEqual(self.point.name, "PV-Anlage")
        entry = history.entries_for(self.point).get(event=HistoryEvent.METERING_POINT_DATA_CHANGED)
        self.assertEqual(entry.detail, {"fields": ["name"]})
        self.assertFalse(buildings.set_metering_point_name(self.person, self.point, "PV-Anlage"))

    def test_a_blocked_account_changes_nothing(self):
        self.person.set_status(type(self.person).Status.BLOCKED)
        self.assertEqual(code_of(lambda: buildings.set_building_details(self.person, self.location, name="x", description="")),
                         "not_allowed")

    def test_the_names_are_never_written_to_the_history(self):
        buildings.set_building_details(self.person, self.location, name="Geheimname", description="Geheimtext")
        buildings.set_metering_point_name(self.person, self.point, "Geheimpunkt")
        for entry in history.entries_for(self.location):
            self.assertNotIn("Geheim", str(entry.detail))
        for entry in history.entries_for(self.point):
            self.assertNotIn("Geheim", str(entry.detail))


@tag("mariadb")
class DirectoryAdapterTests(TestCase):
    """``buildings.find_candidates`` speaks the interface of ``addresses.buildings.find_buildings``."""

    @classmethod
    def setUpTestData(cls):
        cls.person = customer("adapter.customer@example.org")

    def test_the_buildings_of_a_found_search_are_the_candidates_and_the_account_is_passed_for_the_external_search(self):
        from unittest.mock import patch as mock_patch

        from leg_platform.addresses.buildings import FOUND, Building, BuildingSearch

        found = BuildingSearch((Building(EGID, 1, "Ittigenstrasse", "51", "3063", "Ittigen"),), FOUND, "directory")
        with mock_patch("leg_platform.addresses.buildings.find_buildings", return_value=found) as search:
            result = buildings.find_candidates("Ittigenstrasse", "51", "3063", "Ittigen", user=self.person)
        self.assertEqual(result, list(found.buildings))
        search.assert_called_once_with("Ittigenstrasse", "51", "3063", "Ittigen", user=self.person)

    def test_a_draft_takes_the_address_of_the_building_with_its_locality(self):
        from unittest.mock import patch as mock_patch

        from leg_platform.addresses.buildings import FOUND, Building, BuildingSearch
        from leg_platform.community.tests.onboarding import configure_numbers

        configure_numbers()
        application = applications.start_draft(self.person)
        found = BuildingSearch((Building(EGID, None, "Ittigenstrasse", "51", "3063", "Ittigen"),), FOUND, "directory")
        with mock_patch("leg_platform.addresses.buildings.find_buildings", return_value=found):
            choice = applications.choose_building(
                self.person, application, street="ittigenstrasse", house_number="51", postal_code="3063", town="Bern"
            )
        # The official spelling and locality of the directory win over what was typed.
        self.assertEqual(
            (choice.building.street, choice.building.town, choice.building.egid), ("Ittigenstrasse", "Ittigen", EGID)
        )

    def test_several_entrances_of_one_building_are_one_candidate(self):
        from unittest.mock import patch as mock_patch

        from leg_platform.addresses.buildings import FOUND, Building, BuildingSearch

        found = BuildingSearch(
            (Building(EGID, 1, "Ittigenstrasse", "51", "3063", "Ittigen"),
             Building(EGID, 2, "Ittigenstrasse", "51", "3063", "Ittigen")), FOUND, "directory",
        )
        with mock_patch("leg_platform.addresses.buildings.find_buildings", return_value=found):
            candidate_row, egid = buildings.offered_building(("Ittigenstrasse", "51", "3063", "Ittigen"), None)
        self.assertEqual(egid, EGID)

    def test_invalid_and_unavailable_searches_choose_nothing_and_not_found_is_kept_for_the_review(self):
        from unittest.mock import patch as mock_patch

        from leg_platform.addresses.buildings import INVALID, NOT_FOUND, UNAVAILABLE, BuildingSearch

        application = applications.start_draft(self.person)
        # An invalid address and a search that could not be made for no reason that a person can wait out: errors.
        for outcome, expected in ((INVALID, "building_not_found"), (UNAVAILABLE, "directory_unavailable")):
            with self.subTest(outcome=outcome), mock_patch(
                "leg_platform.addresses.buildings.find_buildings", return_value=BuildingSearch((), outcome)
            ):
                self.assertEqual(
                    code_of(lambda: applications.choose_building(self.person, application, **ADDRESS)), expected
                )
        self.assertEqual(application.buildings.count(), 0)
        # Nothing found after every attempt: the address is kept as typed, without an EGID, for the LEG (D-105).
        with mock_patch(
            "leg_platform.addresses.buildings.find_buildings", return_value=BuildingSearch((), NOT_FOUND)
        ):
            choice = applications.choose_building(self.person, application, **ADDRESS)
        self.assertEqual((choice.review, choice.building.egid), (True, None))

    def test_without_the_module_of_the_directory_nothing_is_chosen(self):
        import sys
        from unittest.mock import patch as mock_patch

        application = applications.start_draft(self.person)
        with mock_patch.dict(sys.modules, {"leg_platform.addresses.buildings": None}):
            self.assertEqual(
                code_of(lambda: applications.choose_building(self.person, application, **ADDRESS)),
                "directory_unavailable",
            )


@tag("mariadb")
class FormerHolderTests(TestCase):
    """A holder whose assignment ended has no access to the point, the building or its address (D-103)."""

    @classmethod
    def setUpTestData(cls):
        cls.former = customer("former.holder@example.org")
        cls.current = customer("former.current@example.org")

    def setUp(self):
        self.location = make_location(
            street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen", egid=EGID,
            needs_confirmation=True,
        )
        self.point = make_metering_point(1, location=self.location)
        assign_person(self.point, self.former, date(2025, 1, 1), date(2025, 12, 31))
        assign_person(self.point, self.current, date(2026, 1, 1))

    def test_a_former_holder_can_neither_read_nor_rename_the_point_and_the_new_holders_name_stays(self):
        buildings.set_metering_point_name(self.current, self.point, "Keller")

        self.assertEqual(code_of(lambda: buildings.set_metering_point_name(self.former, self.point, "Hacked")), "not_found")
        self.assertEqual(code_of(lambda: buildings.own_metering_point(self.former, self.point.pk)), "not_found")
        self.assertEqual(buildings.own_metering_point(self.current, self.point.pk), self.point)
        self.point.refresh_from_db()
        self.assertEqual(self.point.name, "Keller")

    def test_a_former_holder_owns_no_building_and_reads_no_words(self):
        buildings.set_building_details(self.current, self.location, name="Neu", description="x")
        CustomerBuilding.objects.create(person=self.former, location=self.location, name="Alt", description="y")

        self.assertFalse(buildings.owns(self.former, self.location))
        self.assertTrue(buildings.owns(self.current, self.location))
        self.assertEqual(code_of(lambda: buildings.own_building(self.former, self.location.pk)), "not_found")
        self.assertEqual(code_of(lambda: buildings.own_building(self.former, 0)), "not_found")
        self.assertEqual(buildings.own_building(self.current, self.location.pk), self.location)
        self.assertEqual(buildings.building_details(self.former, self.location), ("", ""))
        self.assertEqual(
            code_of(lambda: buildings.set_building_details(self.former, self.location, name="x", description="")),
            "not_found",
        )
        self.assertEqual(buildings.building_details(self.current, self.location), ("Neu", "x"))

    def test_a_former_holder_with_an_open_application_of_his_own_reads_his_own_words_for_it(self):
        from leg_platform.community.tests.onboarding import ready_draft

        CustomerBuilding.objects.create(person=self.former, location=self.location, name="Alt", description="y")
        ready_draft(self.former, egid=EGID, number=5, address={"street": "Ittigenstrasse", "house_number": "51"})
        self.assertEqual(buildings.building_details(self.former, self.location), ("Alt", "y"))

    def test_a_former_holder_does_not_change_the_address_and_does_not_lock_it_for_others(self):
        values = {"street": "Neuweg", "house_number": "7", "postal_code": "3000", "town": "Bern"}
        with directory(candidate(EGID + 5, **values)):
            self.assertEqual(
                code_of(lambda: buildings.update_own_address(self.former, self.location, egid=EGID + 5, **values)),
                "not_found",
            )
        self.location.refresh_from_db()
        self.assertEqual(self.location.street, "Ittigenstrasse")
        # The former holder does not count as another user of the building: the current holder may still change it.
        self.assertFalse(buildings.others_use(self.current, self.location))
        self.assertEqual(buildings.claimant_ids(EGID, self.location), {self.current.pk})
        with directory(candidate(EGID + 5, **values)):
            buildings.update_own_address(self.current, self.location, egid=EGID + 5, **values)

    def test_the_directions_of_a_former_holder_are_free_again(self):
        self.assertEqual(buildings.occupied_directions(self.location, self.former, date(2026, 6, 1)), set())
        self.assertEqual(
            buildings.occupied_directions(self.location, self.current, date(2026, 6, 1)), {self.point.direction}
        )

    def test_a_draft_cannot_preselect_the_building_of_a_former_holder(self):
        with self.assertRaises(ApplicationError) as caught:
            applications.start_draft(self.former, location=self.location)
        self.assertEqual(caught.exception.code, "not_found")


@tag("mariadb")
class AddressLockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("lock.customer@example.org")
        cls.other = customer("lock.other@example.org")
        cls.staff = staff_user("lock.staff@example.org")

    def setUp(self):
        self.location = make_location(
            street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen", egid=EGID,
            needs_confirmation=True,
        )
        assign_person(make_metering_point(1, location=self.location), self.person)

    def change(self, person=None, egid=EGID + 5, **address):
        values = {"street": "Neuweg", "house_number": "7", "postal_code": "3000", "town": "Bern", **address}
        with directory(candidate(egid, **values)):
            return buildings.update_own_address(person or self.person, self.location, egid=egid, **values)

    def test_an_unconfirmed_address_can_be_changed_by_its_owner(self):
        self.assertFalse(buildings.address_locked(self.location))

        self.change()

        self.location.refresh_from_db()
        self.assertEqual((self.location.street, self.location.egid), ("Neuweg", EGID + 5))
        self.assertTrue(history.entries_for(self.location).filter(event=HistoryEvent.ADDRESS_CHANGED).exists())

    def test_after_the_confirmation_by_the_staff_the_address_is_locked(self):
        locations.confirm(self.location, self.staff)
        self.location.refresh_from_db()
        self.assertTrue(buildings.address_locked(self.location))

        self.assertEqual(code_of(self.change), "address_locked")
        self.location.refresh_from_db()
        self.assertEqual(self.location.street, "Ittigenstrasse")

    def test_a_location_entered_by_the_staff_is_locked_from_the_start(self):
        self.assertTrue(buildings.address_locked(make_location(street="Staffstrasse", egid=EGID + 9)))

    def test_only_the_owner_and_never_an_archived_location(self):
        self.assertEqual(code_of(lambda: self.change(person=self.other)), "not_found")
        Location.objects.filter(pk=self.location.pk).update(is_archived=True)
        with self.assertRaises(buildings.archived.Archived):
            self.change()

    def test_the_new_egid_must_be_offered_for_the_new_address(self):
        with directory(candidate(EGID + 5, street="Neuweg", house_number="7", postal_code="3000", town="Bern")):
            self.assertEqual(
                code_of(lambda: buildings.update_own_address(
                    self.person, self.location, street="Neuweg", house_number="7", postal_code="3000", town="Bern",
                    egid=999)),
                "egid_not_offered",
            )

    def test_an_address_or_egid_of_another_location_is_the_neutral_refusal_and_nothing_is_merged(self):
        make_location(street="Neuweg", house_number="7", postal_code="3000", town="Bern", egid=EGID + 6)
        make_location(street="Elsewhere", egid=EGID + 7)

        for egid, address in ((EGID + 6, {}), (EGID + 7, {"street": "Fresh", "house_number": "1"})):
            with self.subTest(egid=egid):
                self.assertEqual(code_of(lambda: self.change(egid=egid, **address)), "building_unavailable")
        self.assertEqual(Location.objects.count(), 3)
        self.assertEqual(ApplicationNotice.objects.count(), 2)
        self.location.refresh_from_db()
        self.assertEqual(self.location.street, "Ittigenstrasse")

    def test_a_shared_address_is_not_changed_by_one_customer(self):
        # Another customer of the same building (a person assignment) or an application of another account in flight.
        assign_person(make_metering_point(2, location=self.location), self.other)
        self.assertEqual(code_of(self.change), "address_locked")
        self.location.refresh_from_db()
        self.assertEqual(self.location.street, "Ittigenstrasse")

    def test_an_application_of_another_account_in_flight_for_the_building_locks_the_address_too(self):
        from leg_platform.community.tests.onboarding import TODAY, ready_draft

        other_application, _building = ready_draft(
            self.other, egid=EGID, number=5,
            address={"street": "Ittigenstrasse", "house_number": "51", "postal_code": "3063", "town": "Ittigen"},
        )
        applications.submit(self.other, other_application, today=TODAY)
        self.assertEqual(code_of(self.change), "address_locked")
