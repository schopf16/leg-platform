"""Independent acceptance pass of the management area, part 2: requests that a rule must refuse (steps out of
order, overlapping assignments, an inactive account, archived objects, forged ids in the body, hostile values),
the whole life of the objects at service level with explicit days (D-077 to D-084), and the lists with hostile
parameters, searches that never find a remark and the printouts of hostile names.

Fixtures and the snapshot of the whole domain are those of ``test_acceptance_matrix.py``. Days are explicit
(``today=``) wherever the rule depends on the day, so that no test depends on the day it runs; the status rules are
asked day by day around the edges (the day before, the day itself).
"""

import itertools
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import tag
from django.urls import reverse
from django.utils.http import urlencode

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.community import (
    assignments,
    history,
    leg_services,
    legs,
    locations,
    metering_points,
    numbers,
    problems,
    transformer_circuits,
)
from leg_platform.community.history import HistoryEvent
from leg_platform.community.legs import DiscountTier, LegProblem
from leg_platform.community.models import (
    Direction,
    Leg,
    LegAssignment,
    ListPreference,
    Location,
    ManagementSettings,
    MeteringPoint,
    PersonAssignment,
    PersonRecord,
    TransformerCircuit,
)
from leg_platform.community.status import MeteringPointStatus as Status
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    number,
)
from leg_platform.management.printing import areas, pdf
from leg_platform.management.tests.test_acceptance_matrix import (
    TODAY,
    MatrixBase,
    month_end,
    preferences,
    snapshot,
)

BASE = date(2026, 3, 10)
ZURICH = ZoneInfo("Europe/Zurich")
SEQUENCE = itertools.count(10000)  # numbers of metering points that cannot clash with those of the fixture


def iso(days, base=TODAY):
    return (base + timedelta(days=days)).isoformat()


def url(name, obj=None):
    return reverse(f"management:{name}", args=[obj.pk] if obj is not None else [])


@tag("mariadb")
class RefusedRequestTests(MatrixBase):
    """A direct POST that the rules refuse changes nothing and never ends in an error page."""

    def setUp(self):
        self.client = self.login(self.administrator)

    def post(self, name, obj, data):
        return self.client.post(url(name, obj), data)

    def assert_refused(self, name, obj, data, message=""):
        before = snapshot()
        response = self.post(name, obj, data)
        self.assertIn(response.status_code, (200, 302, 404), f"{name} {data}")
        self.assertEqual(snapshot(), before, f"{name} {data} changed the state")
        return response

    # The steps of a metering point ---------------------------------------------------------------

    def test_a_step_out_of_order_is_refused_in_every_status(self):
        terminated = make_metering_point(
            8, location=self.place, registered_with_operator_on=TODAY - timedelta(days=90),
            confirmed_on=TODAY - timedelta(days=89), start_date=TODAY - timedelta(days=80),
            terminated_on=TODAY - timedelta(days=1),
        )
        ending = make_metering_point(
            9, location=self.place, registered_with_operator_on=TODAY - timedelta(days=90),
            confirmed_on=TODAY - timedelta(days=89), start_date=TODAY - timedelta(days=80),
            terminated_on=month_end(TODAY + timedelta(days=40)),  # recorded, not reached: still active
        )
        steps = {
            "metering_points_notify": {"registered_on": iso(0)},
            "metering_points_confirm": {"confirmed_on": iso(0), "start_date": ""},
            "metering_points_start": {"start_date": iso(0)},
            "metering_points_terminate": {"terminated_on": month_end(TODAY).isoformat()},
        }
        right_step = {
            self.point_registered: "metering_points_notify", self.point_notified: "metering_points_confirm",
            self.point_standby: "metering_points_start", self.point_active: "metering_points_terminate",
            terminated: None, ending: None, self.point_archived: None,
        }
        for point, right in right_step.items():
            for name, data in steps.items():
                if name == right:
                    continue
                with self.subTest(status=point.status().value, step=name):
                    self.assert_refused(name, point, data)

    def test_a_step_cannot_be_done_twice(self):
        for name, point, data in (
            ("metering_points_notify", self.point_registered, {"registered_on": iso(0)}),
            ("metering_points_confirm", self.point_notified, {"confirmed_on": iso(0), "start_date": iso(-1)}),
            ("metering_points_start", self.point_standby, {"start_date": iso(-2)}),
            ("metering_points_terminate", self.point_active, {"terminated_on": month_end(TODAY).isoformat()}),
        ):
            with self.subTest(step=name):
                self.assertEqual(self.post(name, point, data).status_code, 302)
                self.assert_refused(name, point, data)

    def test_the_dates_of_a_step_are_checked_on_the_server(self):
        refused = (
            ("metering_points_notify", self.point_registered, {"registered_on": iso(1)}),  # the future
            ("metering_points_notify", self.point_registered, {"registered_on": ""}),
            ("metering_points_confirm", self.point_notified, {"confirmed_on": iso(-6)}),  # before the registration
            ("metering_points_confirm", self.point_notified, {"confirmed_on": iso(1)}),
            ("metering_points_start", self.point_standby, {"start_date": ""}),
            ("metering_points_terminate", self.point_active, {"terminated_on": iso(0)[:8] + "15"}),  # not a month end
            ("metering_points_terminate", self.point_active, {"terminated_on": "2026-01-31"}),  # before the start
        )
        for name, point, data in refused:
            with self.subTest(step=name, data=data):
                self.assert_refused(name, point, data)
        # A termination in the future is recorded and the metering point stays active until the day.
        future = month_end(TODAY + timedelta(days=45))
        self.post("metering_points_terminate", self.point_active, {"terminated_on": future.isoformat()})
        self.point_active.refresh_from_db()
        self.assertEqual(self.point_active.terminated_on, future)
        self.assertEqual(self.point_active.status(), Status.ACTIVE)
        self.assertEqual(self.point_active.status(future - timedelta(days=1)), Status.ACTIVE)
        self.assertEqual(self.point_active.status(future), Status.TERMINATED)

    def test_hostile_and_extreme_dates_never_end_in_an_error(self):
        hostile = ("9999-12-31", "2026-02-30", "x" * 10000, "1e3", "٢٠٢٦-١٠-١٧")
        for value in hostile:
            for name, point, field in (
                ("metering_points_notify", self.point_registered, "registered_on"),
                ("metering_points_confirm", self.point_notified, "confirmed_on"),
                ("metering_points_start", self.point_standby, "start_date"),
                ("metering_points_terminate", self.point_active, "terminated_on"),
                ("metering_points_person", self.point_registered, "valid_from"),
                ("metering_points_leg", self.point_notified, "valid_from"),
            ):
                data = {field: value}
                if field == "valid_from":
                    data.update({"person": self.anna.email, "leg": str(self.leg.pk)})
                with self.subTest(value=value[:20], step=name):
                    with transaction_rollback():
                        response = self.post(name, point, data)
                        self.assertIn(response.status_code, (200, 302))

    # Assignments -----------------------------------------------------------------------------------

    def test_an_assignment_that_overlaps_or_does_not_start_later_is_refused(self):
        start = TODAY - timedelta(days=30)
        for days in (-30, -60, -31):  # the same start, earlier ones
            with self.subTest(person_from=iso(days)):
                self.assert_refused(
                    "metering_points_person", self.point_active, {"person": self.anna.email, "valid_from": iso(days)}
                )
        # Not after the start of the last one, which lies in the future.
        self.assert_refused(
            "metering_points_person", self.point_future, {"person": self.anna.email, "valid_from": iso(10)}
        )
        self.assert_refused(
            "metering_points_person", self.point_future, {"person": self.anna.email, "valid_from": iso(30)}
        )
        for days in (-30, -31, -100):
            self.assert_refused("metering_points_leg", self.point_active, {"leg": str(self.leg_clean.pk), "valid_from": iso(days)})
        self.assertEqual(start, self.active_assignment.valid_from)

    def test_a_flying_change_leaves_no_gap_and_no_overlap(self):
        before = self.active_assignment.valid_to
        self.assertIsNone(before)
        ruth = create_active_user("ruth.person@example.org", first_name="Ruth", last_name="Beispiel")
        self.assertEqual(
            self.post("metering_points_person", self.point_active, {"person": ruth.email, "valid_from": iso(10)}).status_code, 302
        )
        self.active_assignment.refresh_from_db()
        new = PersonAssignment.objects.get(person=ruth)
        self.assertEqual(self.active_assignment.valid_to, new.valid_from - timedelta(days=1))
        self.assertIsNone(new.valid_to)
        # The person assignments of one point never overlap.
        rows = list(PersonAssignment.objects.filter(metering_point=self.point_active).order_by("valid_from"))
        for earlier, later in zip(rows, rows[1:]):
            self.assertLess(earlier.valid_to, later.valid_from)

    def test_ending_and_deleting_assignments_that_are_not_to_be_ended_or_deleted(self):
        # An open assignment of another point is no target of the point that is addressed.
        self.assert_refused(
            "metering_points_person_end", self.point_active,
            {"valid_to": iso(10), "assignment": str(self.future_assignment.pk)},
        )
        self.assert_refused(
            "metering_points_person_delete", self.point_future, {"assignment": str(self.active_assignment.pk)}
        )
        # An assignment in force is never deleted, the one that was ended is not ended again.
        self.assert_refused(
            "metering_points_person_delete", self.point_active, {"assignment": str(self.active_assignment.pk)}
        )
        for value in ("", "x", "-1", "0", "9" * 40, "٣"):
            self.assert_refused("metering_points_person_end", self.point_active, {"valid_to": iso(10), "assignment": value})
            self.assert_refused("metering_points_person_delete", self.point_future, {"assignment": value})
        self.assert_refused(
            "metering_points_person_end", self.point_active,
            {"valid_to": iso(-31), "assignment": str(self.active_assignment.pk)},  # before the start
        )
        self.assert_refused(
            "metering_points_person_end", self.point_registered, {"valid_to": iso(1), "assignment": "1"}
        )  # nothing to end

    def test_an_account_that_is_not_active_cannot_be_assigned_as_a_person(self):
        blocked = create_active_user("blocked.person@example.org", first_name="Bea", last_name="Gesperrt")
        blocked.set_status(blocked.Status.BLOCKED)
        deactivated = create_active_user("gone.person@example.org", first_name="Gone", last_name="Person")
        deactivated.set_status(deactivated.Status.DEACTIVATED)
        pending = type(blocked).objects.create_user(  # a registration that is not completed has no customer number
            "pending.person@example.org", first_name="Pen", last_name="Ding", street="S", house_number="1", town="Bern",
            swiss_postal_code=3000,
        )
        self.assertIsNone(pending.customer_number_id)
        for who in (blocked, deactivated, pending):
            for text in (who.email, who.last_name, who.first_name + " " + who.last_name):
                with self.subTest(person=who.email, typed=text):
                    self.assert_refused("metering_points_person", self.point_registered, {"person": text, "valid_from": iso(0)})
        # The rule itself, with an account object that was loaded while it was still active (the account is read again
        # under a lock, so that a block in the meantime is respected).
        stale = create_active_user("stale.person@example.org", first_name="Sta", last_name="Le")
        type(stale).objects.get(pk=stale.pk).set_status(stale.Status.BLOCKED)
        self.assertEqual(stale.status, stale.Status.ACTIVE)  # the loaded object does not know it
        for who, code in ((stale, "person_not_active"), (blocked, "person_not_active"), (deactivated, "person_not_active"), (pending, "not_a_person")):
            with self.assertRaises(assignments.AssignmentError) as raised:
                assignments.assign(assignments.PERSON, self.point_registered, who, TODAY)
            self.assertEqual(raised.exception.code, code)
        self.assertFalse(PersonAssignment.objects.filter(metering_point=self.point_registered).exists())
        self.assertFalse(type(blocked).objects.get(pk=blocked.pk).retention_obligation)

    def test_archived_objects_take_no_step_no_assignment_and_no_leg(self):
        for name, data in (
            ("metering_points_notify", {"registered_on": iso(0)}),
            ("metering_points_confirm", {"confirmed_on": iso(0)}),
            ("metering_points_start", {"start_date": iso(0)}),
            ("metering_points_terminate", {"terminated_on": month_end(TODAY).isoformat()}),
            ("metering_points_person", {"person": self.anna.email, "valid_from": iso(0)}),
            ("metering_points_leg", {"leg": str(self.leg.pk), "valid_from": iso(0)}),
        ):
            with self.subTest(step=name):
                self.assert_refused(name, self.point_archived, data)
        # An archived LEG is not offered and a forged id of it is refused.
        self.assert_refused("metering_points_leg", self.point_notified, {"leg": str(self.leg_archived.pk), "valid_from": iso(0)})
        self.assertFalse(LegAssignment.objects.filter(leg=self.leg_archived).exists())

    def test_the_restore_of_an_archived_point_is_the_only_change_it_takes(self):
        self.post("metering_points_archive", self.point_archived, {"archive": "0"})
        self.point_archived.refresh_from_db()
        self.assertFalse(self.point_archived.is_archived)

    # Archived locations and LEGs are read-only like archived points (D-085) ------------------------

    def assert_archived_refusal(self, name, obj, data, text):
        """A direct POST (and the GET of a dialog) is refused with the message of the object, goes back to its
        page and changes nothing."""
        before = snapshot()
        response = self.client.post(url(name, obj), data, follow=True)
        self.assertEqual(response.status_code, 200, name)
        self.assertContains(response, text, msg_prefix=name)
        self.assertEqual(snapshot(), before, f"{name} changed an archived object")

    def test_an_archived_location_takes_no_change_by_a_direct_post(self):
        old = make_location(
            "Oldweg", "1", "3000", "Bern", is_archived=True, needs_confirmation=True, suggested_street="Oldweg",
            suggested_house_number="1", suggested_postal_code="3000", suggested_town="Bern", address_correction_open=True,
        )
        text = "Dieser Standort ist archiviert."
        for name, data in (
            ("locations_address", {"street": "Neuweg", "house_number": "8", "postal_code": "3000", "town": "Bern"}),
            ("locations_remark", {"remark": "changed"}),
            ("locations_circuit", {"transformer_circuit": "TRA2000"}),
            ("locations_circuit_new", {"grid_number": "TRA9999", "name": "Neun"}),
            ("locations_metering_point_new", {"direction": "consumption", "number_digits": "999"}),
            ("locations_confirm", {}),
            ("locations_suggestion", {"action": "dismiss"}),
            ("locations_suggestion", {"action": "accept"}),
            ("locations_delete", {}),
        ):
            with self.subTest(name=name, data=data):
                self.assert_archived_refusal(name, old, data, text)
        for name in ("locations_address", "locations_remark", "locations_circuit_new", "locations_metering_point_new", "locations_delete"):
            with self.subTest(dialog=name):
                response = self.client.get(url(name, old), follow=True)
                self.assertContains(response, text)  # no dialog of an archived location
        # The page offers no edit button, only "Restore".
        page = self.client.get(url("locations_detail", old))
        for name in ("locations_address", "locations_remark", "locations_metering_point_new"):
            self.assertNotContains(page, url(name, old))
        self.assertContains(page, "Standort wiederherstellen")
        # Restore is the one action; afterwards the location can be changed again.
        self.client.post(url("locations_archive", old), {"archive": "0"})
        old.refresh_from_db()
        self.assertFalse(old.is_archived)
        self.client.post(url("locations_remark", old), {"remark": "changed"})
        old.refresh_from_db()
        self.assertEqual(old.remark, "changed")

    def test_an_archived_location_is_read_only_in_the_services_too(self):
        old = make_location("Oldweg", "2", "3000", "Bern", is_archived=True)
        before = snapshot()
        for call in (
            lambda: locations.update_address(old, street="Neuweg", house_number="8", postal_code="3000", town="Bern"),
            lambda: locations.update_remark(old, "x"),
            lambda: locations.set_circuit(old, self.circuit),
            lambda: locations.confirm(old),
            lambda: locations.accept_suggestion(old, None),
            lambda: locations.dismiss_suggestion(old, None),
            lambda: metering_points.create_metering_point(direction=Direction.CONSUMPTION, location=old, number=number(998)),
        ):
            with self.assertRaises(locations.Archived):
                call()
        self.assertEqual(snapshot(), before)

    def test_an_archived_leg_takes_no_change_by_a_direct_post(self):
        text = "Diese LEG ist archiviert."
        for name, data in (
            ("legs_quota", {"quota": "30", "checked_on": iso(0)}),
            ("legs_edit", {"name": "LEG Renamed", "remark": "r"}),
            ("legs_delete", {}),
        ):
            with self.subTest(name=name):
                self.assert_archived_refusal(name, self.leg_archived, data, text)
                self.assertContains(self.client.get(url(name, self.leg_archived), follow=True), text)
        # An archived LEG takes no assignment (the dialog of the point and the service), whatever the way.
        self.assert_refused("metering_points_leg", self.point_notified, {"leg": str(self.leg_archived.pk), "valid_from": iso(0)})
        with self.assertRaises(assignments.AssignmentError) as raised:
            assignments.assign(assignments.LEG, self.point_clean, self.leg_archived, TODAY)
        self.assertEqual(raised.exception.code, "leg_archived")
        with self.assertRaises(leg_services.Archived):
            leg_services.update_quota(self.leg_archived, Decimal("10"), TODAY)
        with self.assertRaises(leg_services.Archived):
            leg_services.update_master_data(self.leg_archived, "LEG Alt", "x")
        with self.assertRaises(assignments.AssignmentError):
            metering_points.create_metering_point(
                direction=Direction.CONSUMPTION, number=number(997), leg=self.leg_archived
            )
        self.assertFalse(MeteringPoint.objects.filter(number=number(997)).exists())
        page = self.client.get(url("legs_detail", self.leg_archived))
        for name in ("legs_quota", "legs_edit"):
            self.assertNotContains(page, url(name, self.leg_archived))
        self.assertContains(page, "LEG wiederherstellen")
        self.client.post(url("legs_archive", self.leg_archived), {"archive": "0"})
        self.leg_archived.refresh_from_db()
        self.assertFalse(self.leg_archived.is_archived)
        self.client.post(url("legs_edit", self.leg_archived), {"name": "LEG Renamed", "remark": "r"})
        self.leg_archived.refresh_from_db()
        self.assertEqual(self.leg_archived.name, "LEG Renamed")

    # Forged and hostile bodies ----------------------------------------------------------------------

    def test_forged_ids_in_the_body_are_ignored(self):
        before = snapshot()
        for forged in ("999999", "-1", "abc", "1; DROP TABLE community_leg", "9" * 40, "٣"):
            for name, obj, data in (
                ("metering_points_leg", self.point_active, {"leg": forged, "valid_from": iso(5)}),
                ("metering_points_leg", self.point_active, {"leg": str(self.leg_clean.pk), "valid_from": iso(5), "also": [forged]}),
                ("locations_circuit", self.place, {"transformer_circuit": forged}),
                ("locations_expanded", None, {"toggle": forged}),
                ("locations_expanded", None, {"ids": [forged], "open": "1"}),
            ):
                with self.subTest(name=name, forged=forged[:12]):
                    response = self.post(name, obj, data)
                    self.assertIn(response.status_code, (200, 302, 400))
        self.assertEqual(snapshot(), before)

    def test_the_saved_widths_and_open_rows_refuse_hostile_bodies(self):
        widths = reverse("management:list_widths", args=["locations"])
        bad = (
            "not json", "[]", "{}", '{"widths": 5}', '{"widths": {"street": -1}}', '{"widths": {"street": 99999}}',
            '{"widths": {"street": true}}', '{"widths": {"street": "200"}}', '{"widths": {"Bad Key": 200}}',
            '{"widths": {"' + "a" * 500 + '": 200}}', '{"widths": {' + ",".join(f'"c{i}": 200' for i in range(40)) + "}}",
            '{"widths": {"street": 1e999}}', "\x00", '{"widths": null}', "x" * 5000,
        )
        for body in bad:
            with self.subTest(body=body[:40]):
                response = self.client.post(widths, data=body, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(preferences(), [])
        for key in ("unknown", "x y", "../etc"):
            self.assertEqual(self.client.post(f"/management/lists/{key}/widths/", data="{}", content_type="application/json").status_code, 404)
        for body in ('{"ids": [0], "open": true}', '{"ids": [true], "open": true}', '{"ids": ["1"], "open": 1}', '{"ids": [1.5], "open": 1}',
                     '{"ids": ' + str(list(range(1, 600))) + ', "open": true}', "[]", "x"):
            with self.subTest(body=body[:40]):
                response = self.client.post(url("locations_expanded"), data=body, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(preferences(), [])
        for toggle in ("0", "-1", "abc", "9" * 5000, "١٢"):
            response = self.client.post(url("locations_expanded"), {"toggle": toggle})
            self.assertIn(response.status_code, (302, 400))

    def test_a_circuit_text_that_is_hostile_is_refused_or_ignored(self):
        before = snapshot()
        for text in ("x" * 10000, "TRA1000 · <script>", "\x00", "TRA-1", "tra 1000", "TRA" + "9" * 40, "ＴＲＡ１０００"):
            with self.subTest(text=text[:20]):
                response = self.post("locations_circuit", self.place, {"transformer_circuit": text})
                self.assertEqual(response.status_code, 302)
        self.assertEqual(snapshot(), before)

    def test_a_circuit_is_not_created_for_a_location_that_does_not_exist(self):
        """F-1: a POST to ``locations/<unknown id>/circuit/new/`` is a 404 before anything is written (the
        location is looked up first, so that no transformer circuit is created for nothing)."""
        before = snapshot()
        response = self.client.post(
            reverse("management:locations_circuit_new", args=[999999]), {"grid_number": "TRA777", "name": "x"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(snapshot(), before)


class transaction_rollback:
    """A block whose changes are rolled back (a savepoint), so that a loop of requests starts from the same data."""

    def __enter__(self):
        from django.db import transaction

        self.atomic = transaction.atomic()
        self.atomic.__enter__()
        return self

    def __exit__(self, *exc):
        from django.db import transaction

        transaction.set_rollback(True)
        return self.atomic.__exit__(*exc)


@tag("mariadb")
class LifeOfTheObjectsTests(MatrixBase):
    """The rules of the specification at service level, with explicit days."""

    # Transformer circuits ---------------------------------------------------------------------------

    def test_a_transformer_circuit_number_is_normalised_and_unique(self):
        created = transformer_circuits.create("  tra555 ", "Fuenf")
        self.assertEqual(created.grid_number, "TRA555")
        for duplicate in ("TRA555", "tra555", " Tra555  "):
            with self.assertRaises(ValidationError):
                transformer_circuits.create(duplicate, "Other")
        self.assertEqual(transformer_circuits.find(" tRa555").pk, created.pk)
        for bad in ("", "TRA", "TR1", "XRA1", "TRA-1", "TRA 1", "TRA1X", "TRA" + "1" * 18, "TRA١٢", "ＴＲＡ１"):
            with self.subTest(number=bad), self.assertRaises(ValidationError):
                transformer_circuits.create(bad)
        # The name is free and not unique.
        transformer_circuits.create("TRA556", "Fuenf")
        self.assertEqual(TransformerCircuit.objects.filter(name="Fuenf").count(), 2)
        self.assertEqual(transformer_circuits.number_of_choice("tra555 · Fuenf"), "TRA555")

    def test_creating_a_circuit_that_exists_uses_the_existing_one_and_changes_it_not(self):
        client = self.login(self.administrator)
        before = TransformerCircuit.objects.count()
        response = client.post(url("circuits_new"), {"grid_number": "tra1000", "name": "Other name"})
        self.assertEqual(response.status_code, 200)  # the hint, with the button "use this one"
        self.assertEqual(TransformerCircuit.objects.count(), before)
        response = client.post(url("circuits_new"), {"grid_number": "tra1000", "name": "Other name", "use_existing": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith(f"circuit={self.circuit.pk}"), response["Location"])
        self.assertEqual(TransformerCircuit.objects.count(), before)
        self.circuit.refresh_from_db()
        self.assertEqual((self.circuit.grid_number, self.circuit.name), ("TRA1000", "Eins"))
        # Changing a circuit into the number of another one is refused.
        response = client.post(url("circuits_edit", self.circuit_two), {"grid_number": "tra1000", "name": "Zwei"})
        self.assertEqual(response.status_code, 200)
        self.circuit_two.refresh_from_db()
        self.assertEqual(self.circuit_two.grid_number, "TRA2000")

    # Locations --------------------------------------------------------------------------------------

    def test_a_location_exists_once_whatever_the_spelling_of_case_accents_and_white_space(self):
        for street, number_, town in (
            ("examplestrasse", "1", "bern"), ("  Examplestrasse  ", " 1 ", "Bern"), ("EXAMPLESTRASSE", "1", "BERN"),
            ("Éxamplestrasse", "1", "Bérn"), ("Example  strasse", "1", "Bern"),
        ):
            with self.subTest(street=street):
                if street == "Example  strasse":
                    # Other letters are another address: only the white space is collapsed, not removed.
                    created = locations.create_location(street=street, house_number=number_, postal_code="3000", town=town)
                    self.assertNotEqual(created.pk, self.place.pk)
                    continue
                with self.assertRaises(ValidationError):
                    locations.create_location(street=street, house_number=number_, postal_code="3000", town=town)
        # The address addition is not part of the address (D-078, 4a).
        with self.assertRaises(ValidationError):
            locations.create_location(
                street="Examplestrasse", house_number="1", postal_code="3000", town="Bern", address_addition="Hinterhaus"
            )
        for bad in ("300", "30000", "0999", "abcd", "٣٠٠٠", " 3000x", ""):
            with self.subTest(postal_code=bad), self.assertRaises(ValidationError):
                locations.create_location(street="Neu", house_number="1", postal_code=bad, town="Bern")

    def test_a_duplicate_address_in_the_dialog_is_refused_and_creates_nothing(self):
        client = self.login(self.administrator)
        before = Location.objects.count()
        data = {"street": "EXAMPLESTRASSE", "house_number": "1", "postal_code": "3000", "town": "bern", "address_addition": "x"}
        response = client.post(url("locations_new"), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Diese Adresse gibt es schon.")
        self.assertEqual(Location.objects.count(), before)

    def test_changing_an_address_into_an_existing_one_needs_the_confirmation_of_the_shown_target(self):
        source = make_location("Quellweg", "4", "3000", "Bern")
        move = make_metering_point(300, location=source)
        address = {"street": "Examplestrasse", "house_number": "1", "postal_code": "3000", "town": "Bern"}
        client = self.login(self.administrator)
        # First answer: the warning (the target is named), nothing is moved; a forged confirmation without the
        # target that was shown is no confirmation.
        for forged in ({}, {"confirm_merge": "1"}, {"confirm_merge": "1", "merge_target": ""}):
            warning = client.post(url("locations_address", source), {**address, **forged})
            self.assertEqual(warning.status_code, 200)
            self.assertContains(warning, "Übertragen und speichern")
            self.assertTrue(Location.objects.filter(pk=source.pk).exists())
        move.refresh_from_db()
        self.assertEqual(move.location_id, source.pk)
        # A confirmation for another target than the one that is the address now is no confirmation.
        for target in ("", "abc", str(self.place_clean.pk), "999999"):
            again = client.post(url("locations_address", source), {**address, "confirm_merge": "1", "merge_target": target})
            self.assertEqual(again.status_code, 200)
            self.assertTrue(Location.objects.filter(pk=source.pk).exists())
        # The second explicit step moves the points, deletes the changed location and logs the merge.
        done = client.post(url("locations_address", source), {**address, "confirm_merge": "1", "merge_target": str(self.place.pk)})
        self.assertEqual(done.status_code, 302)
        self.assertFalse(Location.objects.filter(pk=source.pk).exists())
        move.refresh_from_db()
        self.assertEqual(move.location_id, self.place.pk)
        merged = history.entries_for(self.place).get(event=HistoryEvent.LOCATIONS_MERGED)
        self.assertEqual(merged.detail["metering_points"], 1)
        self.assertNotIn("Quellweg", json.dumps(merged.detail))
        self.assertEqual(merged.actor, self.administrator)

    def test_nothing_is_merged_into_an_archived_location(self):
        source = make_location("Quellweg", "4", "3000", "Bern")
        client = self.login(self.administrator)
        address = {"street": "Altweg", "house_number": "9", "postal_code": "3000", "town": "Bern"}
        for extra in ({}, {"confirm_merge": "1", "merge_target": str(self.place_archived.pk)}):
            response = client.post(url("locations_address", source), {**address, **extra})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(Location.objects.filter(pk=source.pk).exists())

    def test_a_location_with_metering_points_or_a_history_is_archived_not_deleted(self):
        with self.assertRaises(locations.NotDeletable):
            locations.delete(self.place)  # has metering points
        locations.set_archived(self.place_clean, True, self.administrator)
        with self.assertRaises(locations.Archived):
            locations.delete(self.place_clean)  # an archived location is only restored (D-086)
        locations.set_archived(self.place_clean, False, self.administrator)
        with self.assertRaises(locations.NotDeletable):
            locations.delete(self.place_clean)  # has a history now
        client = self.login(self.administrator)
        before = Location.objects.count()
        client.post(url("locations_delete", self.place))
        self.assertEqual(Location.objects.count(), before)

    # Metering point numbers ------------------------------------------------------------------------

    def test_the_number_is_composed_from_the_settings_and_the_typed_digits(self):
        client = self.login(self.administrator)
        response = client.post(url("metering_points_new"), {"number_digits": "777", "direction": "consumption"})
        self.assertEqual(response.status_code, 302)
        created = MeteringPoint.objects.get(number="CH1A2B3C4D5E6" + "777".zfill(20))
        self.assertEqual(len(created.number), 33)
        self.assertTrue(numbers.is_valid_metering_point_number(created.number))
        # The same number again: refused with the notice, nothing is created.
        count = MeteringPoint.objects.count()
        again = client.post(url("metering_points_new"), {"number_digits": "0777", "direction": "feed_in"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(MeteringPoint.objects.count(), count)
        # Other settings give another prefix; 20 digits are the most.
        row = ManagementSettings.load()
        row.country_code, row.identifier = "DE", "ABCDEFGHIJK"
        row.save()
        twenty = "9" * 20
        self.assertEqual(client.post(url("metering_points_new"), {"number_digits": twenty, "direction": "consumption"}).status_code, 302)
        self.assertTrue(MeteringPoint.objects.filter(number="DEABCDEFGHIJK" + twenty).exists())
        count = MeteringPoint.objects.count()
        for digits in ("1" * 21, "12a", "1 2", "٣", "-1", "1.5", "", "   ", "%"):
            with self.subTest(digits=digits):
                response = client.post(url("metering_points_new"), {"number_digits": digits, "direction": "consumption"})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(MeteringPoint.objects.count(), count)

    def test_without_the_identifier_in_the_settings_no_number_can_be_composed(self):
        row = ManagementSettings.load()
        row.identifier = ""
        row.save()
        client = self.login(self.administrator)
        count = MeteringPoint.objects.count()
        response = client.post(url("metering_points_new"), {"number_digits": "5", "direction": "consumption"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MeteringPoint.objects.count(), count)
        for bad in (("ch", "1A2B3C4D5E6"), ("CHE", "1A2B3C4D5E6"), ("CH", "1A2B3C4D5E"), ("CH", "1a2b3c4d5e6"), ("C1", "1A2B3C4D5E6")):
            with self.subTest(settings=bad), self.assertRaises(ValidationError):
                numbers.compose_metering_point_number(*bad, "1")
        self.assertEqual(numbers.compose_metering_point_number("CH", "1A2B3C4D5E6", "007"), "CH1A2B3C4D5E6" + "007".zfill(20))

    def test_a_number_without_digits_is_a_point_that_the_grid_operator_does_not_know_yet(self):
        first = metering_points.create_metering_point(direction=Direction.CONSUMPTION, number=None)
        second = metering_points.create_metering_point(direction=Direction.FEED_IN, number="")
        self.assertIsNone(first.number)
        self.assertIsNone(second.number)
        with self.assertRaises(metering_points.DuplicateNumber) as raised:
            metering_points.create_metering_point(direction=Direction.CONSUMPTION, number=number(1))
        self.assertEqual(raised.exception.existing.pk, self.point_registered.pk)

    def test_the_settings_page_checks_every_value(self):
        client = self.login(self.administrator)
        good = {
            "country_code": "ch", "identifier": "1a2b3c4d5e6", "warning_threshold_percent": "15",
            "sender_street": "Senderweg", "sender_house_number": "4", "sender_postal_code": "3000", "sender_town": "Bern",
        }
        self.assertEqual(client.post(url("settings"), good).status_code, 302)
        row = ManagementSettings.load()
        self.assertEqual((row.country_code, row.identifier, row.warning_threshold_percent), ("CH", "1A2B3C4D5E6", 15))
        events = set(history.entries_for(row).values_list("event", flat=True))
        self.assertIn(HistoryEvent.SENDER_ADDRESS_CHANGED, events)
        before = snapshot()
        for field, value in (
            ("warning_threshold_percent", "0"), ("warning_threshold_percent", "101"), ("warning_threshold_percent", "abc"),
            ("warning_threshold_percent", "10.5"), ("warning_threshold_percent", ""), ("country_code", "CHE"),
            ("country_code", "C1"), ("identifier", "1A2B3C4D5E"), ("identifier", "1A2B3C4D5E6X"), ("sender_postal_code", "30000"),
            ("sender_postal_code", "٣٠٠٠"), ("sender_street", ""), ("sender_town", ""),
        ):
            with self.subTest(field=field, value=value):
                self.assertEqual(client.post(url("settings"), {**good, field: value}).status_code, 200)
        self.assertEqual(snapshot(), before)

    # The status of a metering point ----------------------------------------------------------------

    def test_the_status_follows_the_dates_day_by_day(self):
        point = make_metering_point(200, location=self.place)
        self.assertEqual(point.status(BASE), Status.REGISTERED)
        self.assertEqual(metering_points.next_step(point, has_leg=True, today=BASE), metering_points.NOTIFY)
        self.assertEqual(metering_points.next_step(point, has_leg=False, today=BASE), metering_points.LEG_MISSING)
        with self.assertRaises(metering_points.StatusError) as raised:
            metering_points.mark_notified(point, BASE + timedelta(days=1), today=BASE)
        self.assertEqual(raised.exception.code, "date_in_future")
        point = metering_points.mark_notified(point, BASE, today=BASE)
        self.assertEqual(point.status(BASE), Status.NOTIFIED)
        with self.assertRaises(metering_points.StatusError) as raised:
            metering_points.record_confirmation(point, BASE - timedelta(days=1), today=BASE)
        self.assertEqual(raised.exception.code, "date_before_previous")
        # Without a start date the registration stays on standby, however long.
        point = metering_points.record_confirmation(point, BASE, None, today=BASE)
        for days in (0, 1, 365, 3650):
            self.assertEqual(point.status(BASE + timedelta(days=days)), Status.CONFIRMED)
        self.assertEqual(metering_points.next_step(point, has_leg=True, today=BASE), metering_points.START)
        # A start date in the past makes it active at once.
        past = BASE - timedelta(days=10)
        point = metering_points.record_start_date(point, past, today=BASE)
        self.assertEqual(point.status(past - timedelta(days=1)), Status.CONFIRMED)
        self.assertEqual(point.status(past), Status.ACTIVE)
        self.assertEqual(point.status(BASE), Status.ACTIVE)
        self.assertIsNone(metering_points.next_step(point, has_leg=True, today=BASE))
        # A start date cannot be entered twice.
        with self.assertRaises(metering_points.StatusError):
            metering_points.record_start_date(point, BASE, today=BASE)

    def test_the_start_date_in_the_future_and_the_termination_are_day_exact_and_the_database_agrees(self):
        start, end = BASE + timedelta(days=20), month_end(BASE + timedelta(days=60))
        point = make_metering_point(
            201, location=self.place, registered_with_operator_on=BASE - timedelta(days=5),
            confirmed_on=BASE - timedelta(days=4),
            start_date=start,
        )
        expected = (
            (start - timedelta(days=1), Status.CONFIRMED), (start, Status.ACTIVE), (end - timedelta(days=1), Status.ACTIVE),
        )
        for day, status in expected:
            self.assertEqual(point.status(day), status, day)
        with self.assertRaises(metering_points.StatusError) as raised:
            metering_points.terminate(point, end, today=BASE)  # still confirmed on the day of the test
        self.assertEqual(raised.exception.code, "wrong_status")
        # Terminate from the day the point is active: month ends only, not before the start, once.
        today = start
        for bad, code in ((start + timedelta(days=3), "not_month_end"), (month_end(start - timedelta(days=40)), "date_before_previous")):
            with self.assertRaises(metering_points.StatusError) as raised:
                metering_points.terminate(point, bad, today=today)
            self.assertEqual(raised.exception.code, code, bad)
        point = metering_points.terminate(point, end, today=today)
        for day, status in (
            (end - timedelta(days=1), Status.ACTIVE), (end, Status.TERMINATED), (end + timedelta(days=1), Status.TERMINATED),
        ):
            self.assertEqual(point.status(day), status, day)
            self.assertEqual(
                list(MeteringPoint.objects.with_status(status, day).filter(pk=point.pk).values_list("pk", flat=True)), [point.pk]
            )
            others = [s for s in Status.values if s != status]
            self.assertFalse(any(MeteringPoint.objects.with_status(s, day).filter(pk=point.pk).exists() for s in others))
        with self.assertRaises(metering_points.StatusError) as raised:
            metering_points.terminate(point, month_end(end + timedelta(days=40)), today=today)
        self.assertEqual(raised.exception.code, "wrong_status")
        self.assertTrue(metering_points.is_month_end(date(2028, 2, 29)))
        self.assertFalse(metering_points.is_month_end(date(2028, 2, 28)))
        self.assertTrue(metering_points.is_month_end(date(2027, 2, 28)))

    def test_every_status_condition_of_the_database_equals_the_function(self):
        d = BASE
        rows = [
            dict(),
            dict(registered_with_operator_on=d),
            dict(registered_with_operator_on=d, confirmed_on=d),
            dict(registered_with_operator_on=d, confirmed_on=d, start_date=d),
            dict(registered_with_operator_on=d, confirmed_on=d, start_date=d + timedelta(days=1)),
            dict(registered_with_operator_on=d, confirmed_on=d, start_date=d, terminated_on=d),
            dict(registered_with_operator_on=d, confirmed_on=d, start_date=d, terminated_on=d + timedelta(days=1)),
            dict(
                registered_with_operator_on=d, confirmed_on=d, start_date=d - timedelta(days=9),
                terminated_on=d - timedelta(days=1),
            ),
        ]
        points = [make_metering_point(400 + index, **row) for index, row in enumerate(rows)]
        for day in (d - timedelta(days=1), d, d + timedelta(days=1), d + timedelta(days=30)):
            for point in points:
                function = point.status(day)
                database = [s for s in Status.values if MeteringPoint.objects.with_status(s, day).filter(pk=point.pk).exists()]
                self.assertEqual(database, [function.value], (point.pk, day))

    # Assignments -----------------------------------------------------------------------------------

    def test_the_whole_life_of_an_assignment_chain(self):
        point = make_metering_point(210, location=self.place)
        first = create_active_user("first.person@example.org", first_name="Fritz", last_name="Erster")
        second = create_active_user("second.person@example.org", first_name="Sara", last_name="Zweite")
        third = create_active_user("third.person@example.org", first_name="Dora", last_name="Dritte")
        a = assignments.assign(assignments.PERSON, point, first, BASE - timedelta(days=30), self.administrator, today=BASE)
        first.refresh_from_db()
        self.assertTrue(first.retention_obligation)  # in force: the person is part of the accounting
        # A change that starts later: the previous one ends the day before, the new person has no obligation yet.
        b = assignments.assign(assignments.PERSON, point, second, BASE + timedelta(days=10), self.administrator, today=BASE)
        a.refresh_from_db()
        self.assertEqual(a.valid_to, BASE + timedelta(days=9))
        second.refresh_from_db()
        self.assertFalse(second.retention_obligation)
        self.assertEqual(assignments.in_force(assignments.PERSON, point, BASE).person, first)
        self.assertEqual(assignments.in_force(assignments.PERSON, point, BASE + timedelta(days=9)).person, first)
        self.assertEqual(assignments.in_force(assignments.PERSON, point, BASE + timedelta(days=10)).person, second)
        # Not after the start of the last one: the same start, an earlier one.
        for valid_from in (b.valid_from, b.valid_from - timedelta(days=1), BASE - timedelta(days=100)):
            with self.assertRaises(assignments.AssignmentError) as raised:
                assignments.assign(assignments.PERSON, point, third, valid_from, today=BASE)
            self.assertEqual(raised.exception.code, "starts_not_after_previous")
        # Only an assignment that never took effect and was never ended is deleted; the change is undone.
        self.assertTrue(assignments.is_deletable(b, BASE))
        assignments.delete(assignments.PERSON, b, self.administrator, today=BASE)
        a.refresh_from_db()
        self.assertIsNone(a.valid_to)
        with self.assertRaises(assignments.AssignmentError) as raised:
            assignments.delete(assignments.PERSON, a, today=BASE)  # in force
        self.assertEqual(raised.exception.code, "not_deletable")
        # Ending: not before the start, only once, and an ended one is never deleted.
        with self.assertRaises(assignments.AssignmentError) as raised:
            assignments.end(assignments.PERSON, a, a.valid_from - timedelta(days=1))
        self.assertEqual(raised.exception.code, "end_before_start")
        assignments.end(assignments.PERSON, a, BASE - timedelta(days=5), self.administrator)
        with self.assertRaises(assignments.AssignmentError) as raised:
            assignments.end(assignments.PERSON, a, BASE)
        self.assertEqual(raised.exception.code, "already_ended")
        # A new assignment after one that ended earlier leaves the ended one alone: no gap is closed.
        c = assignments.assign(assignments.PERSON, point, third, BASE + timedelta(days=1), today=BASE)
        a.refresh_from_db()
        self.assertEqual(a.valid_to, BASE - timedelta(days=5))
        self.assertIsNone(c.previous_assignment)
        # The history names the events and never an address or a name.
        events = [entry.event for entry in history.entries_for(point)]
        for event in (HistoryEvent.PERSON_ASSIGNED, HistoryEvent.PERSON_ASSIGNMENT_DELETED, HistoryEvent.PERSON_ASSIGNMENT_ENDED):
            self.assertIn(event, events)
        for entry in history.entries_for(point):
            self.assertNotIn("Erster", json.dumps(entry.detail))
            self.assertNotIn("@", json.dumps(entry.detail))
        # A metering point with a history is archived, not deleted.
        self.assertFalse(metering_points.can_delete(point))
        with self.assertRaises(metering_points.NotDeletable):
            metering_points.delete(point)

    def test_the_leg_chain_follows_the_same_rules_and_a_point_without_history_is_deleted(self):
        point = make_metering_point(211, location=self.place)
        history.record(point, HistoryEvent.CREATED)
        other = make_leg("LEG Other")
        a = assignments.assign(assignments.LEG, point, self.leg, BASE - timedelta(days=3), today=BASE)
        b = assignments.assign(assignments.LEG, point, other, BASE, today=BASE)
        a.refresh_from_db()
        self.assertEqual(a.valid_to, BASE - timedelta(days=1))
        self.assertFalse(assignments.is_deletable(b, BASE))  # starts today: in force
        self.assertTrue(assignments.is_deletable(b, BASE - timedelta(days=1)))
        clean = make_metering_point(212, location=self.place)
        history.record(clean, HistoryEvent.CREATED)
        self.assertTrue(metering_points.can_delete(clean))
        metering_points.delete(clean)
        self.assertFalse(MeteringPoint.objects.filter(pk=clean.pk).exists())
        self.assertFalse(history.entries_for(("metering_point", clean.pk)).exists())

    # LEG figures -----------------------------------------------------------------------------------

    def members(self, leg, *specs, on=BASE):
        """Metering points with a LEG assignment of the LEG: ``(direction, location)`` each."""
        points = []
        for index, (direction, location) in enumerate(specs):
            point = make_metering_point(next(SEQUENCE), direction, location)
            assign_leg(point, leg, on - timedelta(days=2))
            points.append(point)
        return points

    def test_the_discount_tier_follows_the_circuits_of_the_members(self):
        second = make_location("Zweitweg", "2", "3000", "Bern", transformer_circuit=self.circuit)
        elsewhere = make_location("Drittweg", "3", "3000", "Bern", transformer_circuit=self.circuit_two)
        no_circuit = make_location("Viertweg", "4", "3000", "Bern")
        leg = make_leg("LEG Tier")
        summary = lambda: legs.summaries([leg], 10, BASE)[leg.pk]  # noqa: E731
        self.assertEqual(summary().tier, DiscountTier.NONE)
        self.assertIsNone(summary().ratio)
        points = self.members(leg, (Direction.CONSUMPTION, self.place), (Direction.FEED_IN, second))
        self.assertEqual(summary().tier, DiscountTier.OPTIMISED)
        self.assertEqual(summary().ratio, "1 : 1")
        extra = self.members(make_leg("LEG Tmp"), (Direction.CONSUMPTION, elsewhere))[0]
        assign_leg(extra, leg, BASE - timedelta(days=1))
        self.assertEqual(summary().tier, DiscountTier.BASIC)
        # Archived, ended and future members do not count.
        extra.is_archived = True
        extra.save()
        self.assertEqual(summary().tier, DiscountTier.OPTIMISED)
        extra.is_archived = False
        extra.save()
        LegAssignment.objects.filter(metering_point=extra, leg=leg).update(valid_to=BASE - timedelta(days=1))
        self.assertEqual(summary().tier, DiscountTier.OPTIMISED)
        LegAssignment.objects.filter(metering_point=extra, leg=leg).update(valid_from=BASE + timedelta(days=1), valid_to=None)
        self.assertEqual(summary().tier, DiscountTier.OPTIMISED)
        self.assertEqual(summary().metering_points, 2)
        # A member without a location or a location without a circuit cannot be shown to be in the same circuit.
        for place in (None, no_circuit):
            odd = make_metering_point(next(SEQUENCE), Direction.CONSUMPTION, place)
            assign_leg(odd, leg, BASE - timedelta(days=1))
            self.assertEqual(summary().tier, DiscountTier.BASIC)
            LegAssignment.objects.filter(metering_point=odd).delete()
        self.assertEqual(len(points), 2)

    def test_the_problems_of_the_ratio_and_of_an_archived_leg(self):
        leg = make_leg("LEG Ratio")
        self.assertEqual(legs.summaries([leg], 10, BASE)[leg.pk].problems, ())
        consumer = self.members(leg, (Direction.CONSUMPTION, self.place))[0]
        self.assertEqual(legs.summaries([leg], 10, BASE)[leg.pk].problems, (LegProblem.NO_PRODUCER,))
        feed_in = make_metering_point(700, Direction.FEED_IN, self.place)
        assign_leg(feed_in, leg, BASE - timedelta(days=1))
        self.assertEqual(legs.summaries([leg], 10, BASE)[leg.pk].problems, ())
        LegAssignment.objects.filter(metering_point=consumer).delete()
        self.assertEqual(legs.summaries([leg], 10, BASE)[leg.pk].problems, (LegProblem.NO_CONSUMER,))
        leg.is_archived = True
        self.assertEqual(legs.summaries([leg], 10, BASE)[leg.pk].problems, ())

    def test_the_quota_problems_at_the_edges_and_with_the_threshold_of_the_settings(self):
        d = Decimal
        cases = (
            (d("4.99"), 10, [LegProblem.QUOTA_BELOW_LEGAL]), (d("0"), 10, [LegProblem.QUOTA_BELOW_LEGAL]),
            (d("5"), 10, [LegProblem.QUOTA_BELOW_WARNING]), (d("9.99"), 10, [LegProblem.QUOTA_BELOW_WARNING]),
            (d("10"), 10, []), (d("100"), 10, []), (d("15"), 20, [LegProblem.QUOTA_BELOW_WARNING]),
            (d("19.99"), 20, [LegProblem.QUOTA_BELOW_WARNING]), (d("20"), 20, []),
            (d("4"), 3, [LegProblem.QUOTA_BELOW_LEGAL]), (d("7"), 3, []), (None, 10, []),
        )
        for quota, threshold, expected in cases:
            with self.subTest(quota=quota, threshold=threshold):
                self.assertEqual(legs.quota_problems(quota, BASE, [], threshold), expected)
        # Stale is day exact: a member created on the day of the check is fine, one created a day later is stale.
        for created, stale in ((BASE - timedelta(days=1), False), (BASE, False), (BASE + timedelta(days=1), True)):
            with self.subTest(created=created):
                found = legs.quota_problems(d("40"), BASE, [created], 10)
                self.assertEqual(LegProblem.QUOTA_STALE in found, stale)
        self.assertEqual(legs.quota_problems(d("40"), BASE, [], 10), [])
        # The threshold of the settings is read when none is given.
        leg = make_leg("LEG Quota", production_quota_percent=d("15"), quota_checked_on=BASE)
        self.members(leg, (Direction.CONSUMPTION, self.place), (Direction.FEED_IN, self.place))
        MeteringPoint.objects.update(created_at=datetime(2026, 1, 1, 12, tzinfo=ZURICH))
        self.assertEqual(legs.summaries([leg], on=BASE)[leg.pk].problems, ())  # the default threshold is 10
        row = ManagementSettings.load()
        row.warning_threshold_percent = 20
        row.save()
        self.assertEqual(legs.summaries([leg], on=BASE)[leg.pk].problems, (LegProblem.QUOTA_BELOW_WARNING,))

    def test_a_member_created_around_midnight_is_stale_by_the_local_day(self):
        leg = make_leg("LEG Midnight", production_quota_percent=Decimal("40"), quota_checked_on=BASE)
        point = self.members(leg, (Direction.CONSUMPTION, self.place), (Direction.FEED_IN, self.place))[0]
        for moment, stale in (
            (datetime(2026, 3, 10, 23, 59, 59, tzinfo=ZURICH), False), (datetime(2026, 3, 11, 0, 0, 1, tzinfo=ZURICH), True),
            (datetime(2026, 3, 10, 0, 0, 0, tzinfo=ZURICH), False),
        ):
            MeteringPoint.objects.update(created_at=datetime(2026, 1, 1, tzinfo=ZURICH))
            MeteringPoint.objects.filter(pk=point.pk).update(created_at=moment)
            found = legs.summaries([leg], 10, BASE + timedelta(days=5))[leg.pk].problems
            self.assertEqual(LegProblem.QUOTA_STALE in found, stale, moment)

    # Counts and problem filters --------------------------------------------------------------------

    def test_the_overview_counts_every_list_and_links_to_it(self):
        full = problems.problem_counts(TODAY)
        self.assertEqual(set(full), {"locations", "metering_points", "legs"})
        self.assertEqual(full["locations"], 1)  # the one with the open address correction
        only = problems.problem_counts(TODAY, only=["legs"])
        self.assertEqual(set(only), {"legs"})
        self.assertEqual(problems.problem_counts(TODAY, only=[]), {})
        # The metering points without a person, a LEG or a location; archived ones have no problem.
        expected = MeteringPoint.objects.filter(is_archived=False).exclude(
            pk=self.point_active.pk
        ).count()  # the active one has all three; the others have none of at least one
        self.assertEqual(full["metering_points"], expected)
        make_leg("LEG Low", production_quota_percent=Decimal("1"), quota_checked_on=TODAY)
        page = self.login(self.administrator).get(url("overview"))
        self.assertEqual(page.status_code, 200)
        for name in ("legs", "locations", "metering-points"):
            self.assertContains(page, f"/management/{name}/?problems=1")

    def test_the_problem_filter_shows_the_rows_that_the_button_counts(self):
        client = self.login(self.administrator)
        page = client.get(url("metering_points"), {"problems": "1"})
        shown = {p for p in MeteringPoint.objects.all() if p.number[13:] in page.content.decode()}
        self.assertNotIn(self.point_active, shown)  # location, LEG and person: no problem
        self.assertNotIn(self.point_archived, shown)  # archived points have no problem and appear only as archived
        self.assertIn(self.point_registered, shown)
        count = problems.metering_points_with_problem(TODAY).count()
        self.assertContains(page, f"Probleme filtern ({count})")
        page = client.get(url("locations"), {"problems": "1"})
        self.assertContains(page, "Sugweg")
        self.assertNotContains(page, "Cleanweg")
        self.assertNotContains(page, "Altweg")

    # LEGs ------------------------------------------------------------------------------------------

    def test_the_rules_of_a_leg(self):
        client = self.login(self.administrator)
        count = Leg.objects.count()
        for data in (
            {"name": "leg main"}, {"name": "  LEG   Main "}, {"name": "LÉG Main"}, {"name": ""}, {"name": "x" * 121},
            {"name": "A", "quota": "37", "checked_on": ""}, {"name": "A", "quota": "37", "checked_on": iso(1)},
            {"name": "A", "quota": "100.01", "checked_on": iso(0)}, {"name": "A", "quota": "-0.01", "checked_on": iso(0)},
            {"name": "A", "quota": "1.234", "checked_on": iso(0)}, {"name": "A", "quota": "abc", "checked_on": iso(0)},
            {"name": "A", "quota": "NaN", "checked_on": iso(0)},
        ):
            with self.subTest(data=data):
                response = client.post(url("legs_new"), data)
                self.assertEqual(response.status_code, 200)
        self.assertEqual(Leg.objects.count(), count)
        response = client.post(url("legs_new"), {"name": "LEG Comma", "quota": "37,6", "checked_on": iso(0)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Leg.objects.get(name="LEG Comma").production_quota_percent, Decimal("37.60"))
        # Without a quota the date is dropped.
        client.post(url("legs_quota", self.leg), {"quota": "20", "checked_on": iso(0)})
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.production_quota_percent, Decimal("20.00"))
        client.post(url("legs_quota", self.leg), {"quota": "", "checked_on": iso(0)})
        self.leg.refresh_from_db()
        self.assertEqual((self.leg.production_quota_percent, self.leg.quota_checked_on), (None, None))
        # A LEG that ever had a metering point is archived, never deleted; one without a history is deleted.
        before = Leg.objects.count()
        client.post(url("legs_delete", self.leg))
        self.assertEqual(Leg.objects.count(), before)
        client.post(url("legs_delete", self.leg_clean))
        self.assertEqual(Leg.objects.count(), before - 1)


@tag("mariadb")
class HostileListParameterTests(MatrixBase):
    """The address of a list is the input of a stranger: nothing in it ends in an error or in a markup that runs."""

    LISTS = ("persons", "locations", "metering_points", "legs")
    HOSTILE = {
        "page": ["abc", "٣", "99999999999", "9" * 400, "-1", "0", "1.5", "1e5", " 2", "2 ", "２", "\x00", "1 OR 1=1"],
        "per": ["999999999", "all", "ALL", "-5", "٣٠", "0", "30.0", "x" * 300, "", "\x00"],
        "sort": ["nonexistent", "-street", "street; DROP TABLE community_location", "number\x00", "ß", "١", "x" * 300, "", "__class__", "pk"],
        "dir": ["ASC", "desc'--", "descending", "", "\x00", "DESC", "desc,asc"],
        "problems": ["1", "0", "true", "٣", "1&problems=2", "x" * 300],
        "step": ["bogus", "notify' OR '1'='1", "٣", "x" * 300, "archived", "all", "notify"],
        "leg": ["999999", "-1", "abc", "1 OR 1=1", "٣", "9" * 40, ""],
        "circuit": ["TRA9999", "' OR 1=1 --", "x" * 300, "\x00", "tra1000", "TRA1000"],
        "occupancy": ["bogus", "ARCHIVED", "archived", "occupied", "free", "empty", "٣"],
        "status": ["bogus", "blocked", "active", "inactive", "٣", "x" * 300],
        "points": ["bogus", "with", "without", "٣"],
        "display": ["bogus", "archived", "all", "٣"],
        "focus": ["q", "x", "<script>"],
        "q": [
            "<script>alert(1)</script>", '"><img src=x onerror=alert(1)>', "%", "_", "\\", "\x00", "'; DROP TABLE x; --", "😀" * 50,
            "x" * 10000, " ", "a b c d e f g h i j k l m n o p", "ß", "İ", "\u202e", "%%%%%%%%%%%%", "\\%", "' OR '1'='1",
        ],
    }

    COMMON = ("page", "per", "sort", "dir", "problems", "focus", "q")
    SPECIFIC = {  # the filters that a list knows (an unknown parameter is not read at all)
        "persons": ("status", "points"), "locations": ("circuit", "occupancy"), "metering_points": ("step", "leg"),
        "legs": ("display",),
    }

    def test_no_list_ends_in_an_error_for_a_hostile_value_and_nothing_is_echoed_as_markup(self):
        """Every hostile value of every parameter is sent to the first list; the other lists get every value of their
        own filters and the first values of the common parameters (the common code is the same for all lists). The
        parameters are read independently of each other, so one request carries one value of each parameter of the
        list (round by round, the shorter series repeat): an error or an echoed markup in any of them fails the
        request of that round."""
        client = self.login(self.administrator)
        for position, name in enumerate(self.LISTS):
            parameters = (*self.COMMON, *self.SPECIFIC[name])
            # The common parameters are read by the same code for every list: all their values go to the first
            # list, the other lists get as many rounds as their own filters have values.
            rounds = max(len(self.HOSTILE[parameter]) for parameter in (parameters if position == 0 else self.SPECIFIC[name]))
            for index in range(rounds):
                query = {parameter: self.HOSTILE[parameter][index % len(self.HOSTILE[parameter])] for parameter in parameters}
                with self.subTest(list=name, round=index):
                    response = client.get(url(name), query)
                    self.assertEqual(response.status_code, 200, query)
                    body = response.content.decode()
                    self.assertNotIn("<script>alert(1)</script>", body)
                    self.assertNotIn("<img src=x onerror", body)
        self.assertEqual(preferences(), [])

    def test_all_hostile_values_at_once_and_the_print_dialog_with_them(self):
        client = self.login(self.administrator)
        for name in self.LISTS:
            params = {parameter: values[0] for parameter, values in self.HOSTILE.items()}
            with self.subTest(list=name):
                self.assertEqual(client.get(url(name), params).status_code, 200)
                self.assertEqual(client.get(url(f"{name}_print"), params).status_code, 200)
                response = client.post(f"{url(f'{name}_print')}?{urlencode(params)}", {"scope": "list"})
                self.assertEqual(response.status_code, 200)

    def test_a_page_beyond_the_end_shows_the_last_page_and_a_huge_page_the_first(self):
        Leg.objects.bulk_create([Leg(name=f"LEG Bulk {index:04d}") for index in range(70)])
        client = self.login(self.administrator)
        last = client.get(url("legs"), {"page": "9999"})
        self.assertContains(last, "LEG Bulk 0069")
        first = client.get(url("legs"), {"page": "99999999999"})
        self.assertEqual(first.status_code, 200)

    def test_the_search_never_finds_a_remark_a_note_or_the_iban(self):
        place = make_location("Remarkweg", "1", "3000", "Bern", remark="SECRETLOCATIONREMARK")
        point = make_metering_point(800, Direction.CONSUMPTION, place, remark="SECRETPOINTREMARK")
        leg = make_leg("LEG Visible", remark="SECRETLEGREMARK")
        person = create_active_user("note.person@example.org", first_name="Nora", last_name="Notiz")
        PersonRecord.objects.create(
            person=person, staff_note="SECRETNOTE", iban="CH9300762011623852957", operator_customer_number="OP-SECRET-1"
        )
        assign_leg(point, leg, TODAY - timedelta(days=1))
        client = self.login(self.administrator)
        for name, secret, visible in (
            ("locations", "SECRETLOCATIONREMARK", "Remarkweg"), ("locations", "SECRETPOINTREMARK", "Remarkweg"),
            ("metering_points", "SECRETPOINTREMARK", number(800)[13:]), ("metering_points", "SECRETLOCATIONREMARK", number(800)[13:]),
            ("legs", "SECRETLEGREMARK", "LEG Visible"), ("persons", "SECRETNOTE", "Notiz"), ("persons", "CH9300762011623852957", "Notiz"),
            ("persons", "SECRETLOCATIONREMARK", "Notiz"),
        ):
            with self.subTest(list=name, secret=secret):
                body = client.get(url(name), {"q": secret, "display": "all", "occupancy": "all"}).content.decode()
                self.assertNotIn(visible, body)
        # Positive controls: the visible text is found.
        self.assertContains(client.get(url("locations"), {"q": "Remarkweg"}), "Remarkweg")
        self.assertContains(client.get(url("legs"), {"q": "visible"}), "LEG Visible")
        self.assertContains(client.get(url("persons"), {"q": "Notiz"}), "Notiz")
        self.assertContains(client.get(url("metering_points"), {"q": number(800)[20:]}), number(800)[13:])
        # The printout of the search does not find it either (a PDF is made from the same list).
        pdf_response = client.post(url("locations_print") + "?q=SECRETLOCATIONREMARK", {"scope": "all"})
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")

    def test_the_search_finds_what_the_pages_show_to_every_staff_account(self):
        """D-086: persons' names, LEG names and addresses are searchable for every staff account."""
        client = self.login(self.administrator)
        for name, text, marker in (
            ("metering_points", "Muster", number(4)[13:]),  # the name of the person
            ("metering_points", "LEG Main", number(4)[13:]),  # the name of the LEG
            ("metering_points", "Examplestrasse", number(4)[13:]),  # the address
            ("locations", "Muster", "Examplestrasse"),
            ("locations", "LEG Main", "Examplestrasse"),
            ("locations", number(4)[20:], "Examplestrasse"),  # a number of a metering point
        ):
            with self.subTest(list=name, text=text):
                self.assertContains(client.get(url(name), {"q": text}), marker)


@tag("mariadb")
class PrintingHostileInputTests(MatrixBase):
    def test_hostile_names_in_every_list_make_a_pdf_without_links_and_without_fetching(self):
        hostile = '<b>x</b> <a href="https://evil.example/x">link</a> <img src="file:///etc/passwd"/> &#x0; <seq/> <![CDATA[x]]> <para'
        make_leg(hostile)
        make_location(hostile[:100], "1<br/>", "3000", "Bern<font", remark=hostile)
        create_active_user("hostile.person@example.org", first_name=hostile[:25], last_name="</b>&amp;", street=hostile[:40])
        client = self.login(self.administrator)
        for name in ("persons", "locations"):  # the other two lists are rendered below, at the seam
            with self.subTest(list=name):
                response = client.post(url(f"{name}_print"), {"scope": "all"})  # the list and every detail page
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertTrue(response.content.startswith(b"%PDF"))
        # At the seam: no link annotation and no external address in the raw document.
        from django.http import QueryDict

        for key in ("persons", "locations", "legs", "metering_points"):
            area = areas.AREAS[key]
            prepared = area.prepare(QueryDict(""))
            items = prepared.load()
            document = area.document(prepared, items, items, TODAY)
            raw = pdf.render(document, compress=False)
            # The address of the link and the path of the image are printed as text (that is what the name says);
            # a link, an annotation or a fetched image would be an object of the document.
            for forbidden in (b"/URI", b"/Annot", b"/Link", b"/Subtype /Image"):
                self.assertNotIn(forbidden, raw, (key, forbidden))

    def test_a_list_beyond_the_bounds_is_refused_without_a_pdf(self):
        Leg.objects.bulk_create([Leg(name=f"LEG Mass {index:05d}") for index in range(areas.MAX_LIST_ENTRIES + 1)])
        client = self.login(self.administrator)
        dialog = client.get(url("legs_print"))
        self.assertEqual(dialog.status_code, 200)
        self.assertNotContains(dialog, 'type="radio"')
        for scope in ("list", "all", "selected"):
            response = client.post(url("legs_print"), {"scope": scope, "ids": [str(pk) for pk in Leg.objects.values_list("pk", flat=True)[:5]]})
            self.assertEqual(response["Content-Type"], "text/html; charset=utf-8", scope)
        # Narrowed by the search, the same list prints.
        narrow = client.post(url("legs_print") + "?q=LEG Main", {"scope": "all"})
        self.assertEqual(narrow["Content-Type"], "application/pdf")
        # More detail pages than the bound are refused, a forged flood of ids is a 400.
        few = client.post(url("legs_print") + "?q=LEG M", {"scope": "selected", "ids": ["1"] * (areas.MAX_SELECTABLE + 1)})
        self.assertEqual(few.status_code, 400)

    def test_the_three_options_and_the_ids_of_a_selection(self):
        client = self.login(self.administrator)
        legs_url = url("legs_print")
        only = client.post(legs_url, {"scope": "list"})
        everything = client.post(legs_url, {"scope": "all"})
        chosen = client.post(legs_url, {"scope": "selected", "ids": [str(self.leg.pk)]})
        for response in (only, everything, chosen):
            self.assertEqual(response["Content-Type"], "application/pdf")
        count = lambda r: len(re.findall(rb"/Type /Page\b", r.content))  # noqa: E731
        self.assertLess(count(only), count(everything))
        self.assertLessEqual(count(chosen), count(everything))
        for ids in (["٣"], ["1" * 40], ["-1"], ["1.0"], ["x"], [""], [" 1"], [str(self.leg_archived.pk)], ["999999"]):
            with self.subTest(ids=ids):
                response = client.post(legs_url, {"scope": "selected", "ids": ids})
                self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(client.post(legs_url, {"scope": "list", "ids": ["x"] * 400}).status_code, 400)
        self.assertEqual(client.post(legs_url, {"scope": "LIST"}).status_code, 400)
        self.assertEqual(client.post(legs_url, {"scope": ["list", "all"]}).status_code, 200)  # the last value counts
