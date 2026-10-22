"""Forms are accepted with the ``Origin`` header that a real browser sends (D-054).

Django's test client sends no ``Origin`` header, so the CSRF check of the other tests never saw
the behaviour of a browser. A browser takes the Referrer Policy of the page that contains the
form: with ``no-referrer`` it sends ``Origin: null`` for the form POST (Fetch standard, "append
a request Origin header"), which Django's CsrfViewMiddleware refuses. These tests submit the
forms with the CSRF token of the page and the ``Origin`` header of the page, as a browser does.
"""

import re
from urllib.parse import urlencode

from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import MailJob, SecurityEvent, User, VerificationToken
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    VerifiedClient,
    completion_data,
    create_active_user,
    password_data,
    registration_data,
    run_mail_queue,
    verification_path,
)
from leg_platform.accounts.tests.test_password_reset import (
    NEW_PASSWORD,
    reset_path,
    request_reset,
)
from leg_platform.accounts.tests.test_second_factor_reset import (
    administrator,
    link_in,
    reset_url,
    with_second_factor,
)

# The origin of the page and of the form POST in the test client (http, host "testserver").
PAGE_ORIGIN = "http://testserver"
LOGIN_URL = reverse("accounts:login")
CSRF_FIELD = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


def browser_client():
    """A client that checks CSRF like the real server does."""
    return Client(enforce_csrf_checks=True)


def form_token(response):
    """The CSRF token that the form of the page carries."""
    match = CSRF_FIELD.search(response.content.decode())
    assert match, "The page contains no CSRF token."
    return match.group(1)


def submit_like_a_browser(client, url, data, origin=PAGE_ORIGIN):
    """Open the page, then post its form with the token and the given Origin header."""
    page = client.get(url)
    assert page.status_code == 200, page.status_code
    return client.post(
        url, {**data, "csrfmiddlewaretoken": form_token(page)}, headers={"origin": origin}
    )


class BrowserOriginMixin:
    """The same checks for every page with a form; subclasses give the page and its data."""

    def assert_the_form_works_like_in_a_browser(self, client, url, data, success_location):
        # A browser sends "null" if the page has the policy "no-referrer": the check refuses it.
        with self.assertLogs("django.security.csrf", "WARNING") as logs:
            refused = submit_like_a_browser(client, url, data, origin="null")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("Origin checking failed - null", logs.output[0])
        self.assertTemplateUsed(refused, "403_csrf.html")
        # The origin of the page itself is what a browser sends for the project default.
        accepted = submit_like_a_browser(client, url, data)
        if success_location is None:
            # The page itself is the result (no redirect), e.g. "Email address changed".
            self.assertEqual(accepted.status_code, 200, accepted.content.decode()[:2000])
            return accepted
        self.assertEqual(accepted.status_code, 302, accepted.content.decode()[:2000])
        self.assertEqual(accepted["Location"], success_location)

    def assert_the_page_does_not_send_no_referrer(self, client, url):
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Referrer-Policy"], "same-origin")


@tag("mariadb")
class LoginOriginTests(BrowserOriginMixin, TestCase):
    def test_the_login_form_is_accepted_with_the_origin_of_a_browser(self):
        create_active_user("active.person@example.org")
        self.assert_the_page_does_not_send_no_referrer(browser_client(), LOGIN_URL)

        self.assert_the_form_works_like_in_a_browser(
            browser_client(),
            LOGIN_URL,
            {"username": "active.person@example.org", "password": PASSWORD},
            reverse("home"),
        )


@tag("mariadb")
class RegistrationCompletionOriginTests(BrowserOriginMixin, TestCase):
    # Completing the registration reads the address directory (advisory check, D-033).
    databases = {"default", "addresses"}

    def setUp(self):
        self.client = browser_client()
        submit_like_a_browser(
            self.client, reverse("accounts:registration"), registration_data("new.person@example.org")
        )
        run_mail_queue()
        self.link = verification_path(mail.outbox[-1])
        self.password_url = reverse("accounts:registration_password")
        self.url = reverse("accounts:registration_complete")

    def test_the_link_sends_no_referrer_and_the_form_pages_do_not(self):
        opened = self.client.get(self.link)

        self.assertEqual(opened.status_code, 302)
        self.assertEqual(opened["Referrer-Policy"], "no-referrer")
        self.assert_the_page_does_not_send_no_referrer(self.client, self.password_url)

    def test_the_address_lookup_of_the_details_step_is_accepted_with_the_origin_of_a_browser(self):
        # D-066: the script posts the lookup from the details step with the token of the page in
        # the header and, in a browser, the origin of the page; "null" would come from a page with
        # the policy "no-referrer" (D-054, R-01) and is refused.
        self.client.get(self.link)
        reference = VerificationToken.objects.get().pk
        submit_like_a_browser(self.client, self.password_url, password_data(registration=reference))
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        token = form_token(page)
        lookup_url = reverse("addresses:lookup")
        body = urlencode({"street": "", "mode": "suggest", "country": "CH"})

        def post_lookup(origin):
            return self.client.post(
                lookup_url, body, content_type="application/x-www-form-urlencoded",
                HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN=origin,
            )

        with self.assertLogs("django.security.csrf", "WARNING") as logs:
            refused = post_lookup("null")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("Origin checking failed - null", logs.output[0])
        accepted = post_lookup(PAGE_ORIGIN)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["status"], "not_checked")

    def test_the_forms_are_accepted_with_the_origin_of_a_browser(self):
        self.client.get(self.link)
        reference = VerificationToken.objects.get().pk

        self.assert_the_form_works_like_in_a_browser(
            self.client, self.password_url, password_data(registration=reference), self.url
        )
        self.assert_the_page_does_not_send_no_referrer(self.client, self.url)
        self.assert_the_form_works_like_in_a_browser(
            self.client, self.url, completion_data(registration=reference), reverse("home")
        )

        self.assertEqual(User.objects.get().status, User.Status.ACTIVE)
        # The person is logged in by the completion (owner decision, D-055).
        self.assertIn("_auth_user_id", self.client.session)


@tag("mariadb")
class PasswordResetOriginTests(BrowserOriginMixin, TestCase):
    def setUp(self):
        self.user = create_active_user("active.person@example.org")
        request_reset(self.user.email)
        self.link = reset_path(mail.outbox[-1])
        self.url = reverse("accounts:password_reset_set")
        self.client = browser_client()

    def test_the_link_sends_no_referrer_and_the_form_page_does_not(self):
        opened = self.client.get(self.link)

        self.assertEqual(opened["Referrer-Policy"], "no-referrer")
        self.assert_the_page_does_not_send_no_referrer(self.client, self.url)

    def test_the_form_is_accepted_with_the_origin_of_a_browser(self):
        self.client.get(self.link)
        data = {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}

        self.assert_the_form_works_like_in_a_browser(
            self.client, self.url, data, reverse("accounts:login")
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))


@tag("mariadb")
class EmailChangeOriginTests(BrowserOriginMixin, TestCase):
    def setUp(self):
        self.user = create_active_user("active.person@example.org")
        member = VerifiedClient()
        member.force_login(self.user)
        member.post(
            reverse("accounts:email_change_request"),
            {"email": "new.address@example.org", "secondary_email": "", "current_password": PASSWORD},
        )
        run_mail_queue()
        message = next(m for m in mail.outbox if m.to == ["new.address@example.org"])
        match = re.search(r"https?://[^/]+(/email-change/verify/[^/\s]+/)", message.body)
        self.link = match.group(1)
        self.url = reverse("accounts:email_change_confirm")
        self.client = browser_client()

    def test_the_link_sends_no_referrer_and_the_form_page_does_not(self):
        opened = self.client.get(self.link)

        self.assertEqual(opened["Referrer-Policy"], "no-referrer")
        self.assert_the_page_does_not_send_no_referrer(self.client, self.url)

    def test_the_form_is_accepted_with_the_origin_of_a_browser(self):
        self.client.get(self.link)

        accepted = self.assert_the_form_works_like_in_a_browser(self.client, self.url, {}, None)

        self.assertContains(accepted, "E-Mail-Adresse geändert")
        self.assertContains(accepted, "new.address@example.org")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new.address@example.org")


@tag("mariadb")
class TwoFactorResetOriginTests(BrowserOriginMixin, TestCase):
    def setUp(self):
        admin = administrator()
        self.user = create_active_user("member@example.org")
        with_second_factor(self.user)
        starter = VerifiedClient()
        starter.force_login(admin)
        refused = starter.post(reset_url(self.user), {})
        self.assertEqual(refused.status_code, 200)
        self.assertIn("identity_verified", refused.context["form"].errors)
        requested = starter.post(reset_url(self.user), {"identity_verified": "on"})
        self.assertEqual(requested.status_code, 302)
        run_mail_queue()
        self.link = link_in(mail.outbox[-1])
        self.url = reverse("accounts:two_factor_reset_confirm")
        self.client = browser_client()

    def test_the_link_sends_no_referrer_and_the_form_page_does_not(self):
        opened = self.client.get(self.link)

        self.assertEqual(opened["Referrer-Policy"], "no-referrer")
        self.assert_the_page_does_not_send_no_referrer(self.client, self.url)

    def test_the_form_is_accepted_with_the_origin_of_a_browser(self):
        self.client.get(self.link)

        self.assert_the_form_works_like_in_a_browser(
            self.client, self.url, {}, reverse("accounts:login_method")
        )

        self.assertTrue(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.TWO_FACTOR_RESET).exists()
        )
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_RESET).exists())
