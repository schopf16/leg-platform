"""Reset of a lost second factor, emergency command, initial administrator and forced password
change (docs/SECURITY.md, sections 3 and 11; D-004, D-005, D-041)."""

import os
import re
from datetime import timedelta
from io import StringIO
from unittest import mock
from urllib.parse import urlsplit

from django.contrib.sessions.models import Session
from django.core import mail
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import second_factor_reset
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    MailJob,
    RecoveryCodeDevice,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    VerifiedClient,
    code_for,
    complete_second_factor,
    create_active_user,
    run_mail_queue,
)

Event = SecurityEvent.Event
LOGIN_URL = reverse("accounts:login")
PROFILE_URL = reverse("accounts:profile")


def with_second_factor(user):
    device = AuthenticatorDevice.objects.create_device(user, confirmed=True)
    recovery = RecoveryCodeDevice.objects.create(user=user, name="recovery codes", confirmed=True)
    recovery.add_codes([f"{user.pk:04d}abcd", f"{user.pk:04d}efgh"])
    return device


def administrator(email="admin@example.org"):
    user = create_active_user(email)
    user.set_privileges(is_staff=True, is_superuser=True)
    with_second_factor(user)
    return user


def reset_url(user):
    return reverse("admin:accounts_user_reset_second_factor", args=(user.pk,))


def link_in(message):
    match = re.search(r"^(https?://\S+/two-factor-reset/verify/\S+)$", message.body, re.MULTILINE)
    assert match, "The email contains no link."
    return urlsplit(match.group(1)).path


def has_second_factor(user):
    return AuthenticatorDevice.objects.filter(user=user).exists() or (
        RecoveryCodeDevice.objects.filter(user=user).exists()
    )


@tag("mariadb")
class AdministrationResetTests(TestCase):
    client_class = VerifiedClient

    def setUp(self):
        self.administrator = administrator()
        self.user = create_active_user("member@example.org")
        with_second_factor(self.user)

    def test_the_account_page_offers_the_reset_for_other_accounts_only(self):
        self.client.force_login(self.administrator)
        other = self.client.get(reverse("admin:accounts_user_change", args=(self.user.pk,)))
        self.assertContains(other, reset_url(self.user))
        own = self.client.get(reverse("admin:accounts_user_change", args=(self.administrator.pk,)))
        self.assertNotContains(own, reset_url(self.administrator))

    def test_a_staff_user_cannot_use_the_reset_even_with_every_permission(self):
        staff = create_active_user("staff@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        from django.contrib.auth.models import Permission

        staff.user_permissions.set(Permission.objects.filter(content_type__app_label="accounts"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reset_url(self.user)).status_code, 403)
        self.assertEqual(self.client.post(reset_url(self.user), {}).status_code, 403)
        self.assertTrue(has_second_factor(self.user))
        self.assertFalse(MailJob.objects.exists())

    def test_nobody_can_reset_their_own_second_factor_there(self):
        self.client.force_login(self.administrator)
        self.assertEqual(self.client.get(reset_url(self.administrator)).status_code, 403)
        self.assertEqual(self.client.post(reset_url(self.administrator), {}).status_code, 403)
        self.assertTrue(has_second_factor(self.administrator))

    def test_a_user_gets_a_link_and_keeps_the_factor_until_it_is_confirmed(self):
        self.client.force_login(self.administrator)
        response = self.client.post(reset_url(self.user), {})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(has_second_factor(self.user))
        self.assertEqual(MailJob.objects.get().kind, MailJob.Kind.TWO_FACTOR_RESET_LINK)
        entry = SecurityEvent.objects.get()
        self.assertEqual(
            (entry.event, entry.account_id, entry.actor_id, entry.long_retention),
            ("two_factor_reset_requested", self.user.pk, self.administrator.pk, False),
        )
        run_mail_queue()
        (message,) = mail.outbox
        self.assertEqual(message.to, [self.user.email])
        self.assertTrue(has_second_factor(self.user))

    def test_confirming_the_link_removes_the_factor_and_ends_the_sessions(self):
        member = Client()
        member.force_login(self.user)
        self.assertEqual(member.get(PROFILE_URL).status_code, 200)
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {})
        run_mail_queue()
        path = link_in(mail.outbox[0])
        owner = Client()
        opened = owner.get(path)
        self.assertEqual(opened.status_code, 302)
        self.assertEqual(opened["Referrer-Policy"], "no-referrer")
        self.assertNotIn(path.rsplit("/", 2)[-2], opened["Location"])
        # Opening the link changes nothing.
        self.assertTrue(has_second_factor(self.user))
        page = owner.get(opened["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertTrue(has_second_factor(self.user))
        done = owner.post(opened["Location"])
        self.assertRedirects(done, LOGIN_URL, fetch_redirect_response=False)
        self.assertFalse(has_second_factor(self.user))
        # Every session of the account ended; the owner can log in with the password only.
        self.assertEqual(member.get(PROFILE_URL).status_code, 302)
        self.assertEqual(
            Client().post(LOGIN_URL, {"username": self.user.email, "password": PASSWORD}).status_code,
            302,
        )
        reset = SecurityEvent.objects.get(event="two_factor_reset")
        self.assertEqual((reset.account_id, reset.actor_id, reset.long_retention), (self.user.pk, None, False))
        notice = MailJob.objects.get(kind=MailJob.Kind.TWO_FACTOR_RESET)
        self.assertEqual(notice.address, self.user.email)

    def test_the_link_works_once_and_a_newer_link_replaces_the_older(self):
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {})
        run_mail_queue()
        first = link_in(mail.outbox[0])
        self.client.post(reset_url(self.user), {})
        run_mail_queue()
        second = link_in(mail.outbox[1])
        self.assertNotEqual(first, second)
        owner = Client()
        self.assertContains(owner.get(first, follow=True), "ungültig")
        confirm = owner.get(second)["Location"]
        owner.post(confirm)
        self.assertFalse(has_second_factor(self.user))
        # The consumed link cannot be used again, not even from the remembered session.
        self.assertContains(Client().get(second, follow=True), "ungültig")
        self.assertContains(owner.post(confirm), "ungültig")

    def test_an_expired_link_is_not_valid(self):
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {})
        run_mail_queue()
        path = link_in(mail.outbox[0])
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertContains(Client().get(path, follow=True), "ungültig")
        self.assertTrue(has_second_factor(self.user))

    def test_a_link_stops_working_when_the_account_becomes_a_staff_user(self):
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {})
        run_mail_queue()
        path = link_in(mail.outbox[0])
        self.user.set_privileges(is_staff=True, is_superuser=False)
        self.assertContains(Client().get(path, follow=True), "ungültig")
        self.assertTrue(has_second_factor(self.user))

    def test_a_link_is_not_sent_if_the_account_has_no_factor_or_is_privileged(self):
        plain = create_active_user("plain@example.org")
        MailJob.objects.create(
            kind=MailJob.Kind.TWO_FACTOR_RESET_LINK,
            account=plain,
            address=plain.email,
            next_attempt_at=timezone.now(),
        )
        self.user.set_privileges(is_staff=True, is_superuser=False)
        MailJob.objects.create(
            kind=MailJob.Kind.TWO_FACTOR_RESET_LINK,
            account=self.user,
            address=self.user.email,
            next_attempt_at=timezone.now(),
        )
        run_mail_queue()
        self.assertEqual(mail.outbox, [])
        self.assertFalse(VerificationToken.objects.exists())

    def test_a_link_does_not_reach_another_account_after_the_address_changed(self):
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {})
        requested_address = self.user.email
        self.user.change_email("moved.person@example.org")
        # Another person now holds the address of the request and has a second factor.
        taker = create_active_user(requested_address)
        with_second_factor(taker)

        run_mail_queue()

        self.assertEqual(mail.outbox, [])
        self.assertFalse(VerificationToken.objects.exists())
        self.assertFalse(MailJob.objects.exists())
        self.assertTrue(has_second_factor(taker))

    def test_a_staff_user_is_reset_at_once_after_the_identity_confirmation(self):
        staff = create_active_user("staff@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        with_second_factor(staff)
        staff_client = VerifiedClient()
        staff_client.force_login(staff)
        self.assertEqual(staff_client.get(reverse("admin:index")).status_code, 200)
        self.client.force_login(self.administrator)
        # Without the confirmation nothing happens.
        refused = self.client.post(reset_url(staff), {})
        self.assertEqual(refused.status_code, 200)
        self.assertTrue(has_second_factor(staff))
        done = self.client.post(reset_url(staff), {"identity_verified": "on"})
        self.assertEqual(done.status_code, 302)
        self.assertFalse(has_second_factor(staff))
        # The sessions of the account ended.
        self.assertEqual(staff_client.get(reverse("admin:index")).status_code, 302)
        entry = SecurityEvent.objects.get(event="two_factor_reset")
        self.assertEqual(
            (entry.account_id, entry.actor_id, entry.long_retention),
            (staff.pk, self.administrator.pk, True),
        )
        self.assertEqual(MailJob.objects.get().kind, MailJob.Kind.TWO_FACTOR_RESET)
        # At the next login the account must set the factor up before anything else.
        fresh = Client()
        fresh.post(LOGIN_URL, {"username": staff.email, "password": PASSWORD})
        self.assertEqual(fresh.get(PROFILE_URL)["Location"], reverse("accounts:two_factor_setup"))

    def test_a_system_administrator_can_be_reset_by_another(self):
        other = administrator("other.admin@example.org")
        self.client.force_login(self.administrator)
        self.client.post(reset_url(other), {"identity_verified": "on"})
        self.assertFalse(has_second_factor(other))
        self.assertTrue(has_second_factor(self.administrator))

    def test_an_account_without_a_factor_has_nothing_to_reset(self):
        plain = create_active_user("plain@example.org")
        self.client.force_login(self.administrator)
        self.assertContains(self.client.get(reset_url(plain)), "keine Zwei-Faktor-Authentifizierung")
        self.client.post(reset_url(plain), {})
        self.assertFalse(MailJob.objects.exists())
        self.assertFalse(SecurityEvent.objects.exists())

    def test_the_rules_are_checked_again_in_the_account_logic(self):
        staff = create_active_user("staff@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        with_second_factor(staff)
        plain_actor = create_active_user("actor@example.org")
        refused = (
            (plain_actor, self.user, second_factor_reset.request_link),
            (staff, self.user, second_factor_reset.request_link),
            (self.administrator, self.administrator, second_factor_reset.reset_privileged),
            (self.administrator, self.user, second_factor_reset.reset_privileged),
            (self.administrator, staff, second_factor_reset.request_link),
        )
        for actor, target, function in refused:
            with self.subTest(actor=actor.email, target=target.email, function=function.__name__):
                with self.assertRaises(second_factor_reset.ResetRefused):
                    function(actor, target)
        blocked = create_active_user("blocked@example.org")
        with_second_factor(blocked)
        blocked.set_status(User.Status.BLOCKED)
        with self.assertRaises(second_factor_reset.ResetRefused):
            second_factor_reset.request_link(self.administrator, blocked)
        self.assertFalse(MailJob.objects.exists())
        self.assertFalse(SecurityEvent.objects.exists())

    def test_a_password_reset_does_not_remove_or_replace_the_link(self):
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {})
        run_mail_queue()
        self.assertTrue(VerificationToken.objects.filter(purpose="two_factor_reset").exists())
        self.assertTrue(has_second_factor(self.user))


@tag("mariadb")
class AdministrationSessionTests(TestCase):
    client_class = VerifiedClient

    def setUp(self):
        self.administrator = administrator()
        self.member = create_active_user("member@example.org")

    def test_a_stale_form_cannot_restore_the_password_or_the_session_version(self):
        from django.contrib import admin
        from django.test import RequestFactory

        stale = User.objects.get(pk=self.member.pk)
        self.member.set_password(OTHER_PASSWORD)
        self.member.save()
        self.member.end_all_sessions()
        model_admin = admin.site._registry[User]
        request = RequestFactory().post("/")
        request.user = self.administrator
        form = model_admin.get_form(request, stale)(instance=stale)
        stale.first_name = "Changed"
        model_admin.save_model(request, stale, form, change=True)
        stored = User.objects.get(pk=self.member.pk)
        self.assertTrue(stored.check_password(OTHER_PASSWORD))
        self.assertEqual(stored.session_version, 1)
        self.assertEqual(stored.first_name, "Changed")

    def test_changing_the_rights_of_an_account_ends_its_sessions(self):
        from django.contrib.auth.models import Group

        member_client = Client()
        member_client.force_login(self.member)
        self.assertEqual(member_client.get(PROFILE_URL).status_code, 200)
        group = Group.objects.create(name="Test role")
        self.client.force_login(self.administrator)
        data = {
            "first_name": "Anna",
            "last_name": "Example",
            "town": "Bern",
            "street": "Examplestrasse",
            "house_number": "12a",
            "postal_code": "3000",
            "country": "CH",
        }
        # An unchanged save keeps the sessions.
        self.client.post(reverse("admin:accounts_user_change", args=(self.member.pk,)), data)
        self.assertEqual(member_client.get(PROFILE_URL).status_code, 200)
        self.client.post(
            reverse("admin:accounts_user_change", args=(self.member.pk,)),
            {**data, "groups": [str(group.pk)]},
        )
        self.assertEqual(member_client.get(PROFILE_URL).status_code, 302)
        event = SecurityEvent.objects.get(event="access_relations_changed")
        self.assertEqual((event.account_id, event.actor_id), (self.member.pk, self.administrator.pk))

    def test_deleting_a_role_is_logged(self):
        from django.contrib.auth.models import Group

        group = Group.objects.create(name="Test role")
        self.client.force_login(self.administrator)
        self.client.post(
            reverse("admin:auth_group_delete", args=(group.pk,)), {"post": "yes"}
        )
        self.assertFalse(Group.objects.filter(pk=group.pk).exists())
        self.assertEqual(SecurityEvent.objects.filter(event="access_relations_changed").count(), 1)


@tag("mariadb")
class SessionEndTests(TestCase):
    client_class = VerifiedClient

    def test_ending_all_sessions_keeps_only_a_session_that_takes_the_new_hash(self):
        user = create_active_user()
        first, second = Client(), Client()
        first.force_login(user)
        second.force_login(user)
        self.assertEqual(first.get(PROFILE_URL).status_code, 200)
        user.end_all_sessions()
        self.assertEqual(first.get(PROFILE_URL).status_code, 302)
        self.assertEqual(second.get(PROFILE_URL).status_code, 302)
        self.assertEqual(User.objects.get(pk=user.pk).session_version, 1)

    def test_the_session_hash_covers_the_password_and_the_version(self):
        user = create_active_user()
        before = user.get_session_auth_hash()
        user.end_all_sessions()
        self.assertNotEqual(before, user.get_session_auth_hash())
        user.set_password(OTHER_PASSWORD)
        self.assertNotEqual(before, user.get_session_auth_hash())


@tag("mariadb")
class EmergencyCommandTests(TestCase):
    def run_command(self, *arguments, answer=None):
        out = StringIO()
        if answer is None:
            call_command("emergency_access", "--no-input", *arguments, stdout=out)
        else:
            with mock.patch("builtins.input", return_value=answer):
                call_command("emergency_access", *arguments, stdout=out)
        return out.getvalue()

    def test_it_resets_the_second_factor_of_the_last_system_administrator(self):
        last = administrator()
        client = VerifiedClient()
        client.force_login(last)
        self.assertEqual(client.get(reverse("admin:index")).status_code, 200)
        output = self.run_command(
            "reset-second-factor", str(last.pk), "--allow-last-administrator", answer=last.email
        )
        self.assertIn(last.email, output)
        self.assertEqual(output.splitlines()[-1], f"The second factor of account {last.pk} was removed.")
        self.assertFalse(has_second_factor(last))
        self.assertEqual(client.get(reverse("admin:index")).status_code, 302)
        entry = SecurityEvent.objects.get()
        self.assertEqual(
            (entry.event, entry.account_id, entry.actor_id, entry.long_retention),
            ("emergency_two_factor_reset", last.pk, None, True),
        )
        self.assertEqual(MailJob.objects.get().kind, MailJob.Kind.TWO_FACTOR_RESET)

    def test_it_refuses_unknown_accounts_and_accounts_without_a_factor(self):
        plain = create_active_user()
        with self.assertRaisesMessage(CommandError, "No account has the internal number"):
            self.run_command("reset-second-factor", "999999")
        with self.assertRaisesMessage(CommandError, "has no second factor"):
            self.run_command("reset-second-factor", str(plain.pk))
        self.assertFalse(SecurityEvent.objects.exists())

    def test_it_refuses_the_last_system_administrator_without_the_explicit_flag(self):
        last = administrator()
        with self.assertRaisesMessage(CommandError, "last active system administrator"):
            self.run_command("reset-second-factor", str(last.pk), answer=last.email)
        self.assertTrue(has_second_factor(last))
        self.assertFalse(SecurityEvent.objects.exists())

    def test_it_does_not_allow_unattended_changes_to_privileged_accounts(self):
        last = administrator()
        with self.assertRaisesMessage(CommandError, "can only be changed interactively"):
            self.run_command("reset-second-factor", str(last.pk), "--allow-last-administrator")
        self.assertTrue(has_second_factor(last))

    def test_it_requires_the_typed_email_address_for_privileged_accounts(self):
        administrator()
        other = administrator("other.admin@example.org")
        other.set_status(User.Status.BLOCKED)
        with self.assertRaisesMessage(CommandError, "Cancelled."):
            self.run_command("unblock-account", str(other.pk), answer="yes")
        self.assertEqual(User.objects.get(pk=other.pk).status, User.Status.BLOCKED)

    def test_it_cancels_cleanly_when_no_input_is_available(self):
        plain = create_active_user()
        with_second_factor(plain)
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaisesMessage(CommandError, "Cancelled."):
                call_command("emergency_access", "reset-second-factor", str(plain.pk), stdout=StringIO())
        self.assertTrue(has_second_factor(plain))

    def test_it_unblocks_a_blocked_system_administrator_only(self):
        admin_user = administrator()
        other = administrator("other.admin@example.org")
        other.set_status(User.Status.BLOCKED)
        with self.assertRaisesMessage(CommandError, "is not blocked"):
            self.run_command("unblock-account", str(admin_user.pk), answer=admin_user.email)
        self.run_command("unblock-account", str(other.pk), answer=other.email)
        self.assertEqual(User.objects.get(pk=other.pk).status, User.Status.ACTIVE)
        entry = SecurityEvent.objects.get()
        self.assertEqual((entry.event, entry.account_id, entry.long_retention), ("emergency_account_unblocked", other.pk, True))

    def test_it_asks_for_confirmation_unless_told_not_to(self):
        user = create_active_user()
        with_second_factor(user)
        with mock.patch("builtins.input", return_value="no"):
            with self.assertRaisesMessage(CommandError, "Cancelled."):
                call_command("emergency_access", "reset-second-factor", str(user.pk), stdout=StringIO())
        self.assertTrue(has_second_factor(user))
        with mock.patch("builtins.input", return_value="yes"):
            call_command("emergency_access", "reset-second-factor", str(user.pk), stdout=StringIO())
        self.assertFalse(has_second_factor(user))

    def test_it_encrypts_the_keys_again_after_the_secret_key_was_replaced(self):
        old, new = "old-key-" + "a1" * 30, "new-key-" + "b2" * 30
        with override_settings(SECRET_KEY=old):
            user = create_active_user()
            device = with_second_factor(user)
            old_token = AuthenticatorDevice.objects.get(pk=device.pk).encrypted_key
        with override_settings(SECRET_KEY=new, SECRET_KEY_FALLBACKS=[old]):
            output = self.run_command("reencrypt-second-factor-keys")
            self.assertIn("1 keys were encrypted again", output)
            current = AuthenticatorDevice.objects.get(pk=device.pk)
            self.assertNotEqual(current.encrypted_key, old_token)
            self.assertTrue(current.verify_token(code_for(current)))
        with override_settings(SECRET_KEY=new, SECRET_KEY_FALLBACKS=[]):
            current = AuthenticatorDevice.objects.get(pk=device.pk)
            self.assertTrue(current.verify_token(code_for(current)))
            output = self.run_command("reencrypt-second-factor-keys")
            self.assertIn("0 keys were encrypted again, 1 were already current", output)
        entries = SecurityEvent.objects.filter(event="emergency_keys_reencrypted")
        self.assertEqual(entries.count(), 2)
        self.assertTrue(all(entry.long_retention for entry in entries))

    def test_it_reports_keys_that_cannot_be_decrypted(self):
        with override_settings(SECRET_KEY="old-key-" + "a1" * 30):
            user = create_active_user()
            with_second_factor(user)
        with override_settings(SECRET_KEY="new-key-" + "b2" * 30, SECRET_KEY_FALLBACKS=[]):
            output = self.run_command("reencrypt-second-factor-keys")
        self.assertIn("1 cannot be decrypted", output)


INITIAL_PASSWORD = "initial passphrase of the operator"
NEW_PASSWORD = "paper lantern rests under silver rain"


@tag("mariadb")
class InitialAdministratorTests(TransactionTestCase):
    # create_superuser() refuses to run inside a transaction, so TestCase cannot be used.

    def run_command(self, email="admin@example.org", passwords=(INITIAL_PASSWORD, INITIAL_PASSWORD),
                    tty=True):
        out = StringIO()
        with (
            mock.patch.dict(os.environ),
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: tty)),
            mock.patch("getpass.getpass", side_effect=list(passwords)) as ask,
        ):
            if email is None:
                os.environ.pop("BOOTSTRAP_SYSTEM_ADMIN_EMAIL", None)
            else:
                os.environ["BOOTSTRAP_SYSTEM_ADMIN_EMAIL"] = email
            call_command("setup_initial_administrator", stdout=out)
        return out.getvalue(), ask

    def test_it_creates_the_administrator_and_prints_no_secret(self):
        output, ask = self.run_command()
        user = User.objects.get()
        self.assertTrue(user.is_superuser and user.is_staff and user.is_active)
        self.assertTrue(user.password_change_required)
        self.assertTrue(user.check_password(INITIAL_PASSWORD))
        self.assertTrue(user.customer_number_id)
        self.assertNotIn(INITIAL_PASSWORD, output)
        self.assertNotIn(user.email, output)
        self.assertEqual(ask.call_count, 2)
        entry = SecurityEvent.objects.get()
        self.assertEqual(
            (entry.event, entry.account_id, entry.long_retention),
            ("initial_administrator_created", user.pk, True),
        )

    def test_it_never_runs_while_a_system_administrator_exists(self):
        self.run_command()
        with self.assertRaisesMessage(CommandError, "already exists"):
            self.run_command(email="second@example.org")
        # A blocked or deactivated system administrator counts as well (set directly in the
        # database, because the last administrator cannot be blocked through the account logic).
        for status in ("blocked", "deactivated"):
            with self.subTest(status=status), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounts_user SET status = %s, is_active = 0 WHERE is_superuser = 1",
                    [status],
                )
                with self.assertRaisesMessage(CommandError, "already exists"):
                    self.run_command(email="second@example.org")
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(User.objects.get().check_password(INITIAL_PASSWORD))

    def test_it_needs_the_email_variable_a_terminal_and_matching_valid_passwords(self):
        with self.assertRaisesMessage(CommandError, "BOOTSTRAP_SYSTEM_ADMIN_EMAIL is not set"):
            self.run_command(email=None)
        with self.assertRaisesMessage(CommandError, "interactively"):
            self.run_command(tty=False)
        with self.assertRaisesMessage(CommandError, "do not match"):
            self.run_command(passwords=(INITIAL_PASSWORD, "something else entirely here"))
        with self.assertRaises(CommandError) as raised:
            self.run_command(passwords=("pw-sentinel-1", "pw-sentinel-1"))
        self.assertIn("not accepted", str(raised.exception))
        self.assertNotIn("pw-sentinel-1", str(raised.exception))
        with self.assertRaises(CommandError):
            self.run_command(email="not an address")
        self.assertFalse(User.objects.exists())
        self.assertFalse(SecurityEvent.objects.exists())

    def test_the_password_is_not_read_from_a_file_or_a_pipe(self):
        with self.assertRaisesMessage(CommandError, "interactively"):
            self.run_command(tty=False, passwords=())

    def test_the_first_login_requires_a_new_password_and_then_the_second_factor(self):
        self.run_command()
        client = Client()
        client.post(LOGIN_URL, {"username": "admin@example.org", "password": INITIAL_PASSWORD})
        password_change = reverse("accounts:password_change")
        # Nothing but the password change and logging out.
        for url in (PROFILE_URL, reverse("admin:index"), reverse("accounts:two_factor"),
                    reverse("accounts:two_factor_setup")):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertNotEqual(response["Location"], url)
                self.assertTrue(
                    response["Location"] == password_change or response["Location"].startswith(LOGIN_URL),
                    response["Location"],
                )
        page = client.get(password_change)
        self.assertContains(page, "Sie müssen ein neues Passwort wählen")
        # The page can be rendered: the branding stylesheet is presentation only.
        self.assertEqual(client.get(reverse("instance_stylesheet")).status_code, 200)
        # The new password must differ from the initial one.
        same = client.post(
            password_change,
            {
                "current_password": INITIAL_PASSWORD,
                "new_password1": INITIAL_PASSWORD,
                "new_password2": INITIAL_PASSWORD,
            },
        )
        self.assertEqual(same.status_code, 200)
        self.assertTrue(User.objects.get().password_change_required)
        changed = client.post(
            password_change,
            {
                "current_password": INITIAL_PASSWORD,
                "new_password1": NEW_PASSWORD,
                "new_password2": NEW_PASSWORD,
            },
        )
        self.assertEqual(changed.status_code, 302)
        user = User.objects.get()
        self.assertFalse(user.password_change_required)
        self.assertTrue(user.check_password(NEW_PASSWORD))
        # Now the second factor is the next thing, and the administration stays closed.
        self.assertEqual(client.get(PROFILE_URL)["Location"], reverse("accounts:two_factor_setup"))
        self.assertEqual(client.get(reverse("accounts:two_factor_setup")).status_code, 200)
        self.assertEqual(client.get(reverse("admin:index")).status_code, 302)

    def test_logging_out_is_possible_during_the_forced_change(self):
        self.run_command()
        client = Client()
        client.post(LOGIN_URL, {"username": "admin@example.org", "password": INITIAL_PASSWORD})
        self.assertEqual(client.post(reverse("accounts:logout")).status_code, 302)
        self.assertNotIn("_auth_user_id", client.session)

    def test_a_password_reset_by_link_also_ends_the_forced_change(self):
        from leg_platform.accounts.password_reset import complete_reset
        from leg_platform.accounts.forms import NewPasswordForm

        self.run_command()
        user = User.objects.get()
        raw = VerificationToken.objects.issue(
            user, VerificationToken.Purpose.PASSWORD_RESET, timedelta(hours=1)
        )
        token = VerificationToken.objects.find_valid(raw, VerificationToken.Purpose.PASSWORD_RESET)
        form = NewPasswordForm(
            user, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
        )
        self.assertTrue(form.is_valid())
        self.assertIsNotNone(complete_reset(token, form))
        self.assertFalse(User.objects.get().password_change_required)
