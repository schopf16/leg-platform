"""Request limits and the mail queue (docs/SECURITY.md, sections 3 and 11; D-037)."""

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock, patch

from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.db import OperationalError, connection, connections
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import mail_addresses
from leg_platform.accounts import mail_queue, rate_limits
from leg_platform.accounts.models import (
    MailJob,
    RateLimitBucket,
    RateLimitEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    create_active_user,
    hour_and_day_limits,
    registration_data,
    reset_request_data,
    run_mail_queue,
)

ADDRESS_LIMITS = rate_limits.MAIL_ADDRESS_LIMITS[:2]
HOURLY, DAILY = ADDRESS_LIMITS[0][0], ADDRESS_LIMITS[1][0]


def post_registration(address, remote_addr="10.20.0.1"):
    return Client().post(
        reverse("accounts:registration"),
        registration_data(address),
        REMOTE_ADDR=remote_addr,
    )


def post_reset(address, remote_addr="10.20.0.1"):
    return Client().post(
        reverse("accounts:password_reset"), reset_request_data(address), REMOTE_ADDR=remote_addr
    )


def age_events(delta):
    RateLimitEvent.objects.update(occurred_at=timezone.now() - delta)


@tag("mariadb")
class RateLimitTests(TestCase):
    def test_a_request_above_the_hourly_limit_is_refused(self):
        results = [rate_limits.consume("scope", "a", ADDRESS_LIMITS) for _ in range(HOURLY + 2)]

        self.assertEqual(results, [True] * HOURLY + [False, False])

    def test_the_window_slides_and_refused_requests_do_not_extend_it(self):
        for _ in range(HOURLY + 2):
            rate_limits.consume("scope", "a", ADDRESS_LIMITS)

        age_events(timedelta(hours=1, minutes=1))

        # Only the counted requests aged; the two refused ones were never recorded.
        self.assertEqual(RateLimitEvent.objects.count(), HOURLY)
        self.assertTrue(rate_limits.consume("scope", "a", ADDRESS_LIMITS))

    def test_the_daily_limit_applies_over_several_hours(self):
        for _ in range(DAILY):
            age_events(timedelta(hours=2))
            self.assertTrue(rate_limits.consume("scope", "a", ADDRESS_LIMITS))

        # The daily number of requests is used up with none in the last hour except the latest.
        self.assertFalse(rate_limits.consume("scope", "a", ADDRESS_LIMITS))

    def test_keys_are_independent_per_scope_and_value(self):
        for _ in range(HOURLY):
            rate_limits.consume("scope", "a", ADDRESS_LIMITS)

        self.assertFalse(rate_limits.consume("scope", "a", ADDRESS_LIMITS))
        self.assertTrue(rate_limits.consume("scope", "b", ADDRESS_LIMITS))
        self.assertTrue(rate_limits.consume("other", "a", ADDRESS_LIMITS))

    def test_only_keyed_digests_are_stored(self):
        rate_limits.consume("mail-address", "person@example.org", ADDRESS_LIMITS)

        digest = RateLimitBucket.objects.get().digest
        self.assertRegex(digest, r"\A[0-9a-f]{64}\Z")
        self.assertNotIn("person", digest)

    def test_unused_buckets_are_deleted_with_their_events(self):
        rate_limits.consume("scope", "old", ADDRESS_LIMITS)
        rate_limits.consume("scope", "recent", ADDRESS_LIMITS)
        old = RateLimitBucket.objects.order_by("pk").first()
        RateLimitBucket.objects.filter(pk=old.pk).update(
            last_seen_at=timezone.now() - timedelta(hours=25)
        )

        self.assertEqual(rate_limits.delete_unused_buckets(), 1)

        self.assertEqual(RateLimitBucket.objects.count(), 1)
        self.assertEqual(RateLimitEvent.objects.count(), 1)


@tag("mariadb")
class MailWorkerTransactionTests(TransactionTestCase):
    # TestCase wraps every test in a transaction, so connection.in_atomic_block would always be true.
    def test_no_database_transaction_is_open_while_the_email_is_sent(self):
        post_registration("new.person@example.org")
        states = []

        def record(*_args, **_kwargs):
            states.append(connection.in_atomic_block)

        with patch("leg_platform.accounts.emails.EmailMessage.send", record):
            mail_queue.process_pending()

        self.assertEqual(states, [False])


class IpKeyTests(SimpleTestCase):
    def test_ipv6_is_counted_by_its_64_prefix_and_ipv4_as_it_is(self):
        self.assertEqual(rate_limits.ip_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd"), "2001:db8:1:2::/64")
        self.assertEqual(rate_limits.ip_key("192.0.2.7"), "192.0.2.7")
        self.assertEqual(rate_limits.ip_key(None), "unknown")
        self.assertEqual(rate_limits.ip_key(""), "unknown")


@tag("mariadb")
class ConcurrentRateLimitTests(TransactionTestCase):
    def test_parallel_requests_cannot_exceed_the_limit(self):
        barrier = Barrier(HOURLY + 4)

        def attempt(_index):
            try:
                barrier.wait()
                return rate_limits.consume("scope", "same", ADDRESS_LIMITS)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=HOURLY + 4) as executor:
            results = list(executor.map(attempt, range(HOURLY + 4)))

        self.assertEqual(sum(results), HOURLY)


@tag("mariadb")
@hour_and_day_limits
class MailRequestTests(TestCase):
    def test_the_request_only_stores_a_job_and_sends_nothing(self):
        response = post_registration("new.person@example.org")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(User.objects.count(), 0)
        job = MailJob.objects.get()
        self.assertEqual((job.kind, job.address, job.suppressed), ("registration", "new.person@example.org", False))

    def test_the_worker_sends_the_email_and_removes_the_job(self):
        post_registration("new.person@example.org")

        self.assertEqual(run_mail_queue(), 1)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_the_request_path_never_looks_at_accounts_whatever_the_state_of_the_address(self):
        create_active_user("active.person@example.org")
        blocked = create_active_user("blocked.person@example.org")
        blocked.set_status(User.Status.BLOCKED)
        User.objects.create_user("pending.person@example.org")
        shapes = {}
        # The instance-wide counters (one per kind) exist after the first request of each kind;
        # every later request then takes the same path.
        post_registration("warmup.person@example.org", remote_addr="10.21.0.250")
        post_reset("warmup.person@example.org", remote_addr="10.21.0.251")
        for name, address in (
            ("new", "new.person@example.org"),
            ("active", "active.person@example.org"),
            ("blocked", "blocked.person@example.org"),
            ("pending", "pending.person@example.org"),
            ("unknown for reset", "unknown.person@example.org"),
        ):
            post = post_reset if name.endswith("reset") else post_registration
            with CaptureQueriesContext(connection) as queries:
                post(address, remote_addr=f"10.21.0.{len(shapes) + 1}")
            statements = [query["sql"] for query in queries]
            self.assertFalse(
                [sql for sql in statements if "accounts_user" in sql], name
            )
            shapes[name] = sorted(sql.split()[0] for sql in statements)

        # The same statements for every address. Their order depends on the keyed digests of the
        # buckets, which are locked in digest order, so it is compared as a multiset.
        first = next(iter(shapes.values()))
        for name, shape in shapes.items():
            self.assertEqual(shape, first, name)

    def test_variants_of_an_address_share_one_budget(self):
        for variant in ("New.Person@EXAMPLE.org", " new.person@example.org", "NEW.PERSON@example.org"):
            post_registration(variant)
        for _ in range(HOURLY - 3):
            post_registration("new.person@example.org")
        post_registration("new.person@example.org")

        suppressed = list(MailJob.objects.order_by("pk").values_list("suppressed", flat=True))
        self.assertEqual(suppressed, [False] * HOURLY + [True])

    def test_registration_and_password_reset_share_the_budget(self):
        for _ in range(HOURLY // 2):
            post_registration("new.person@example.org")
            post_reset("new.person@example.org")
        post_reset("new.person@example.org")

        suppressed = list(MailJob.objects.order_by("pk").values_list("suppressed", flat=True))
        self.assertEqual(suppressed, [False] * HOURLY + [True])

    def test_a_suppressed_request_gets_the_same_response_and_sends_nothing(self):
        responses = [post_registration("new.person@example.org") for _ in range(HOURLY + 1)]

        self.assertEqual({(r.status_code, r["Location"]) for r in responses}, {(302, reverse("accounts:registration_sent"))})
        run_mail_queue()
        # The allowed requests send one email each; the last one is discarded by the worker.
        self.assertEqual(len(mail.outbox), HOURLY)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_a_suppressed_request_replaces_no_pending_registration(self):
        post_registration("new.person@example.org")
        run_mail_queue()
        pending = User.objects.get()
        for _ in range(HOURLY - 1):
            post_registration("new.person@example.org")
            run_mail_queue()
        post_registration("new.person@example.org")  # above the limit
        run_mail_queue()

        self.assertEqual(len(mail.outbox), HOURLY)
        self.assertEqual(User.objects.count(), 1)
        self.assertNotEqual(User.objects.get().pk, pending.pk)
        # The link of the last allowed email still works, because the next request changed nothing.
        token = VerificationToken.objects.get()
        self.assertEqual(token.user_id, User.objects.get().pk)

    def test_one_client_address_is_limited_across_different_email_addresses(self):
        limit = rate_limits.MAIL_IP_LIMITS[0][0]
        for number in range(limit):
            post_registration(f"person{number}@example.org", remote_addr="10.30.0.5")

        refused = post_registration("one.more@example.org", remote_addr="10.30.0.5")

        # Over the limit of the client address: the visible refusal, and nothing is stored.
        self.assertEqual(refused.status_code, 429)
        self.assertContains(refused, "Zu viele Anfragen, bitte versuchen Sie es später erneut.", status_code=429)
        self.assertEqual(MailJob.objects.count(), limit)
        self.assertFalse(MailJob.objects.filter(suppressed=True).exists())
        self.assertFalse(User.objects.filter(email="one.more@example.org").exists())
        post_registration("other@example.org", remote_addr="10.30.0.6")
        self.assertFalse(MailJob.objects.order_by("pk").last().suppressed)

    def test_an_unknown_client_address_shares_one_strict_bucket(self):
        limit = rate_limits.MAIL_IP_LIMITS[0][0]
        with patch("leg_platform.accounts.mail_queue.axes_client_ip", return_value=None):
            for number in range(limit):
                post_registration(f"person{number}@example.org")
            refused = post_registration("one.more@example.org")

        self.assertEqual(refused.status_code, 429)
        self.assertEqual(MailJob.objects.count(), limit)

    def test_a_refused_client_address_does_not_use_up_the_budget_of_a_victims_address(self):
        limit = rate_limits.MAIL_IP_LIMITS[0][0]
        for number in range(limit):
            post_registration(f"filler{number}@example.org", remote_addr="10.31.0.5")

        refused = [
            post_registration("victim.person@example.org", remote_addr="10.31.0.5") for _ in range(5)
        ]

        # The attacker's address is over its limit, so nothing was counted or stored for the victim.
        self.assertEqual({response.status_code for response in refused}, {429})
        self.assertEqual(MailJob.objects.count(), limit)
        self.assertFalse(RateLimitEvent.objects.filter(bucket__digest=rate_limits.keyed_digest(
            "mail-address\nvictim.person@example.org", rate_limits.RATE_LIMIT_PURPOSE
        )).exists())
        post_registration("victim.person@example.org", remote_addr="10.31.0.6")
        self.assertFalse(MailJob.objects.order_by("pk").last().suppressed)
        self.assertEqual(
            RateLimitEvent.objects.filter(
                bucket__digest=rate_limits.keyed_digest(
                    "mail-address\nvictim.person@example.org", rate_limits.RATE_LIMIT_PURPOSE
                )
            ).count(),
            1,
        )

    def test_a_suppressed_job_keeps_no_address(self):
        for _ in range(HOURLY + 1):
            post_registration("new.person@example.org")

        suppressed = MailJob.objects.get(suppressed=True)
        self.assertEqual(suppressed.address, "")

    def test_the_instance_wide_cap_refuses_and_is_logged(self):
        with patch.object(rate_limits, "MAIL_GLOBAL_LIMITS", ((2, timedelta(hours=1)),)):
            with patch.object(mail_queue, "_last_rejection_log", float("-inf")):
                with self.assertLogs("leg_platform.accounts.mail_queue", "WARNING") as logs:
                    responses = [
                        post_registration(f"person{number}@example.org", remote_addr=f"10.32.0.{number}")
                        for number in range(4)
                    ]

        self.assertEqual([response.status_code for response in responses], [302, 302, 429, 429])
        self.assertEqual(MailJob.objects.count(), 2)
        self.assertFalse(MailJob.objects.filter(suppressed=True).exists())
        # The log is limited to one line a minute: two refusals, one line.
        self.assertEqual(len(logs.output), 1)
        self.assertIn("limit is used up", logs.output[0])

    def test_ipv6_addresses_of_one_prefix_share_the_budget(self):
        limit = rate_limits.MAIL_IP_LIMITS[0][0]
        for number in range(limit):
            post_registration(f"person{number}@example.org", remote_addr=f"2001:db8:1:2::{number + 1:x}")

        refused = post_registration("one.more@example.org", remote_addr="2001:db8:1:2::ffff")

        self.assertEqual(refused.status_code, 429)
        self.assertEqual(MailJob.objects.count(), limit)
        post_registration("other@example.org", remote_addr="2001:db8:1:3::1")
        self.assertFalse(MailJob.objects.order_by("pk").last().suppressed)

    def test_the_information_email_is_sent_at_most_once_a_day(self):
        create_active_user("taken.person@example.org")

        for _ in range(3):
            post_registration("taken.person@example.org")
            run_mail_queue()

        self.assertEqual(len(mail.outbox), 1)
        age_events(timedelta(hours=25))
        post_registration("taken.person@example.org")
        run_mail_queue()
        self.assertEqual(len(mail.outbox), 2)

    def test_emails_are_sent_from_the_configured_sender(self):
        post_registration("new.person@example.org")
        run_mail_queue()

        self.assertEqual(mail.outbox[0].from_email, mail_addresses.sender())


@tag("mariadb")
@hour_and_day_limits
class MailWorkerTests(TestCase):
    def test_a_failing_job_is_repeated_later_and_then_given_up(self):
        post_registration("new.person@example.org")

        with patch("leg_platform.accounts.emails.EmailMessage.send", side_effect=OSError("x")):
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR") as logs:
                self.assertEqual(mail_queue.process_pending(), (0, 1))
                # Not due yet: the next attempt waits.
                self.assertEqual(mail_queue.process_pending(), (0, 0))
                for attempt in (2, 3):
                    MailJob.objects.update(next_attempt_at=timezone.now())
                    self.assertEqual(mail_queue.process_pending(), (0, 1), attempt)
                # The fourth turn gives the job up.
                MailJob.objects.update(next_attempt_at=timezone.now())
                self.assertEqual(mail_queue.process_pending(), (0, 1))

        self.assertEqual(MailJob.objects.count(), 0)
        self.assertIn("given up", "\n".join(logs.output))
        # Only the pending registration of the last attempt is left; it expires like any other.
        self.assertEqual(list(User.objects.values_list("status", flat=True)), ["unverified"])

    def test_a_job_that_is_being_processed_is_not_taken_by_another_worker(self):
        post_registration("new.person@example.org")

        first = mail_queue._claim()
        second = mail_queue._claim()

        self.assertIsInstance(first, tuple)
        self.assertIsNone(second)
        self.assertEqual(MailJob.objects.get().attempts, 1)

    def test_a_database_error_in_the_handler_needs_no_further_write(self):
        post_registration("new.person@example.org")

        with patch.dict(
            mail_queue.HANDLERS,
            {MailJob.Kind.REGISTRATION: Mock(side_effect=OperationalError(1213, "Deadlock found"))},
        ):
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR"):
                result = mail_queue.process_pending()

        self.assertEqual(result, (0, 1))
        self.assertEqual(MailJob.objects.get().attempts, 1)

    def test_a_database_error_while_claiming_ends_the_round_without_raising(self):
        with patch.object(mail_queue, "_claim", side_effect=OperationalError(2006, "gone away")):
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR") as logs:
                self.assertEqual(mail_queue.process_pending(), (0, 1))

        self.assertIn("OperationalError", "\n".join(logs.output))

    def test_a_notification_job_is_sent_without_a_request_limit(self):
        for _ in range(5):
            mail_queue.enqueue(MailJob.Kind.PASSWORD_CHANGED, "taken.person@example.org")

        self.assertEqual(run_mail_queue(), 5)
        self.assertEqual(len(mail.outbox), 5)

    def test_stale_jobs_are_deleted(self):
        post_registration("new.person@example.org")
        MailJob.objects.update(created_at=timezone.now() - timedelta(hours=2))
        post_registration("fresh.person@example.org")

        self.assertEqual(mail_queue.delete_stale_jobs(), 1)

        self.assertEqual(MailJob.objects.get().address, "fresh.person@example.org")

    def test_a_notification_survives_a_worker_that_was_down_for_some_hours(self):
        mail_queue.enqueue(MailJob.Kind.PASSWORD_CHANGED, "taken.person@example.org")
        MailJob.objects.update(created_at=timezone.now() - timedelta(hours=5))

        self.assertEqual(mail_queue.delete_stale_jobs(), 0)

        MailJob.objects.update(created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(mail_queue.delete_stale_jobs(), 1)

    def test_the_instance_wide_cap_of_one_kind_does_not_block_the_other(self):
        caps = ((2, timedelta(hours=1)),)
        with patch.object(mail_queue.rate_limits, "MAIL_GLOBAL_LIMITS", caps):
            for number in range(3):
                post_registration(f"person{number}@example.org", remote_addr=f"10.33.0.{number}")
            post_reset("reset.person@example.org", remote_addr="10.33.0.9")

        suppressed = list(MailJob.objects.order_by("pk").values_list("suppressed", flat=True))
        self.assertEqual(suppressed, [False, False, False])
        # The reset is a kind of its own: the registration cap did not refuse it (the third
        # registration was refused and stored nothing).
        self.assertEqual(
            list(MailJob.objects.order_by("pk").values_list("kind", flat=True)),
            [MailJob.Kind.REGISTRATION, MailJob.Kind.REGISTRATION, MailJob.Kind.PASSWORD_RESET],
        )

    def test_the_worker_command_processes_the_queue(self):
        post_registration("new.person@example.org")
        output = io.StringIO()

        call_command("process_mail_queue", stdout=output)

        self.assertIn("Processed 1 mail jobs, 0 failed.", output.getvalue())
        self.assertEqual(len(mail.outbox), 1)

    def test_the_cleanup_command_deletes_stale_data(self):
        rate_limits.consume("scope", "old", ADDRESS_LIMITS)
        RateLimitBucket.objects.update(last_seen_at=timezone.now() - timedelta(hours=30))
        output = io.StringIO()

        call_command("cleanup_mail_requests", stdout=output)

        self.assertIn("Deleted 1 unused request counters", output.getvalue())

    def test_a_token_is_never_stored_in_a_job(self):
        post_registration("new.person@example.org")
        run_mail_queue()

        self.assertEqual(MailJob.objects.count(), 0)
        self.assertEqual(VerificationToken.objects.count(), 1)
        self.assertNotIn(PASSWORD, mail.outbox[0].body)
