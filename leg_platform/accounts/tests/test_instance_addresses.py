"""The addresses of the instance in the portal settings (D-116): the address of the portal for links, the sender of every
email and the address of the LEG for the messages of the contact form."""

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from leg_platform.accounts import emails
from leg_platform.accounts.models import PortalSettings
from leg_platform.accounts.models.common import validate_sender_address
from leg_platform.accounts.models.portal import validate_instance_origin

PRODUCTION = {"APP_ENV": "production", "ALLOWED_HOSTS": ["portal.example.org"]}
LOCAL = {"APP_ENV": "local", "ALLOWED_HOSTS": ["127.0.0.1", "localhost"]}


@override_settings(**PRODUCTION)
class OriginTests(SimpleTestCase):
    def test_an_https_origin_of_an_allowed_host_is_accepted(self):
        for value in ("https://portal.example.org", "https://portal.example.org/", ""):
            with self.subTest(value=value):
                validate_instance_origin(value)

    def test_other_forms_are_refused(self):
        for value in (
            "http://portal.example.org",
            "https://portal.example.org/path",
            "https://portal.example.org/?query=1",
            "https://user:secret@portal.example.org",
            "https://evil.example.org",
            "https://portal.example.org:99999",
            "javascript:alert(1)",
            "portal.example.org",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_instance_origin(value)


@override_settings(**LOCAL)
class LocalOriginTests(SimpleTestCase):
    def test_local_development_allows_the_http_origin_of_the_development_server(self):
        validate_instance_origin("http://127.0.0.1:8000")

    def test_local_development_still_needs_an_allowed_host(self):
        with self.assertRaises(ValidationError):
            validate_instance_origin("http://evil.example.org:8000")


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


class PortalAddressesTests(TestCase):
    def test_without_the_address_of_the_portal_and_the_sender_nothing_is_sent(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"base_url": "", "sender_address": ""})

        with self.assertRaises(ImproperlyConfigured):
            PortalSettings.instance_origin()
        with self.assertRaises(ImproperlyConfigured):
            emails.absolute_url("/register/verify/x/")
        with self.assertRaises(ImproperlyConfigured):
            PortalSettings.sender()
        self.assertEqual(PortalSettings.contact_recipient(), "")

    def test_the_values_are_read_from_the_portal_settings(self):
        PortalSettings.objects.update_or_create(
            pk=1,
            defaults={
                "base_url": "https://portal.example.org",
                "sender_address": "noreply@example.org",
                "contact_address": "leg@example.org",
            },
        )

        self.assertEqual(PortalSettings.instance_origin(), "https://portal.example.org")
        self.assertEqual(emails.absolute_url("/x/"), "https://portal.example.org/x/")
        self.assertEqual(PortalSettings.sender(), "noreply@example.org")
        self.assertEqual(PortalSettings.contact_recipient(), "leg@example.org")
