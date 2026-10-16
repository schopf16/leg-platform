"""Links of emails render no page under the token URL, and key pages are not cached.

Security review of 2026-10-14 (findings 1 and 5): a logged-in person who opened an invalid link got
a page with ``Referrer-Policy: no-referrer`` under the token URL, and a form on it would post
``Origin: null`` and be refused by the CSRF check. Since D-067 every link of an email redirects
to a fixed page (no token in the URL, project default policy), so no such page exists any more.
(The colour-mode form of the footer is gone since D-056: the colour mode is chosen in the browser
and posts nothing.)
"""

from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.tests.helpers import create_active_user

INVALID_TOKEN = "x" * 43
TOKEN_VERIFY_VIEWS = (
    "accounts:registration_verify",
    "accounts:password_reset_verify",
    "accounts:email_change_verify",
    "accounts:email_change_stop_verify",
    "accounts:two_factor_reset_verify",
)


@tag("mariadb")
class InvalidLinkPageFormsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(create_active_user())

    def test_an_invalid_link_redirects_with_no_referrer_and_renders_no_page(self):
        for name in TOKEN_VERIFY_VIEWS:
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=[INVALID_TOKEN]))

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Referrer-Policy"], "no-referrer")
                self.assertEqual(response.content, b"")
                self.assertNotIn(INVALID_TOKEN, response["Location"])

    def test_the_pages_that_the_links_lead_to_have_no_logout_form_and_no_colour_mode_form(self):
        for name in TOKEN_VERIFY_VIEWS:
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=[INVALID_TOKEN]), follow=True)

                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertNotIn(reverse("accounts:logout"), html)
                # No page posts a colour-mode choice any more.
                self.assertNotIn("/appearance/", html)
                self.assertNotIn('name="mode"', html)

    def test_the_normal_pages_keep_the_logout_form(self):
        response = self.client.get(reverse("accounts:profile"))

        self.assertEqual(response["Referrer-Policy"], "same-origin")
        html = response.content.decode()
        self.assertIn(reverse("accounts:logout"), html)
        self.assertNotIn("/appearance/", html)


@tag("mariadb")
class SetupPagesAreNotCachedTests(TestCase):
    def test_the_start_and_two_factor_pages_are_not_stored(self):
        client = Client()
        client.force_login(create_active_user())

        for name in ("home", "accounts:two_factor", "accounts:two_factor_setup"):
            with self.subTest(view=name):
                response = client.get(reverse(name))

                self.assertIn(response.status_code, {200, 302})
                self.assertIn("no-store", response.get("Cache-Control", ""))
