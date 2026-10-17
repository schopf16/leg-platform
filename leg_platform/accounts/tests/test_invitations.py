"""The steps that an invited person takes (D-087): the link of the email, the password, the confirmation of the data
that the staff entered, and the activation with the customer number. The account is created by the staff
(``community.persons.create_person``); these tests start there and follow the pages of the registration. The rules of
the creation and of the changes are in ``community/tests/test_person_support.py``."""

import re
from datetime import timedelta

from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import email_texts, emails, password_reset
from leg_platform.accounts.models import EmailText, MailJob, SecurityEvent, User, VerificationToken
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    completion_data,
    password_data,
    run_mail_queue,
    verification_path,
)
from leg_platform.community import persons
from leg_platform.community.tests.helpers import staff_user

DATA = {
    "salutation": "mrs", "first_name": "Erika", "last_name": "Neu", "address_line_1": "c/o Treuhand",
    "street": "Neuweg", "house_number": "3", "swiss_postal_code": 3000, "town": "Bern", "country": "CH",
    "phone_number": "+41311234567",
}


def reference_of(page):
    return re.search(r'name="registration" value="(\d+)"', page.content.decode()).group(1)


@tag("mariadb")
class InvitationFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.invitation@example.org")

    def invite(self, email="erika.neu@example.org", **values):
        person = persons.create_person(self.staff, email, {**DATA, **values})
        run_mail_queue()
        return person, verification_path(mail.outbox[-1])

    def open(self, client, path):
        client.get(path)
        page = client.get(reverse("accounts:registration_password"))
        self.assertEqual(page.status_code, 200)
        reference = reference_of(page)
        response = client.post(reverse("accounts:registration_password"), password_data(PASSWORD, registration=reference))
        self.assertRedirects(response, reverse("accounts:registration_complete"))
        return reference

    def test_the_link_leads_through_the_password_and_the_confirmation_of_the_data_to_the_activation(self):
        person, path = self.invite()
        client = Client()
        # Opening the link changes nothing (scanners open links); the account stays what it was.
        client.get(path)
        person.refresh_from_db()
        self.assertEqual((person.status, person.customer_number_id, person.has_usable_password()), ("unverified", None, False))
        reference = self.open(client, path)
        page = client.get(reverse("accounts:registration_complete"))
        html = page.content.decode()
        # The person finds the data of the staff, and confirms it together with the privacy notice.
        for value in ('value="Erika"', 'value="Neu"', 'value="Neuweg"', 'value="3"', 'value="3000"', 'value="Bern"'):
            self.assertIn(value, html)
        self.assertIn('data-country-value="CH"', html)
        self.assertEqual(
            self.complete(client, reference, first_name="Erika", last_name="Neu", street="Neuweg", house_number="3",
                          postal_code="3000", town="Bern").status_code,
            302,
        )
        person.refresh_from_db()
        self.assertEqual((person.status, person.is_active), ("active", True))
        self.assertRegex(person.customer_number_id, r"^\d{6}$")  # issued now (D-021)
        self.assertTrue(person.check_password(PASSWORD))
        self.assertEqual((person.is_staff, person.is_superuser, person.retention_obligation), (False, False, False))
        self.assertFalse(person.groups.exists() or person.user_permissions.exists())
        # The data of the staff that the steps do not show stays, the consent is stored.
        self.assertEqual(
            (person.salutation, person.address_line_1, person.phone_number.as_e164, person.country.code),
            ("mrs", "c/o Treuhand", "+41311234567", "CH"),
        )
        self.assertIsNotNone(person.privacy_accepted_at)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.REGISTRATION_COMPLETED, account_id=person.pk).count(), 1)
        self.assertFalse(VerificationToken.objects.filter(user=person).exists())  # single use
        self.assertEqual(client.get(reverse("home")).status_code, 200)  # logged in, as after a registration (D-055)

    def complete(self, client, reference, **overrides):
        data = completion_data(registration=reference, **overrides)
        return client.post(reverse("accounts:registration_complete"), data)

    def test_the_link_cannot_be_used_twice_and_an_expired_link_leads_nowhere(self):
        person, path = self.invite()
        client = Client()
        reference = self.open(client, path)
        self.assertEqual(self.complete(client, reference, first_name="Erika", last_name="Neu").status_code, 302)
        again = Client().get(path)
        self.assertRedirects(again, reverse("accounts:registration_invalid"))
        # An expired link: no step is shown (the account itself is deleted by the scheduled cleanup, D-016).
        other, other_path = self.invite("expired.person@example.org")
        VerificationToken.objects.filter(user=other).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertRedirects(Client().get(other_path), reverse("accounts:registration_invalid"))
        self.assertEqual(Client().get(reverse("accounts:registration_password")).status_code, 302)
        other.refresh_from_db()
        self.assertEqual((other.status, other.customer_number_id), ("unverified", None))

    def test_a_replaced_link_ends_and_the_account_cannot_log_in_or_reset_a_password_before_the_registration_is_complete(self):
        person, first = self.invite()
        from unittest.mock import patch

        from leg_platform.accounts import rate_limits

        with patch.object(rate_limits, "INVITATION_ACCOUNT_LIMITS", ((1000, timedelta(seconds=1)),)):
            persons.resend_invitation(person, self.staff)
        run_mail_queue()
        second = verification_path(mail.outbox[-1])
        self.assertNotEqual(first, second)
        self.assertRedirects(Client().get(first), reverse("accounts:registration_invalid"))
        self.assertRedirects(Client().get(second), reverse("accounts:registration_password"), fetch_redirect_response=False)
        # No login is possible without a password, and a password reset sends nothing for the account.
        person.refresh_from_db()
        self.assertEqual((person.is_active, person.has_usable_password()), (False, False))
        self.assertIsNone(password_reset.process_reset_request(person.email))

    def test_the_steps_ignore_every_field_but_their_own(self):
        person, path = self.invite()
        client = Client()
        reference = self.open(client, path)
        self.complete(
            client, reference, first_name="Erika", last_name="Neu", is_staff="on", is_superuser="on", status="blocked",
            customer_number="111111", retention_obligation="on", groups="1", email="evil@example.org",
        )
        person.refresh_from_db()
        self.assertEqual((person.is_staff, person.is_superuser, person.status, person.email), (False, False, "active", "erika.neu@example.org"))
        self.assertNotEqual(person.customer_number_id, "111111")

    def test_an_invitation_with_a_foreign_address_keeps_the_country_and_asks_for_its_postal_code(self):
        person, path = self.invite(
            "abroad.person@example.org", country="DE", swiss_postal_code=None, foreign_postal_code="10115", town="Berlin",
        )
        client = Client()
        reference = self.open(client, path)
        html = client.get(reverse("accounts:registration_complete")).content.decode()
        self.assertIn('data-country-value="DE"', html)
        self.assertIn('value="10115"', html)
        self.assertIn('maxlength="15"', html)
        # The foreign postal code is the one of this address (the Swiss rule of the registration does not apply).
        done = self.complete(client, reference, first_name="Erika", last_name="Neu", town="Berlin", postal_code="10115")
        self.assertEqual(done.status_code, 302)
        person.refresh_from_db()
        self.assertEqual((person.country.code, person.swiss_postal_code, person.foreign_postal_code), ("DE", None, "10115"))
        self.assertEqual(person.status, "active")

    def test_the_last_step_never_saves_every_column_of_the_account(self):
        """L-2: only the fields of the form and of the consent are written, from the account read under the lock."""
        from unittest.mock import patch

        person, path = self.invite()
        client = Client()
        reference = self.open(client, path)
        calls = []
        real_save = User.save

        def spy(self, **kwargs):
            calls.append(kwargs.get("update_fields"))
            return real_save(self, **kwargs)

        with patch.object(User, "save", spy):
            self.assertEqual(self.complete(client, reference, first_name="Erika", last_name="Neu").status_code, 302)
        self.assertTrue(calls and all(fields is not None for fields in calls), calls)
        data_step = next(fields for fields in calls if "privacy_accepted_at" in fields)
        self.assertEqual(
            set(data_step),
            {"first_name", "last_name", "street", "house_number", "town", "swiss_postal_code", "foreign_postal_code",
             "privacy_accepted_at", "privacy_accepted_url"},
        )
        person.refresh_from_db()
        self.assertEqual(person.status, "active")

    def test_a_registration_of_somebody_else_keeps_working_as_before(self):
        """The ordinary registration is not changed: the address of a plain pending registration is replaced."""
        from leg_platform.accounts import registration

        pending = User.objects.create_user("plain.registration@example.org")
        send = registration.process_registration("plain.registration@example.org")
        send()
        self.assertFalse(User.objects.filter(pk=pending.pk).exists())
        self.assertEqual(mail.outbox[-1].to, ["plain.registration@example.org"])


@tag("mariadb")
class InvitationEmailTextTests(TestCase):
    def test_the_text_is_neutral_editable_and_in_swiss_german(self):
        Key = EmailText.Key
        self.assertIn(Key.INVITATION, Key.values)
        EmailText.objects.ensure_all()
        self.assertTrue(EmailText.objects.filter(key=Key.INVITATION).exists())
        subject, body = emails.preview(Key.INVITATION, "", "")
        self.assertIn("Guten Tag", body)
        self.assertIn("Für Sie wurde ein Konto angelegt.", body)
        self.assertIn("7 Tage", body)
        self.assertIn("/register/verify/", body)
        self.assertNotRegex(body, r"\{[a-z_]+\}")
        self.assertNotIn("ß", subject + body)
        # The personal link is required in a text that the administrator writes; the days are a placeholder.
        names = {placeholder.name for placeholder in email_texts.placeholders_of(Key.INVITATION)}
        self.assertLessEqual({"verification_url", "lifetime_days", "instance_name"}, names)
        errors = []
        try:
            email_texts.validate_body(Key.INVITATION, "Hallo {lifetime_days}")
        except Exception as error:  # a ValidationError with the messages
            errors = error.messages
        self.assertEqual(len(errors), 1)
        email_texts.validate_body(Key.INVITATION, "Hallo, Ihr Link: {verification_url} ({lifetime_days} Tage)")

    def test_a_queued_invitation_is_a_request_of_the_queue_with_no_notification_and_no_name(self):
        self.assertNotIn(MailJob.Kind.INVITATION, MailJob.NOTIFICATION_KINDS)
