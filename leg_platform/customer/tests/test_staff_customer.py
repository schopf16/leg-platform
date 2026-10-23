"""Staff accounts and system administrators take part as customers (D-113): they register buildings and metering points,
see the start page of a participant, keep a bank connection and can delete or deactivate their account under the same
rules as everybody. What stays: the management of a staff account is limited to system administrators
(``persons.may_change_customer_data``, D-087), and the last system administrator cannot be deleted or deactivated (D-016).
The rules of the access check are walked in ``test_access.py``; the menus in ``accounts/tests/test_workspaces.py``."""

from datetime import date, timedelta

from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import AuthenticatorDevice, MailJob, SecurityEvent, User
from leg_platform.accounts.tests.helpers import code_for
from leg_platform.community import applications, contracts, deregistrations, persons
from leg_platform.community.models import Direction, MeteringPoint
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.customer.tests.helpers import (
    BUILDINGS,
    HOME,
    configure_numbers,
    customer,
    draft_with_building,
    draft_with_points,
    login,
    publish_contract,
)
from leg_platform.customer.tests.test_start_page import complete_application

DELETE = reverse("accounts:account_delete")
PROFILE = reverse("accounts:profile")
LONG_AGO = date.today() - timedelta(days=400)


def active_point(last, person, location, direction=Direction.CONSUMPTION):
    point = make_metering_point(
        last, direction, location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO, start_date=LONG_AGO
    )
    assign_person(point, person, valid_from=LONG_AGO)
    return point


def delete_account(client, user, **extra):
    """The deletion of the account with the proof of a staff account (its number code, D-062)."""
    device = AuthenticatorDevice.objects.get(user=user, confirmed=True)
    return client.post(DELETE, {"confirm": "on", "otp_token": code_for(device), **extra})


@tag("mariadb")
class StaffCustomerFlowTests(TestCase):
    """The existing flow, run by a staff account and by a system administrator."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.reviewer = staff_user("reviewer.staffcustomer@example.org")
        cls.staff = staff_user("staff.staffcustomer@example.org")
        cls.administrator = staff_user("administrator.staffcustomer@example.org", superuser=True)

    def setUp(self):
        configure_numbers()
        publish_contract()

    def test_the_services_serve_a_staff_account_like_a_customer(self):
        for number, person in enumerate((self.staff, self.administrator), start=1):
            with self.subTest(person=person.pk):
                application, building = draft_with_points(person, number=number * 10, egid=90004270 + number)
                self.assertEqual(application.person_id, person.pk)
                self.assertIsNotNone(applications.progress(application))
                self.assertEqual(building.application_id, application.pk)

    def test_a_staff_account_runs_the_whole_registration_and_the_participation_shows_on_the_start_page(self):
        complete_application(self.staff, self.reviewer)
        client = login(self.staff)

        html = client.get(HOME).content.decode()
        self.assertIn("Sie nehmen seit dem", html)
        page = client.get(BUILDINGS)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Ittigenstrasse")
        self.assertTrue(MeteringPoint.objects.filter(person_assignments__person=self.staff).exists())

    def test_a_staff_account_can_use_the_pages_of_the_wizard(self):
        application, _building = draft_with_building(self.administrator)
        client = login(self.administrator)
        for name in ("application_building", "application_metering_points"):
            with self.subTest(name=name):
                self.assertEqual(client.get(reverse(f"customer:{name}", args=[application.pk])).status_code, 200)
        self.assertContains(client.get(HOME), "Ihr Weg in die LEG")

    def test_the_mail_of_the_application_goes_to_the_account_like_for_everybody(self):
        application, _building = draft_with_points(self.staff, number=33)
        applications.accept_contract_step(
            self.staff, application, contracts.current_version().pk, agrees_contract=True, agrees_data_exchange=True
        )
        applications.set_start(self.staff, application, asap=True)
        applications.submit(self.staff, application)
        self.assertTrue(
            MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED, address="staff.staffcustomer@example.org").exists()
        )

    def test_the_staff_see_a_staff_account_with_an_application_in_the_management_area_and_nothing_crashes(self):
        complete_application(self.staff, self.reviewer)
        draft_with_building(self.administrator, egid=90004299, street="Seitenweg", house_number="2")
        client = login(self.reviewer)
        person_page = client.get(reverse("management:persons_detail", args=[self.staff.pk]))
        self.assertEqual(person_page.status_code, 200)
        self.assertContains(person_page, "Example")
        for name in ("management:persons", "management:processes", "management:locations", "management:metering_points",
                     "management:overview", "management:legs"):
            with self.subTest(name=name):
                self.assertEqual(client.get(reverse(name)).status_code, 200)
        for query in ("Example", self.staff.customer_number_display, "staff.staffcustomer"):
            with self.subTest(query=query):
                listing = client.get(reverse("management:persons"), {"q": query})
                self.assertEqual(listing.status_code, 200)
        self.assertContains(client.get(reverse("management:processes")), "Example")
        self.assertEqual(client.get(reverse("management:persons_detail", args=[self.administrator.pk])).status_code, 200)

    def test_the_management_of_a_staff_account_stays_limited_to_system_administrators(self):
        # D-087 unchanged: taking part does not make the account any easier to change.
        self.assertFalse(persons.may_change_customer_data(self.reviewer, self.staff))
        self.assertFalse(persons.may_change_customer_data(self.reviewer, self.administrator))
        self.assertFalse(persons.may_change_customer_data(self.staff, self.staff))
        self.assertTrue(persons.may_change_customer_data(self.administrator, self.staff))
        self.assertTrue(persons.may_change_customer_data(self.administrator, self.administrator))
        ordinary = customer("ordinary.staffcustomer@example.org")
        self.assertTrue(persons.may_change_customer_data(self.reviewer, ordinary))

    def test_the_own_staff_privileges_are_not_part_of_the_customer_pages(self):
        # The customer pages offer no way to change is_staff, is_superuser or the customer number.
        client = login(self.staff)
        client.post(reverse("customer:bank_connection"), {"is_staff": "", "is_superuser": "on", "iban": ""})
        stored = User.objects.get(pk=self.staff.pk)
        self.assertEqual((stored.is_staff, stored.is_superuser), (True, False))


@tag("mariadb")
class StaffAccountDeletionTests(TestCase):
    """The deletion rules of D-016, D-055, D-086, D-087 and D-105 for a staff account that takes part."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)

    def test_a_staff_account_without_data_is_deleted_like_any_account(self):
        staff = staff_user("delete.staff@example.org")
        spare = staff_user("delete.spare@example.org", superuser=True)  # another system administrator exists

        response = delete_account(login(staff), staff)

        self.assertContains(response, "Ihr Konto wurde gelöscht")
        self.assertFalse(User.objects.filter(pk=staff.pk).exists())
        self.assertTrue(User.objects.filter(pk=spare.pk).exists())

    def test_a_staff_account_with_a_running_point_is_blocked_before_the_proof(self):
        staff = staff_user("blocked.staff@example.org")
        active_point(1, staff, self.location)

        response = delete_account(login(staff), staff)

        self.assertRedirects(response, f"{PROFILE}#card-delete", fetch_redirect_response=False)
        self.assertEqual(User.objects.get(pk=staff.pk).status, User.Status.ACTIVE)

    def test_once_the_deregistration_is_started_a_staff_account_is_deactivated_and_keeps_its_data(self):
        staff = staff_user("deactivated.staff@example.org")
        staff.set_retention_obligation()
        active_point(1, staff, self.location)
        deregistrations.request_deregistration(
            staff, metering_point_ids=[p.pk for p in deregistrations.eligible_points(staff)]
        )

        response = delete_account(login(staff), staff)

        self.assertContains(response, "Ihr Konto wurde deaktiviert")
        stored = User.objects.get(pk=staff.pk)
        self.assertEqual(stored.status, User.Status.DEACTIVATED)
        self.assertTrue(stored.is_staff)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DEACTIVATED).count(), 1)

    def test_the_last_system_administrator_is_neither_deleted_nor_deactivated(self):
        administrator = staff_user("last.administrator@example.org", superuser=True)
        self.assertEqual(User.objects.filter(is_superuser=True, status=User.Status.ACTIVE).count(), 1)

        response = delete_account(login(administrator), administrator)

        self.assertRedirects(response, f"{PROFILE}#card-delete", fetch_redirect_response=False)
        stored = User.objects.get(pk=administrator.pk)
        self.assertEqual((stored.status, stored.is_superuser, stored.is_staff), (User.Status.ACTIVE, True, True))

    def test_the_last_system_administrator_with_a_retention_obligation_is_not_deactivated_either(self):
        administrator = staff_user("last.retained@example.org", superuser=True)
        administrator.set_retention_obligation()

        response = delete_account(login(administrator), administrator)

        self.assertRedirects(response, f"{PROFILE}#card-delete", fetch_redirect_response=False)
        self.assertEqual(User.objects.get(pk=administrator.pk).status, User.Status.ACTIVE)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DEACTIVATED).exists())

    def test_a_system_administrator_may_be_deleted_while_another_one_remains(self):
        first = staff_user("first.administrator@example.org", superuser=True)
        second = staff_user("second.administrator@example.org", superuser=True)

        response = delete_account(login(first), first)

        self.assertContains(response, "Ihr Konto wurde gelöscht")
        self.assertTrue(User.objects.get(pk=second.pk).is_active)

    def test_the_deletion_card_of_a_staff_account_with_points_leads_to_the_deregistration(self):
        staff = staff_user("card.staff@example.org")
        active_point(1, staff, self.location)

        html = login(staff).get(f"{PROFILE}?edit=delete").content.decode()

        self.assertIn(f'href="{reverse("customer:deregistration_start")}?for=account_deletion"', html)
        self.assertNotIn('name="confirm"', html)
