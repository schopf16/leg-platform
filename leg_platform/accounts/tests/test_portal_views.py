"""Login, logout, profile and the security of the portal pages (docs/SECURITY.md, section 11)."""

import re

from django.core import mail
from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts.login_protection import LOGIN_FAILURE_LIMIT
from leg_platform.accounts.models import AuthenticatorDevice, User, VerificationToken
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    age_authentication,
    code_for,
    completion_data,
    create_active_user,
    registration_data,
    run_mail_queue,
    verification_path,
)

SHARP_S = "\u00df"
LOGIN_URL = reverse("accounts:login")
# Texts of the pages that must never appear as English source texts (default language de-ch).
ENGLISH_TEXTS = ("Log in", "Register", "Check your email", "Complete your registration")


def login(client, email, password=PASSWORD, address="10.0.0.1", **extra):
    return client.post(
        LOGIN_URL, {"username": email, "password": password}, REMOTE_ADDR=address, **extra
    )


def error_text(response):
    """Return the visible error of the login form."""
    return re.findall(r'<div class="alert alert-danger"[^>]*>(.*?)</div>', response.content.decode())


@tag("mariadb")
class LoginTests(TestCase):
    def setUp(self):
        self.user = create_active_user("active.person@example.org")

    def test_login_works_with_the_email_address_in_any_case(self):
        for number, address in enumerate(
            ("active.person@example.org", " Active.Person@EXAMPLE.org ")
        ):
            with self.subTest(address=address):
                client = Client()
                response = login(client, address, address=f"10.0.1.{number}")
                self.assertRedirects(response, reverse("home"))
                self.assertEqual(client.session["_auth_user_id"], str(self.user.pk))

    def test_the_customer_number_is_not_a_login_identifier(self):
        response = login(self.client, self.user.customer_number_id)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_every_failure_shows_the_same_message(self):
        blocked = create_active_user("blocked.person@example.org")
        blocked.set_status(User.Status.BLOCKED)
        deactivated = create_active_user("former.person@example.org")
        deactivated.set_status(User.Status.DEACTIVATED)
        pending = User.objects.create_user("pending.person@example.org")
        attempts = [
            ("active.person@example.org", "wrong password for this account"),
            ("unknown.person@example.org", PASSWORD),
            ("blocked.person@example.org", PASSWORD),
            ("former.person@example.org", PASSWORD),
            ("pending.person@example.org", PASSWORD),
        ]

        results = []
        for number, (address, password) in enumerate(attempts):
            response = login(Client(), address, password, address=f"10.0.2.{number}")
            results.append((response.status_code, error_text(response)))

        self.assertEqual(len(set(map(str, results))), 1, results)
        self.assertEqual(results[0][0], 200)
        self.assertTrue(results[0][1])

    def test_an_external_redirect_target_is_ignored(self):
        for target in ("https://attacker.example.net/", "//attacker.example.net/", "javascript:alert(1)"):
            with self.subTest(target=target):
                client = Client()
                response = client.post(
                    LOGIN_URL,
                    {"username": self.user.email, "password": PASSWORD, "next": target},
                    REMOTE_ADDR="10.0.3.1",
                )
                self.assertRedirects(response, reverse("home"))

    def test_an_internal_redirect_target_is_followed(self):
        response = self.client.post(
            LOGIN_URL,
            {"username": self.user.email, "password": PASSWORD, "next": "/profile/"},
            REMOTE_ADDR="10.0.3.2",
        )

        self.assertRedirects(response, "/profile/")

    def test_the_session_id_changes_at_login(self):
        session = self.client.session
        session["probe"] = "value"
        session.save()
        before = self.client.session.session_key

        login(self.client, self.user.email)

        self.assertNotEqual(self.client.session.session_key, before)

    def test_the_login_post_needs_a_csrf_token(self):
        response = Client(enforce_csrf_checks=True).post(
            LOGIN_URL, {"username": self.user.email, "password": PASSWORD}
        )

        self.assertEqual(response.status_code, 403)

    def test_a_locked_ip_gets_the_lockout_page_even_with_correct_credentials(self):
        for attempt in range(LOGIN_FAILURE_LIMIT):
            login(Client(), f"person{attempt}@example.org", "wrong password", address="10.0.4.1")

        response = login(Client(), self.user.email, PASSWORD, address="10.0.4.1")

        self.assertEqual(response.status_code, 429)
        self.assertNotIn(self.user.email, response.content.decode())
        self.assertNotIn("_auth_user_id", response.wsgi_request.session)


@tag("mariadb")
class LogoutAndProfileTests(TestCase):
    def setUp(self):
        self.user = create_active_user("active.person@example.org")
        self.other = create_active_user("other.person@example.org", first_name="Berta")

    def test_logout_works_only_by_post_and_ends_the_session(self):
        login(self.client, self.user.email)

        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)
        response = self.client.post(reverse("accounts:logout"))

        self.assertRedirects(response, LOGIN_URL)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertRedirects(
            self.client.get(reverse("accounts:profile")),
            f"{LOGIN_URL}?next=/profile/",
        )

    def test_the_logout_post_needs_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        self.assertEqual(client.post(reverse("accounts:logout")).status_code, 403)

    def test_the_profile_requires_a_login(self):
        response = self.client.get(reverse("accounts:profile"))

        self.assertRedirects(response, f"{LOGIN_URL}?next=/profile/")

    def test_the_profile_shows_only_the_own_account(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("accounts:profile"), {"id": self.other.pk})
        content = response.content.decode()

        self.assertContains(response, self.user.customer_number_display)
        self.assertContains(response, "active.person@example.org")
        self.assertNotContains(response, self.other.customer_number_display)
        self.assertNotContains(response, "other.person@example.org")
        self.assertNotIn("Berta", content)

    def test_the_customer_number_is_shown_in_two_blocks(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("accounts:profile"))

        self.assertRegex(response.content.decode(), r"\b[0-9]{3} [0-9]{3}\b")

    def test_the_root_leads_to_the_login_page_or_shows_the_start_page(self):
        self.assertRedirects(self.client.get("/"), LOGIN_URL, fetch_redirect_response=False)
        self.client.force_login(self.user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/home.html")

    def test_pages_with_personal_data_or_forms_are_not_cached(self):
        self.client.force_login(self.user)
        for name in ("accounts:profile", "accounts:login", "accounts:registration"):
            with self.subTest(name=name):
                self.assertIn("no-store", self.client.get(reverse(name))["Cache-Control"])


@tag("mariadb")
class PageSecurityAndLanguageTests(TestCase):
    """All pages follow the Content Security Policy and the language rules (D-010, D-028)."""

    def pages(self):
        user = create_active_user("active.person@example.org")
        anonymous = Client()
        anonymous.post(
            reverse("accounts:registration"), registration_data("pending.person@example.org")
        )
        run_mail_queue()
        path = verification_path(mail.outbox[-1])
        reference = VerificationToken.objects.get().pk
        pending = Client()
        pending.get(path)
        member = Client()
        member.force_login(user)
        complete_url = reverse("accounts:registration_complete")

        def complete(password="1234567890123456", **overrides):
            data = completion_data(password, registration=reference, **overrides)
            return pending.post(complete_url, data)

        pages = {
            "registration invalid address": anonymous.post(
                reverse("accounts:registration"), {"email": "not-an-email"}
            ),
            "complete invalid values": complete(
                first_name="x" * 40,
                phone_number="123",
                country="XX",
                salutation="invalid",
                postal_code="ABC",
                post_office_box_number="1" * 12,
            ),
            "complete foreign address": complete(country="DE", postal_code=""),
            "complete different passwords": complete(
                password2="paper lantern rests under silver rain", first_name=""
            ),
        }
        for name, password in (
            ("numeric", "1234567890123456"),
            ("short", "kettle orchard"),
            ("common", "1q2w3e4r5t6y7u8i9o0p"),
            ("similar", "pending.person@example.org"),
        ):
            pages[f"complete {name} password"] = complete(password)
        return {
            **pages,
            **self.second_factor_pages(member),
            "login": anonymous.get(LOGIN_URL),
            "password reset": anonymous.get(reverse("accounts:password_reset")),
            "password reset invalid address": anonymous.post(
                reverse("accounts:password_reset"), {"email": "not-an-email"}
            ),
            "password reset sent": anonymous.get(reverse("accounts:password_reset_sent")),
            "password reset invalid link": anonymous.get(
                reverse("accounts:password_reset_verify", args=["x"])
            ),
            "login failed": login(Client(), "unknown.person@example.org", "wrong", address="10.0.5.1"),
            "registration": anonymous.get(reverse("accounts:registration")),
            "registration sent": anonymous.get(reverse("accounts:registration_sent")),
            "complete": pending.get(complete_url),
            "link invalid": anonymous.get(reverse("accounts:registration_verify", args=["x"])),
            "profile": member.get(reverse("accounts:profile")),
            "lockout": self.lockout_response(),
        }
    def second_factor_pages(self, member):
        """Password change and the pages of the second factor (D-040)."""
        holder = create_active_user("holder.person@example.org")
        device = AuthenticatorDevice.objects.create_device(holder, confirmed=True)
        holder_client = Client()
        holder_client.force_login(holder)
        step = Client()
        login(step, holder.email, address="10.0.6.1")
        enrolling = create_active_user("enrolling.person@example.org")
        enrolling_client = Client()
        enrolling_client.force_login(enrolling)
        enrolling_client.post(reverse("accounts:two_factor_setup"), {})
        # A session that logged in just now starts the setup without the password; an older one
        # is asked for it (D-055).
        fresh = Client()
        fresh.force_login(create_active_user("fresh.person@example.org"))
        pages = {
            "two-factor setup without password": fresh.get(reverse("accounts:two_factor_setup")),
            "welcome": fresh.get(reverse("accounts:welcome")),
        }
        age_authentication(member)
        pages |= {
            "password change": member.get(reverse("accounts:password_change")),
            "password change failed": member.post(
                reverse("accounts:password_change"),
                {"current_password": "wrong", "new_password1": "x", "new_password2": "y"},
                REMOTE_ADDR="10.0.6.3",
            ),
            "two-factor off": member.get(reverse("accounts:two_factor")),
            "two-factor setup": member.get(reverse("accounts:two_factor_setup")),
            "two-factor setup failed": member.post(
                reverse("accounts:two_factor_setup"),
                {"current_password": "wrong"},
                REMOTE_ADDR="10.0.6.4",
            ),
            "two-factor on": holder_client.get(reverse("accounts:two_factor")),
            "recovery codes new": holder_client.get(reverse("accounts:recovery_codes")),
            "two-factor disable": holder_client.get(reverse("accounts:two_factor_disable")),
            "second step": step.get(reverse("accounts:login_second_factor")),
            "second step failed": step.post(
                reverse("accounts:login_second_factor"),
                {"otp_token": "000000"},
                REMOTE_ADDR="10.0.6.1",
            ),
            "two-factor confirm": enrolling_client.get(reverse("accounts:two_factor_confirm")),
            "two-factor confirm failed": enrolling_client.post(
                reverse("accounts:two_factor_confirm"), {"otp_token": "000000"}
            ),
        }
        pages["recovery codes"] = holder_client.post(
            reverse("accounts:recovery_codes"),
            {"current_password": PASSWORD, "otp_token": code_for(device)},
            REMOTE_ADDR="10.0.6.5",
        )
        return pages

    def lockout_response(self):
        for attempt in range(LOGIN_FAILURE_LIMIT):
            login(Client(), f"person{attempt}@example.org", "wrong", address="10.0.5.9")
        return login(Client(), "person@example.org", "wrong", address="10.0.5.9")

    @override_settings(
        INSTANCE_PRIVACY_URL="https://www.example.org/privacy",
        INSTANCE_WEBSITE_URL="https://www.example.org/",
    )
    def test_pages_contain_no_inline_script_or_style(self):
        for name, response in self.pages().items():
            html = response.content.decode()
            with self.subTest(name):
                # Only external scripts from the instance itself are allowed (address widget);
                # inline scripts are not (docs/SECURITY.md, section 6).
                self.assertNotRegex(html.lower(), r"<script(?![^>]*\ssrc=)")
                self.assertNotRegex(html.lower(), r"<script[^>]*\ssrc=\"(?!/static/)")
                self.assertNotIn("<style", html.lower())
                self.assertNotRegex(html, r"(?i)\sstyle\s*=")
                self.assertNotRegex(html, r"(?i)\son[a-z]+\s*=")
                self.assertNotIn("javascript:", html.lower())
                self.assertIn("default-src 'self'", response["Content-Security-Policy"])
                self.assertNotIn("'unsafe-inline'", response["Content-Security-Policy"])

    @override_settings(INSTANCE_PRIVACY_URL="https://www.example.org/privacy")
    def test_pages_are_in_swiss_german_without_sharp_s(self):
        for name, response in self.pages().items():
            html = response.content.decode()
            with self.subTest(name):
                self.assertNotIn(SHARP_S, html)
                self.assertIn('lang="de-ch"', html)
                for text in ENGLISH_TEXTS:
                    self.assertNotIn(f">{text}<", html)

    def test_pages_without_login_link_the_privacy_notice(self):
        with override_settings(INSTANCE_PRIVACY_URL="https://www.example.org/privacy"):
            pages = self.pages()
        for name in ("login", "registration", "registration sent", "link invalid", "lockout"):
            with self.subTest(name):
                self.assertContains(
                    pages[name],
                    'href="https://www.example.org/privacy"',
                    status_code=pages[name].status_code,
                )

    def test_a_failed_login_shows_the_generic_message_in_swiss_german(self):
        pages = self.pages()

        self.assertContains(pages["login failed"], "Die E-Mail-Adresse oder das Passwort ist falsch.")

    def test_emails_are_in_swiss_german_without_sharp_s(self):
        client = Client()
        client.post(reverse("accounts:registration"), registration_data())
        create_active_user("taken.person@example.org")
        client.post(reverse("accounts:registration"), registration_data("taken.person@example.org"))
        run_mail_queue()

        for message in mail.outbox:
            with self.subTest(subject=message.subject):
                text = message.subject + message.body
                self.assertNotIn(SHARP_S, text)
                self.assertIn("Guten Tag", message.body)
                self.assertNotIn("Hello", message.body)
