"""A person whom staff created (D-087, owner 2026-11-19) signs in like any customer after "Forgot password?".

The account is active with a customer number and gets no email. Its password is a random secret that nobody knows, so the
person chooses one with the reset (the answers stay generic, ``docs/SECURITY.md`` section 3); the reset asks for no second
factor that does not exist. Someone who registers the address themselves gets the notice for an existing account. The
service itself is tested in ``community/tests/test_person_support.py``, the dialog in
``management/tests/test_person_data_pages.py``."""

from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts import mail_queue, second_factor
from leg_platform.accounts.models import MailJob, RegistrationChallenge, SecurityEvent, User, VerificationToken
from leg_platform.accounts.tests.helpers import OTHER_PASSWORD, registration_data, reset_request_data, run_mail_queue
from leg_platform.accounts.tests.test_password_reset import REQUEST_URL, login, request_reset, reset_path
from leg_platform.community import person_search, persons
from leg_platform.community.tests.helpers import staff_user

DATA = {
    "salutation": "mrs", "first_name": "Erika", "last_name": "Neu", "street": "Neuweg", "house_number": "3",
    "swiss_postal_code": 3000, "town": "Bern", "country": "CH",
}
ADDRESS = "erika.neu@example.org"
HOME = reverse("home")
LOGIN_URL = reverse("accounts:login")


@tag("mariadb")
class CreatedPersonSignInTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.creation@example.org")

    def setUp(self):
        self.person = persons.create_person(self.staff, ADDRESS, DATA)
        mail.outbox.clear()

    def test_the_person_cannot_sign_in_before_a_password_is_chosen_and_the_answer_is_the_generic_one(self):
        known = login(Client(), ADDRESS, OTHER_PASSWORD, "10.70.0.1")
        unknown = login(Client(), "nobody.creation@example.org", OTHER_PASSWORD, "10.70.0.2")

        self.assertEqual((known.status_code, unknown.status_code), (200, 200))
        self.assertContains(known, "Die E-Mail-Adresse oder das Passwort ist falsch.")
        self.assertEqual(known.content.decode().count("alert-danger"), unknown.content.decode().count("alert-danger"))
        self.assertNotIn("_auth_user_id", known.wsgi_request.session)

    def test_the_reset_gives_the_person_a_password_and_the_account_then_behaves_like_any_customer_account(self):
        # The account has never had a password and has no second factor; the reset asks for none.
        self.assertFalse(second_factor.has_second_factor(self.person))
        self.assertFalse(second_factor.requires_second_factor(self.person))
        number = self.person.customer_number_id
        client = Client()

        request_reset(ADDRESS, client)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [ADDRESS])
        path = reset_path(mail.outbox[0])
        opened = Client().get(path)
        self.assertRedirects(opened, reverse("accounts:password_reset_set"), fetch_redirect_response=False)
        setter = Client()
        setter.get(path)
        page = setter.get(reverse("accounts:password_reset_set"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="otp_token"')  # no code of a second factor is asked
        done = setter.post(
            reverse("accounts:password_reset_set"), {"new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD}
        )
        self.assertRedirects(done, LOGIN_URL, fetch_redirect_response=False)

        person = User.objects.get(pk=self.person.pk)
        self.assertTrue(person.check_password(OTHER_PASSWORD))
        self.assertEqual(
            (person.status, person.customer_number_id, person.authentication_method, person.is_staff),
            ("active", number, User.AuthenticationMethod.PASSWORD, False),
        )
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.PASSWORD_RESET, account_id=person.pk).count(), 1)
        # The person signs in and reaches the pages of a customer; no second factor is demanded.
        signed_in = Client()
        response = login(signed_in, ADDRESS, OTHER_PASSWORD, "10.70.0.3")
        self.assertEqual(response.status_code, 302)
        start = signed_in.get(HOME)
        self.assertEqual(start.status_code, 200)
        self.assertContains(start, "Guten Tag, Erika Neu")
        self.assertEqual(signed_in.get(reverse("accounts:profile")).status_code, 200)
        self.assertEqual(signed_in.get(reverse("customer:buildings")).status_code, 200)
        # The reset link is used up.
        self.assertEqual(Client().get(path).status_code, 302)
        self.assertFalse(VerificationToken.objects.filter(user=person, purpose=VerificationToken.Purpose.PASSWORD_RESET).exists())

    def test_the_request_gets_the_same_answer_for_a_created_person_and_for_an_unknown_address(self):
        answers = []
        for address, remote in ((ADDRESS, "10.70.1.1"), ("nobody.creation@example.org", "10.70.1.2")):
            response = Client().post(REQUEST_URL, reset_request_data(address), REMOTE_ADDR=remote)
            answers.append((response.status_code, response["Location"]))
        self.assertEqual(len(set(answers)), 1)
        # In the background only the person gets a mail.
        run_mail_queue()
        self.assertEqual([message.to for message in mail.outbox], [[ADDRESS]])

    def test_somebody_who_registers_the_address_gets_the_notice_that_points_to_forgot_password(self):
        before = User.objects.get(pk=self.person.pk)
        client = Client()

        first = client.post(reverse("accounts:registration"), registration_data(ADDRESS), REMOTE_ADDR="10.70.2.1")
        unknown = Client().post(
            reverse("accounts:registration"), registration_data("nobody.creation@example.org"), REMOTE_ADDR="10.70.2.2"
        )
        mail_queue.process_pending()

        # The answer of the page does not tell that the address is taken.
        self.assertEqual((first.status_code, first["Location"]), (unknown.status_code, unknown["Location"]))
        notice = [message for message in mail.outbox if message.to == [ADDRESS]]
        self.assertEqual(len(notice), 1)
        body = notice[0].body
        self.assertIn("Für diese Adresse besteht bereits ein Konto.", body)
        self.assertIn("«Passwort vergessen?»", body)
        self.assertNotIn("Einladung", body)
        self.assertNotRegex(body, r"\b\d{6}\b")  # no code
        # No code for the address, and the account is as it was.
        self.assertFalse(RegistrationChallenge.objects.filter(address=ADDRESS).exclude(code_digest="").exists())
        after = User.objects.get(pk=self.person.pk)
        self.assertEqual(
            (after.pk, after.status, after.customer_number_id, after.password, after.first_name),
            (before.pk, "active", before.customer_number_id, before.password, "Erika"),
        )
        self.assertEqual(User.objects.filter(normalized_email=ADDRESS).count(), 1)

    def test_the_person_is_found_for_an_assignment_at_once(self):
        found = person_search.search("Neu")

        self.assertEqual([person.pk for person in found], [self.person.pk])

    def test_creating_a_person_sends_no_mail_and_stores_no_job(self):
        run_mail_queue()

        self.assertEqual(mail.outbox, [])
        self.assertFalse(MailJob.objects.filter(account=self.person).exists())
