"""Profile, verified email change and account lifecycle (D-016, D-040)."""

import re
import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.contrib.messages import get_messages
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import email_change
from leg_platform.accounts.models import (
    AccountIntegrityError,
    AuthenticatorDevice,
    MailJob,
    PendingEmailChange,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.views import EMAIL_CHANGE_SESSION_KEY
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    PROFILE,
    VerifiedClient,
    code_for,
    complete_second_factor,
    create_active_user,
    run_mail_queue,
)

USER_MODEL = get_user_model()
PROFILE_URL = reverse("accounts:profile")
PROFILE_EDIT_URL = reverse("accounts:profile_edit")
EMAIL_CHANGE_URL = reverse("accounts:email_change_request")
DELETE_URL = reverse("accounts:account_delete")


@tag("mariadb")
class ProfileEditingTests(TestCase):
    # Saving the profile reads the address directory (advisory check, D-033).
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_the_account_can_edit_only_profile_fields(self):
        response = self.client.post(
            PROFILE_EDIT_URL,
            {
                **PROFILE,
                "first_name": "New",
                "is_staff": "on",
                "is_superuser": "on",
                "retention_obligation": "on",
                "status": User.Status.BLOCKED,
                "email": "changed@example.org",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "New")
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertFalse(self.user.retention_obligation)
        self.assertEqual(self.user.status, User.Status.ACTIVE)
        self.assertEqual(self.user.email, "active.person@example.org")

    def test_an_anonymous_post_cannot_edit_a_profile(self):
        self.client.logout()

        response = self.client.post(PROFILE_EDIT_URL, {**PROFILE, "first_name": "Attacker"})

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Anna")

    def test_saving_the_address_is_not_checked_against_the_directory_and_stores_the_form_values(self):
        # D-066: a lookup is made only while the address is edited (by the script of the form),
        # never when it is saved or shown.
        with patch("leg_platform.addresses.services.lookup") as lookup, \
                patch("leg_platform.addresses.services.directory_status") as status, \
                patch("leg_platform.addresses.views.lookup") as view_lookup, \
                patch("leg_platform.addresses.views.allow_request") as view_limit:
            response = self.client.post(PROFILE_EDIT_URL, {**PROFILE, "street": "Changed street"})

        self.assertEqual(response.status_code, 302)
        lookup.assert_not_called()
        status.assert_not_called()
        view_lookup.assert_not_called()
        view_limit.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(self.user.street, "Changed street")
        self.assertEqual([str(message) for message in get_messages(response.wsgi_request)], ["Gespeichert."])

    def test_profile_form_contains_the_address_widget_and_local_script(self):
        response = self.client.get(PROFILE_EDIT_URL)

        self.assertContains(response, 'data-address-directory')
        self.assertContains(response, '/addresses/lookup/')
        self.assertContains(response, 'js/address-directory.js')


@tag("mariadb")
class EmailChangeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()

    def setUp(self):
        self.client = VerifiedClient()
        self.client.force_login(self.user)

    def request_change(self, email, **credentials):
        credentials.setdefault("current_password", PASSWORD)
        return self.client.post(
            EMAIL_CHANGE_URL,
            {"email": email, "secondary_email": "", **credentials},
        )

    def test_requests_have_a_generic_queued_response_and_send_no_synchronous_email(self):
        occupied = create_active_user("occupied@example.org")

        first = self.request_change("new.address@example.org")
        second = self.request_change(occupied.email)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(first["Location"], f"{PROFILE_URL}#card-email")
        self.assertEqual(second.status_code, 302)
        self.assertEqual(second["Location"], first["Location"])
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(
            list(
                MailJob.objects.filter(kind=MailJob.Kind.EMAIL_CHANGE_REQUEST)
                .order_by("id")
                .values_list("address", "account_id")
            ),
            [
                ("new.address@example.org", self.user.pk),
                ("occupied@example.org", self.user.pk),
            ],
        )

    def test_the_request_requires_the_current_password_and_second_factor(self):
        wrong_password = self.request_change(
            "new.address@example.org", current_password="incorrect"
        )
        self.assertEqual(wrong_password.status_code, 200)
        self.assertContains(wrong_password, "Das Passwort stimmt nicht")
        self.assertEqual(MailJob.objects.count(), 0)

        device = AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        valid_code = code_for(device)
        invalid_code = f"{(int(valid_code) + 1) % 1_000_000:06d}"
        complete_second_factor(self.client, self.user)
        wrong_code = self.request_change(
            "new.address@example.org", otp_token=invalid_code
        )
        self.assertEqual(wrong_code.status_code, 200)
        self.assertEqual(MailJob.objects.count(), 0)

        accepted = self.request_change("new.address@example.org", otp_token=valid_code)
        self.assertEqual(accepted.status_code, 302)
        # The link for the new address and the notice for the old one.
        self.assertEqual(
            sorted(MailJob.objects.values_list("kind", flat=True)),
            sorted([MailJob.Kind.EMAIL_CHANGE_REQUEST, MailJob.Kind.EMAIL_CHANGE_NOTICE]),
        )

    def test_confirmation_changes_the_address_and_invalidates_sessions(self):
        other_session = Client()
        other_session.force_login(self.user)
        self.request_change("new.address@example.org")
        self.assertEqual(run_mail_queue(), 2)
        message = next(m for m in mail.outbox if m.to == ["new.address@example.org"])
        match = re.search(
            r"https?://[^/]+(/email-change/verify/([^/\s]+)/)", message.body
        )
        self.assertIsNotNone(match)
        verification_path = match.group(1)
        raw_token = match.group(2)

        response = self.client.get(verification_path)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("accounts:email_change_confirm"))
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertNotIn(raw_token, response["Location"])
        self.assertEqual(
            self.client.session[EMAIL_CHANGE_SESSION_KEY],
            VerificationToken.objects.get(
                user=self.user, purpose=VerificationToken.Purpose.EMAIL_CHANGE
            ).digest,
        )
        self.assertNotEqual(self.client.session[EMAIL_CHANGE_SESSION_KEY], raw_token)
        confirmation = self.client.post(
            reverse("accounts:email_change_confirm"), {"confirm": "on"}
        )

        # The result page "E-Mail-Adresse geändert" (D-058) is the response itself: the link was
        # consumed, and every session of the account has ended (the person logs in again).
        self.assertEqual(confirmation.status_code, 200)
        self.assertTemplateUsed(confirmation, "accounts/email_change_done.html")
        self.assertContains(confirmation, "E-Mail-Adresse geändert")
        self.assertContains(confirmation, "new.address@example.org")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new.address@example.org")
        self.assertEqual(self.user.session_version, 1)
        self.assertRedirects(
            other_session.get(PROFILE_URL),
            f"{reverse('accounts:login')}?next={PROFILE_URL}",
            fetch_redirect_response=False,
        )
        self.assertEqual(
            list(MailJob.objects.values_list("kind", "address")),
            [(MailJob.Kind.EMAIL_CHANGED, "active.person@example.org")],
        )
        self.assertFalse(
            VerificationToken.objects.filter(
                user=self.user, purpose=VerificationToken.Purpose.EMAIL_CHANGE
            ).exists()
        )

    def pend(self, address, kind=PendingEmailChange.Kind.LOGIN):
        """The request that a confirmation link belongs to (D-059)."""
        return PendingEmailChange.objects.create(
            user=self.user,
            kind=kind,
            address=address,
            expires_at=timezone.now() + email_change.EMAIL_CHANGE_LIFETIME,
        )

    def test_confirmation_removes_a_pending_registration_for_the_address(self):
        pending = USER_MODEL.objects.create_user("new.address@example.org")
        self.pend("new.address@example.org")
        token = VerificationToken.objects.issue(
            self.user,
            VerificationToken.Purpose.EMAIL_CHANGE,
            email_change.EMAIL_CHANGE_LIFETIME,
            target_email="new.address@example.org",
        )
        stored = VerificationToken.objects.find_valid(
            token, VerificationToken.Purpose.EMAIL_CHANGE
        )

        changed = email_change.confirm_change(stored)

        self.assertEqual(changed.pk, self.user.pk)
        self.assertFalse(USER_MODEL.objects.filter(pk=pending.pk).exists())
        self.assertEqual(USER_MODEL.objects.get(pk=self.user.pk).email, "new.address@example.org")

    def test_a_link_without_its_pending_request_changes_nothing(self):
        # The request was cancelled or replaced: the link is dead even if the token still exists.
        token = VerificationToken.objects.issue(
            self.user,
            VerificationToken.Purpose.EMAIL_CHANGE,
            email_change.EMAIL_CHANGE_LIFETIME,
            target_email="new.address@example.org",
        )
        stored = VerificationToken.objects.find_valid(token, VerificationToken.Purpose.EMAIL_CHANGE)

        self.assertIsNone(email_change.confirm_change(stored))
        self.assertEqual(USER_MODEL.objects.get(pk=self.user.pk).email, "active.person@example.org")

    def test_confirmation_refuses_an_address_taken_after_the_request(self):
        self.pend("new.address@example.org")
        token = VerificationToken.objects.issue(
            self.user,
            VerificationToken.Purpose.EMAIL_CHANGE,
            email_change.EMAIL_CHANGE_LIFETIME,
            target_email="new.address@example.org",
        )
        stored = VerificationToken.objects.find_valid(
            token, VerificationToken.Purpose.EMAIL_CHANGE
        )
        owner = create_active_user("new.address@example.org")

        changed = email_change.confirm_change(stored)

        self.assertIsNone(changed)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "active.person@example.org")
        self.assertEqual(USER_MODEL.objects.get(pk=owner.pk).email, "new.address@example.org")
        self.assertFalse(
            VerificationToken.objects.filter(
                user=self.user, purpose=VerificationToken.Purpose.EMAIL_CHANGE
            ).exists()
        )


@tag("mariadb")
class AccountDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user()

    def setUp(self):
        self.client = VerifiedClient()
        self.client.force_login(self.user)

    def delete_account(self, **credentials):
        credentials.setdefault("current_password", PASSWORD)
        return self.client.post(
            DELETE_URL,
            {"confirm": "on", **credentials},
        )

    def test_deletion_requires_correct_reauthentication(self):
        response = self.delete_account(current_password="wrong")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertContains(response, "Das Passwort stimmt nicht")

    def test_an_account_without_a_retention_obligation_is_deleted(self):
        response = self.delete_account()

        # The result page is the answer itself: the session ended with the deletion (D-059).
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/account_deleted.html")
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DELETED).count(), 1)

    def test_a_retained_account_is_deactivated_and_its_obligation_is_one_way(self):
        self.user.set_retention_obligation()

        response = self.delete_account()

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.DEACTIVATED)
        self.assertTrue(self.user.retention_obligation)
        self.assertIsNone(self.user.normalized_email)
        with self.assertRaises(AccountIntegrityError):
            self.user._change(retention_obligation=False)

    def delete_card(self, client):
        page = client.get(f"{PROFILE_URL}?edit=delete").content.decode()
        card = re.search(r'<section class="profile-card profile-card-danger".*?</section>', page, re.S)
        return re.sub(r'value="[^"]*"', "", card.group(0))

    def test_the_confirmation_says_what_the_button_does(self):
        card = self.delete_card(self.client)

        self.assertIn("Konto wirklich löschen?", card)
        self.assertIn("Mit dem Löschen verlieren Sie den Zugang zum Portal.", card)
        self.assertIn("Rechnungen und Buchungsbelege bewahren wir so lange auf, wie das Gesetz es verlangt.", card)
        self.assertIn("Konto endgültig löschen</button>", card)
        self.assertIn("Ich weiss, dass mein Konto und meine Angaben gelöscht werden", card)

    def test_the_card_of_a_retained_account_says_it_is_deactivated_and_the_plain_card_says_nothing_of_it(self):
        # Owner decision 2026-10-19 (kundenprofil/PROFIL.md section 6; amends D-055): an account that is only
        # deactivated is told so before; an account that is deleted sees the card of D-059 unchanged.
        retained = create_active_user("retained.person@example.org")
        retained.set_retention_obligation()
        other = VerifiedClient()
        other.force_login(retained)

        plain = self.delete_card(self.client)
        kept = self.delete_card(other)

        for word in ("Aufbewahrung", "deaktiviert", "retention", "retain", "closed"):
            self.assertNotIn(word, plain)
        self.assertIn("Ihr Konto wird deaktiviert. Ihre Angaben bleiben aus Aufbewahrungsgründen erhalten.", kept)
        self.assertNotIn("Rechnungen und Buchungsbelege", kept)

    def test_the_checkbox_is_never_ticked_again_after_an_error(self):
        # Ticked, but with a wrong password: the card is shown again with the box unticked.
        wrong = self.delete_account(current_password="wrong")

        self.assertEqual(wrong.status_code, 200)
        box = re.search(r'<input[^>]*name="confirm"[^>]*>', wrong.content.decode()).group(0)
        self.assertNotIn("checked", box)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_a_missing_confirmation_has_a_specific_message(self):
        response = self.client.post(DELETE_URL, {"current_password": PASSWORD})

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Bitte setzen Sie das Häkchen, um zu bestätigen, dass Sie Ihr Konto löschen möchten."
        )
        self.assertNotContains(response, "zwingend erforderlich")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_the_result_says_deleted_or_deactivated_and_a_retained_account_stays_deactivated(self):
        deleted = self.client.post(DELETE_URL, {"confirm": "on", "current_password": PASSWORD}, follow=True)

        retained_user = create_active_user("retained.person@example.org")
        retained_user.set_retention_obligation()
        client = VerifiedClient()
        client.force_login(retained_user)
        kept = client.post(DELETE_URL, {"confirm": "on", "current_password": PASSWORD}, follow=True)

        self.assertContains(deleted, "Ihr Konto wurde gelöscht")
        for word in ("geschlossen", "aufbewahr", "gesetzlich", "deaktiviert"):
            self.assertNotContains(deleted, word)
        # Owner decision 2026-10-19 (amends D-055): the result tells an account that is kept.
        self.assertContains(kept, "Ihr Konto wurde deaktiviert")
        self.assertContains(kept, "Ihre Angaben bleiben aus Aufbewahrungsgründen erhalten.")
        self.assertNotContains(kept, "Ihr Konto wurde gelöscht")
        self.assertEqual(deleted.redirect_chain, kept.redirect_chain)
        # Behind the message the retained account stays in the database, deactivated.
        retained_user.refresh_from_db()
        self.assertEqual(retained_user.status, User.Status.DEACTIVATED)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())


@tag("mariadb")
class AdminLifecycleTests(TestCase):
    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("administrator@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)

    def setUp(self):
        self.client.force_login(self.administrator)

    def lifecycle_url(self, account, operation):
        return reverse("admin:accounts_user_lifecycle", args=(account.pk, operation))

    def test_an_administrator_can_block_and_unblock_an_account(self):
        account = create_active_user("target@example.org")

        blocked = self.client.post(self.lifecycle_url(account, "block"), {"confirm": "on"})
        account.refresh_from_db()
        self.assertEqual(blocked.status_code, 302)
        self.assertEqual(account.status, User.Status.BLOCKED)

        unblocked = self.client.post(self.lifecycle_url(account, "unblock"), {"confirm": "on"})
        account.refresh_from_db()
        self.assertEqual(unblocked.status_code, 302)
        self.assertEqual(account.status, User.Status.ACTIVE)

    def test_an_administrator_can_end_all_sessions_of_another_account(self):
        # H-2 of the review of 2026-10-17: also a session with "Stay signed in" ends at its next request (D-100).
        account = create_active_user("target.sessions@example.org")
        other = VerifiedClient()
        other.force_login(account)
        self.assertEqual(other.get(reverse("accounts:profile")).status_code, 200)
        before = User.objects.get(pk=account.pk).session_version
        url = self.lifecycle_url(account, "end_sessions")
        self.assertContains(self.client.get(url), "Alle Sitzungen beenden")
        # Without the confirmation nothing happens.
        self.client.post(url, {})
        self.assertEqual(User.objects.get(pk=account.pk).session_version, before)

        response = self.client.post(url, {"confirm": "on"})

        self.assertEqual(response.status_code, 302)
        self.assertGreater(User.objects.get(pk=account.pk).session_version, before)
        ended = other.get(reverse("accounts:profile"))
        self.assertEqual(ended.status_code, 302)
        self.assertTrue(ended["Location"].startswith(reverse("accounts:login")))
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.ACCOUNT_SESSIONS_ENDED_BY_ADMIN)
        self.assertEqual((event.account_id, event.actor_id), (account.pk, self.administrator.pk))
        self.assertTrue(event.long_retention)
        # The account stays as it was: no block, no change of status.
        self.assertEqual(User.objects.get(pk=account.pk).status, User.Status.ACTIVE)
        # The button is on the page of the other account and not on the own page.
        self.assertContains(
            self.client.get(reverse("admin:accounts_user_change", args=(account.pk,))), url
        )
        self.assertNotContains(
            self.client.get(reverse("admin:accounts_user_change", args=(self.administrator.pk,))),
            self.lifecycle_url(self.administrator, "end_sessions"),
        )

    def test_the_own_account_a_staff_user_and_a_deleted_account_cannot_be_ended_this_way(self):
        before = User.objects.get(pk=self.administrator.pk).session_version
        own = self.lifecycle_url(self.administrator, "end_sessions")
        self.assertEqual(self.client.get(own).status_code, 403)
        self.assertEqual(self.client.post(own, {"confirm": "on"}).status_code, 403)
        self.assertEqual(User.objects.get(pk=self.administrator.pk).session_version, before)
        # A deleted account (retention obligation) has no sessions to end: refused, nothing changes.
        retained = create_active_user("retained.sessions@example.org")
        retained.set_retention_obligation()
        retained.delete_or_deactivate()
        before = User.objects.get(pk=retained.pk).session_version
        self.client.post(self.lifecycle_url(retained, "end_sessions"), {"confirm": "on"})
        self.assertEqual(User.objects.get(pk=retained.pk).session_version, before)
        # Staff without system administrator rights, and an account without staff rights: 403, nothing changes.
        account = create_active_user("target.sessions2@example.org")
        before = User.objects.get(pk=account.pk).session_version
        for who in ("staff", "person"):
            actor = create_active_user(f"{who}.sessions@example.org")
            if who == "staff":
                actor.set_privileges(is_staff=True, is_superuser=False)
            client = VerifiedClient()
            client.force_login(actor)
            response = client.post(self.lifecycle_url(account, "end_sessions"), {"confirm": "on"})
            self.assertIn(response.status_code, (302, 403), who)
            if response.status_code == 302:
                self.assertTrue(response["Location"].startswith("/admin/login/") or "login" in response["Location"])
            self.assertEqual(User.objects.get(pk=account.pk).session_version, before, who)
        self.assertEqual(Client().post(self.lifecycle_url(account, "end_sessions"), {"confirm": "on"}).status_code, 302)
        self.assertEqual(User.objects.get(pk=account.pk).session_version, before)

    def test_a_staff_user_cannot_use_the_lifecycle_endpoint(self):
        staff = create_active_user("staff@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        account = create_active_user("target@example.org")
        self.client.force_login(staff)

        response = self.client.post(self.lifecycle_url(account, "block"), {"confirm": "on"})

        self.assertEqual(response.status_code, 403)
        account.refresh_from_db()
        self.assertEqual(account.status, User.Status.ACTIVE)

    def test_the_last_system_administrator_cannot_be_blocked_or_deleted(self):
        for operation in ("block", "delete"):
            with self.subTest(operation=operation):
                response = self.client.post(
                    self.lifecycle_url(self.administrator, operation), {"confirm": "on"}
                )
                self.assertEqual(response.status_code, 302)
                self.administrator.refresh_from_db()
                self.assertEqual(self.administrator.status, User.Status.ACTIVE)
                self.assertTrue(self.administrator.is_superuser)

    def test_reactivation_removes_a_pending_registration_for_the_same_address(self):
        retained = create_active_user("retained@example.org")
        retained.set_retention_obligation()
        retained.delete_or_deactivate()
        pending = USER_MODEL.objects.create_user(retained.email)

        response = self.client.post(
            self.lifecycle_url(retained, "reactivate"), {"confirm": "on"}
        )

        retained.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(retained.status, User.Status.ACTIVE)
        self.assertFalse(USER_MODEL.objects.filter(pk=pending.pk).exists())

    def test_admin_deletion_deactivates_an_account_with_a_retention_obligation(self):
        retained = create_active_user("retained@example.org")
        retained.set_retention_obligation()

        response = self.client.post(
            self.lifecycle_url(retained, "delete"), {"confirm": "on"}
        )

        retained.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(retained.status, User.Status.DEACTIVATED)
        self.assertTrue(retained.retention_obligation)
        self.assertIsNone(retained.normalized_email)

    def test_admin_deactivation_also_purges_the_one_time_messages_and_return_reasons(self):
        from leg_platform.community import customer_messages
        from leg_platform.community.models import CustomerMessage

        retained = create_active_user("retained.purge@example.org")
        retained.set_retention_obligation()
        customer_messages.record(retained, CustomerMessage.Kind.APPLICATION_REJECTED, "intern")

        self.client.post(self.lifecycle_url(retained, "delete"), {"confirm": "on"})

        retained.refresh_from_db()
        self.assertEqual(retained.status, User.Status.DEACTIVATED)
        self.assertFalse(CustomerMessage.objects.filter(person=retained).exists())

    def test_reactivation_refuses_an_address_owned_by_an_active_account(self):
        retained = create_active_user("retained@example.org")
        retained.set_retention_obligation()
        retained.delete_or_deactivate()
        USER_MODEL.objects.create_user("replacement@example.org")
        replacement = create_active_user("retained@example.org")

        response = self.client.post(
            self.lifecycle_url(retained, "reactivate"), {"confirm": "on"}
        )

        retained.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(retained.status, User.Status.DEACTIVATED)
        self.assertEqual(replacement.email, "retained@example.org")

    def test_a_deleted_account_with_a_retention_obligation_is_shown_understandably(self):
        retained = create_active_user("retained@example.org")
        retained.set_retention_obligation()
        retained.delete_or_deactivate()
        url = reverse("admin:accounts_user_changelist")

        german = self.client.get(url)
        self.assertContains(german, "Gelöscht (Aufbewahrungspflicht)")
        self.assertContains(german, "Aufbewahrungspflicht")
        self.assertNotContains(german, "Deaktiviert")
        # The stored key is unchanged (D-016); the filters offer the new label.
        self.assertEqual(User.Status.DEACTIVATED.value, "deactivated")
        filtered = self.client.get(url, {"status__exact": "deactivated"})
        self.assertContains(filtered, "retained@example.org")
        detail = self.client.get(reverse("admin:accounts_user_change", args=(retained.pk,)))
        self.assertContains(detail, "Konto wiederherstellen")
        self.assertNotContains(detail, "Konto reaktivieren")


@tag("mariadb")
class ConcurrentEmailChangeTests(TransactionTestCase):
    reset_sequences = True

    def test_two_accounts_cannot_claim_the_same_address_concurrently(self):
        first = create_active_user("first@example.org")
        second = create_active_user("second@example.org")
        for user in (first, second):
            PendingEmailChange.objects.create(
                user=user,
                kind=PendingEmailChange.Kind.LOGIN,
                address="shared@example.org",
                expires_at=timezone.now() + email_change.EMAIL_CHANGE_LIFETIME,
            )
            VerificationToken.objects.issue(
                user,
                VerificationToken.Purpose.EMAIL_CHANGE,
                email_change.EMAIL_CHANGE_LIFETIME,
                target_email="shared@example.org",
            )
        token_ids = [
            VerificationToken.objects.get(
                user=user, purpose=VerificationToken.Purpose.EMAIL_CHANGE
            ).pk
            for user in (first, second)
        ]
        barrier = threading.Barrier(2)
        outcomes = []
        errors = []

        def confirm(token_id):
            close_old_connections()
            try:
                stored = VerificationToken.objects.get(pk=token_id)
                barrier.wait(timeout=10)
                outcomes.append(email_change.confirm_change(stored) is not None)
            except Exception as error:  # surfaced below as a test failure
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=confirm, args=(token_id,)) for token_id in token_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(
            User.objects.filter(normalized_email="shared@example.org").count(), 1
        )
