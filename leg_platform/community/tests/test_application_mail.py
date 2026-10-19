"""The mails of the onboarding (D-103): every kind has a text, a handler and a place in the administration; the texts carry
only the greeting and the link to the portal (no address, IBAN or metering point number); the queue sends them to every
address of the account."""

import re

from django.conf import settings
from django.core import mail as django_mail
from django.test import SimpleTestCase, TestCase, tag
from django.utils import translation

from leg_platform.accounts import email_texts, emails, mail_queue
from leg_platform.accounts.admin import EMAIL_TEXT_PROCESSES
from leg_platform.accounts.models import EmailText, MailJob
from leg_platform.community import deregistrations, own_iban
from leg_platform.community.tests.onboarding import GOOD_IBAN, customer

Key = EmailText.Key
NEW_KINDS = (
    MailJob.Kind.APPLICATION_SUBMITTED,
    MailJob.Kind.APPLICATION_REMINDER,
    MailJob.Kind.APPLICATION_ROLLED_BACK,
    MailJob.Kind.CONTRACT_REMINDER,
    MailJob.Kind.CONTRACT_ROLLED_BACK,
    MailJob.Kind.BANK_CONNECTION_MISSING,
    MailJob.Kind.DEREGISTRATION_SUBMITTED,
    MailJob.Kind.DEREGISTRATION_EFFECTIVE,
    MailJob.Kind.IBAN_CHANGED,
    MailJob.Kind.OPERATOR_CONFIRMATION_LAPSED,
    MailJob.Kind.APPLICATION_RETURNED,
    MailJob.Kind.APPLICATION_REJECTED,
    MailJob.Kind.APPLICATION_REJECTED_OPERATOR,
)
# Mails to a customer (the one kind for the staff is the exception).
CUSTOMER_KINDS = tuple(kind for kind in NEW_KINDS if kind != MailJob.Kind.OPERATOR_CONFIRMATION_LAPSED)


class DefinitionTests(SimpleTestCase):
    def test_every_kind_has_a_key_a_definition_a_handler_and_is_a_notification(self):
        for kind in NEW_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, Key.values)
                self.assertIn(kind, email_texts.DEFINITIONS)
                self.assertIn(kind, mail_queue.HANDLERS)
                self.assertIn(kind, MailJob.NOTIFICATION_KINDS)
                self.assertIn(email_texts.GREETING, email_texts.placeholders_of(kind))

    def test_every_key_is_in_the_process_list_once(self):
        listed = [key for _name, keys in EMAIL_TEXT_PROCESSES for key in keys]
        for kind in NEW_KINDS:
            self.assertEqual(listed.count(kind), 1, kind)
        self.assertEqual(sorted(listed), sorted(Key.values))

    def test_the_default_texts_hold_the_link_and_no_data(self):
        for kind in NEW_KINDS:
            with self.subTest(kind=kind):
                subject, body = email_texts.default_texts(kind)
                self.assertIn("{instance_name}", subject)
                self.assertIn("{login_url}", body)
                self.assertTrue(body.startswith("{greeting}\n\n"))
                # Nothing that identifies a place, a point or an account.
                self.assertIsNone(re.search(r"\bCH\d|[A-Z]{2}\d{2}[A-Z0-9]{10,}|@", body))
                for required in email_texts.required_placeholders(kind):
                    self.assertIn("{%s}" % required.name, body)


@tag("mariadb")
class SendingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("mail.customer@example.org")

    def sent(self, kind):
        django_mail.outbox.clear()
        mail_queue.enqueue_for_account(kind, self.person)
        mail_queue.process_pending()
        return list(django_mail.outbox)

    def test_each_mail_is_sent_with_the_greeting_and_the_link_and_nothing_else(self):
        for kind in NEW_KINDS:
            with self.subTest(kind=kind), translation.override("de-ch"):
                (message,) = self.sent(kind)
                self.assertEqual(message.to, ["mail.customer@example.org"])
                self.assertEqual(message.extra_headers["Auto-Submitted"], "auto-generated")
                self.assertTrue(message.body.startswith("Guten Tag Anna Example"), message.body[:40])
                self.assertIn(f"{settings.INSTANCE_BASE_URL}/", message.body)
                self.assertEqual(len(re.findall(r"https?://", message.body)), 1)
                self.assertNotIn("\u00df", message.subject + message.body)
                self.assertNotIn("{", message.body)
                self.assertNotIn("%(", message.body)
                # Translated: the German texts differ from the English source.
                self.assertNotIn("Your application", message.body)

    def test_the_link_is_the_login_page_of_the_instance_never_built_from_a_request(self):
        from django.urls import reverse

        (message,) = self.sent(MailJob.Kind.APPLICATION_REMINDER)
        links = re.findall(r"^https?://\S+$", message.body, re.MULTILINE)
        self.assertEqual(links, [emails.absolute_url(reverse("accounts:login"))])
        self.assertTrue(links[0].startswith(settings.INSTANCE_BASE_URL + "/"))

    def test_the_iban_mail_names_neither_the_iban_nor_a_part_of_it(self):
        django_mail.outbox.clear()
        own_iban.set_own_iban(self.person, GOOD_IBAN)  # the first entry sends no mail
        own_iban.set_own_iban(self.person, "DE89370400440532013000")
        mail_queue.process_pending()
        (message,) = django_mail.outbox
        for part in (GOOD_IBAN, "CH93", "0076", "6238", "5295", "DE89", "3704"):
            self.assertNotIn(part, message.subject + message.body)

    def test_the_second_address_gets_every_mail_too(self):
        self.person.set_secondary_email("mail.second@example.org")
        for kind in NEW_KINDS:
            with self.subTest(kind=kind):
                recipients = sorted(message.to[0] for message in self.sent(kind))
                self.assertEqual(recipients, ["mail.customer@example.org", "mail.second@example.org"])

    def test_the_sender_functions_are_the_handlers(self):
        for kind in NEW_KINDS:
            with self.subTest(kind=kind):
                django_mail.outbox.clear()
                getattr(emails, f"send_{kind}")("direct@example.org", "")
                self.assertEqual(len(django_mail.outbox), 1)

    def test_the_deregistration_confirmation_carries_no_building_or_number(self):
        from datetime import date

        from leg_platform.community.models import Direction
        from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point
        from leg_platform.community.tests.onboarding import TODAY

        location = make_location(street="Geheimstrasse", house_number="99", egid=95_001)
        point = make_metering_point(
            1, direction=Direction.CONSUMPTION, location=location, registered_with_operator_on=date(2025, 1, 1),
            confirmed_on=date(2025, 2, 1), start_date=date(2025, 6, 1),
        )
        assign_person(point, self.person, valid_from=date(2025, 6, 1))
        django_mail.outbox.clear()

        deregistrations.request_deregistration(self.person, location_id=location.pk, today=TODAY)
        mail_queue.process_pending()

        (message,) = django_mail.outbox
        for secret in ("Geheimstrasse", point.number):
            self.assertNotIn(secret, message.subject + message.body)


class PlainWordsTests(SimpleTestCase):
    """The customer mails use plain words (design walkthrough W2-03): no "rolled back" jargon, and a contact line."""

    JARGON = ("rolled back", "roll back", "rückabgewickelt", "rückgängig", "rückabwick")

    def test_no_customer_default_text_uses_the_jargon_in_english_or_german(self):
        for language in ("en", "de-ch"):
            for kind in CUSTOMER_KINDS:
                with self.subTest(kind=kind, language=language), self.settings(LANGUAGE_CODE=language):
                    subject, body = email_texts.default_texts(kind)
                    for word in self.JARGON:
                        self.assertNotIn(word, (subject + body).lower())

    def test_the_rejection_texts_say_what_happened_and_what_stays(self):
        with self.settings(LANGUAGE_CODE="en"):
            _subject, plain = email_texts.default_texts(Key.APPLICATION_REJECTED)
            subject, operator = email_texts.default_texts(Key.APPLICATION_REJECTED_OPERATOR)
        self.assertIn("was not accepted", subject)
        self.assertIn("was not accepted", plain)
        self.assertIn("For privacy reasons we deleted your building and metering point details. Your account remains.", plain)
        self.assertIn("You can start a new application at any time", plain)
        self.assertNotIn("grid operator", plain)
        self.assertIn("The grid operator did not accept the registration of your metering points.", operator)

    def test_the_return_text_has_no_reason_and_points_to_the_portal(self):
        with self.settings(LANGUAGE_CODE="en"):
            subject, body = email_texts.default_texts(Key.APPLICATION_RETURNED)
        self.assertIn("returned your application", body)
        self.assertIn("after you sign in", body)
        self.assertIn("{login_url}", body)
        self.assertEqual(email_texts.placeholders_of(Key.APPLICATION_RETURNED).count(email_texts.GREETING), 1)
        # No placeholder can carry the reason: the mail jobs have no payload.
        self.assertEqual(
            {p.name for p in email_texts.DEFINITIONS[Key.APPLICATION_RETURNED].placeholders}, {"greeting"}
        )


@tag("mariadb")
class ContactLineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("mail.contact@example.org")

    def sent(self, kind):
        django_mail.outbox.clear()
        mail_queue.enqueue_for_account(kind, self.person)
        mail_queue.process_pending()
        (message,) = django_mail.outbox
        return message

    def test_every_customer_mail_has_the_contact_line_when_the_instance_has_a_website(self):
        from leg_platform.accounts.models import PortalSettings

        PortalSettings.objects.update_or_create(pk=1, defaults={"website_url": "https://leg.example.org/"})
        for kind in CUSTOMER_KINDS:
            with self.subTest(kind=kind), translation.override("de-ch"):
                body = self.sent(kind).body
                self.assertIn("Kontaktinformationen: https://leg.example.org/", body)
                self.assertEqual(body.count("https://leg.example.org/"), 1)

    def test_without_a_website_the_line_is_left_out(self):
        for kind in CUSTOMER_KINDS:
            with self.subTest(kind=kind), translation.override("de-ch"):
                body = self.sent(kind).body
                self.assertNotIn("Kontaktinformationen", body)
                self.assertNotIn("Contact information", body)

    def test_the_new_mails_say_it_in_german_in_plain_words(self):
        with translation.override("de-ch"):
            rejected = self.sent(MailJob.Kind.APPLICATION_REJECTED)
            operator = self.sent(MailJob.Kind.APPLICATION_REJECTED_OPERATOR)
            returned = self.sent(MailJob.Kind.APPLICATION_RETURNED)
        self.assertIn("wurde nicht angenommen", rejected.subject)
        self.assertIn("Ihr Konto bleibt bestehen", rejected.body)
        self.assertIn("Der Netzbetreiber hat die Registrierung Ihrer Messpunkte nicht angenommen", operator.body)
        self.assertIn("muss korrigiert werden", returned.subject)
        self.assertIn("zur Korrektur zurückgegeben", returned.body)
