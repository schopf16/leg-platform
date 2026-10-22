"""The staff's corrections of an application in flight (D-109): the address of a building and the metering points.

What is proven: the corrections apply while the LEG works on the application (submitted, in review, registration at the
grid operator started, confirmed) and are refused for a draft, a paused draft and a completed application; only an active
staff account may correct, and an id of another application is "not found"; the rules are the customer wizard's (the
directory, the EGID, duplicates, the number rule of D-102, one draw and one feed-in, plant figures only for a feed-in) plus
what the submission requires; the state is read under the lock after the account (a customer's change or a review that came
first wins and the stale dialog is refused, a row that changed in between is refused with ``changed``); the audit trail
(history names the row and the fields, never a value; one security event; no log line with data); nothing is written when
nothing changed or a rule refuses.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, tag
from django.test.utils import CaptureQueriesContext

from leg_platform.accounts.models import SecurityEvent
from leg_platform.community import application_corrections, application_staff, applications, history
from leg_platform.community.application_common import MAX_UNRESOLVED_NOTICES_PER_PERSON, ApplicationError
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Application,
    ApplicationBuilding,
    ApplicationMeteringPoint,
    ApplicationNotice,
    Direction,
    MeteringPoint,
)
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import (
    ADDRESS,
    TODAY,
    candidate,
    configure_numbers,
    customer,
    directory,
    full_number,
    ready_draft,
    start_with_building,
)

State = Application.State
STARTED = date(2026, 6, 1)
CONFIRMED = date(2026, 7, 1)
START = date(2026, 9, 1)


def code_of(call):
    try:
        call()
    except ApplicationError as error:
        return error.code
    raise AssertionError("ApplicationError was not raised.")


@tag("mariadb")
class CorrectionCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("fix.customer@example.org")
        cls.staff = staff_user("fix.staff@example.org")

    def setUp(self):
        configure_numbers()
        self.application, self.building = ready_draft(self.person, feed_in=True)
        applications.submit(self.person, self.application, today=TODAY)
        self.application.refresh_from_db()

    # ---- helpers ----

    def row(self):
        return ApplicationBuilding.objects.get(pk=self.building.pk)

    def point(self, direction=Direction.CONSUMPTION):
        return ApplicationMeteringPoint.objects.get(building=self.building, direction=direction)

    def fix_building(self, building=None, application=None, **values):
        building = ApplicationBuilding.objects.get(pk=(building or self.building).pk)
        actor = values.pop("actor", self.staff)
        values = {
            **{k: v for k, v in ADDRESS.items()}, "expected": application_corrections.building_token(building), **values,
        }
        return application_corrections.correct_building(application or self.application, actor, building.pk, **values)

    def fix_point(self, point=None, application=None, **values):
        point = ApplicationMeteringPoint.objects.get(pk=(point or self.point()).pk)
        actor = values.pop("actor", self.staff)
        values = {
            "entered_number": point.number[13:], "direction": point.direction, "pv_power_kwp": point.pv_power_kwp,
            "battery_kwh": point.battery_kwh, "wallbox_kw": point.wallbox_kw,
            "expected": application_corrections.point_token(point), **values,
        }
        return application_corrections.correct_metering_point(
            application or self.application, actor, point.pk, **values
        )

    def set_state(self, state):
        Application.objects.filter(pk=self.application.pk).update(state=state)
        self.application.refresh_from_db()

    def reopened(self):
        applications.reopen_for_change(self.person, self.application)
        self.application.refresh_from_db()
        return self.application

    def corrected(self, event):
        return list(history.entries_for(self.application).filter(event=event))

    def events(self):
        return list(history.entries_for(self.application).values_list("event", flat=True))


class BuildingCorrectionTests(CorrectionCase):
    def test_the_address_is_checked_like_the_wizard_and_saved_with_the_spelling_and_egid_of_the_directory(self):
        with directory(candidate(90004270, street="Ittigenstrasse", house_number="52")) as lookup:
            result = self.fix_building(street="ittigenstrasse", house_number=" 52 ", town="ittigen")

        lookup.assert_called_once()
        row = self.row()
        self.assertEqual((row.street, row.house_number, row.postal_code, row.town, row.egid),
                         ("Ittigenstrasse", "52", "3063", "Ittigen", 90004270))
        self.assertEqual(result.fields, ("house_number", "egid"))
        self.assertIsNone(row.location_id)

    def test_it_is_recorded_with_field_names_only_and_one_security_event_without_a_line_that_holds_data(self):
        with directory(candidate(90004270, house_number="52")):
            with self.assertLogs("leg_platform.security", level="WARNING") as logged:
                with self.captureOnCommitCallbacks(execute=True):
                    self.fix_building(house_number="52")

        entry = self.corrected(HistoryEvent.APPLICATION_BUILDING_CORRECTED)[0]
        self.assertEqual(entry.detail, {"building_id": self.building.pk, "fields": ["house_number", "egid"]})
        self.assertEqual(entry.actor_id, self.staff.pk)
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF)
        self.assertEqual((event.account_id, event.actor_id), (self.person.pk, self.staff.pk))
        line = " ".join(logged.output)
        self.assertIn(f"application {self.application.pk}, fields house_number, egid", line)
        # Internal numbers and field names only: nothing of the address, the EGID or the new house number.
        self.assertRegex(line, r"changed by staff \(.*application \d+, fields house_number, egid\)\.$")
        for value in ("Ittigenstrasse", "Ittigen", "90004270", "3063"):
            self.assertNotIn(value, line)
        self.assertNotIn("Ittigenstrasse", str(entry.detail))
        # The customer sees the corrected row; the application counts as changed.
        self.assertEqual(self.row().house_number, "52")

    def test_it_applies_while_the_leg_works_on_the_application_and_not_to_a_draft_or_a_completed_one(self):
        for index, state in enumerate(
            (State.SUBMITTED, State.IN_REVIEW, State.OPERATOR_STARTED, State.OPERATOR_CONFIRMED), start=60
        ):
            with self.subTest(state=state):
                self.set_state(state)
                with directory(candidate(90004300 + index, house_number=str(index))):
                    self.fix_building(house_number=str(index))
                self.assertEqual(self.row().house_number, str(index))
        for state in (State.DRAFT, State.PAUSED, State.COMPLETED):
            with self.subTest(state=state):
                self.set_state(state)
                with directory(candidate(90004399, house_number="53")) as lookup:
                    self.assertEqual(code_of(lambda: self.fix_building(house_number="53")), "wrong_state")
                lookup.assert_not_called()  # no request to the directory (and none to swisstopo) for such a state
        self.assertEqual(self.row().house_number, "63")

    def test_a_draft_that_the_customer_changed_in_between_refuses_the_stale_dialog_and_the_customer_keeps_the_data(self):
        token = application_corrections.building_token(self.row())
        applications.reopen_for_change(self.person, self.application)  # the customer's click came first

        with directory(candidate(90004270, house_number="52")):
            code = code_of(lambda: self.fix_building(expected=token, house_number="52"))

        self.assertEqual(code, "wrong_state")
        self.assertEqual(self.row().house_number, "51")
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, State.DRAFT)

    def test_when_the_staff_came_first_the_customer_who_changes_it_afterwards_works_on_the_corrected_row(self):
        with directory(candidate(90004270, house_number="52")):
            self.fix_building(house_number="52")
        applications.reopen_for_change(self.person, self.application)

        self.assertEqual(self.row().house_number, "52")
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, State.DRAFT)

    def test_a_row_that_changed_in_between_is_refused_and_nothing_is_saved(self):
        token = application_corrections.building_token(self.row())
        # Another staff member corrected the same row first (the token the first dialog showed is stale now).
        with directory(candidate(90004270, house_number="52")):
            self.fix_building(house_number="52")
        before = self.events()

        with directory(candidate(90004271, house_number="53")):
            code = code_of(lambda: self.fix_building(expected=token, house_number="53"))

        self.assertEqual(code, "changed")
        self.assertEqual((self.row().house_number, self.row().egid), ("52", 90004270))
        self.assertEqual(self.events(), before)

    def test_nothing_changed_writes_nothing(self):
        before = self.events()
        events = SecurityEvent.objects.count()
        with directory(candidate(90004269)):
            self.assertEqual(code_of(lambda: self.fix_building()), "unchanged")
        self.assertEqual((self.events(), SecurityEvent.objects.count()), (before, events))

    def test_an_address_that_fails_the_rules_is_refused(self):
        for values in ({"postal_code": "30"}, {"postal_code": "abcd"}, {"street": ""}, {"town": "x" * 101}):
            with self.subTest(values=values):
                with directory(candidate(90004269)):
                    with self.assertRaises(ValidationError):
                        self.fix_building(**values)
        self.assertEqual(self.row().postal_code, "3063")

    def test_an_egid_must_be_one_the_directory_offers_for_the_address(self):
        with directory(candidate(90004270, house_number="52")):
            self.assertEqual(code_of(lambda: self.fix_building(house_number="52", egid=90004999)), "egid_not_offered")
            self.assertEqual(code_of(lambda: self.fix_building(house_number="52", egid="12a")), "egid_invalid")
        self.assertEqual(self.row().egid, 90004269)

    def test_several_buildings_at_an_address_ask_for_the_egid_and_a_chosen_one_is_taken(self):
        with directory(candidate(90004270, house_number="52"), candidate(90004271, house_number="52")):
            with self.assertRaises(ApplicationError) as refused:
                self.fix_building(house_number="52")
            self.assertEqual((refused.exception.code, refused.exception.details["egids"]), ("ambiguous", [90004270, 90004271]))
            self.fix_building(house_number="52", egid=90004271)
        self.assertEqual(self.row().egid, 90004271)

    def test_an_address_the_directory_does_not_know_is_kept_as_entered_without_an_egid_only_when_confirmed(self):
        with directory():
            self.assertEqual(code_of(lambda: self.fix_building(street="Phantasiegasse")), "building_not_found")
            self.assertEqual(self.row().street, "Ittigenstrasse")
            result = self.fix_building(street="Phantasiegasse", allow_unlisted=True)

        row = self.row()
        self.assertEqual((row.street, row.egid), ("Phantasiegasse", None))
        self.assertEqual(result.fields, ("street", "egid"))
        # An EGID that the person names is never "kept as entered": the directory must offer it (it offers nothing).
        with directory():
            self.assertEqual(
                code_of(lambda: self.fix_building(street="Phantasiegasse", house_number="9", egid=12345, allow_unlisted=True,
                                                  expected=application_corrections.building_token(self.row()))),
                "egid_not_offered",
            )

    def test_a_stored_building_with_the_same_egid_is_attached_to_like_in_the_wizard(self):
        stored = make_location("Ittigenstrasse", "52", "3063", "Ittigen", egid=90004270)
        with directory(candidate(90004270, house_number="52")):
            result = self.fix_building(house_number="52")

        row = self.row()
        self.assertEqual((row.location_id, row.egid, row.street, row.town), (stored.pk, 90004270, "", ""))
        # The row now has the stored building's address: the typed parts are cleared, the location is set.
        self.assertEqual(result.fields, ("street", "house_number", "postal_code", "town", "egid", "location"))
        self.assertEqual(self.corrected(HistoryEvent.APPLICATION_BUILDING_CORRECTED)[0].detail["fields"],
                         ["street", "house_number", "postal_code", "town", "egid", "location"])
        # Its address is the location's from now on.
        with directory(candidate(90004270, house_number="52")):
            self.assertEqual(code_of(lambda: self.fix_building(house_number="52")), "building_stored")

    def test_the_address_of_a_building_that_is_stored_already_is_the_locations_and_is_not_changed_here(self):
        make_location("Ittigenstrasse", "99", "3063", "Ittigen", egid=90004299)
        application, _building = start_with_building(self.person, 90004299, house_number="99")
        row = application.buildings.get()
        self.assertIsNotNone(row.location_id)
        Application.objects.filter(pk=application.pk).update(state=State.SUBMITTED)

        with directory(candidate(90004299, house_number="98")):
            code = code_of(lambda: application_corrections.correct_building(
                application, self.staff, row.pk, expected=application_corrections.building_token(row),
                street="Ittigenstrasse", house_number="98", postal_code="3063", town="Ittigen",
            ))

        self.assertEqual(code, "building_stored")

    def test_an_archived_building_and_an_egid_that_contradicts_the_address_are_refused(self):
        make_location("Ittigenstrasse", "52", "3063", "Ittigen", egid=90004270, is_archived=True)
        with directory(candidate(90004270, house_number="52")):
            self.assertEqual(code_of(lambda: self.fix_building(house_number="52")), "building_unavailable")
        make_location("Ittigenstrasse", "54", "3063", "Ittigen", egid=90004272)
        with directory(candidate(90004273, house_number="54")):
            # The stored building at this address has another EGID: the neutral refusal of the wizard.
            self.assertEqual(code_of(lambda: self.fix_building(house_number="54")), "building_unavailable")
        self.assertEqual(self.row().house_number, "51")

    def test_the_building_of_another_application_or_a_missing_one_is_not_found(self):
        other = customer("fix.other@example.org")
        other_application, other_building = ready_draft(other, egid=90004280, number=30, address={"house_number": "80"})
        applications.submit(other, other_application, today=TODAY)
        token = application_corrections.building_token(other_building)

        with directory(candidate(90004269)):
            wrong_application = code_of(lambda: application_corrections.correct_building(
                self.application, self.staff, other_building.pk, expected=token, **ADDRESS,
            ))
            missing = code_of(lambda: application_corrections.correct_building(
                self.application, self.staff, 999999, expected=token, **ADDRESS,
            ))
            garbage = code_of(lambda: application_corrections.correct_building(
                self.application, self.staff, "x", expected=token, **ADDRESS,
            ))

        self.assertEqual((wrong_application, missing, garbage), ("not_found", "not_found", "not_found"))
        self.assertEqual(ApplicationBuilding.objects.get(pk=other_building.pk).house_number, "80")

    def test_a_second_building_cannot_take_the_egid_or_the_address_of_the_first(self):
        with directory(candidate(90004270, house_number="52")):
            choice = applications.choose_building(self.person, self.reopened(), **{**ADDRESS, "house_number": "52"})
        self.set_state(State.SUBMITTED)
        second = ApplicationBuilding.objects.get(pk=choice.building.pk)

        with directory(candidate(90004269)):
            code = code_of(lambda: self.fix_building(building=second, **ADDRESS))

        self.assertEqual(code, "already_added")
        self.assertEqual(ApplicationBuilding.objects.get(pk=second.pk).house_number, "52")


class AuthorizationTests(CorrectionCase):
    def test_only_an_active_staff_account_may_correct(self):
        blocked = staff_user("fix.blocked@example.org")
        blocked.set_status(type(blocked).Status.BLOCKED)
        customer_account = customer("fix.other.customer@example.org")
        before = self.events()
        point = self.point()
        for actor in (self.person, customer_account, blocked, None):
            with self.subTest(actor=getattr(actor, "email", None)):
                with directory(candidate(90004270, house_number="52")):
                    self.assertEqual(code_of(lambda: self.fix_building(actor=actor, house_number="52")), "not_allowed")
                self.assertEqual(
                    code_of(lambda: self.fix_point(point, actor=actor, entered_number="9")), "not_allowed"
                )
        self.assertEqual((self.row().house_number, self.point().number), ("51", point.number))
        self.assertEqual(self.events(), before)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF).exists())

    def test_a_demoted_staff_account_is_refused_because_the_actor_is_read_again(self):
        stale = self.staff
        self.staff.set_privileges(is_staff=False, is_superuser=False)
        with directory(candidate(90004270, house_number="52")):
            self.assertEqual(code_of(lambda: self.fix_building(actor=stale, house_number="52")), "not_allowed")

    def test_the_service_locks_the_account_then_the_application_then_the_row(self):
        with directory(candidate(90004270, house_number="52")):
            with CaptureQueriesContext(connection) as queries:
                self.fix_building(house_number="52")
        locks = [query["sql"] for query in queries if "FOR UPDATE" in query["sql"]]
        tables = [next(table for table in ("accounts_user", "community_application`", "community_applicationbuilding")
                       if table in sql) for sql in locks[:3]]
        self.assertEqual(tables, ["accounts_user", "community_application`", "community_applicationbuilding"])


class MeteringPointCorrectionTests(CorrectionCase):
    def test_the_number_is_corrected_with_the_rule_of_the_wizard_and_recorded_without_a_value(self):
        with self.captureOnCommitCallbacks(execute=True):
            result = self.fix_point(entered_number="5")

        point = self.point()
        self.assertEqual(point.number, full_number(5))
        self.assertEqual(result.fields, ("number",))
        entry = self.corrected(HistoryEvent.APPLICATION_POINT_CORRECTED)[0]
        self.assertEqual(entry.detail, {"point_id": point.pk, "fields": ["number"]})
        self.assertNotIn(full_number(5), str(entry.detail))
        self.assertEqual(
            SecurityEvent.objects.filter(
                event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF, account_id=self.person.pk, actor_id=self.staff.pk
            ).count(),
            1,
        )

    def test_white_space_is_ignored_and_a_pasted_complete_number_loses_the_prefix(self):
        pasted = "CH1A2B3C4D5E6\u00a00000\u00a00000\u00a00000\u00a00000\u00a00008"
        self.fix_point(entered_number=pasted)
        self.assertEqual(self.point().number, full_number(8))
        self.fix_point(entered_number=" 0000 0000 0000 0000 0009 ")
        self.assertEqual(self.point().number, full_number(9))
        self.fix_point(entered_number="ab-1")
        self.assertEqual(self.point().number, "CH1A2B3C4D5E6" + "0" * 16 + "AB-1")

    def test_a_number_that_fails_the_rule_is_refused(self):
        for typed, code in (("1" * 21, "too_long"), ("a!", "not_allowed"), ("", "empty")):
            with self.subTest(typed=typed):
                with self.assertRaises(ValidationError) as refused:
                    self.fix_point(entered_number=typed)
                self.assertEqual(refused.exception.code, code)
        self.assertEqual(self.point().number, full_number(1))

    def test_the_plant_details_are_corrected_and_an_unusual_figure_only_warns(self):
        feed_in = self.point(Direction.FEED_IN)
        result = self.fix_point(
            feed_in, direction=Direction.FEED_IN, pv_power_kwp=Decimal("9.5"), battery_kwh=Decimal("10"),
            wallbox_kw=Decimal("5000"),
        )

        feed_in.refresh_from_db()
        self.assertEqual((feed_in.pv_power_kwp, feed_in.battery_kwh, feed_in.wallbox_kw),
                         (Decimal("9.50"), Decimal("10.00"), Decimal("5000.00")))
        self.assertEqual(result.fields, ("pv_power_kwp", "battery_kwh", "wallbox_kw"))
        self.assertEqual(result.warnings, (("wallbox_kw", "unusually_large"),))
        self.assertEqual(self.corrected(HistoryEvent.APPLICATION_POINT_CORRECTED)[0].detail["fields"],
                         ["pv_power_kwp", "battery_kwh", "wallbox_kw"])
        # An empty figure clears it.
        self.fix_point(feed_in, direction=Direction.FEED_IN, pv_power_kwp=None, battery_kwh=None, wallbox_kw=None)
        feed_in.refresh_from_db()
        self.assertEqual((feed_in.pv_power_kwp, feed_in.battery_kwh, feed_in.wallbox_kw), (None, None, None))

    def test_a_figure_that_fails_the_rules_and_plant_details_for_a_draw_are_refused(self):
        feed_in = self.point(Direction.FEED_IN)
        with self.assertRaises(ValidationError):
            self.fix_point(feed_in, direction=Direction.FEED_IN, pv_power_kwp=Decimal("-1"))
        self.assertEqual(
            code_of(lambda: self.fix_point(self.point(), pv_power_kwp=Decimal("3"))), "plant_data_feed_in_only"
        )

    def test_the_number_must_be_free_and_a_number_of_another_account_gives_the_staff_no_notice(self):
        make_metering_point(5)  # a stored metering point of nobody here
        mine = make_metering_point(6)
        assign_person(mine, self.person)
        other = customer("fix.other.number@example.org")
        other_application, _building = ready_draft(other, egid=90004281, number=7, address={"house_number": "81"})
        applications.submit(other, other_application, today=TODAY)
        feed_in = self.point(Direction.FEED_IN)
        notices = ApplicationNotice.objects.count()

        results = {
            typed: code_of(lambda: self.fix_point(self.point(), entered_number=typed))
            for typed in ("2", "5", "6", "7")  # the feed-in of this application, stored, own stored, another account's
        }

        self.assertEqual(results, {"2": "number_in_application", "5": "number_taken", "6": "number_own", "7": "number_taken"})
        self.assertEqual(self.point().number, full_number(1))
        self.assertEqual(ApplicationNotice.objects.count(), notices)
        self.assertEqual(feed_in.number, full_number(2))

    def test_the_number_of_a_draft_of_another_account_blocks_nobody(self):
        other = customer("fix.other.draft@example.org")
        ready_draft(other, egid=90004282, number=8, address={"house_number": "82"})  # a draft: not submitted
        self.fix_point(entered_number="8")
        self.assertEqual(self.point().number, full_number(8))

    def test_a_stored_prefix_that_differs_from_the_settings_stays_unless_the_number_changes(self):
        foreign = "CH" + "9" * 11 + "0" * 19 + "1"
        ApplicationMeteringPoint.objects.filter(pk=self.point().pk).update(number=foreign)

        self.assertEqual(code_of(lambda: self.fix_point(entered_number="1")), "unchanged")  # nothing changed at all
        self.assertEqual(self.point().number, foreign)
        feed_in = self.point(Direction.FEED_IN)
        self.fix_point(feed_in, direction=Direction.FEED_IN, pv_power_kwp=Decimal("4"))
        self.assertEqual(self.point().number, foreign)
        self.fix_point(self.point(), entered_number="3")
        self.assertEqual(self.point().number, full_number(3))

    def test_the_directions_of_a_draw_and_a_feed_in_entered_the_wrong_way_round_are_exchanged(self):
        draw, feed_in = self.point(), self.point(Direction.FEED_IN)
        # The feed-in has plant details: a draw cannot have them, so they have to be cleared first.
        ApplicationMeteringPoint.objects.filter(pk=feed_in.pk).update(pv_power_kwp=Decimal("5"))
        self.assertEqual(
            code_of(lambda: self.fix_point(draw, direction=Direction.FEED_IN, pv_power_kwp=Decimal("5"))),
            "plant_data_swap",
        )
        self.fix_point(feed_in, pv_power_kwp=None)

        self.fix_point(draw, direction=Direction.FEED_IN, pv_power_kwp=Decimal("6"))

        draw.refresh_from_db()
        feed_in.refresh_from_db()
        self.assertEqual((draw.direction, draw.pv_power_kwp), (Direction.FEED_IN, Decimal("6.00")))
        self.assertEqual((feed_in.direction, feed_in.pv_power_kwp), (Direction.CONSUMPTION, None))
        details = [entry.detail for entry in self.corrected(HistoryEvent.APPLICATION_POINT_CORRECTED)]
        self.assertIn({"point_id": feed_in.pk, "fields": ["direction"]}, details)
        self.assertEqual(self.building.metering_points.count(), 2)

    def test_a_direction_that_would_break_what_the_submission_requires_is_refused(self):
        feed_in = self.point(Direction.FEED_IN)
        # Without a draw the building cannot go on: the only draw of a building cannot become a feed-in.
        applications_row = ApplicationMeteringPoint.objects.get(pk=feed_in.pk)
        applications_row.delete()
        draw = self.point()
        self.assertEqual(
            code_of(lambda: self.fix_point(draw, direction=Direction.FEED_IN)), "draw_required"
        )
        self.assertEqual(self.point().direction, Direction.CONSUMPTION)

    def test_a_feed_in_needs_the_bank_connection_of_the_customer(self):
        from leg_platform.community import own_iban

        # A building with a draw only (the feed-in row is deleted, as a customer without PV would have it).
        ApplicationMeteringPoint.objects.filter(building=self.building, direction=Direction.FEED_IN).delete()
        own_iban.remove_own_iban(self.person)

        self.assertEqual(code_of(lambda: self.fix_point(direction=Direction.FEED_IN)), "iban_required")
        self.assertEqual(self.point().direction, Direction.CONSUMPTION)

    def test_it_applies_only_in_the_states_in_which_the_leg_works_on_the_application(self):
        for state in (State.DRAFT, State.PAUSED, State.COMPLETED):
            with self.subTest(state=state):
                self.set_state(state)
                self.assertEqual(code_of(lambda: self.fix_point(entered_number="9")), "wrong_state")
        for state in (State.SUBMITTED, State.IN_REVIEW, State.OPERATOR_STARTED, State.OPERATOR_CONFIRMED):
            with self.subTest(state=state):
                self.set_state(state)
                self.fix_point(entered_number="9", direction=Direction.CONSUMPTION)
                self.fix_point(entered_number="1", direction=Direction.CONSUMPTION)
        self.assertEqual(self.point().number, full_number(1))

    def test_a_stale_dialog_after_the_customer_changed_the_application_changes_nothing(self):
        point = self.point()
        token = application_corrections.point_token(point)
        applications.reopen_for_change(self.person, self.application)
        applications.replace_metering_point(self.person, self.application, point.pk, entered_number="12")
        applications.submit(self.person, self.application, today=TODAY)  # submitted again: the state fits, the row does not

        code = code_of(lambda: self.fix_point(point, entered_number="9", direction=Direction.CONSUMPTION, expected=token))

        self.assertEqual(code, "changed")
        self.assertEqual(self.point().number, full_number(12))

    def test_a_stale_application_object_is_read_again_under_the_lock(self):
        point = self.point()
        stale = Application.objects.get(pk=self.application.pk)  # the page was built while submitted
        application_staff.start_review(self.application, self.staff)  # allowed: in review is a state of the LEG's work
        self.fix_point(point, application=stale, entered_number="4", direction=Direction.CONSUMPTION)
        self.assertEqual(self.point().number, full_number(4))
        # The customer cannot take it back any more once the review started: the staff's work is not undone.
        self.assertEqual(code_of(lambda: applications.reopen_for_change(self.person, self.application)), "review_started")

    def test_the_point_of_another_application_or_a_missing_one_is_not_found(self):
        other = customer("fix.other.point@example.org")
        other_application, other_building = ready_draft(other, egid=90004283, number=20, address={"house_number": "83"})
        applications.submit(other, other_application, today=TODAY)
        foreign = other_building.metering_points.get()

        results = [
            code_of(lambda: self.fix_point(foreign, entered_number="21")),
            code_of(lambda: application_corrections.correct_metering_point(
                self.application, self.staff, 999999, entered_number="3", direction=Direction.CONSUMPTION, expected="x",
            )),
        ]

        self.assertEqual(results, ["not_found", "not_found"])
        self.assertEqual(ApplicationMeteringPoint.objects.get(pk=foreign.pk).number, full_number(20))

    def test_a_wrong_direction_value_and_an_unconfigured_prefix_are_refused(self):
        self.assertEqual(code_of(lambda: self.fix_point(direction="sideways")), "direction_invalid")
        from leg_platform.community.models import ManagementSettings

        ManagementSettings.objects.filter(id=1).update(identifier="")
        self.assertEqual(code_of(lambda: self.fix_point(entered_number="15")), "prefix_not_configured")

    def test_the_correction_does_not_touch_stored_metering_points(self):
        stored = MeteringPoint.objects.count()
        self.fix_point(entered_number="4")
        self.assertEqual(MeteringPoint.objects.count(), stored)


class BuildingCompleteTests(CorrectionCase):
    def test_a_stored_building_where_the_customer_has_a_draw_and_a_feed_in_is_refused_like_in_the_wizard(self):
        stored = make_location("Ittigenstrasse", "52", "3063", "Ittigen", egid=90004270)
        for last, direction in ((40, Direction.CONSUMPTION), (41, Direction.FEED_IN)):
            assign_person(make_metering_point(last, direction=direction, location=stored), self.person)
        before = self.row()

        with directory(candidate(90004270, house_number="52")):
            code = code_of(lambda: self.fix_building(house_number="52"))

        self.assertEqual(code, "building_complete")
        after = self.row()
        self.assertEqual((after.location_id, after.egid, after.house_number), (before.location_id, before.egid, "51"))
        self.assertEqual(self.corrected(HistoryEvent.APPLICATION_BUILDING_CORRECTED), [])

    def test_a_stored_building_with_only_one_direction_of_the_customer_is_still_attached_to(self):
        # The draw is the application's own (consumption) and the feed-in at the stored building is free: no refusal.
        stored = make_location("Ittigenstrasse", "52", "3063", "Ittigen", egid=90004270)
        assign_person(make_metering_point(40, direction=Direction.FEED_IN, location=stored), self.person)
        ApplicationMeteringPoint.objects.filter(building=self.building, direction=Direction.FEED_IN).delete()
        self.assertEqual(self.building.metering_points.count(), 1)

        with directory(candidate(90004270, house_number="52")):
            self.fix_building(house_number="52")

        self.assertEqual(self.row().location_id, stored.pk)


class AtTheGridOperatorTests(CorrectionCase):
    """A correction after the registration at the grid operator started (security review L-3, L-4)."""

    def other_application(self, number, email="fix.operator.other@example.org", earlier=True):
        other = customer(email)
        application, _building = ready_draft(other, egid=90004290, number=number, address={"house_number": "90"})
        applications.submit(other, application, today=TODAY)
        if earlier:
            Application.objects.filter(pk=application.pk).update(
                submitted_at=self.application.submitted_at - timedelta(days=1)
            )
        return application

    def test_a_number_of_another_application_is_refused_in_every_state_that_is_at_the_grid_operator(self):
        self.other_application(7)
        for state in (State.OPERATOR_STARTED, State.OPERATOR_CONFIRMED):
            with self.subTest(state=state):
                self.set_state(state)
                self.assertEqual(code_of(lambda: self.fix_point(entered_number="7")), "number_taken")
                self.assertEqual(self.point().number, full_number(1))

    def test_the_number_is_checked_again_after_the_save_with_the_rule_of_the_review(self):
        # The earlier check (``applications._check_number``) is replaced to show the second line of defence: a number that
        # was stored for a metering point or named by an earlier application in between (the window of a race).
        make_metering_point(5)
        self.other_application(7)
        for state in (State.OPERATOR_STARTED, State.OPERATOR_CONFIRMED):
            for typed in ("5", "7"):
                with self.subTest(state=state, typed=typed), patch.object(applications, "_check_number"):
                    self.set_state(state)
                    notices = ApplicationNotice.objects.count()
                    self.assertEqual(code_of(lambda: self.fix_point(entered_number=typed)), "number_taken")
                    self.assertEqual(self.point().number, full_number(1))
                    self.assertEqual(self.corrected(HistoryEvent.APPLICATION_POINT_CORRECTED), [])
                    # Rolled back: no notice of the data change either.
                    self.assertEqual(ApplicationNotice.objects.count(), notices)
        # An application that was submitted later yields (the review's rule): its number is not a conflict for this one.
        later = self.other_application(8, "fix.operator.later@example.org", earlier=False)
        later.refresh_from_db()
        self.application.refresh_from_db()
        self.assertGreater(later.submitted_at, self.application.submitted_at)
        self.set_state(State.OPERATOR_STARTED)
        with patch.object(applications, "_check_number"):
            self.fix_point(entered_number="8")
        self.assertEqual(self.point().number, full_number(8))

    def test_a_correction_after_the_registration_writes_a_notice_for_the_staff(self):
        for state in (State.OPERATOR_STARTED, State.OPERATOR_CONFIRMED):
            with self.subTest(state=state):
                ApplicationNotice.objects.all().delete()
                self.set_state(state)
                self.fix_point(entered_number="9", direction=Direction.CONSUMPTION)
                notice = ApplicationNotice.objects.get()
                self.assertEqual(
                    (notice.person_id, notice.kind, notice.egid, notice.numbers, notice.resolved_at),
                    (self.person.pk, "operator_changed", None, [full_number(9)], None),
                )
                # The same data again does not pile up notices; another correction is another notice.
                self.fix_point(entered_number="1", direction=Direction.CONSUMPTION)
                self.assertEqual(ApplicationNotice.objects.count(), 2)
                self.fix_point(entered_number="9", direction=Direction.CONSUMPTION)
                self.assertEqual(ApplicationNotice.objects.filter(numbers=[full_number(9)]).count(), 1)
                self.fix_point(entered_number="1", direction=Direction.CONSUMPTION)

    def test_a_correction_of_the_address_after_the_registration_writes_the_notice_with_the_egid(self):
        self.set_state(State.OPERATOR_STARTED)
        with directory(candidate(90004270, house_number="52")):
            self.fix_building(house_number="52")
        notice = ApplicationNotice.objects.get()
        self.assertEqual((notice.kind, notice.egid, notice.numbers), ("operator_changed", 90004270, []))

    def test_no_notice_before_the_registration_and_none_for_a_refused_or_an_unchanged_correction(self):
        for state in (State.SUBMITTED, State.IN_REVIEW):
            with self.subTest(state=state):
                self.set_state(state)
                self.fix_point(entered_number="9", direction=Direction.CONSUMPTION)
                self.fix_point(entered_number="1", direction=Direction.CONSUMPTION)
        self.set_state(State.OPERATOR_STARTED)
        self.assertEqual(code_of(lambda: self.fix_point(entered_number="1")), "unchanged")
        self.assertEqual(code_of(lambda: self.fix_point(entered_number="2")), "number_in_application")
        self.assertEqual(ApplicationNotice.objects.count(), 0)

    def test_the_notice_is_written_although_the_account_has_the_most_open_notices(self):
        for index in range(MAX_UNRESOLVED_NOTICES_PER_PERSON):
            ApplicationNotice.objects.create(person=self.person, kind="building_unavailable", egid=1000 + index)
        self.set_state(State.OPERATOR_CONFIRMED)

        self.fix_point(entered_number="9", direction=Direction.CONSUMPTION)

        self.assertEqual(ApplicationNotice.objects.filter(kind="operator_changed").count(), 1)
