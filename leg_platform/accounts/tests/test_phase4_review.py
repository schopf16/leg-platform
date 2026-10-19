"""Corrections of the security review of phase 4 (D-059; block 4b).

H1 the stop link only has a meaning while a request is open; M1 the profile saves only its fields;
M2 the clean-up of links and requests of address changes; M3 a refused request leaves nothing
behind; M4 silent requests are counted; M5 the text for the second address; and the
recommendations (order of the mails, the code at the password, the box of "Fertig").
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import email_change, mail_queue, rate_limits
from leg_platform.accounts.forms import ProfileForm
from leg_platform.accounts.models import (
    MailJob,
    PendingEmailChange,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    PROFILE,
    code_for,
    create_active_user,
    registration_data,
    run_mail_queue,
)
from leg_platform.accounts.tests.test_profile import (
    NEW,
    PROFILE_URL,
    SECOND,
    link_in,
    mails_to,
)
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp

STOP = VerificationToken.Purpose.EMAIL_CHANGE_STOP
EMAIL_URL = reverse("accounts:email_change_request")
STOP_CONFIRM = reverse("accounts:email_change_stop_confirm")
INVALID_TEXT = "Link nicht mehr gültig"


def request_login_change(client, address=NEW, secondary="", user_email="holder.person@example.org"):
    return client.post(
        EMAIL_URL,
        {"email": address, "secondary_email": secondary, "current_password": PASSWORD},
        REMOTE_ADDR="10.80.0.1",
    )


@tag("mariadb")
class StopLinkMeaningTests(TestCase):
    """H1: the link of a notice is dead as soon as no request is open."""

    def setUp(self):
        self.user = create_active_user("holder.person@example.org")
        self.client.force_login(self.user)
        request_login_change(self.client)
        run_mail_queue()
        self.stop_path = link_in(mails_to("holder.person@example.org")[0], "email-change/stop")
        self.confirm_path = link_in(mails_to(NEW)[0], "email-change/verify")

    def assert_dead(self, path=None):
        """The link shows the invalid page, ends no session and logs nothing."""
        self.user.refresh_from_db()
        survivor = Client()
        survivor.force_login(self.user)
        before = SecurityEvent.objects.filter(event=SecurityEvent.Event.EMAIL_CHANGE_STOPPED).count()
        anonymous = Client()

        opened = anonymous.get(path or self.stop_path, follow=True)
        posted = anonymous.post(STOP_CONFIRM, {}, follow=True)

        self.assertContains(opened, INVALID_TEXT)
        self.assertContains(posted, INVALID_TEXT)
        self.assertEqual(survivor.get(PROFILE_URL).status_code, 200)
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.EMAIL_CHANGE_STOPPED).count(), before
        )

    def test_after_the_confirmation_the_link_is_dead_and_ends_no_session(self):
        confirming = Client()
        confirming.get(self.confirm_path)
        confirming.post(reverse("accounts:email_change_confirm"), {})

        self.assertFalse(VerificationToken.objects.filter(purpose=STOP).exists())
        self.assert_dead()

    def test_after_the_cancel_the_link_is_dead(self):
        self.client.post(reverse("accounts:email_change_cancel", args=["login"]))

        self.assertFalse(VerificationToken.objects.filter(purpose=STOP).exists())
        self.assert_dead()

    def test_after_a_replacement_the_old_link_is_dead_and_the_new_one_works(self):
        request_login_change(self.client, address="newer.address@example.org")
        run_mail_queue()
        new_stop = link_in(mails_to("holder.person@example.org")[-1], "email-change/stop")

        self.assertNotEqual(new_stop, self.stop_path)
        self.assert_dead(self.stop_path)
        stopper = Client()
        stopper.get(new_stop)
        done = stopper.post(STOP_CONFIRM, {})
        self.assertContains(done, "Änderung gestoppt")
        self.assertFalse(PendingEmailChange.objects.exists())

    def test_after_the_second_address_was_confirmed_the_link_is_dead(self):
        PendingEmailChange.objects.all().delete()
        VerificationToken.objects.all().delete()
        mail.outbox.clear()
        request_login_change(self.client, address=self.user.email, secondary=SECOND)
        run_mail_queue()
        stop_path = link_in(mails_to("holder.person@example.org")[-1], "email-change/stop")
        confirming = Client()
        confirming.get(link_in(mails_to(SECOND)[0], "email-change/verify"))
        confirming.post(reverse("accounts:email_change_confirm"), {})

        self.assertFalse(VerificationToken.objects.filter(purpose=STOP).exists())
        self.assert_dead(stop_path)

    def test_an_expired_request_makes_the_link_dead(self):
        PendingEmailChange.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        self.assert_dead()

    def test_a_request_that_ends_between_the_page_and_the_button_stops_nothing(self):
        stopper = Client()
        stopper.get(self.stop_path)
        self.assertEqual(stopper.get(STOP_CONFIRM).status_code, 200)
        PendingEmailChange.objects.all().delete()
        survivor = Client()
        survivor.force_login(self.user)

        posted = stopper.post(STOP_CONFIRM, {}, follow=True)
        self.assertEqual(survivor.get(PROFILE_URL).status_code, 200)

    def test_stop_all_without_a_request_returns_nothing_and_ends_no_session(self):
        PendingEmailChange.objects.all().delete()
        version = User.objects.get(pk=self.user.pk).session_version

        self.assertIsNone(email_change.stop_all(self.user))

        self.assertEqual(User.objects.get(pk=self.user.pk).session_version, version)

    def test_the_result_page_recommends_a_new_password_and_names_the_ended_sessions(self):
        stopper = Client()
        stopper.get(self.stop_path)

        done = stopper.post(STOP_CONFIRM, {})

        self.assertContains(done, "Alle Sitzungen Ihres Kontos wurden beendet.")
        self.assertContains(done, "wählen Sie jetzt ein neues Passwort")
        self.assertContains(done, f'href="{reverse("accounts:password_reset")}"')

    def test_the_notice_is_queued_before_the_confirmation_link(self):
        MailJob.objects.all().delete()
        PendingEmailChange.objects.all().delete()

        request_login_change(self.client, address="third.address@example.org")

        kinds = list(MailJob.objects.order_by("pk").values_list("kind", flat=True))
        self.assertEqual(kinds, [MailJob.Kind.EMAIL_CHANGE_NOTICE, MailJob.Kind.EMAIL_CHANGE_REQUEST])


@tag("mariadb")
class ProfileSaveTests(TestCase):
    """M1: a stale instance does not write back what a parallel request changed."""

    databases = {"default", "addresses"}

    def test_a_stale_instance_keeps_the_end_of_the_sessions_the_password_and_the_second_address(self):
        user = create_active_user("holder.person@example.org")
        stale = User.objects.get(pk=user.pk)
        fresh = User.objects.get(pk=user.pk)
        fresh.end_all_sessions()
        fresh.set_secondary_email(SECOND)
        fresh.set_password(OTHER_PASSWORD)
        fresh.save(update_fields=["password"])
        version = User.objects.get(pk=user.pk).session_version

        form = ProfileForm({**PROFILE, "first_name": "Berta"}, instance=stale)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        stored = User.objects.get(pk=user.pk)
        self.assertEqual(stored.first_name, "Berta")
        self.assertEqual(stored.session_version, version)
        self.assertEqual(stored.secondary_email, SECOND)
        self.assertTrue(stored.check_password(OTHER_PASSWORD))

    def test_the_postal_code_and_the_other_fields_are_still_saved(self):
        user = create_active_user("holder.person@example.org")
        form = ProfileForm(
            {**PROFILE, "postal_code": "3063", "town": "Ittigen", "phone_number": "031 123 45 67"},
            instance=User.objects.get(pk=user.pk),
        )
        self.assertTrue(form.is_valid(), form.errors)

        form.save()

        stored = User.objects.get(pk=user.pk)
        self.assertEqual((stored.swiss_postal_code, stored.town), (3063, "Ittigen"))
        self.assertEqual(stored.phone_number.as_e164, "+41311234567")


@tag("mariadb")
class CleanupTests(TestCase):
    """M2: links and requests of address changes do not stay in the database."""

    def setUp(self):
        self.user = create_active_user("holder.person@example.org")
        self.past = timezone.now() - timedelta(minutes=1)

    def token(self, purpose, expires_at, target=""):
        VerificationToken.objects.issue(self.user, purpose, timedelta(hours=1), target_email=target)
        VerificationToken.objects.filter(user=self.user, purpose=purpose).update(expires_at=expires_at)

    def test_the_command_removes_expired_second_address_and_stop_links_and_expired_requests(self):
        self.token(VerificationToken.Purpose.SECONDARY_EMAIL, self.past, SECOND)
        self.token(STOP, self.past)
        PendingEmailChange.objects.create(
            user=self.user, kind="secondary", address=SECOND, expires_at=self.past
        )

        call_command("cleanup_mail_requests", stdout=StringIO())

        self.assertFalse(VerificationToken.objects.exists())
        self.assertFalse(PendingEmailChange.objects.exists())

    def test_open_requests_and_their_links_stay(self):
        future = timezone.now() + timedelta(hours=1)
        self.token(VerificationToken.Purpose.SECONDARY_EMAIL, future, SECOND)
        self.token(STOP, future)
        PendingEmailChange.objects.create(
            user=self.user, kind="secondary", address=SECOND, expires_at=future
        )

        call_command("cleanup_mail_requests", stdout=StringIO())

        self.assertEqual(VerificationToken.objects.count(), 2)
        self.assertEqual(PendingEmailChange.objects.count(), 1)

    def test_a_stop_link_without_an_open_request_is_removed(self):
        self.token(STOP, timezone.now() + timedelta(hours=1))

        call_command("cleanup_mail_requests", stdout=StringIO())

        self.assertFalse(VerificationToken.objects.exists())

    def test_the_deletion_of_a_retained_account_removes_every_link_of_an_address_change(self):
        self.user.set_retention_obligation()
        future = timezone.now() + timedelta(hours=1)
        for purpose in (
            VerificationToken.Purpose.EMAIL_CHANGE,
            VerificationToken.Purpose.SECONDARY_EMAIL,
            STOP,
        ):
            self.token(purpose, future, NEW)

        self.assertEqual(self.user.delete_or_deactivate(), "deactivated")

        self.assertFalse(VerificationToken.objects.filter(user=self.user).exists())


@tag("mariadb")
class RefusedChangeTests(TestCase):
    """M3: a refused request leaves nothing behind, and a refusal does not use up a code."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def test_a_refusal_of_the_second_request_rolls_back_the_first(self):
        request_login_change(self.client, address="first.address@example.org")
        before = list(PendingEmailChange.objects.values_list("kind", "address"))
        jobs = MailJob.objects.count()

        with patch.object(mail_queue, "submit_mail_request", side_effect=[True, False]):
            response = request_login_change(self.client, address=NEW, secondary=SECOND)

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Zu viele Anfragen, bitte versuchen Sie es später erneut.", status_code=429)
        self.assertEqual(list(PendingEmailChange.objects.values_list("kind", "address")), before)
        self.assertEqual(MailJob.objects.count(), jobs)

    def test_a_request_for_the_second_address_only_is_checked_for_its_own_kind(self):
        asked = []

        def refuse(request, kind):
            asked.append(kind)
            return True

        with patch("leg_platform.accounts.profile_views.requests_refused", side_effect=refuse):
            response = request_login_change(self.client, address=self.user.email, secondary=SECOND)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(asked, [MailJob.Kind.SECONDARY_EMAIL_REQUEST])
        self.assertFalse(PendingEmailChange.objects.exists())

    def test_a_refusal_comes_before_the_check_of_the_password_and_the_code(self):
        with patch("leg_platform.accounts.profile_views.requests_refused", return_value=True):
            response = self.client.post(
                EMAIL_URL,
                {"email": NEW, "secondary_email": "", "current_password": "not the password"},
            )

        self.assertEqual(response.status_code, 429)

    def test_a_request_that_changes_nothing_is_not_counted(self):
        with patch("leg_platform.accounts.profile_views.requests_refused", return_value=True):
            response = self.client.post(
                EMAIL_URL,
                {"email": self.user.email, "secondary_email": "", "current_password": PASSWORD},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Es wurde nichts geändert.")


@tag("mariadb")
class SilentRequestTests(TestCase):
    """M4: hits of the bot check and repeats within the pause are counted per client address."""

    def post(self, url, data, client=None):
        return (client or Client()).post(url, data, REMOTE_ADDR="10.81.0.1")

    def test_bot_hits_above_the_limit_get_the_refusal_without_a_session(self):
        data = registration_data("a.person@example.org", contact_fax_number="x")
        with patch.object(rate_limits, "MAIL_SILENT_LIMITS", ((2, timedelta(hours=1)),)):
            first = self.post(reverse("accounts:registration"), data)
            second = self.post(reverse("accounts:registration"), data)
            third = self.post(reverse("accounts:registration"), data)

        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(third.status_code, 429)
        self.assertNotIn("sessionid", third.cookies)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_repeats_within_the_pause_are_counted_too(self):
        client = Client()
        url = reverse("accounts:registration")
        with patch.object(rate_limits, "MAIL_SILENT_LIMITS", ((1, timedelta(hours=1)),)):
            first = self.post(url, registration_data("a.person@example.org"), client)
            repeat = self.post(url, registration_data("a.person@example.org"), client)
            again = self.post(url, registration_data("a.person@example.org"), client)

        self.assertEqual((first.status_code, repeat.status_code), (302, 302))
        self.assertEqual(again.status_code, 429)
        self.assertEqual(MailJob.objects.count(), 1)

    def test_a_normal_request_is_not_counted_as_silent(self):
        with patch.object(rate_limits, "MAIL_SILENT_LIMITS", ((1, timedelta(hours=1)),)):
            responses = [
                self.post(reverse("accounts:registration"), registration_data(f"p{n}@example.org"))
                for n in range(3)
            ]

        self.assertEqual({r.status_code for r in responses}, {302})


@tag("mariadb")
class SecondAddressMailTests(TestCase):
    """M5: the text for the second address has no sentence about logging in again."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")
        cls.user.set_secondary_email(SECOND)

    def setUp(self):
        self.client.force_login(self.user)

    def test_removing_the_second_address_sends_the_text_without_the_login_sentence(self):
        self.client.post(
            EMAIL_URL, {"email": self.user.email, "secondary_email": "", "current_password": PASSWORD}
        )
        run_mail_queue()

        bodies = [m.body for m in mail.outbox]
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            self.assertIn("Die zweite E-Mail-Adresse für Ihr Konto", body)
            self.assertNotIn("anmelden", body)
        self.assertTrue(
            MailJob.objects.count() == 0
            and SecurityEvent.objects.filter(event=SecurityEvent.Event.SECONDARY_EMAIL_CHANGED).exists()
        )

    def test_the_text_of_a_changed_login_address_still_says_to_log_in_again(self):
        mail.outbox.clear()
        mail_queue.enqueue(MailJob.Kind.EMAIL_CHANGED, "holder.person@example.org")
        run_mail_queue()

        self.assertIn("erneut einloggen", mail.outbox[0].body)

    @override_settings(INSTANCE_WEBSITE_URL="https://www.example.org/contact?a=1&b=2")
    def test_the_contact_link_is_not_escaped_in_either_text(self):
        for kind in (MailJob.Kind.EMAIL_CHANGED, MailJob.Kind.SECONDARY_EMAIL_CHANGED):
            mail.outbox.clear()
            mail_queue.enqueue(kind, "holder.person@example.org")
            run_mail_queue()
            with self.subTest(kind=kind):
                self.assertIn("contact?a=1&b=2", mail.outbox[0].body)
                self.assertNotIn("&amp;", mail.outbox[0].body)


@tag("mariadb")
class PasswordCardCodeTests(TestCase):
    """The card "Passwort" asks for the code where a second factor is set up (K12)."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp("holder.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("accounts:password_change")

    def data(self, **extra):
        return {
            "current_password": PASSWORD,
            "new_password1": OTHER_PASSWORD,
            "new_password2": OTHER_PASSWORD,
            **extra,
        }

    def test_the_card_shows_the_code_field(self):
        self.assertContains(self.client.get(f"{PROFILE_URL}?edit=password"), 'name="otp_token"')

    def test_without_the_code_the_password_stays(self):
        response = self.client.post(self.url, self.data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Geben Sie den 2FA-Authentifizierungscode ein.")
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(PASSWORD))

    def test_a_wrong_code_keeps_the_password(self):
        response = self.client.post(self.url, self.data(otp_token="000000"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(PASSWORD))

    def test_with_the_code_the_password_is_changed(self):
        response = self.client.post(self.url, self.data(otp_token=code_for(self.device)))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(OTHER_PASSWORD))

    def test_an_account_without_a_second_factor_sees_no_code_field(self):
        plain = create_active_user("plain.person@example.org")
        client = Client()
        client.force_login(plain)

        self.assertNotContains(client.get(f"{PROFILE_URL}?edit=password"), 'name="otp_token"')


@tag("mariadb")
class RecoveryDoneBoxTests(TestCase):
    def test_the_box_of_done_is_required_by_the_browser_too(self):
        user = create_active_user("holder.person@example.org")
        client = Client()
        client.force_login(user)
        from leg_platform.accounts.models import AuthenticatorDevice

        client.post(reverse("accounts:two_factor_setup"), {}, REMOTE_ADDR="10.82.0.1")
        device = AuthenticatorDevice.objects.get(user=user)

        page = client.post(reverse("accounts:two_factor_confirm"), {"otp_token": code_for(device)})

        self.assertRegex(page.content.decode(), r'<input[^>]*name="saved"[^>]*\brequired\b')
