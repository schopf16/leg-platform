"""Pages without a referrer carry no form that would be refused, and key pages are not cached.

Security review of 2026-10-14 (findings 1 and 5): a logged-in person who opens an invalid link
gets a page with ``Referrer-Policy: no-referrer``; a form on it would post ``Origin: null`` and be
refused by the CSRF check, so such a page has no logout or colour-mode form.
"""

from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.tests.helpers import create_active_user

INVALID_TOKEN = "x" * 43
TOKEN_VERIFY_VIEWS = (
    "accounts:registration_verify",
    "accounts:password_reset_verify",
    "accounts:email_change_verify",
    "accounts:two_factor_reset_verify",
)


@tag("mariadb")
class InvalidLinkPageFormsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(create_active_user())

    def test_an_invalid_link_page_sends_no_referrer_and_has_no_logout_or_colour_form(self):
        for name in TOKEN_VERIFY_VIEWS:
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=[INVALID_TOKEN]))

                self.assertEqual(response["Referrer-Policy"], "no-referrer")
                html = response.content.decode()
                self.assertNotIn(reverse("accounts:logout"), html)
                self.assertNotIn(reverse("colour_mode"), html)

    def test_the_normal_pages_keep_the_logout_and_the_colour_mode_forms(self):
        response = self.client.get(reverse("accounts:profile"))

        self.assertEqual(response["Referrer-Policy"], "same-origin")
        html = response.content.decode()
        self.assertIn(reverse("accounts:logout"), html)
        self.assertIn(reverse("colour_mode"), html)


@tag("mariadb")
class SetupPagesAreNotCachedTests(TestCase):
    def test_the_welcome_and_two_factor_pages_are_not_stored(self):
        client = Client()
        client.force_login(create_active_user())

        for name in ("accounts:welcome", "accounts:two_factor", "accounts:two_factor_setup"):
            with self.subTest(view=name):
                response = client.get(reverse(name))

                self.assertIn(response.status_code, {200, 302})
                self.assertIn("no-store", response.get("Cache-Control", ""))
