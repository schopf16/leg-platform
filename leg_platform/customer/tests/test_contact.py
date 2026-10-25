"""The contact form "Contact the LEG" (D-115): the message to the LEG, the answer by mail or by phone, the copy."""

from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.mail import EmailMessage
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts import mail_addresses
from leg_platform.accounts.tests.helpers import instance_mail_addresses
from leg_platform.customer.contact import swiss_phone
from leg_platform.customer.tests.helpers import customer, login
from leg_platform.mail import GraphEmailError

PAGE = reverse("customer:contact")
LEG = "leg@example.org"


@tag("customer")
class SwissPhoneTests(SimpleTestCase):
    def test_international_and_national_forms_are_accepted(self):
        for raw in ("+41 79 123 45 67", "0041 79 123 45 67", "079 123 45 67"):
            with self.subTest(raw=raw):
                self.assertEqual(swiss_phone(raw), "+41791234567")
        self.assertEqual(swiss_phone("+49 30 1234567"), "+49301234567")

    def test_a_number_without_the_leading_zero_or_too_short_is_refused(self):
        for raw in ("79 123 45 67", "079 12345", "+41 79 123", "abc", ""):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                swiss_phone(raw)


@tag("customer")
@instance_mail_addresses(contact_address=LEG)
class ContactTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.contact@example.org")

    def post(self, **values):
        data = {"message": "Hallo LEG", "reply_by": "email", "email": "anna.mail@example.org", "copy": ""}
        data.update(values)
        return login(self.mine).post(PAGE, data)

    def test_the_email_and_phone_fields_tell_password_managers_to_leave_them_alone(self):
        html = login(self.mine).get(PAGE).content.decode()
        for name in ("email", "phone"):
            field = html[html.index(f'name="{name}"') - 300 : html.index(f'name="{name}"') + 300]
            for attribute in ("data-lpignore", "data-1p-ignore", "data-bwignore"):
                with self.subTest(field=name, attribute=attribute):
                    self.assertIn(attribute, field)

    def test_the_page_offers_the_mail_answer_with_the_address_of_the_account(self):
        html = login(self.mine).get(PAGE).content.decode()
        self.assertIn('name="message"', html)
        self.assertIn('value="' + self.mine.email + '"', html)
        self.assertIn('data-reveal-value="email"', html)
        self.assertNotIn('value="+41', html)

    @instance_mail_addresses(contact_address="")
    def test_without_the_address_of_the_leg_the_page_is_not_offered(self):
        client = login(self.mine)
        self.assertIn("noch nicht verfügbar", client.get(PAGE).content.decode())
        self.assertEqual(client.post(PAGE, {"message": "x", "reply_by": "email", "email": "a@example.org"}).status_code, 404)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_mail_goes_to_the_leg_from_the_standard_sender_with_the_reply_to_address(self):
        response = self.post(email="anna.mail@example.org")
        self.assertRedirects(response, PAGE, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, [LEG])
        self.assertEqual(sent.from_email, mail_addresses.sender())
        self.assertEqual(sent.reply_to, ["anna.mail@example.org"])
        self.assertIn("Hallo LEG", sent.body)
        self.assertIn(self.mine.customer_number_display, sent.body)
        self.assertNotIn("Auto-Submitted", sent.extra_headers)

    def test_a_mail_answer_does_not_change_the_profile_address_or_the_phone(self):
        self.post(email="changed@example.org", phone="079 123 45 67")
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.email, "mine.contact@example.org")
        self.assertFalse(self.mine.phone_number)

    def test_the_phone_answer_sends_the_number_and_saves_it_to_the_profile(self):
        self.post(reply_by="phone", phone="079 123 45 67")
        sent = mail.outbox[0]
        self.assertEqual(sent.reply_to, [self.mine.email])
        self.assertIn("+41 79 123 45 67", sent.body)
        self.mine.refresh_from_db()
        self.assertEqual(str(self.mine.phone_number), "+41791234567")

    def test_a_changed_number_replaces_the_one_in_the_profile(self):
        self.mine.phone_number = "+41791112233"
        self.mine.save(update_fields=["phone_number"])
        self.post(reply_by="phone", phone="+41 79 999 88 77")
        self.mine.refresh_from_db()
        self.assertEqual(str(self.mine.phone_number), "+41799998877")

    def test_the_copy_goes_to_the_verified_address_of_the_account(self):
        self.post(copy="on")
        self.assertEqual(len(mail.outbox), 2)
        copy = mail.outbox[1]
        self.assertEqual(copy.to, [self.mine.email])
        self.assertIn("Hallo LEG", copy.body)
        self.assertEqual(copy.reply_to, [])

    def test_an_invalid_phone_number_is_refused_and_nothing_is_sent_or_saved(self):
        for raw in ("79 123 45 67", "079 12345", "abc"):
            with self.subTest(raw=raw):
                response = self.post(reply_by="phone", phone=raw)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Bitte geben Sie eine gültige Telefonnummer ein", response.content.decode())
        self.assertEqual(len(mail.outbox), 0)
        self.mine.refresh_from_db()
        self.assertFalse(self.mine.phone_number)

    def test_a_mail_answer_needs_an_address(self):
        response = self.post(email="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bitte geben Sie eine E-Mail-Adresse ein.", response.content.decode())
        self.assertEqual(len(mail.outbox), 0)

    def test_a_failed_delivery_is_shown_and_the_number_is_not_saved(self):
        with patch.object(EmailMessage, "send", side_effect=GraphEmailError("refused")):
            response = self.post(reply_by="phone", phone="079 123 45 67")
        self.assertIn("Die Nachricht konnte nicht gesendet werden.", response.content.decode())
        self.mine.refresh_from_db()
        self.assertFalse(self.mine.phone_number)

    def test_the_menu_links_to_the_page(self):
        self.assertIn('href="' + PAGE + '"', login(self.mine).get(reverse("customer:contract")).content.decode())
