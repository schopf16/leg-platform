"""The way out of the participation (owner decisions 2026-10-24 and 2026-10-29; D-126 to D-128): the fork page "What would you
like to do?", the deletion page with its proof and the box, and the deletion without any gate: it deregisters the running
and the not yet active points by itself and withdraws the applications. Negative cases: anonymous visitors, no CSRF token, a
wrong proof, a missing box, an id of another account."""

from datetime import date, timedelta

from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import User
from leg_platform.accounts.tests.helpers import PASSWORD, VerifiedClient
from leg_platform.community import own_iban
from leg_platform.community.models import Application, Deregistration, Direction
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point
from leg_platform.customer.tests.helpers import GOOD_IBAN, customer, login
from leg_platform.customer.tests.test_card_edit import submitted
from leg_platform.customer.tests.test_deregistration import LONG_AGO, START, active_point

END = reverse("accounts:account_end")
DELETE = reverse("accounts:account_delete")
PROFILE = reverse("accounts:profile")


@tag("mariadb")
class ForkPageTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)

    def setUp(self):
        self.mine = customer("fork.mine@example.org")

    def test_a_customer_with_a_running_point_sees_the_fork_with_two_calm_ways(self):
        active_point(1, self.mine, self.location)

        response = login(self.mine).get(END)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        for text in ("Was möchten Sie tun?", "Nur Messpunkte abmelden", "Ganzes Konto löschen", "Abbrechen"):
            self.assertIn(text, html)
        self.assertIn(f'href="{START}?from=end"', html)
        self.assertIn(f'href="{DELETE}"', html)
        self.assertIn(f'href="{PROFILE}#card-delete"', html)
        # No red on the fork: the danger styling and the proof are on the deletion page.
        self.assertNotIn("btn-danger", html)
        self.assertNotIn('name="current_password"', html)

    def test_a_registration_in_progress_also_shows_the_fork(self):
        submitted(self.mine, feed_in=False, egid=90004271, number=21)

        response = login(self.mine).get(END)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Was möchten Sie tun?", response.content.decode())

    def test_a_customer_without_a_point_goes_straight_to_the_deletion_page(self):
        response = login(self.mine).get(END)

        self.assertRedirects(response, DELETE, fetch_redirect_response=False)

    def test_a_get_changes_nothing_and_a_post_is_not_offered(self):
        active_point(1, self.mine, self.location)
        client = login(self.mine)

        client.get(END)

        self.assertEqual(client.post(END, {"current_password": PASSWORD, "confirm": "on"}).status_code, 405)
        self.assertEqual(User.objects.get(pk=self.mine.pk).status, User.Status.ACTIVE)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_the_profile_card_leads_to_the_fork_and_is_not_red_on_the_buildings_page(self):
        client = login(self.mine)
        card = client.get(PROFILE).content.decode()
        card = card[card.index('id="card-delete"') :]

        self.assertIn("Teilnahme beenden oder Konto löschen", card)
        self.assertIn(f'href="{END}"', card)
        buildings = client.get(reverse("customer:buildings")).content.decode()
        self.assertNotIn(f'href="{DELETE}"', buildings)

    def test_anonymous_visitors_are_sent_to_the_login(self):
        for path in (END, DELETE):
            with self.subTest(path=path):
                response = Client().get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("accounts:login"), response["Location"])


@tag("mariadb")
class DeletionWithoutGateTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)

    def setUp(self):
        self.mine = customer("gate.mine@example.org")
        self.other = customer("gate.other@example.org")
        self.client = login(self.mine)

    def delete(self, **extra):
        return self.client.post(DELETE, {"current_password": PASSWORD, "confirm": "on", **extra})

    def test_the_page_asks_for_the_proof_and_the_box_and_names_no_gate(self):
        active_point(1, self.mine, self.location)

        html = self.client.get(DELETE).content.decode()

        for text in ("Konto unwiderruflich löschen", "Doch lieber nur Messpunkte abmelden", "Abbrechen"):
            self.assertIn(text, html)
        self.assertIn('name="current_password"', html)
        self.assertIn('name="confirm"', html)
        self.assertIn(f'href="{START}?from=delete"', html)
        self.assertNotIn("zuerst", html)

    def test_every_running_point_gets_exactly_one_deregistration_and_nothing_has_to_be_done_first(self):
        draw = active_point(1, self.mine, self.location)
        feed_in = active_point(2, self.mine, self.location, Direction.FEED_IN)
        # Neither a missing IBAN nor an open deregistration of someone else refuses it.
        own_iban.remove_own_iban(self.mine)
        active_point(3, self.other, self.location)

        response = self.delete()

        self.assertTemplateUsed(response, "accounts/account_deleted.html")
        dereg = Deregistration.objects.get(person=self.mine)
        self.assertEqual(sorted(dereg.items.values_list("metering_point_id", flat=True)), [draw.pk, feed_in.pk])
        self.assertEqual(Deregistration.objects.count(), 1)
        self.assertEqual(User.objects.get(pk=self.mine.pk).status, User.Status.DEACTIVATED)
        # The sign-in is gone at once.
        self.assertNotEqual(self.client.get(PROFILE).status_code, 200)

    def test_the_account_deletion_withdraws_the_applications(self):
        application, _building = submitted(self.mine, feed_in=False, egid=90004272, number=31)
        self.assertEqual(application.state, Application.State.SUBMITTED)

        response = self.delete()

        self.assertTemplateUsed(response, "accounts/account_deleted.html")
        self.assertFalse(Application.objects.filter(pk=application.pk).exists())
        self.assertFalse(Application.objects.filter(person=self.mine).exists())

    def test_a_point_that_is_not_active_yet_is_deregistered_like_a_running_one(self):
        later = date.today() + timedelta(days=60)
        kinds = (
            {},
            {"registered_with_operator_on": LONG_AGO},
            {"registered_with_operator_on": LONG_AGO, "confirmed_on": LONG_AGO, "start_date": later},
        )
        points = []
        for number, extra in enumerate(kinds, start=40):
            point = make_metering_point(number, Direction.CONSUMPTION, self.location, **extra)
            assign_person(point, self.mine, valid_from=LONG_AGO)
            points.append(point)

        self.delete()

        dereg = Deregistration.objects.get(person=self.mine)
        self.assertEqual(sorted(dereg.items.values_list("metering_point_id", flat=True)), sorted(p.pk for p in points))

    def test_without_the_box_nothing_happens(self):
        active_point(1, self.mine, self.location)

        response = self.client.post(DELETE, {"current_password": PASSWORD})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bitte setzen Sie das Häkchen, um zu bestätigen, dass Sie Ihr Konto löschen möchten.")
        self.assertTemplateNotUsed(response, "accounts/account_deleted.html")
        self.assertEqual(User.objects.get(pk=self.mine.pk).status, User.Status.ACTIVE)
        self.assertEqual(Deregistration.objects.count(), 0)
        self.assertEqual(self.client.get(PROFILE).status_code, 200)

    def test_a_wrong_or_missing_password_changes_nothing(self):
        active_point(1, self.mine, self.location)

        for data in ({"current_password": "not the password at all", "confirm": "on"}, {"confirm": "on"}):
            with self.subTest(data=sorted(data)):
                response = self.client.post(DELETE, data)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateNotUsed(response, "accounts/account_deleted.html")
        self.assertEqual(User.objects.get(pk=self.mine.pk).status, User.Status.ACTIVE)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_a_post_without_the_csrf_token_is_refused(self):
        active_point(1, self.mine, self.location)
        guarded = VerifiedClient(enforce_csrf_checks=True)
        guarded.force_login(self.mine)

        response = guarded.post(DELETE, {"current_password": PASSWORD, "confirm": "on"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(User.objects.get(pk=self.mine.pk).status, User.Status.ACTIVE)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_an_anonymous_post_does_nothing(self):
        response = Client().post(DELETE, {"current_password": PASSWORD, "confirm": "on"})

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])
        self.assertEqual(User.objects.get(pk=self.mine.pk).status, User.Status.ACTIVE)


@tag("mariadb")
class DeregistrationPageAuthorizationTests(TestCase):
    """The ids of the request are never trusted (D-103): another account's building or point is a 404 on the page and on
    its POST, and nothing is created."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        cls.stranger_location = make_location("Fremdstrasse", "9", "3063", "Ittigen", egid=90001235)

    def setUp(self):
        self.mine = customer("authz.mine@example.org")
        self.other = customer("authz.other@example.org")
        self.own = active_point(1, self.mine, self.location)
        self.foreign = active_point(2, self.other, self.stranger_location)
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        self.client = login(self.mine)

    def test_another_accounts_building_and_point_are_a_404(self):
        for query in ({"building": self.stranger_location.pk}, {"point": [self.foreign.pk]}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(START, query).status_code, 404)
                response = self.client.post(
                    f"{START}?building={self.stranger_location.pk}",
                    {"metering_points": [str(self.foreign.pk)], "termination": "earliest", "current_password": PASSWORD},
                )
                self.assertEqual(response.status_code, 404)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_without_the_right_proof_nothing_is_created(self):
        for proof in ({"current_password": "wrong wrong wrong"}, {}):
            with self.subTest(proof=proof):
                response = self.client.post(
                    START, {"metering_points": [str(self.own.pk)], "termination": "earliest", **proof}
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_without_the_csrf_token_nothing_is_created(self):
        guarded = VerifiedClient(enforce_csrf_checks=True)
        guarded.force_login(self.mine)

        response = guarded.post(
            START, {"metering_points": [str(self.own.pk)], "termination": "earliest", "current_password": PASSWORD}
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_anonymous_visitors_are_sent_to_the_login(self):
        response = Client().get(START)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])
