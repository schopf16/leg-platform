"""The E-Mail chapter of the administration (D-116): the sender and the contact address are read from
GraphConfiguration. The base address of the portal comes from the environment (INSTANCE_BASE_URL, D-013)."""

from django.core import mail
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from leg_platform.accounts import emails, mail_addresses
from leg_platform.accounts.models import GraphConfiguration
from leg_platform.accounts.models.common import validate_sender_address


class SenderAddressTests(SimpleTestCase):
    def test_one_plain_address_is_accepted(self):
        for value in ("noreply@example.org", "Portal.Sender+x@example.org", ""):
            with self.subTest(value=value):
                validate_sender_address(value)

    def test_other_values_are_refused(self):
        for value in (
            "portal",
            "a@b",
            "a b@example.org",
            "x@example.org, y@example.org",
            "*@example.org",
            "Name <x@example.org>",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_sender_address(value)


class MailAddressesTests(TestCase):
    def store(self, **values):
        GraphConfiguration.objects.update_or_create(pk=1, defaults=values)

    def test_without_the_sender_nothing_is_sent(self):
        self.store(sender_address="", contact_address="")

        with self.assertRaises(ImproperlyConfigured):
            mail_addresses.sender()
        with self.assertRaises(ImproperlyConfigured):
            emails.send_sign_in_changed("person@example.org")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(mail_addresses.contact_recipient(), "")

    def test_the_values_are_read_from_the_e_mail_chapter(self):
        self.store(sender_address="noreply@example.org", contact_address="leg@example.org")

        self.assertEqual(mail_addresses.sender(), "noreply@example.org")
        self.assertEqual(mail_addresses.contact_recipient(), "leg@example.org")
        emails.send_sign_in_changed("person@example.org")
        self.assertEqual(mail.outbox[0].from_email, "noreply@example.org")

    def test_the_sender_does_not_need_the_microsoft_graph_certificate(self):
        # Console mode sends from the same sender without a certificate (D-116).
        self.store(sender_address="noreply@example.org", certificate_pem="", encrypted_private_key="")

        self.assertFalse(GraphConfiguration.load().is_configured)
        self.assertEqual(mail_addresses.sender(), "noreply@example.org")

    @override_settings(INSTANCE_BASE_URL="https://portal.example.org")
    def test_links_are_built_from_the_base_address_of_the_environment(self):
        self.assertEqual(emails.absolute_url("/x/"), "https://portal.example.org/x/")
