"""An address that the directory does not know, or cannot be asked about now (``BuildingSearch.needs_review``): the draft keeps
the address as typed, without an EGID, for the review by the LEG, and the customer goes on (D-105). A visitor who is not
signed in, an invalid address and a chosen EGID that cannot be checked stay errors."""

from unittest.mock import patch

from django.test import TestCase, tag

from leg_platform.addresses import buildings as directory_module
from leg_platform.addresses.buildings import BuildingSearch
from leg_platform.addresses.models import ImportRequest
from leg_platform.community import applications, buildings
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import ApplicationBuilding, Direction, Location
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point
from leg_platform.community.tests.onboarding import ADDRESS, customer
from leg_platform.community import history

EGID = 62_001


def search(outcome, reason=""):
    return BuildingSearch((), outcome, reason=reason)


def code_of(call):
    try:
        call()
    except ApplicationError as error:
        return error.code, error.details
    raise AssertionError("ApplicationError was not raised.")


@tag("mariadb")
class NeedsReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("review.customer@example.org")

    def choose(self, result, **extra):
        application = getattr(self, "application", None) or applications.start_draft(self.person)
        self.application = application
        with patch.object(directory_module, "find_buildings", return_value=result):
            return applications.choose_building(self.person, application, **ADDRESS, **extra)

    def test_a_search_that_needs_a_review_keeps_the_address_without_an_egid_and_the_customer_goes_on(self):
        for outcome, reason in (
            (directory_module.NOT_FOUND, ""),
            (directory_module.UNAVAILABLE, directory_module.REASON_LIMIT),
            (directory_module.UNAVAILABLE, directory_module.REASON_CIRCUIT_OPEN),
            (directory_module.UNAVAILABLE, directory_module.REASON_FAILED),
        ):
            with self.subTest(outcome=outcome, reason=reason):
                self.application = None
                choice = self.choose(search(outcome, reason), name="Haus")
                row = choice.building
                self.assertTrue(choice.review)
                self.assertFalse(choice.existing)
                self.assertEqual(
                    (row.egid, row.location_id, row.street, row.house_number, row.postal_code, row.town, row.name),
                    (None, None, "Ittigenstrasse", "51", "3063", "Ittigen", "Haus"),
                )
                entry = history.entries_for(self.application).get(event=HistoryEvent.APPLICATION_BUILDING_SET)
                self.assertTrue(entry.detail["needs_review"])
                self.assertNotIn("Ittigenstrasse", str(entry.detail))
                applications.cancel(self.person, self.application)
        self.assertEqual(Location.objects.count(), 0)

    def test_a_search_that_could_not_be_made_notes_the_update_flag(self):
        ImportRequest.objects.filter(pk=1).delete()

        self.choose(search(directory_module.UNAVAILABLE, directory_module.REASON_LIMIT))

        self.assertIsNotNone(ImportRequest.objects.get(pk=1).update_flag_set_at)

    def test_a_visitor_who_is_not_signed_in_stays_an_error_and_so_does_an_invalid_address(self):
        application = applications.start_draft(self.person)
        for result, expected in (
            (search(directory_module.UNAVAILABLE, directory_module.REASON_NOT_SIGNED_IN), "directory_unavailable"),
            (search(directory_module.INVALID), "building_not_found"),
        ):
            with self.subTest(expected=expected), patch.object(directory_module, "find_buildings", return_value=result):
                code, details = code_of(lambda: applications.choose_building(self.person, application, **ADDRESS))
                self.assertEqual(code, expected)
                self.assertFalse(details.get("review"))
        self.assertEqual(ApplicationBuilding.objects.count(), 0)

    def test_a_chosen_egid_is_never_accepted_unchecked(self):
        application = applications.start_draft(self.person)
        with patch.object(directory_module, "find_buildings", return_value=search(directory_module.NOT_FOUND)):
            code, _details = code_of(
                lambda: applications.choose_building(self.person, application, **ADDRESS, egid=EGID)
            )
        self.assertEqual(code, "building_not_found")
        self.assertEqual(ApplicationBuilding.objects.count(), 0)

    def test_the_same_address_is_added_once_and_a_stranger_location_without_an_egid_is_not_attached(self):
        make_location("Andereweg", "1", "3000", "Bern")  # a stored location without an EGID, another address
        first = self.choose(search(directory_module.NOT_FOUND))
        self.assertTrue(first.review)
        self.assertEqual(first.building.location_id, None)
        code, _details = code_of(lambda: self.choose(search(directory_module.NOT_FOUND)))
        self.assertEqual(code, "already_added")
        self.assertEqual(ApplicationBuilding.objects.count(), 1)

    def test_a_stored_building_at_that_address_is_attached_with_its_own_egid(self):
        location = make_location("Ittigenstrasse", "51", "3063", "Ittigen", egid=EGID)

        choice = self.choose(search(directory_module.UNAVAILABLE, directory_module.REASON_FAILED))

        self.assertEqual((choice.building.location, choice.building.egid, choice.existing), (location, EGID, True))
        self.assertFalse(choice.review)

    def test_a_stored_building_without_an_egid_stays_a_review_case(self):
        location = make_location("Ittigenstrasse", "51", "3063", "Ittigen")

        choice = self.choose(search(directory_module.NOT_FOUND))

        self.assertEqual((choice.building.location, choice.building.egid), (location, None))
        self.assertTrue(choice.review)

    def test_a_building_of_the_account_with_a_full_set_of_points_is_still_refused(self):
        location = make_location("Ittigenstrasse", "51", "3063", "Ittigen", egid=EGID)
        for last, direction in ((1, Direction.CONSUMPTION), (2, Direction.FEED_IN)):
            point = make_metering_point(
                last, direction, location, registered_with_operator_on=None, confirmed_on=None
            )
            assign_person(point, self.person)

        code, _details = code_of(lambda: self.choose(search(directory_module.NOT_FOUND)))

        self.assertEqual(code, "building_complete")

    def test_the_lookup_of_the_wizard_still_returns_the_found_buildings(self):
        found = (directory_module.Building(EGID, None, "Ittigenstrasse", "51", "3063", "Ittigen"),)
        with patch.object(
            directory_module, "find_buildings", return_value=BuildingSearch(found, directory_module.FOUND)
        ):
            self.assertEqual(
                buildings.find_candidates("Ittigenstrasse", "51", "3063", "Ittigen", user=self.person), list(found)
            )
