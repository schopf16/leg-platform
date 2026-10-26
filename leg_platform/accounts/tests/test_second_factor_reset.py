"""Reset of a lost second factor, emergency command, initial administrator and forced password
change (docs/SECURITY.md, sections 3 and 11; D-004, D-005, D-041)."""

import os
import re
from datetime import timedelta
from io import StringIO
from unittest import mock
from urllib.parse import urlsplit

from django.core import mail
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import email_change, second_factor_reset
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    MailJob,
    PendingEmailChange,
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


def login_change_token(user, address):
    """Prepare the same pending request/token consumed by the real confirmation service."""
    PendingEmailChange.objects.create(
        user=user, kind=PendingEmailChange.Kind.LOGIN, address=address,
        expires_at=timezone.now() + timedelta(hours=24),
    )
    raw = VerificationToken.objects.issue(
        user, VerificationToken.Purpose.EMAIL_CHANGE, timedelta(hours=24), target_email=address,
    )
    return VerificationToken.objects.find_valid(raw, VerificationToken.Purpose.EMAIL_CHANGE)


@tag("mariadb")
class RecoveryAddressBindingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("recovery.owner@example.org")
        with_second_factor(cls.user)

    def issued_link(self):
        send = second_factor_reset.process_reset_link(self.user.pk, self.user.email)
        self.assertIsNotNone(send)
        with mock.patch.object(second_factor_reset.emails, "send_two_factor_reset_link") as delivery:
            send()
        raw = delivery.call_args.args[1]
        return raw, second_factor_reset.find_link(raw)

    def test_issued_and_opened_link_is_revoked_by_confirmed_login_change_and_fresh_link_works(self):
        raw, old_token = self.issued_link()
        self.assertEqual(old_token.target_email, self.user.normalized_email)
        owner = Client()
        path = reverse("accounts:two_factor_reset_verify", args=[raw])
        confirm_path = owner.get(path)["Location"]
        self.assertTrue(has_second_factor(self.user))
        token = login_change_token(self.user, "new.recovery.owner@example.org")
        self.assertIsNotNone(email_change.confirm_change(token))
        self.user.refresh_from_db()
        self.assertFalse(VerificationToken.objects.filter(pk=old_token.pk).exists())
        self.assertIsNone(second_factor_reset.find_link(raw))
        self.assertIsNone(second_factor_reset.find_link_by_digest(old_token.digest))
        self.assertIsNone(second_factor_reset.confirm_link(old_token))
        self.assertContains(owner.post(confirm_path, follow=True), "Link nicht mehr gültig")
        self.assertTrue(has_second_factor(self.user))
        fresh_raw, fresh_token = self.issued_link()
        self.assertNotEqual(raw, fresh_raw)
        self.assertEqual(fresh_token.target_email, self.user.normalized_email)
        self.assertIsNotNone(second_factor_reset.confirm_link(fresh_token))
        self.assertFalse(has_second_factor(self.user))

    def test_lookup_and_locked_confirmation_reject_mismatched_and_unbound_tokens(self):
        for address in ("", "former.owner@example.org"):
            with self.subTest(address=address):
                raw = VerificationToken.objects.issue(
                    self.user, VerificationToken.Purpose.TWO_FACTOR_RESET,
                    timedelta(hours=24), target_email=address,
                )
                token = VerificationToken.objects.find_valid(
                    raw, VerificationToken.Purpose.TWO_FACTOR_RESET
                )
                self.assertIsNone(second_factor_reset.find_link(raw))
                self.assertIsNone(second_factor_reset.find_link_by_digest(token.digest))
                self.assertIsNone(second_factor_reset.confirm_link(token))
                self.assertTrue(has_second_factor(self.user))

    def test_locked_confirmation_rechecks_address_after_lookup(self):
        raw, token = self.issued_link()
        self.user.change_email("changed.after.lookup@example.org")
        self.assertTrue(VerificationToken.objects.filter(pk=token.pk).exists())
        self.assertIsNone(second_factor_reset.find_link(raw))
        self.assertIsNone(second_factor_reset.find_link_by_digest(token.digest))
        self.assertIsNone(second_factor_reset.confirm_link(token))
        self.assertTrue(has_second_factor(self.user))

    def test_pending_login_change_and_confirmed_secondary_change_keep_current_mailbox_link(self):
        raw, token = self.issued_link()
        login_change_token(self.user, "still.pending@example.org")
        self.assertIsNotNone(email_change.process_request(self.user.pk, "still.pending@example.org"))
        self.assertIsNotNone(second_factor_reset.find_link(raw))
        PendingEmailChange.objects.create(
            user=self.user, kind=PendingEmailChange.Kind.SECONDARY,
            address="new.secondary@example.org", expires_at=timezone.now() + timedelta(hours=24),
        )
        secondary_raw = VerificationToken.objects.issue(
            self.user, VerificationToken.Purpose.SECONDARY_EMAIL, timedelta(hours=24),
            target_email="new.secondary@example.org",
        )
        secondary_token = VerificationToken.objects.find_valid(
            secondary_raw, VerificationToken.Purpose.SECONDARY_EMAIL
        )
        self.assertIsNotNone(email_change.confirm_change(secondary_token))
        self.assertIsNotNone(second_factor_reset.find_link(raw))
        self.assertIsNotNone(second_factor_reset.confirm_link(token))

    def test_delayed_dispatch_rechecks_recipient_and_superseded_token(self):
        old_send = second_factor_reset.process_reset_link(self.user.pk, self.user.email)
        newer_send = second_factor_reset.process_reset_link(self.user.pk, self.user.email)
        with mock.patch.object(second_factor_reset.emails, "send_two_factor_reset_link") as delivery:
            old_send()
            delivery.assert_not_called()
            newer_send()
            delivery.assert_called_once()
        delayed_send = second_factor_reset.process_reset_link(self.user.pk, self.user.email)
        self.assertIsNotNone(email_change.confirm_change(
            login_change_token(self.user, "changed.before.dispatch@example.org")
        ))
        with mock.patch.object(second_factor_reset.emails, "send_two_factor_reset_link") as delivery:
            delayed_send()
            delivery.assert_not_called()


@tag("mariadb")
class RecoveryIssuanceRaceTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("Real row-lock races require MariaDB.")
        self.user = create_active_user("recovery.race@example.org")
        with_second_factor(self.user)

    def test_dispatch_holds_account_lock_until_delivery_returns_then_address_change_revokes_link(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.db import connections

        token = login_change_token(self.user, "changed.during.dispatch@example.org")
        send = second_factor_reset.process_reset_link(self.user.pk, self.user.email)
        self.assertIsNotNone(send)
        delivering, change_query, delivered, changed = Event(), Event(), Event(), Event()
        observed = {}

        def pause_delivery(address, raw, lifetime, greeting_name):
            # Pause inside the actual delivery boundary, not token issuance.
            observed["dispatch_atomic"] = connections["default"].in_atomic_block
            observed["address"], observed["raw"] = address, raw
            delivering.set()
            if not change_query.wait(timeout=10):
                raise AssertionError("Address change did not reach its account lock.")
            observed["changed_before_delivery_returned"] = changed.wait(timeout=1)
            delivered.set()

        def dispatch():
            connections.close_all()
            try:
                send()
            finally:
                connections.close_all()

        def change():
            connections.close_all()
            try:
                if not delivering.wait(timeout=10):
                    raise AssertionError("Dispatch did not enter the delivery callback.")

                def record_lock(execute, sql, params, many, context):
                    if "FOR UPDATE" in sql.upper() and User._meta.db_table in sql:
                        change_query.set()
                    return execute(sql, params, many, context)

                with connections["default"].execute_wrapper(record_lock):
                    result = email_change.confirm_change(token)
                observed["delivery_returned_before_change"] = delivered.is_set()
                changed.set()
                return result is not None
            finally:
                connections.close_all()

        with mock.patch.object(
            second_factor_reset.emails, "send_two_factor_reset_link", side_effect=pause_delivery
        ) as delivery:
            with ThreadPoolExecutor(max_workers=2) as executor:
                dispatch_future = executor.submit(dispatch)
                change_future = executor.submit(change)
                dispatch_future.result(timeout=20)
                self.assertTrue(change_future.result(timeout=20))
            delivery.assert_called_once()
        self.assertTrue(observed["dispatch_atomic"])
        self.assertFalse(observed["changed_before_delivery_returned"])
        self.assertTrue(observed["delivery_returned_before_change"])
        self.assertEqual(observed["address"], self.user.email)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "changed.during.dispatch@example.org")
        self.assertIsNone(second_factor_reset.find_link(observed["raw"]))
        self.assertFalse(VerificationToken.objects.filter(
            user=self.user, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
        ).exists())
        self.assertTrue(has_second_factor(self.user))

    def test_worker_issuance_serializes_with_confirmed_login_change(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.db import connections

        token = login_change_token(self.user, "recovery.race.changed@example.org")
        issuing, change_query, changed = Event(), Event(), Event()
        real_issue = VerificationToken.objects.issue
        observed = {}
        user_id, address = self.user.pk, self.user.email

        def pause_issue(user, purpose, lifetime, **kwargs):
            issuing.set()
            if not change_query.wait(timeout=10):
                raise AssertionError("Address change did not reach its account lock.")
            observed["change_completed_before_issue"] = changed.wait(timeout=1)
            observed["raw"] = real_issue(user, purpose, lifetime, **kwargs)
            return observed["raw"]

        def worker():
            connections.close_all()
            try:
                with mock.patch.object(VerificationToken.objects, "issue", side_effect=pause_issue):
                    return second_factor_reset.process_reset_link(user_id, address)
            finally:
                connections.close_all()

        def change():
            connections.close_all()
            try:
                if not issuing.wait(timeout=10):
                    raise AssertionError("Worker did not reach issuance.")

                def record_lock(execute, sql, params, many, context):
                    if "FOR UPDATE" in sql.upper() and User._meta.db_table in sql:
                        change_query.set()
                    return execute(sql, params, many, context)

                with connections["default"].execute_wrapper(record_lock):
                    result = email_change.confirm_change(token)
                changed.set()
                return result is not None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            worker_future = executor.submit(worker)
            change_future = executor.submit(change)
            send = worker_future.result(timeout=20)
            self.assertTrue(change_future.result(timeout=20))
        self.assertFalse(observed["change_completed_before_issue"])
        self.assertIsNone(second_factor_reset.find_link(observed["raw"]))
        self.assertFalse(VerificationToken.objects.filter(
            user=self.user, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
        ).exists())
        with mock.patch.object(second_factor_reset.emails, "send_two_factor_reset_link") as delivery:
            send()
            delivery.assert_not_called()
        self.user.refresh_from_db()
        self.assertIsNone(second_factor_reset.process_reset_link(user_id, address))
        self.assertIsNotNone(second_factor_reset.process_reset_link(user_id, self.user.email))
        self.assertTrue(has_second_factor(self.user))

    def test_parallel_workers_leave_only_one_current_link(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections

        barrier = Barrier(2)
        user_id, address = self.user.pk, self.user.email

        def worker(_):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                return second_factor_reset.process_reset_link(user_id, address)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            sends = list(executor.map(worker, range(2)))
        self.assertEqual(VerificationToken.objects.filter(
            user=self.user, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
        ).count(), 1)
        with mock.patch.object(second_factor_reset.emails, "send_two_factor_reset_link") as delivery:
            for send in sends:
                self.assertIsNotNone(send)
                send()
            delivery.assert_called_once()
        token = second_factor_reset.find_link(delivery.call_args.args[1])
        self.assertEqual(token.target_email, self.user.normalized_email)
        self.assertIsNotNone(second_factor_reset.confirm_link(token))


@tag("mariadb")
class AdministrationResetTests(TestCase):
    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = administrator()
        cls.user = create_active_user("member@example.org")
        with_second_factor(cls.user)

    def test_account_settings_offer_recovery_including_the_own_account(self):
        self.client.force_login(self.administrator)
        other = self.client.get(reverse("admin:accounts_user_change", args=(self.user.pk,)))
        self.assertContains(other, reset_url(self.user))
        own = self.client.get(reverse("admin:accounts_user_change", args=(self.administrator.pk,)))
        self.assertContains(own, reset_url(self.administrator))

    def test_staff_can_open_recovery_but_must_confirm_the_id_check(self):
        staff = create_active_user("staff@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        from django.contrib.auth.models import Permission

        staff.user_permissions.set(Permission.objects.filter(content_type__app_label="accounts"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reset_url(self.user)).status_code, 200)
        self.assertEqual(self.client.post(reset_url(self.user), {}).status_code, 200)
        self.assertTrue(has_second_factor(self.user))
        self.assertFalse(MailJob.objects.exists())

    def test_own_recovery_requires_the_same_id_confirmation(self):
        self.client.force_login(self.administrator)
        self.assertEqual(self.client.get(reset_url(self.administrator)).status_code, 200)
        self.assertEqual(self.client.post(reset_url(self.administrator), {}).status_code, 200)
        self.assertTrue(has_second_factor(self.administrator))

    def test_a_user_gets_a_link_and_keeps_the_factor_until_it_is_confirmed(self):
        self.client.force_login(self.administrator)
        response = self.client.post(reset_url(self.user), {"identity_verified": "on"})
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
        self.client.post(reset_url(self.user), {"identity_verified": "on"})
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
        self.assertRedirects(done, reverse("accounts:login_method"), fetch_redirect_response=False)
        self.assertFalse(has_second_factor(self.user))
        # Every prior session ended; old passwords cannot bypass restricted recovery.
        self.assertEqual(member.get(PROFILE_URL).status_code, 302)
        self.assertEqual(
            Client().post(LOGIN_URL, {"username": self.user.email, "password": PASSWORD}).status_code,
            200,
        )
        reset = SecurityEvent.objects.get(event="two_factor_reset")
        self.assertEqual((reset.account_id, reset.actor_id, reset.long_retention), (self.user.pk, None, False))
        notice = MailJob.objects.get(kind=MailJob.Kind.TWO_FACTOR_RESET)
        self.assertEqual(notice.address, self.user.email)

    def test_the_link_works_once_and_a_newer_link_replaces_the_older(self):
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {"identity_verified": "on"})
        run_mail_queue()
        first = link_in(mail.outbox[0])
        self.client.post(reset_url(self.user), {"identity_verified": "on"})
        run_mail_queue()
        second = link_in(mail.outbox[1])
        self.assertNotEqual(first, second)
        owner = Client()
        self.assertContains(owner.get(first, follow=True), "Link nicht mehr gültig")
        confirm = owner.get(second)["Location"]
        owner.post(confirm)
        self.assertFalse(has_second_factor(self.user))
        # The consumed link cannot be used again, not even from the remembered session.
        self.assertContains(Client().get(second, follow=True), "Link nicht mehr gültig")
        self.assertContains(owner.post(confirm, follow=True), "Link nicht mehr gültig")

    def test_an_expired_link_is_not_valid(self):
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {"identity_verified": "on"})
        run_mail_queue()
        path = link_in(mail.outbox[0])
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertContains(Client().get(path, follow=True), "Link nicht mehr gültig")
        self.assertTrue(has_second_factor(self.user))

    def test_a_recovery_link_stays_valid_after_promotion_without_mutating_the_factor(self):
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {"identity_verified": "on"})
        run_mail_queue()
        path = link_in(mail.outbox[0])
        self.user.set_privileges(is_staff=True, is_superuser=False)
        self.assertEqual(Client().get(path).status_code, 302)
        self.assertEqual(second_factor_reset.find_link_by_digest(
            VerificationToken.objects.get(user=self.user, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET).digest
        ).user_id, self.user.pk)
        self.assertTrue(has_second_factor(self.user))

    def test_a_link_is_sent_for_both_password_only_and_staff_accounts(self):
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
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual({message.to[0] for message in mail.outbox}, {plain.email, self.user.email})
        self.assertEqual(set(VerificationToken.objects.filter(
            purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
        ).values_list("user_id", flat=True)), {plain.pk, self.user.pk})

    def test_a_link_does_not_reach_another_account_after_the_address_changed(self):
        self.client.force_login(self.administrator)
        self.client.post(reset_url(self.user), {"identity_verified": "on"})
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

    def test_staff_recovery_waits_for_owner_confirmation_then_ends_all_sessions(self):
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
        self.assertTrue(has_second_factor(staff))
        self.assertEqual(staff_client.get(reverse("admin:index")).status_code, 200)
        run_mail_queue()
        token = VerificationToken.objects.get(user=staff, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET)
        self.assertIsNotNone(second_factor_reset.confirm_link(token))
        self.assertFalse(has_second_factor(staff))
        self.assertEqual(staff_client.get(reverse("admin:index")).status_code, 302)
        entry = SecurityEvent.objects.get(event="two_factor_reset")
        self.assertEqual(
            (entry.account_id, entry.actor_id, entry.long_retention),
            (staff.pk, None, True),
        )
        self.assertEqual(MailJob.objects.get().kind, MailJob.Kind.TWO_FACTOR_RESET)
        # At the next login the account must set the factor up before anything else.
        fresh = Client()
        self.assertEqual(fresh.post(LOGIN_URL, {"username": staff.email, "password": PASSWORD}).status_code, 200)
        self.assertTrue(fresh.get(PROFILE_URL)["Location"].startswith(LOGIN_URL))

    def test_a_system_administrator_can_be_reset_by_another(self):
        other = administrator("other.admin@example.org")
        self.client.force_login(self.administrator)
        self.client.post(reset_url(other), {"identity_verified": "on"})
        self.assertTrue(has_second_factor(other))
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_RESET_LINK, account=other).exists())
        self.assertTrue(has_second_factor(self.administrator))

    def test_an_active_account_without_a_factor_uses_the_same_recovery_link(self):
        plain = create_active_user("plain@example.org")
        self.client.force_login(self.administrator)
        self.assertContains(self.client.get(reset_url(plain)), 'name="identity_verified"')
        self.client.post(reset_url(plain), {"identity_verified": "on"})
        self.assertTrue(MailJob.objects.filter(
            kind=MailJob.Kind.TWO_FACTOR_RESET_LINK, account=plain
        ).exists())
        plain.refresh_from_db()
        self.assertTrue(plain.check_password(PASSWORD))

    def test_the_rules_are_checked_again_in_the_account_logic(self):
        staff = create_active_user("staff@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        with_second_factor(staff)
        plain_actor = create_active_user("actor@example.org")
        refused = (
            (plain_actor, self.user, second_factor_reset.request_link),
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
        self.client.post(reset_url(self.user), {"identity_verified": "on"})
        run_mail_queue()
        self.assertTrue(VerificationToken.objects.filter(purpose="two_factor_reset").exists())
        self.assertTrue(has_second_factor(self.user))


@tag("mariadb")
class AdministrationSessionTests(TestCase):
    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = administrator()
        cls.member = create_active_user("member@example.org")

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
        self.assertEqual(client.get(PROFILE_URL)["Location"], reverse("accounts:login_method"))
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
