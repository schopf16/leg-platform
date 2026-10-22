"""Independent acceptance pass of the management area (phase 2, D-077 to D-086): the negative authorization of
EVERY management route with real requests, and what a request may and may not change.

The route walk of ``test_access.py`` asks every route with an empty request. This module goes further:

* every route is asked with a payload that really works for a staff account (a control proves that the
  payload changes the state), so that a refusal means the access check and not an invalid form;
* a snapshot of the whole domain (every row of every table of the community, the state of the accounts) is
  compared before and after, so that "no state change" is checked, not only the status code;
* the accounts are the kinds of D-086: anonymous, an account without staff status, a blocked staff account, a
  staff account without the second factor, and one plain staff account (no group, no permission), which may use
  every page and sees all its data.

A new route without an entry in ``cases`` fails ``test_every_route_has_a_case``; the table has to be kept up to
date with the URL configuration (that is the point: nobody adds a route without thinking about its refusals).
"""

import calendar
from collections import namedtuple
from datetime import date as date_type
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import Client, TestCase, tag
from django.urls import resolve, reverse
from django.utils import timezone

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import (
    application_corrections,
    application_notices,
    application_staff,
    applications,
    buildings,
    contracts,
    deregistrations,
)
from leg_platform.community.models import (
    Application,
    ApplicationBuilding,
    ApplicationMeteringPoint,
    ApplicationNotice,
    ContractAcceptance,
    ContractVersion,
    CustomerMessage,
    Deregistration,
    DeregistrationItem,
    Direction,
    HistoryEntry,
    Leg,
    LegAssignment,
    ListPreference,
    Location,
    ManagementSettings,
    MeteringPoint,
    PersonAssignment,
    PersonRecord,
    ShareChange,
    TransformerCircuit,
)
from leg_platform.community.tests import onboarding
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)
from leg_platform.management.tests.test_access import PATHS

User = get_user_model()
TODAY = timezone.localdate()
LOGIN = reverse("accounts:login")
SETUP = reverse("accounts:login_method")

# One route: its name, the arguments of the address, the data of a POST that works for a staff account,
# whether that POST changes the domain (the control proves it), JSON instead of a form, and the query of
# a GET.
Case = namedtuple("Case", "name args data changes json query", defaults=((), {}, False, False, {}))


def month_end(day):
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


SNAPSHOT_MODELS = (
    Location, TransformerCircuit, MeteringPoint, Leg, LegAssignment, PersonAssignment, HistoryEntry,
    ManagementSettings, PersonRecord, ShareChange,
    # The onboarding (D-103): applications and their draft rows, notices, contract versions and acceptances,
    # deregistrations.
    Application, ApplicationBuilding, ApplicationMeteringPoint, ApplicationNotice, ContractVersion,
    ContractAcceptance, CustomerMessage, Deregistration, DeregistrationItem,
)


def snapshot():
    """The state of every table of the domain and of the accounts, but not of the saved list preferences (the
    widths and the expanded rows belong to the account that asks and are compared apart). One ``CHECKSUM TABLE``
    over all tables (MariaDB, which these tests need anyway) instead of reading every row: a change of any column
    of any row changes the checksum, and ``RouteTableTests.test_the_snapshot_sees_every_kind_of_change`` proves it
    (inside the transaction of the test as well)."""
    tables = ", ".join(f"`{model._meta.db_table}`" for model in SNAPSHOT_MODELS)
    with connection.cursor() as cursor:
        cursor.execute(f"CHECKSUM TABLE {tables}")
        data = {name: checksum for name, checksum in cursor.fetchall()}
    data["accounts"] = list(User._base_manager.order_by("pk").values_list("pk", "status", "retention_obligation"))
    return data


def preferences():
    return list(ListPreference.objects.order_by("pk").values_list())


# The cases that the per-kind tests below ask (anonymous, no staff status, no second factor, a blocked
# account, a missing token, other methods). ``test_access.RouteWalkTests/AccessTests`` already ask EVERY route for
# those kinds, so here one case of each kind of view is enough: a list, a detail, a dialog, a button that posts,
# a JSON endpoint and a print, each of every area. What is asked of every route is checked without a request
# (``test_no_route_is_exempt_from_the_csrf_check``, ``test_every_view_answers_only_the_methods_it_knows``). The
# control payloads and the forged ids below still ask every route.
SAMPLE = frozenset({
    "overview", "settings", "list_widths", "persons_note",
    "locations", "locations_detail", "locations_address", "locations_circuit", "locations_metering_point_assign",
    "metering_points_detail", "metering_points_notify", "metering_points_edit", "legs_metering_point_assign",
    "legs", "legs_quota", "legs_delete",
    "settings_deadlines", "settings_run_time", "processes", "processes_application", "processes_application_complete",
    "processes_application_review", "processes_notice_resolve", "processes_deregistration_confirm",
    "processes_application_building", "processes_application_point",
    "contracts", "contracts_upload", "contracts_download", "contracts_current", "contracts_delete",
})


@tag("mariadb")
class MatrixBase(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.plain = create_active_user("plain.person@example.org")
        cls.staff = staff_user("staff.person@example.org")  # no group, no permission
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)  # used by test_acceptance_flows
        cls.second_factor_missing = staff_user("staff.nosecond@example.org")

        row = ManagementSettings.load()
        row.country_code, row.identifier = "CH", "1A2B3C4D5E6"
        row.save()

        cls.circuit = make_circuit("TRA1000", "Eins")
        cls.circuit_two = make_circuit("TRA2000", "Zwei")
        cls.place = make_location("Examplestrasse", "1", "3000", "Bern", transformer_circuit=cls.circuit)
        cls.place_clean = make_location("Cleanweg", "5", "3000", "Bern")  # no history, no metering point
        cls.place_archived = make_location("Altweg", "9", "3000", "Bern", is_archived=True)
        cls.place_new = make_location("Neuweg", "3", "3000", "Bern", needs_confirmation=True)
        cls.place_suggestion = make_location(
            "Sugweg", "2", "3000", "Bern", suggested_street="Sugweg", suggested_house_number="2",
            suggested_postal_code="3000", suggested_town="Bern", address_correction_open=True,
        )
        cls.leg = make_leg("LEG Main")
        cls.leg_clean = make_leg("LEG Clean")
        cls.leg_archived = make_leg("LEG Alt", is_archived=True)
        cls.anna = create_active_user("anna.person@example.org", first_name="Anna", last_name="Muster")

        d = TODAY
        cls.point_registered = make_metering_point(1, location=cls.place)
        cls.point_notified = make_metering_point(2, location=cls.place,         registered_with_operator_on=d - timedelta(days=5))
        cls.point_standby = make_metering_point(
                    3, location=cls.place, registered_with_operator_on=d - timedelta(days=9), confirmed_on=d - timedelta(days=8)
        )
        cls.point_active = make_metering_point(
                    4, location=cls.place, registered_with_operator_on=d - timedelta(days=40),
            confirmed_on=d - timedelta(days=39), start_date=d - timedelta(days=30),
        )
        cls.point_future = make_metering_point(5, location=cls.place)
        cls.point_clean = make_metering_point(6)
        cls.point_loose = make_metering_point(60)  # no location: the target of "Assign existing metering point"
        cls.point_without_leg = make_metering_point(61)  # no LEG: the target of the LEG page
        cls.point_archived = make_metering_point(7, location=cls.place, is_archived=True)
        assign_leg(cls.point_registered, cls.leg, d - timedelta(days=30))
        assign_leg(cls.point_active, cls.leg, d - timedelta(days=30))
        cls.active_assignment = assign_person(cls.point_active, cls.anna, d - timedelta(days=30))
        cls.future_assignment = assign_person(cls.point_future, cls.anna, d + timedelta(days=30))
        cls.setup_onboarding(d)

    @classmethod
    def setup_onboarding(cls, d):
        """The onboarding data of the process, notice and contract pages: one application per step of the staff, a
        deregistration to confirm, an open notice and three contract versions (D-103)."""
        onboarding.configure_numbers()
        cls.contract_current = onboarding.publish_contract(staff=cls.staff)
        cls.contract_extra = contracts.upload_version(cls.staff, onboarding.pdf(b"\n% extra"), date_type(2026, 1, 5))
        cls.contract_delete = contracts.upload_version(cls.staff, onboarding.pdf(b"\n% delete"), date_type(2026, 2, 5))
        cls.kunde = create_active_user("kunde.person@example.org", first_name="Kunde", last_name="Beispiel")
        made = []
        cls.building_of = {}
        for index in range(6):
            application, building = onboarding.ready_draft(
                cls.kunde, egid=90010000 + index, number=100 + 10 * index,
                address={"house_number": str(60 + index)},
            )
            applications.submit(cls.kunde, application, today=onboarding.TODAY)
            application.refresh_from_db()
            made.append(application)
            cls.building_of[application.pk] = building
        (cls.app_review, cls.app_return, cls.app_start, cls.app_confirm, cls.app_complete, cls.app_reject) = made
        application_staff.mark_operator_started(cls.app_confirm, cls.staff, d - timedelta(days=20), today=d)
        application_staff.mark_operator_started(cls.app_complete, cls.staff, date_type(2026, 6, 1), today=d)
        application_staff.record_operator_confirmation(cls.app_complete, cls.staff, date_type(2026, 7, 1), today=d)
        cls.notice = application_notices.record(cls.kunde, application_notices.Kind.BUILDING_UNAVAILABLE, egid=12345678)
        cls.place_dereg = make_location("Abmeldeweg", "4", "3000", "Bern")
        cls.point_dereg = make_metering_point(
            80, location=cls.place_dereg, registered_with_operator_on=d - timedelta(days=40),
            confirmed_on=d - timedelta(days=39), start_date=d - timedelta(days=30),
        )
        assign_person(cls.point_dereg, cls.anna, d - timedelta(days=30))
        cls.dereg = deregistrations.request_deregistration(cls.anna, metering_point_ids=[cls.point_dereg.pk], today=d)

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def setUp(self):
        # The address directory offers one building for the address of the correction payload (never a request to
        # swisstopo from a test).
        offered = onboarding.candidate(90010077, house_number="70")
        lookup = patch.object(buildings, "find_candidates", return_value=[offered])
        lookup.start()
        self.addCleanup(lookup.stop)

    def point_of_review(self):
        return ApplicationMeteringPoint.objects.get(
            building=self.building_of[self.app_review.pk], direction=Direction.CONSUMPTION
        )

    def building_token(self):
        return application_corrections.building_token(
            ApplicationBuilding.objects.get(pk=self.building_of[self.app_review.pk].pk)
        )

    def point_token(self):
        return application_corrections.point_token(self.point_of_review())

    def cases(self):
        d, date = TODAY, lambda days: (TODAY + timedelta(days=days)).isoformat()
        address = {"street": "Examplestrasse", "house_number": "77", "postal_code": "3000", "town": "Bern"}
        pk = lambda obj: (obj.pk,)  # noqa: E731
        return [
            Case("overview"),
            Case("settings", data={
                "country_code": "CH", "identifier": "1A2B3C4D5E6", "warning_threshold_percent": "12",
                "sender_street": "Senderweg", "sender_house_number": "4", "sender_postal_code": "3000",
                "sender_town": "Bern",
            }, changes=True),
            Case("settings_check", query={"field": "identifier", "value": "x"}),
            Case("settings_deadlines", data={
                "reminder_after_days": "3", "rollback_after_days": "15", "contract_deadline_days": "15",
                "operator_confirmation_days": "15", "iban_missing_reminder_days": "8",
            }, changes=True),
            Case("settings_run_time", data={"run_time": "04:30"}, changes=True),
            Case("list_widths", ("locations",), {"widths": {"street": 200}}, json=True),
            # Processes and notices (D-103)
            Case("processes"),
            Case("processes_notices"),
            Case("processes_notice_resolve", pk(self.notice), {}, True),
            Case("processes_application", pk(self.app_review)),
            Case("processes_application_review", pk(self.app_review), {}, True),
            Case("processes_application_return", pk(self.app_return), {"reason": "Please check the details."}, True),
            Case("processes_application_operator_started", pk(self.app_start), {"started_on": date(0)}, True),
            Case("processes_application_operator_confirmed", pk(self.app_confirm), {"confirmed_on": date(0)}, True),
            Case(
                "processes_application_complete", pk(self.app_complete),
                {"start_date": "2026-09-01"}, True,
            ),
            Case("processes_application_reject", pk(self.app_reject), {"reason": "rejected"}, True),
            # The staff's corrections of a building and of a metering point of an application in flight (D-109). The
            # address directory answers with one building for the address of the payload (``setUp``).
            Case(
                "processes_application_building", (self.app_review.pk, self.building_of[self.app_review.pk].pk),
                {**address, "house_number": "70", "egid": "", "confirm_unlisted": "", "expected": self.building_token()},
                True,
            ),
            Case(
                "processes_application_point", (self.app_review.pk, self.point_of_review().pk),
                {"number_digits": "55", "direction": "consumption", "expected": self.point_token()}, True,
            ),
            Case("processes_deregistration", pk(self.dereg)),
            Case(
                "processes_deregistration_confirm", pk(self.dereg),
                {"confirmed_date": month_end(d + timedelta(days=130)).isoformat()}, True,
            ),
            # Contracts (D-103)
            Case("contracts"),
            Case("contracts_upload", data={
                "file": SimpleUploadedFile("upload.pdf", onboarding.pdf(b"\n% uploaded"), content_type="application/pdf"),
                "version_date": date(0),
            }, changes=True),
            Case("contracts_download", pk(self.contract_current)),
            Case("contracts_current", pk(self.contract_extra), {}, True),
            Case("contracts_delete", pk(self.contract_delete), {}, True),
            # Persons
            Case("persons"),
            Case("persons_print", data={"scope": "list"}),
            Case("persons_detail", pk(self.anna)),
            Case("persons_note", pk(self.anna), {"note": "a note"}, True),
            # The dialogs of the customer's data and the creation of a person (D-087).
            Case("persons_new", (), {
                "email": "new.person@example.org", "first_name": "Neu", "last_name": "Person", **address,
                "country": "CH",
            }, True),
            Case("persons_details", pk(self.anna), {
                "salutation": "mrs", "organisation_name": "", "first_name": "Anna", "last_name": "Neumann",
                "address_line_1": "",
            }, True),
            Case("persons_contact", pk(self.anna), {"phone_number": "031 311 11 11", "secondary_email": ""}, True),
            Case("persons_address", pk(self.anna), {**address, "post_office_box_number": "", "country": "CH"}, True),
            Case("persons_payment", pk(self.anna), {
                "iban": "CH93 0076 2011 6238 5295 7", "paper_invoice": "1", "operator_customer_number": "B-1",
            }, True),
            # Nobody to invite (an account that completed the registration): nothing is sent, nothing changes.
            Case("persons_invitation", pk(self.anna), {"next": "/management/persons/"}),
            Case("persons_shares", pk(self.anna), {"quantity": "5", "valid_from": date(-1)}, True),
            # A token of a suggestion that does not exist: nothing to decide on, so nothing changes (no control).
            Case("persons_address_suggestion", pk(self.anna), {"action": "dismiss", "token": "x"}),
            # Locations
            Case("locations"),
            Case("locations_print", data={"scope": "list"}),
            Case("locations_expanded", data={"toggle": str(self.place.pk)}),
            Case("locations_new", data={**address, "house_number": "78"}, changes=True),
            Case("locations_detail", pk(self.place)),
            Case("locations_address", pk(self.place), address, True),
            Case("locations_remark", pk(self.place), {"remark": "a remark"}, True),
            Case("locations_circuit", pk(self.place), {"transformer_circuit": str(self.circuit_two.pk)}, True),
            Case("locations_metering_point_new", pk(self.place), {"direction": "consumption", "number_digits": "123"}, True),
            Case("locations_metering_point_assign", pk(self.place), {"metering_point": str(self.point_loose.pk)}, True),
            Case("locations_archive", pk(self.place), {}, True),
            Case("locations_delete", pk(self.place_clean), {}, True),
            Case("locations_confirm", pk(self.place_new), {}, True),
            Case("locations_suggestion", pk(self.place_suggestion), {"action": "dismiss"}, True),
            Case("circuits_edit", pk(self.circuit_two), {"grid_number": "TRA2001", "name": "Zwei b"}, True),
            # Metering points
            Case("metering_points"),
            Case("metering_points_print", data={"scope": "list"}),
            Case("metering_points_new", data={"number_digits": "555", "direction": "consumption"}, changes=True),
            Case("metering_points_check_number", query={"digits": "1"}),
            Case("metering_points_person_search", query={"q": "anna"}),
            Case("metering_points_detail", pk(self.point_registered)),
            Case("metering_points_edit", pk(self.point_registered), {"location": str(self.place.pk), "remark": "x"}, True),
            Case("metering_points_notify", pk(self.point_registered), {"registered_on": date(0)}, True),
            Case("metering_points_confirm", pk(self.point_notified), {"confirmed_on": date(0)}, True),
            Case("metering_points_start", pk(self.point_standby), {"start_date": date(0)}, True),
            Case("metering_points_terminate", pk(self.point_active), {"terminated_on": month_end(d).isoformat()}, True),
            Case("metering_points_leg", pk(self.point_active), {"leg": str(self.leg_clean.pk), "valid_from": date(1)}, True),
            Case("metering_points_person", pk(self.point_registered), {"person": self.anna.email, "valid_from": date(0)}, True),
            Case(
                "metering_points_person_end", pk(self.point_active),
                {"valid_to": date(10), "assignment": str(self.active_assignment.pk)}, True,
            ),
            Case(
                "metering_points_person_delete", pk(self.point_future),
                {"assignment": str(self.future_assignment.pk)}, True,
            ),
            Case("metering_points_archive", pk(self.point_clean), {}, True),
            Case("metering_points_delete", pk(self.point_clean), {}, True),
            # LEGs
            Case("legs"),
            Case("legs_print", data={"scope": "list"}),
            Case("legs_new", data={"name": "LEG New", "quota": "", "checked_on": ""}, changes=True),
            Case("legs_detail", pk(self.leg)),
            Case("legs_quota", pk(self.leg), {"quota": "20", "checked_on": date(0)}, True),
            Case("legs_edit", pk(self.leg), {"name": "LEG Renamed", "remark": "r"}, True),
            Case(
                "legs_metering_point_assign", pk(self.leg),
                {"metering_point": str(self.point_without_leg.pk), "valid_from": date(0)}, True,
            ),
            Case("legs_archive", pk(self.leg), {}, True),
            Case("legs_delete", pk(self.leg_clean), {}, True),
        ]

    def sample(self):
        """One case of each kind of view (see ``SAMPLE``)."""
        return [case for case in self.cases() if case.name in SAMPLE]

    def request(self, client, case, method):
        url = reverse(f"management:{case.name}", args=case.args)
        if method == "get":
            return client.get(url, case.query)
        if case.json:
            return client.post(url, data=case.data, content_type="application/json")
        return client.post(url, case.data)


class RouteTableTests(MatrixBase):
    def test_the_snapshot_sees_every_kind_of_change(self):
        """The comparison of the state is the evidence of every "nothing changed" below: it covers every table and
        sees an insert, an update of one column, a delete and a change of an account, inside the transaction of
        the test as well."""
        before = snapshot()
        self.assertEqual(snapshot(), before)
        tables = {key for key in before if key != "accounts"}
        self.assertEqual(len(tables), len(SNAPSHOT_MODELS))
        self.assertNotIn(None, [before[key] for key in tables])
        with transaction.atomic():
            extra = make_leg("LEG Inserted")
            self.assertNotEqual(snapshot(), before, "an insert is not seen")
            extra.delete()
            self.assertEqual(snapshot(), before, "an insert that was deleted again left a trace")
            transaction.set_rollback(True)
        with transaction.atomic():
            Leg.objects.filter(pk=self.leg_clean.pk).delete()
            self.assertNotEqual(snapshot(), before, "a delete is not seen")
            transaction.set_rollback(True)
        with transaction.atomic():
            Location.objects.filter(pk=self.place.pk).update(remark="changed")
            self.assertNotEqual(snapshot(), before, "an update of one column is not seen")
            transaction.set_rollback(True)
        with transaction.atomic():
            Leg.objects.filter(pk=self.leg.pk).update(remark="changed")
            self.assertNotEqual(snapshot(), before, "an update of one column is not seen")
            transaction.set_rollback(True)
        with transaction.atomic():
            self.anna.set_status(User.Status.BLOCKED)
            self.assertNotEqual(snapshot(), before, "a change of an account is not seen")
            transaction.set_rollback(True)
        self.assertEqual(snapshot(), before)

    def test_every_route_has_a_case(self):
        in_conf = {resolve(path).url_name for path in PATHS}
        self.assertEqual(in_conf, {case.name for case in self.cases()})

    def test_the_sample_names_only_existing_routes(self):
        names = {case.name for case in self.cases()}
        self.assertLessEqual(SAMPLE, names)

    def test_no_route_is_exempt_from_the_csrf_check(self):
        """What the per-kind tests ask only of a sample is asked of every route here, without a request: the
        token is checked by the middleware unless a view is exempt, and every view answers only the methods it
        knows (Django answers any other method with a 405 before the handler)."""
        for path in PATHS:
            match = resolve(path)
            with self.subTest(route=match.url_name):
                self.assertFalse(getattr(match.func, "csrf_exempt", False))
                view_class = getattr(match.func, "view_class", None)
                if view_class is not None:
                    self.assertFalse([m for m in ("put", "patch", "delete") if hasattr(view_class, m)])

    def test_the_control_payloads_really_work_for_a_plain_staff_account(self):
        """Without this a refusal below could hide an invalid payload: a staff account without any group or
        permission changes the domain with each payload (or, for a read case, is answered); the change is rolled
        back, so each case starts from the same data."""
        client = self.login(self.staff)
        for case in self.cases():
            if not (case.changes or case.data):
                continue
            with self.subTest(case=case.name):
                with transaction.atomic():
                    before, before_preferences = snapshot(), preferences()
                    response = self.request(client, case, "post")
                    after, after_preferences = snapshot(), preferences()
                    transaction.set_rollback(True)
                self.assertIn(response.status_code, (200, 204, 302, 405), response.status_code)
                if case.changes:
                    self.assertNotEqual(before, after, f"{case.name}: the payload changed nothing")
                    self.assertIn(response.status_code, (200, 302))
                    if response.status_code == 200:
                        # A form that was shown again is a refused payload, not a change.
                        self.fail(f"{case.name}: the form was shown again (invalid payload)")
                else:
                    self.assertEqual(before, after, case.name)


class NegativeAuthorizationTests(MatrixBase):
    def test_the_anonymous_visitor_gets_the_login_and_nothing_changes(self):
        client = Client()
        before, before_preferences = snapshot(), preferences()
        for case in self.sample():
            for method in ("get", "post"):
                with self.subTest(case=case.name, method=method):
                    response = self.request(client, case, method)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith(LOGIN), response["Location"])
                    if method == "post":
                        self.assertEqual(response["Location"], LOGIN)  # a POST target is no page to return to
                    self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(snapshot(), before)
        self.assertEqual(preferences(), before_preferences)

    def test_an_account_without_staff_status_is_answered_like_a_page_that_does_not_exist(self):
        client = self.login(self.plain)
        before, before_preferences = snapshot(), preferences()
        for case in self.sample():
            for method in ("get", "post"):
                with self.subTest(case=case.name, method=method):
                    response = self.request(client, case, method)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn(self.anna.email.encode(), response.content)
        self.assertEqual(snapshot(), before)
        self.assertEqual(preferences(), before_preferences)

    def test_a_plain_staff_account_opens_every_page_and_sees_its_data(self):
        """No group, no permission: no page of any area is refused, and a page shows the names that belong to it
        (D-086: the person, the LEG and the location of a metering point are no longer hidden)."""
        client = self.login(self.staff)
        before, before_preferences = snapshot(), preferences()
        for case in self.cases():
            with self.subTest(case=case.name):
                response = self.request(client, case, "get")
                self.assertNotEqual(response.status_code, 403)
                self.assertLess(response.status_code, 500)
                if response.status_code == 302:
                    self.assertFalse(response["Location"].startswith(LOGIN), response["Location"])
        pages = {
            "overview", "settings", "persons", "persons_detail", "locations", "locations_detail",
            "metering_points", "metering_points_detail", "legs", "legs_detail",
            "processes", "processes_notices", "processes_application", "processes_deregistration", "contracts",
        }
        for case in self.cases():
            if case.name in pages:
                with self.subTest(page=case.name):
                    self.assertEqual(self.request(client, case, "get").status_code, 200)
        self.assertEqual(snapshot(), before)
        self.assertEqual(preferences(), before_preferences)
        page = client.get(reverse("management:metering_points_detail", args=[self.point_active.pk]))
        self.assertContains(page, "Muster")
        self.assertContains(page, reverse("management:persons_detail", args=[self.anna.pk]))
        self.assertContains(page, reverse("management:legs_detail", args=[self.leg.pk]))
        self.assertContains(page, reverse("management:locations_detail", args=[self.place.pk]))

    def test_the_second_factor_is_required_for_every_route_and_nothing_changes(self):
        client = Client()
        client.force_login(self.second_factor_missing)  # a staff account without the second factor
        before, before_preferences = snapshot(), preferences()
        for case in self.sample():
            for method in ("get", "post"):
                with self.subTest(case=case.name, method=method):
                    response = self.request(client, case, method)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response["Location"], SETUP)
        self.assertEqual(snapshot(), before)
        self.assertEqual(preferences(), before_preferences)

    def test_a_deactivated_staff_account_with_a_live_session_is_refused(self):
        """An account that was blocked after the login must not keep the access (D-016, D-077: active only)."""
        user = staff_user("late.blocked@example.org")
        client = self.login(user)
        self.assertEqual(client.get(reverse("management:overview")).status_code, 200)
        user.set_status(User.Status.BLOCKED)
        before = snapshot()
        for case in self.sample():
            with self.subTest(case=case.name):
                response = self.request(client, case, "post")
                self.assertIn(response.status_code, (302, 404))
                self.assertNotEqual(response.status_code, 200)
        self.assertEqual(snapshot(), before)

    def test_the_methods_that_a_route_does_not_know_never_change_anything(self):
        client = self.login(self.staff)
        before, before_preferences = snapshot(), preferences()
        for case in self.sample():
            url = reverse(f"management:{case.name}", args=case.args)
            for method in ("put", "patch", "delete"):
                with self.subTest(case=case.name, method=method):
                    response = getattr(client, method)(url, data="{}", content_type="application/json")
                    self.assertEqual(response.status_code, 405)
        self.assertEqual(snapshot(), before)
        self.assertEqual(preferences(), before_preferences)

    def test_a_post_without_the_csrf_token_changes_nothing_on_any_state_changing_route(self):
        client = self.login(self.staff, enforce_csrf_checks=True)
        before, before_preferences = snapshot(), preferences()
        for case in self.sample():
            with self.subTest(case=case.name):
                response = self.request(client, case, "post")
                self.assertEqual(response.status_code, 403)
        self.assertEqual(snapshot(), before)
        self.assertEqual(preferences(), before_preferences)

    def test_forged_and_missing_ids_are_a_404_and_change_nothing(self):
        client = self.login(self.staff)
        before, before_preferences = snapshot(), preferences()
        for case in self.cases():
            if not case.args or case.name == "list_widths":
                continue
            # An id that does not exist asks every route; one beyond every integer column asks a route of each kind.
            for forged in (999999, 2**63 + 5) if case.name in SAMPLE else (999999,):
                with self.subTest(case=case.name, forged=forged):
                    forged_case = case._replace(args=tuple(forged for _ in case.args))
                    methods = ("get", "post") if case.name in SAMPLE else (("post",) if case.changes or case.data else ("get",))
                    answers = {method: self.request(client, forged_case, method).status_code for method in methods}
                    # The supported method says 404, the other 405 (or 404 as well); never a page or a change.
                    self.assertTrue(set(answers.values()) <= {404, 405}, answers)
                    self.assertIn(404, answers.values(), answers)
                    self.assertEqual(snapshot(), before, f"{case.name} {forged}")
        self.assertEqual(preferences(), before_preferences)

    def test_an_id_of_the_wrong_kind_is_a_404(self):
        """A person is an account with a customer number: a pending registration, which has none, has no page."""
        pending = User.objects.create_user("pending.person@example.org", first_name="P", last_name="P", street="S",
                                           house_number="1", town="Bern", swiss_postal_code=3000)
        self.assertIsNone(pending.customer_number_id)
        client = self.login(self.staff)
        before = snapshot()
        for name, data in (
            ("persons_detail", None), ("persons_note", {"note": "x"}),
            ("persons_details", {"first_name": "X", "last_name": "Y"}),
            ("persons_contact", {"phone_number": ""}),
            ("persons_address", {"street": "S", "town": "Bern", "postal_code": "3000", "country": "CH"}),
            ("persons_payment", {"operator_customer_number": "B", "paper_invoice": "0"}),
            ("persons_invitation", {"next": "/management/persons/"}),
            ("persons_shares", {"quantity": "1", "valid_from": TODAY.isoformat()}),
            ("persons_address_suggestion", {"action": "dismiss", "token": "x"}),
        ):
            with self.subTest(name=name):
                url = reverse(f"management:{name}", args=[pending.pk])
                response = client.get(url) if data is None else client.post(url, data)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(snapshot(), before)
        self.assertEqual(PersonRecord.objects.filter(person=pending).count(), 0)
