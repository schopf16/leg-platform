"""The profile in cards, the second address and the second factor inline (D-059).

The negative tests come first in each class: a person without a login, without the CSRF token or
with the wrong password gets nothing, and an address that belongs to another account gets the
same answer as a free one.
"""

import re
from datetime import timedelta

from django.core import mail
from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import bot_check, second_factor
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    MailJob,
    PendingEmailChange,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    VerifiedClient,
    age_authentication,
    code_for,
    create_active_user,
    reset_request_data,
    run_mail_queue,
)
from leg_platform.accounts.tests.test_second_factor import (
    RECOVERY_CODES,
    codes_in,
    create_user_with_totp,
)

PROFILE_URL = reverse("accounts:profile")
LOGIN_URL = reverse("accounts:login")
CARD_ACTIONS = (
    "profile_edit",
    "email_change_request",
    "account_delete",
    "password_change",
    "two_factor_setup",
    "two_factor_confirm",
    "recovery_codes",
    "recovery_codes_done",
    "two_factor_disable",
)
SECOND = "second.address@example.org"
NEW = "new.address@example.org"


def link_in(message, fragment):
    """The path of the link of a mail that contains ``fragment`` (checked to be on the base URL)."""
    match = re.search(rf"^(https?://\S+/{fragment}/\S+)$", message.body, re.MULTILINE)
    assert match, f"The mail has no link with {fragment}."
    return re.sub(r"^https?://[^/]+", "", match.group(1))


def mails_to(address):
    return [message for message in mail.outbox if address in message.to]


@tag("mariadb")
class CardPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def test_the_actions_need_a_login(self):
        anonymous = Client()
        for name in ("profile", *CARD_ACTIONS):
            with self.subTest(name=name):
                url = reverse(f"accounts:{name}")
                self.assertRedirects(
                    anonymous.get(url), f"{LOGIN_URL}?next={url}", fetch_redirect_response=False
                )
                self.assertRedirects(
                    anonymous.post(url, {}), f"{LOGIN_URL}?next={url}", fetch_redirect_response=False
                )

    def test_the_posts_need_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        urls = [reverse(f"accounts:{name}") for name in CARD_ACTIONS]
        urls.append(reverse("accounts:email_change_cancel", args=["login"]))
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(client.post(url, {}).status_code, 403)

    def test_the_profile_shows_the_five_cards_and_no_form(self):
        page = self.client.get(PROFILE_URL)

        self.assertContains(page, "Mein Konto")
        for card in ("card-personal", "card-email", "card-password", "card-two-factor", "card-delete"):
            self.assertContains(page, f'id="{card}"')
        self.assertNotContains(page, 'name="current_password"')
        self.assertNotContains(page, 'name="street"')
        self.assertNotContains(page, "Notfallcode")

    def test_a_card_opens_in_place_and_only_one_at_a_time(self):
        for edit, field in (
            ("personal", 'name="street"'),
            ("email", 'name="secondary_email"'),
            ("password", 'name="new_password1"'),
            ("delete", 'name="confirm"'),
        ):
            with self.subTest(edit=edit):
                page = self.client.get(f"{PROFILE_URL}?edit={edit}")
                self.assertContains(page, field)
                # No other card has its form open.
                opened = [
                    name
                    for name in ('name="street"', 'name="secondary_email"', 'name="new_password1"', 'name="confirm"')
                    if name in page.content.decode()
                ]
                self.assertEqual(opened, [field])

    def test_an_unknown_state_shows_the_cards_only(self):
        page = self.client.get(f"{PROFILE_URL}?edit=nothing&step=whatever")

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="current_password"')

    def test_every_page_of_the_profile_is_not_stored(self):
        urls = [
            PROFILE_URL,
            f"{PROFILE_URL}?edit=email",
            reverse("accounts:profile_edit"),
            reverse("accounts:password_change"),
            reverse("accounts:two_factor"),
            reverse("accounts:two_factor_setup"),
            reverse("accounts:account_delete"),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertIn("no-store", self.client.get(url)["Cache-Control"])

    def test_the_old_pages_show_the_card_instead(self):
        for name, expected in (
            ("profile_edit", 'name="street"'),
            ("email_change_request", 'name="secondary_email"'),
            ("password_change", 'name="new_password1"'),
            ("account_delete", 'name="confirm"'),
        ):
            with self.subTest(name=name):
                self.assertContains(self.client.get(reverse(f"accounts:{name}")), expected)


@tag("mariadb")
class PersonalCardTests(TestCase):
    # Saving reads the address directory (advisory check, D-033).
    databases = {"default", "addresses", "address_import"}

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def test_the_details_are_saved_and_the_card_is_shown_again(self):
        response = self.client.post(
            reverse("accounts:profile_edit"),
            {
                "first_name": "Berta",
                "last_name": "Muster",
                "street": "Neue Strasse",
                "house_number": "3",
                "postal_code": "3063",
                "town": "Ittigen",
                "country": "CH",
                "salutation": "mrs",
                "phone_number": "031 000 00 00",
                "organisation_name": "Muster AG",
                "address_line_1": "c/o Anna",
            },
        )

        self.assertRedirects(response, f"{PROFILE_URL}#card-personal", fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.town), ("Berta", "Ittigen"))
        self.assertEqual(self.user.organisation_name, "Muster AG")
        self.assertEqual(self.user.address_line_1, "c/o Anna")

    def test_an_invalid_form_keeps_the_card_open_and_saves_nothing(self):
        response = self.client.post(
            reverse("accounts:profile_edit"),
            {"first_name": "", "last_name": "", "street": "Neue Strasse", "town": "", "country": "CH"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="street"')
        self.user.refresh_from_db()
        self.assertEqual(self.user.street, "Examplestrasse")

    def test_the_further_details_are_under_their_own_heading(self):
        page = self.client.get(f"{PROFILE_URL}?edit=personal")

        self.assertContains(page, "Weitere Angaben (optional)")
        for name in ("salutation", "organisation_name", "address_line_1", "post_office_box_number", "phone_number"):
            self.assertContains(page, f'name="{name}"')


@tag("mariadb")
class EmailCardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("accounts:email_change_request")

    def request(self, email=NEW, secondary="", password=PASSWORD, **extra):
        return self.client.post(
            self.url,
            {"email": email, "secondary_email": secondary, "current_password": password, **extra},
            REMOTE_ADDR="10.60.0.1",
        )

    def test_a_wrong_password_changes_nothing(self):
        response = self.request(password=OTHER_PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Das Passwort stimmt nicht. Bitte versuchen Sie es erneut.")
        self.assertFalse(PendingEmailChange.objects.exists())
        self.assertFalse(MailJob.objects.exists())

    def test_nothing_changed_is_said(self):
        response = self.request(email=self.user.email)

        self.assertContains(response, "Es wurde nichts geändert.")
        self.assertFalse(PendingEmailChange.objects.exists())

    def test_the_second_address_must_differ_from_the_login_address(self):
        response = self.request(email=self.user.email, secondary=self.user.email)

        self.assertContains(response, "Die zweite Adresse muss sich von der Adresse für die Anmeldung unterscheiden.")
        self.assertFalse(PendingEmailChange.objects.exists())

    def test_a_request_is_pending_and_the_old_address_stays_valid(self):
        response = self.request()

        self.assertRedirects(response, f"{PROFILE_URL}#card-email", fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "holder.person@example.org")
        pending = PendingEmailChange.objects.get()
        self.assertEqual((pending.kind, pending.address), ("login", NEW))
        self.assertAlmostEqual(
            pending.expires_at, timezone.now() + timedelta(hours=24), delta=timedelta(minutes=1)
        )
        page = self.client.get(PROFILE_URL)
        self.assertContains(page, NEW)
        self.assertContains(page, "Ausstehend")
        self.assertContains(page, "Anfrage abbrechen")

    def test_the_new_address_gets_the_link_and_the_old_address_gets_the_notice_with_the_stop_link(self):
        self.request()
        run_mail_queue()

        link_mail = mails_to(NEW)
        notice = mails_to("holder.person@example.org")
        self.assertEqual(len(link_mail), 1)
        self.assertIn("/email-change/verify/", link_mail[0].body)
        self.assertEqual(len(notice), 1)
        self.assertIn("/email-change/stop/", notice[0].body)
        self.assertIn("Haben Sie diese Änderung nicht veranlasst?", notice[0].body)
        self.assertNotIn("/email-change/verify/", notice[0].body)

    def test_an_address_of_another_account_gets_the_same_answer_as_a_free_one(self):
        create_active_user("taken.person@example.org")
        free = self.request(email=NEW)
        PendingEmailChange.objects.all().delete()
        MailJob.objects.all().delete()
        taken = self.request(email="taken.person@example.org")

        self.assertEqual((free.status_code, free["Location"]), (taken.status_code, taken["Location"]))
        pending = PendingEmailChange.objects.get()
        self.assertEqual(pending.address, "taken.person@example.org")
        mail.outbox.clear()
        run_mail_queue()
        # The other account is told that somebody used its address; it gets no confirmation link.
        for message in mails_to("taken.person@example.org"):
            self.assertNotIn("/email-change/verify/", message.body)
        self.assertFalse(
            VerificationToken.objects.filter(purpose=VerificationToken.Purpose.EMAIL_CHANGE).exists()
        )

    def test_the_link_changes_the_address_and_ends_every_session(self):
        other = Client()
        other.force_login(self.user)
        self.request()
        run_mail_queue()
        path = link_in(mails_to(NEW)[0], "email-change/verify")

        opened = Client().get(path)
        confirm_client = Client()
        confirm_client.get(path)
        done = confirm_client.post(reverse("accounts:email_change_confirm"), {})

        self.assertRedirects(opened, reverse("accounts:email_change_confirm"), fetch_redirect_response=False)
        self.assertContains(done, "E-Mail-Adresse geändert")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW)
        self.assertFalse(PendingEmailChange.objects.exists())
        for client in (self.client, other):
            self.assertRedirects(
                client.get(PROFILE_URL), f"{LOGIN_URL}?next={PROFILE_URL}", fetch_redirect_response=False
            )
        mail.outbox.clear()
        run_mail_queue()
        self.assertEqual(len(mails_to("holder.person@example.org")), 1)

    def test_opening_the_link_changes_nothing_and_a_reload_of_the_result_is_neutral(self):
        self.request()
        run_mail_queue()
        path = link_in(mails_to(NEW)[0], "email-change/verify")
        client = Client()

        client.get(path)

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "holder.person@example.org")
        client.post(reverse("accounts:email_change_confirm"), {})
        again = client.post(reverse("accounts:email_change_confirm"), {}, follow=True)
        self.assertContains(
            again,
            "Dieser Link kann nicht mehr verwendet werden. Falls Sie die Änderung soeben bestätigt "
            "haben, melden Sie sich mit der neuen Adresse an.",
        )
        self.assertNotContains(again, "wurde nicht geändert")

    def test_the_request_can_be_cancelled_and_the_link_is_dead_afterwards(self):
        self.request()
        run_mail_queue()
        path = link_in(mails_to(NEW)[0], "email-change/verify")

        response = self.client.post(reverse("accounts:email_change_cancel", args=["login"]))

        self.assertRedirects(response, f"{PROFILE_URL}#card-email", fetch_redirect_response=False)
        self.assertFalse(PendingEmailChange.objects.exists())
        client = Client()
        client.get(path)
        client.post(reverse("accounts:email_change_confirm"), {})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "holder.person@example.org")

    def test_an_unknown_kind_is_a_404_and_only_the_own_requests_are_touched(self):
        other = create_active_user("other.person@example.org")
        PendingEmailChange.objects.create(
            user=other, kind="login", address="x@example.org", expires_at=timezone.now() + timedelta(hours=1)
        )

        self.assertEqual(
            self.client.post(reverse("accounts:email_change_cancel", args=["whatever"])).status_code, 404
        )
        self.client.post(reverse("accounts:email_change_cancel", args=["login"]))

        self.assertTrue(PendingEmailChange.objects.filter(user=other).exists())

    def test_a_new_request_replaces_the_pending_one(self):
        self.request(email="first.address@example.org")
        self.request(email="second.try@example.org")

        self.assertEqual(PendingEmailChange.objects.get().address, "second.try@example.org")

    def test_the_form_needs_the_code_where_a_second_factor_is_set_up(self):
        user, device = create_user_with_totp("coded.person@example.org")
        client = Client()
        client.force_login(user)

        without = client.post(
            self.url, {"email": NEW, "secondary_email": "", "current_password": PASSWORD}, REMOTE_ADDR="10.60.0.2"
        )
        with_code = client.post(
            self.url,
            {"email": NEW, "secondary_email": "", "current_password": PASSWORD, "otp_token": code_for(device)},
            REMOTE_ADDR="10.60.0.2",
        )

        self.assertEqual(without.status_code, 200)
        self.assertEqual(with_code.status_code, 302)
        self.assertEqual(PendingEmailChange.objects.get().user, user)

    def test_an_exhausted_client_limit_refuses_the_request_and_stores_nothing(self):
        from unittest.mock import patch

        with patch("leg_platform.accounts.profile_views.requests_refused", return_value=True):
            response = self.request()

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Zu viele Anfragen, bitte versuchen Sie es später erneut.", status_code=429)
        self.assertFalse(PendingEmailChange.objects.exists())
        self.assertFalse(MailJob.objects.exists())


@tag("mariadb")
class SecondAddressTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def add_second(self):
        self.client.post(
            reverse("accounts:email_change_request"),
            {"email": self.user.email, "secondary_email": SECOND, "current_password": PASSWORD},
            REMOTE_ADDR="10.61.0.1",
        )
        run_mail_queue()
        path = link_in(mails_to(SECOND)[0], "email-change/verify")
        client = Client()
        client.get(path)
        done = client.post(reverse("accounts:email_change_confirm"), {})
        # The jobs that the confirmation made (the notice of the change) are sent and forgotten.
        run_mail_queue()
        return done

    def test_the_second_address_is_stored_only_after_its_confirmation(self):
        self.client.post(
            reverse("accounts:email_change_request"),
            {"email": self.user.email, "secondary_email": SECOND, "current_password": PASSWORD},
            REMOTE_ADDR="10.61.0.1",
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.secondary_email, "")
        self.assertEqual(PendingEmailChange.objects.get().kind, "secondary")

        run_mail_queue()
        client = Client()
        client.get(link_in(mails_to(SECOND)[0], "email-change/verify"))
        done = client.post(reverse("accounts:email_change_confirm"), {})

        self.assertContains(done, "Zweite Adresse bestätigt")
        self.user.refresh_from_db()
        self.assertEqual(self.user.secondary_email, SECOND)
        self.assertEqual(self.user.email, "holder.person@example.org")
        # A second address leaves the sessions alone.
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)

    def test_the_second_address_never_logs_in_and_never_receives_a_reset(self):
        self.add_second()
        mail.outbox.clear()

        login = Client().post(
            LOGIN_URL, {"username": SECOND, "password": PASSWORD}, REMOTE_ADDR="10.61.0.2"
        )
        Client().post(
            reverse("accounts:password_reset"), reset_request_data(SECOND), REMOTE_ADDR="10.61.0.3"
        )
        run_mail_queue()

        self.assertEqual(login.status_code, 200)
        self.assertNotIn("_auth_user_id", login.wsgi_request.session)
        self.assertEqual(mails_to(SECOND), [])
        self.assertEqual(
            User.objects.get(pk=self.user.pk).email, "holder.person@example.org"
        )

    def test_an_address_of_another_account_cannot_be_registered_with_the_second_address(self):
        self.add_second()

        register = Client().post(
            reverse("accounts:registration"),
            {"email": SECOND, "form_started": bot_check.new_stamp()},
            REMOTE_ADDR="10.61.0.4",
        )
        run_mail_queue()

        # The second address is no login address of anybody: it can register an account of its own.
        self.assertEqual(register.status_code, 302)
        self.assertTrue(User.objects.filter(email=SECOND).exists())

    def test_every_notification_goes_to_both_addresses(self):
        self.add_second()
        mail.outbox.clear()

        response = self.client.post(
            reverse("accounts:password_change"),
            {"current_password": PASSWORD, "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
        )
        run_mail_queue()

        self.assertEqual(response.status_code, 302)
        recipients = sorted(address for message in mail.outbox for address in message.to)
        self.assertEqual(recipients, sorted(["holder.person@example.org", SECOND]))

    def test_the_second_address_can_be_removed_with_the_password_and_both_are_told(self):
        self.add_second()
        mail.outbox.clear()

        wrong = self.client.post(
            reverse("accounts:email_change_request"),
            {"email": self.user.email, "secondary_email": "", "current_password": OTHER_PASSWORD},
        )
        removed = self.client.post(
            reverse("accounts:email_change_request"),
            {"email": self.user.email, "secondary_email": "", "current_password": PASSWORD},
        )
        run_mail_queue()

        self.assertEqual(wrong.status_code, 200)
        self.assertEqual(removed.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.secondary_email, "")
        self.assertEqual(len(mails_to(SECOND)), 1)
        self.assertEqual(len(mails_to("holder.person@example.org")), 1)
        self.assertTrue(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.SECONDARY_EMAIL_CHANGED).exists()
        )

    def test_the_notice_of_a_request_goes_to_the_second_address_too(self):
        self.add_second()
        mail.outbox.clear()

        self.client.post(
            reverse("accounts:email_change_request"),
            {"email": NEW, "secondary_email": SECOND, "current_password": PASSWORD},
            REMOTE_ADDR="10.61.0.9",
        )
        run_mail_queue()

        for address in ("holder.person@example.org", SECOND):
            notices = [m for m in mails_to(address) if "/email-change/stop/" in m.body]
            self.assertEqual(len(notices), 1, address)


@tag("mariadb")
class StopLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse("accounts:email_change_request"),
            {"email": NEW, "secondary_email": "", "current_password": PASSWORD},
            REMOTE_ADDR="10.62.0.1",
        )
        run_mail_queue()
        self.stop_path = link_in(mails_to("holder.person@example.org")[0], "email-change/stop")
        self.confirm_path = link_in(mails_to(NEW)[0], "email-change/verify")

    def test_opening_the_link_stops_nothing(self):
        anonymous = Client()

        opened = anonymous.get(self.stop_path)
        page = anonymous.get(reverse("accounts:email_change_stop_confirm"))

        self.assertRedirects(opened, reverse("accounts:email_change_stop_confirm"), fetch_redirect_response=False)
        self.assertContains(page, "stoppen")
        self.assertTrue(PendingEmailChange.objects.exists())
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)

    def test_the_button_stops_the_change_without_a_login_and_ends_the_sessions(self):
        anonymous = Client()
        anonymous.get(self.stop_path)

        done = anonymous.post(reverse("accounts:email_change_stop_confirm"), {})

        self.assertContains(done, "Änderung gestoppt")
        self.assertFalse(PendingEmailChange.objects.exists())
        self.assertFalse(
            VerificationToken.objects.filter(
                purpose__in=[
                    VerificationToken.Purpose.EMAIL_CHANGE,
                    VerificationToken.Purpose.SECONDARY_EMAIL,
                    VerificationToken.Purpose.EMAIL_CHANGE_STOP,
                ]
            ).exists()
        )
        self.assertRedirects(
            self.client.get(PROFILE_URL), f"{LOGIN_URL}?next={PROFILE_URL}", fetch_redirect_response=False
        )
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.EMAIL_CHANGE_STOPPED).count(), 1
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "holder.person@example.org")

    def test_the_confirmation_link_is_dead_after_the_stop(self):
        stopper = Client()
        stopper.get(self.stop_path)
        stopper.post(reverse("accounts:email_change_stop_confirm"), {})

        client = Client()
        client.get(self.confirm_path)
        client.post(reverse("accounts:email_change_confirm"), {})

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "holder.person@example.org")

    def test_the_link_works_once_and_a_forged_link_is_invalid(self):
        stopper = Client()
        stopper.get(self.stop_path)
        stopper.post(reverse("accounts:email_change_stop_confirm"), {})

        again = Client().get(self.stop_path, follow=True)
        forged = Client().get(reverse("accounts:email_change_stop_verify", args=["x" * 43]), follow=True)
        direct = Client().post(reverse("accounts:email_change_stop_confirm"), {}, follow=True)

        for response in (again, forged, direct):
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Link nicht mehr gültig")

    def test_the_stop_needs_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.get(self.stop_path)

        response = client.post(reverse("accounts:email_change_stop_confirm"), {})

        self.assertEqual(response.status_code, 403)
        self.assertTrue(PendingEmailChange.objects.exists())

    def test_the_stop_link_answers_with_no_referrer_and_is_not_stored(self):
        response = Client().get(self.stop_path)

        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response["Cache-Control"])


@tag("mariadb")
class PasswordCardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("accounts:password_change")

    def test_a_wrong_current_password_changes_nothing(self):
        response = self.client.post(
            self.url,
            {"current_password": OTHER_PASSWORD, "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Das Passwort stimmt nicht. Bitte versuchen Sie es erneut.")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_the_password_is_changed_and_other_sessions_end(self):
        other = Client()
        other.force_login(self.user)

        response = self.client.post(
            self.url,
            {"current_password": PASSWORD, "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
        )

        self.assertRedirects(response, f"{PROFILE_URL}#card-password", fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OTHER_PASSWORD))
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)
        self.assertEqual(other.get(PROFILE_URL).status_code, 302)

    def test_the_rules_are_listed_without_an_example_password(self):
        page = self.client.get(f"{PROFILE_URL}?edit=password")

        self.assertContains(page, 'class="password-rules"')
        self.assertContains(page, "Passwortregeln")


@tag("mariadb")
class TwoFactorCardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")
        cls.user.set_secondary_email(SECOND)

    def setUp(self):
        self.client.force_login(self.user)
        age_authentication(self.client)

    def start(self, password=PASSWORD):
        return self.client.post(
            reverse("accounts:two_factor_setup"), {"current_password": password}, REMOTE_ADDR="10.63.0.1"
        )

    def enable(self):
        self.start()
        device = AuthenticatorDevice.objects.get()
        return self.client.post(reverse("accounts:two_factor_confirm"), {"otp_token": code_for(device)})

    def test_the_card_offers_the_setup_when_nothing_is_set_up(self):
        page = self.client.get(PROFILE_URL)

        self.assertContains(page, "Nicht eingerichtet")
        self.assertContains(page, "2FA einrichten")

    def test_the_setup_has_two_steps_with_the_key_and_the_qr_code(self):
        self.assertEqual(self.start("wrong password for this account").status_code, 200)
        self.assertFalse(AuthenticatorDevice.objects.exists())

        response = self.start()
        step = self.client.get(f"{PROFILE_URL}?edit=two_factor&step=app")

        self.assertRedirects(response, reverse("accounts:two_factor_confirm"))
        self.assertContains(step, reverse("accounts:two_factor_qr"))
        self.assertNotContains(step, "data:image")
        self.assertContains(step, "Schritt 1 von 2")

    def test_the_codes_are_shown_once_in_the_answer_and_both_addresses_are_told(self):
        response = self.enable()
        run_mail_queue()

        codes = codes_in(response)
        self.assertEqual(len(codes), 10)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, 'name="saved"')
        reload = self.client.get(PROFILE_URL)
        for code in codes:
            self.assertNotContains(reload, f"{code[:4]}-{code[4:]}")
        self.assertEqual(
            sorted(address for message in mail.outbox for address in message.to),
            sorted(["holder.person@example.org", SECOND]),
        )
        self.assertTrue(second_factor.has_second_factor(self.user))

    def test_a_wrong_code_does_not_turn_it_on(self):
        self.start()

        response = self.client.post(reverse("accounts:two_factor_confirm"), {"otp_token": "000000"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(second_factor.has_second_factor(self.user))

    def test_done_is_checked_on_the_server(self):
        self.enable()
        url = reverse("accounts:recovery_codes_done")

        without = self.client.post(url, {}, follow=True)
        with_tick = self.client.post(url, {"saved": "on"}, follow=True)

        self.assertContains(without, "Bitte bestätigen Sie, dass Sie die Codes gespeichert haben.")
        self.assertNotContains(without, "Die Wiederherstellungscodes sind gespeichert.")
        self.assertContains(with_tick, "Die Wiederherstellungscodes sind gespeichert.")

    def test_the_term_is_recovery_code_everywhere(self):
        self.enable()
        page = self.client.get(PROFILE_URL)

        self.assertContains(page, "Wiederherstellungscodes")
        self.assertNotContains(page, "Notfallcode")


@tag("mariadb")
class TwoFactorActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp("holder.person@example.org")
        cls.user.set_secondary_email(SECOND)

    def setUp(self):
        self.client.force_login(self.user)

    def test_new_codes_need_the_code_and_the_password_alone_is_no_proof(self):
        url = reverse("accounts:recovery_codes")

        wrong_code = self.client.post(url, {"otp_token": "000000"})
        password_only = self.client.post(url, {"current_password": PASSWORD})

        for response in (wrong_code, password_only):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(codes_in(response), [])

    def test_new_codes_replace_the_old_ones_and_are_shown_once(self):
        response = self.client.post(
            reverse("accounts:recovery_codes"),
            {"otp_token": code_for(self.device)},
        )
        run_mail_queue()

        codes = codes_in(response)
        self.assertEqual(len(codes), 10)
        self.assertFalse(set(codes) & set(RECOVERY_CODES))
        self.assertEqual(len(mail.outbox), 2)
        self.assertNotIn("/email-change/", mail.outbox[0].body)

    def test_disabling_needs_the_password_and_the_code_and_tells_both_addresses(self):
        url = reverse("accounts:two_factor_disable")

        refused = self.client.post(url, {"current_password": PASSWORD})
        self.assertTrue(second_factor.has_second_factor(self.user))
        done = self.client.post(url, {"current_password": PASSWORD, "otp_token": code_for(self.device)})
        run_mail_queue()

        self.assertEqual(refused.status_code, 200)
        self.assertRedirects(done, f"{PROFILE_URL}#card-two-factor", fetch_redirect_response=False)
        self.assertFalse(second_factor.has_second_factor(self.user))
        self.assertEqual(
            sorted(address for message in mail.outbox for address in message.to),
            sorted(["holder.person@example.org", SECOND]),
        )

    def test_staff_cannot_disable_the_second_factor(self):
        staff = create_user_with_totp("staff.person@example.org")[0]
        staff.set_privileges(is_staff=True, is_superuser=False)
        client = VerifiedClient()
        client.force_login(staff)

        response = client.post(
            reverse("accounts:two_factor_disable"), {"current_password": PASSWORD}
        )

        self.assertRedirects(response, reverse("accounts:two_factor"))
        self.assertTrue(second_factor.has_second_factor(staff))

    def test_the_card_of_an_active_account_names_the_remaining_codes(self):
        page = self.client.get(PROFILE_URL)

        self.assertContains(page, "Aktiv")
        self.assertContains(page, "Sie haben 3 unbenutzte Wiederherstellungscodes.")


@tag("mariadb")
class RestrictedAccountTests(TestCase):
    def test_a_staff_account_without_a_second_factor_sees_the_second_factor_card_only(self):
        staff = create_active_user("staff.person@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        client = Client()
        client.post(LOGIN_URL, {"username": staff.email, "password": PASSWORD}, REMOTE_ADDR="10.64.0.1")

        page = client.get(reverse("accounts:two_factor_setup"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="card-two-factor"')
        for card in ("card-personal", "card-email", "card-password", "card-delete"):
            self.assertNotContains(page, f'id="{card}"')

    def test_the_forced_password_change_keeps_the_page_of_its_own(self):
        user = create_active_user("forced.person@example.org")
        user.password_change_required = True
        user.save()
        client = Client()
        client.force_login(user)

        response = client.get(reverse("accounts:password_change"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/password_change.html")


@tag("mariadb")
class DeleteCardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")
        cls.user.set_secondary_email(SECOND)

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("accounts:account_delete")

    def test_the_start_state_is_a_red_card_without_a_form(self):
        page = self.client.get(PROFILE_URL)

        self.assertContains(page, "Konto löschen")
        self.assertNotContains(page, 'name="confirm"')

    @override_settings(INSTANCE_PRIVACY_URL="https://www.example.org/privacy")
    def test_the_confirmation_has_the_notice_with_the_privacy_link_and_an_empty_box(self):
        page = self.client.get(f"{PROFILE_URL}?edit=delete")

        self.assertContains(page, 'href="https://www.example.org/privacy"')
        self.assertContains(page, 'name="confirm"')
        self.assertNotContains(page, "checked")

    def test_a_wrong_password_or_a_missing_tick_deletes_nothing(self):
        wrong = self.client.post(self.url, {"current_password": OTHER_PASSWORD, "confirm": "on"})
        unticked = self.client.post(self.url, {"current_password": PASSWORD})

        self.assertEqual(wrong.status_code, 200)
        self.assertEqual(unticked.status_code, 200)
        self.assertContains(unticked, "Bitte setzen Sie das Häkchen")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_the_deletion_shows_the_result_page_at_once_and_tells_both_addresses(self):
        response = self.client.post(self.url, {"current_password": PASSWORD, "confirm": "on"})
        run_mail_queue()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ihr Konto wurde gelöscht")
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertEqual(
            sorted(address for message in mail.outbox for address in message.to),
            sorted(["holder.person@example.org", SECOND]),
        )
        self.assertRedirects(
            self.client.get(PROFILE_URL), f"{LOGIN_URL}?next={PROFILE_URL}", fetch_redirect_response=False
        )
