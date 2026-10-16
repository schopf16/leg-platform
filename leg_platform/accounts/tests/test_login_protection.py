"""Database tests of the independent IP lockout state (D-017, D-023)."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from axes.handlers.database import AxesDatabaseHandler
from axes.helpers import get_client_ip_address
from axes.models import AccessAttempt
from django.contrib.auth import authenticate
from django.core.management import call_command
from django.db import OperationalError, connection, transaction
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
    tag,
)
from django.utils import timezone

from leg_platform.accounts.login_protection import (
    IP_ATTEMPT_WINDOW,
    LOGIN_FAILURE_LIMIT,
    axes_username,
    client_ip_digest,
    reserve_client_ip_attempt,
    settle_client_ip_attempt,
)
from leg_platform.accounts.models import LoginIpAttempt, LoginIpBucket, User


class ClientIpResolutionTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(AXES_IPWARE_PROXY_COUNT=0)
    def test_forwarded_header_is_ignored_without_trusted_proxies(self):
        request = self.factory.post(
            "/accounts/login/",
            REMOTE_ADDR="10.0.0.10",
            HTTP_X_FORWARDED_FOR="10.0.0.20",
        )

        self.assertEqual(get_client_ip_address(request), "10.0.0.10")

    @override_settings(AXES_IPWARE_PROXY_COUNT=1)
    def test_configured_proxy_count_resolves_client_from_chain(self):
        request = self.factory.post(
            "/accounts/login/",
            REMOTE_ADDR="10.0.0.10",
            HTTP_X_FORWARDED_FOR="10.0.0.20, 10.0.0.10",
        )

        self.assertEqual(get_client_ip_address(request), "10.0.0.20")


@tag("mariadb")
class LoginIpAttemptTests(TestCase):
    address = "192.0.2.10"

    def make_request(self):
        return SimpleNamespace(axes_ip_address=self.address)

    def record_failure(self):
        request = self.make_request()
        self.assertTrue(reserve_client_ip_attempt(request))
        settle_client_ip_attempt(request, succeeded=False)

    def test_fifth_failed_attempt_blocks_the_next_request(self):
        for _attempt in range(LOGIN_FAILURE_LIMIT):
            self.record_failure()

        self.assertEqual(LoginIpAttempt.objects.count(), LOGIN_FAILURE_LIMIT)
        self.assertFalse(reserve_client_ip_attempt(self.make_request()))

    def test_successful_attempt_releases_its_reserved_slot(self):
        request = self.make_request()
        self.assertTrue(reserve_client_ip_attempt(request))
        settle_client_ip_attempt(request, succeeded=True)

        self.assertEqual(LoginIpAttempt.objects.count(), 0)
        self.assertTrue(reserve_client_ip_attempt(self.make_request()))

    def test_reservation_retries_if_cleanup_removes_the_bucket(self):
        request = self.make_request()
        select_for_update = LoginIpBucket.objects.select_for_update
        removed = False

        def remove_bucket_once():
            nonlocal removed
            if not removed:
                removed = True
                LoginIpBucket.objects.filter(
                    digest=client_ip_digest(self.address)
                ).delete()
            return select_for_update()

        with patch.object(
            LoginIpBucket.objects,
            "select_for_update",
            side_effect=remove_bucket_once,
        ):
            self.assertTrue(reserve_client_ip_attempt(request))

        self.assertEqual(LoginIpAttempt.objects.count(), 1)

    def test_expired_attempts_no_longer_block(self):
        self.record_failure()
        LoginIpAttempt.objects.update(
            attempted_at=timezone.now() - IP_ATTEMPT_WINDOW - timezone.timedelta(seconds=1)
        )

        self.assertTrue(reserve_client_ip_attempt(self.make_request()))
        self.assertEqual(LoginIpAttempt.objects.count(), 1)

    def test_axes_username_reset_does_not_clear_ip_attempts(self):
        for _attempt in range(LOGIN_FAILURE_LIMIT):
            self.record_failure()

        username = axes_username(None, {"username": "person@example.org"})
        AccessAttempt.objects.create(
            username=username,
            ip_address=self.address,
            user_agent="test-agent",
            http_accept="text/html",
            path_info="/accounts/login/",
            get_data="",
            post_data="",
            failures_since_start=1,
        )
        AxesDatabaseHandler().reset_attempts(username=username)

        self.assertFalse(reserve_client_ip_attempt(self.make_request()))


@tag("mariadb")
class ConcurrentLoginIpAttemptTests(TransactionTestCase):
    def test_parallel_attempts_cannot_exceed_ip_failure_limit(self):
        barrier = Barrier(LOGIN_FAILURE_LIMIT + 3)
        address = "10.0.0.40"

        def reserve_attempt(_index):
            request = SimpleNamespace(axes_ip_address=address)
            barrier.wait()
            reserved = reserve_client_ip_attempt(request)
            if reserved:
                settle_client_ip_attempt(request, succeeded=False)
            return reserved

        with ThreadPoolExecutor(max_workers=LOGIN_FAILURE_LIMIT + 3) as executor:
            results = list(executor.map(reserve_attempt, range(LOGIN_FAILURE_LIMIT + 3)))

        self.assertEqual(sum(results), LOGIN_FAILURE_LIMIT)
        self.assertEqual(LoginIpAttempt.objects.count(), LOGIN_FAILURE_LIMIT)

    def assert_settling_waits_for_the_bucket(self, address, succeeded):
        # The lock order of D-072: every statement on the attempts of a bucket runs under the lock
        # of the bucket. A settle that did not take it ran against the reservation of a parallel
        # request (index scan against delete by primary key) and could deadlock (MariaDB 1213).
        request = SimpleNamespace(axes_ip_address=address)
        self.assertTrue(reserve_client_ip_attempt(request))
        digest = client_ip_digest(address)
        locked, release, entered, done = Event(), Event(), Event(), Event()
        errors = []
        select_for_update = LoginIpBucket.objects.select_for_update

        def hold():
            try:
                with transaction.atomic():
                    LoginIpBucket.objects.select_for_update().get(digest=digest)
                    locked.set()
                    release.wait(20)
            finally:
                connection.close()

        def entering():
            # The settle has reached the lock of the bucket; the wait below starts from here.
            entered.set()
            return select_for_update()

        def settle():
            try:
                settle_client_ip_attempt(request, succeeded=succeeded)
            except Exception as error:  # reported by the assertion below
                errors.append(error)
            finally:
                done.set()
                connection.close()

        holder = Thread(target=hold)
        holder.start()
        self.assertTrue(locked.wait(10))
        settler = Thread(target=settle)
        with patch.object(LoginIpBucket.objects, "select_for_update", side_effect=entering):
            settler.start()
            try:
                self.assertTrue(entered.wait(10), "The settle did not reach the lock.")
                self.assertFalse(
                    done.wait(1.0), "The settle did not wait for the lock of the bucket."
                )
                # Still the reserved slot, still pending, nothing changed under the lock.
                self.assertEqual(LoginIpAttempt.objects.filter(pending=True).count(), 1)
            finally:
                release.set()
                holder.join()
                settler.join()
        self.assertEqual(errors, [])
        if succeeded:
            self.assertEqual(LoginIpAttempt.objects.count(), 0)
        else:
            # The failed attempt stays counted until its window expires, no longer pending.
            self.assertEqual(LoginIpAttempt.objects.count(), 1)
            self.assertFalse(LoginIpAttempt.objects.get().pending)

    def test_settling_waits_for_the_lock_of_the_bucket(self):
        self.assert_settling_waits_for_the_bucket("10.0.0.41", succeeded=True)

    def test_settling_a_failure_waits_for_the_lock_of_the_bucket(self):
        self.assert_settling_waits_for_the_bucket("10.0.0.43", succeeded=False)

    def test_settling_inside_the_transaction_that_holds_the_bucket_does_not_wait_for_itself(self):
        # The callers in the transaction of the account lock reserved the slot in the same
        # transaction, so they hold the lock of the bucket already (D-072).
        address = "10.0.0.44"
        digest = client_ip_digest(address)
        # Committed before, so that the parallel request finds it and waits for its row lock.
        LoginIpBucket.objects.create(digest=digest)
        for succeeded in (True, False):
            with self.subTest(succeeded=succeeded):
                LoginIpAttempt.objects.all().delete()
                request = SimpleNamespace(axes_ip_address=address)
                reserved, parallel_done = Event(), Event()
                errors = []

                def parallel_reservation():
                    # A parallel request of the same address: it waits for the bucket.
                    try:
                        reserved.wait(10)
                        other = SimpleNamespace(axes_ip_address=address)
                        self.assertTrue(reserve_client_ip_attempt(other))
                    except Exception as error:  # reported by the assertion below
                        errors.append(error)
                    finally:
                        parallel_done.set()
                        connection.close()

                parallel = Thread(target=parallel_reservation)
                parallel.start()
                with transaction.atomic():
                    self.assertTrue(reserve_client_ip_attempt(request))
                    reserved.set()
                    self.assertFalse(parallel_done.wait(0.5))
                    settle_client_ip_attempt(request, succeeded=succeeded)
                    self.assertFalse(hasattr(request, "login_ip_attempt_id"))
                parallel.join(20)

                self.assertEqual(errors, [])
                self.assertTrue(parallel_done.is_set())
                bucket_attempts = LoginIpAttempt.objects.filter(bucket__digest=digest)
                # The settled slot (released or kept) and the slot of the parallel request.
                self.assertEqual(bucket_attempts.count(), 1 if succeeded else 2)

    def test_settling_after_the_cleanup_removed_the_bucket_between_lookup_and_lock(self):
        address = "10.0.0.45"
        request = SimpleNamespace(axes_ip_address=address)
        self.assertTrue(reserve_client_ip_attempt(request))
        digest = client_ip_digest(address)
        select_for_update = LoginIpBucket.objects.select_for_update

        def remove_bucket_in_another_connection():
            def remove():
                try:
                    LoginIpBucket.objects.filter(digest=digest).delete()
                finally:
                    connection.close()

            thread = Thread(target=remove)
            thread.start()
            thread.join(20)
            return select_for_update()

        with patch.object(
            LoginIpBucket.objects,
            "select_for_update",
            side_effect=remove_bucket_in_another_connection,
        ):
            settle_client_ip_attempt(request, succeeded=True)

        # The bucket and, by the cascade, its attempts are gone; nothing failed.
        self.assertFalse(LoginIpBucket.objects.filter(digest=digest).exists())
        self.assertFalse(LoginIpAttempt.objects.exists())
        self.assertFalse(hasattr(request, "login_ip_attempt_id"))

    def test_a_failing_settle_is_no_success_and_the_slot_stays_pending(self):
        # A deadlock or a lock wait timeout in the settle (MariaDB 1213, 1205) is an exception:
        # it is not swallowed, the request does not go on as if the slot was released, and the
        # slot stays reserved and pending, so the IP budget is not bypassed (D-072).
        address = "10.0.0.46"
        request = SimpleNamespace(axes_ip_address=address)
        self.assertTrue(reserve_client_ip_attempt(request))
        attempt_id = request.login_ip_attempt_id

        for succeeded in (True, False):
            with self.subTest(succeeded=succeeded):
                with patch.object(
                    LoginIpBucket.objects,
                    "select_for_update",
                    side_effect=OperationalError(1213, "Deadlock found when trying to get lock"),
                ):
                    with self.assertRaises(OperationalError):
                        settle_client_ip_attempt(request, succeeded=succeeded)

                self.assertEqual(request.login_ip_attempt_id, attempt_id)
                self.assertTrue(LoginIpAttempt.objects.get(pk=attempt_id).pending)

        # A repeat without the fault settles the same slot.
        settle_client_ip_attempt(request, succeeded=True)
        self.assertFalse(LoginIpAttempt.objects.exists())

    def test_parallel_reserve_and_settle_of_one_address_do_not_deadlock(self):
        count = 12
        barrier = Barrier(count)
        address = "10.0.0.42"
        errors = []

        def attempt(_index):
            request = SimpleNamespace(axes_ip_address=address)
            try:
                barrier.wait()
                for _round in range(5):
                    if reserve_client_ip_attempt(request):
                        settle_client_ip_attempt(request, succeeded=True)
            except Exception as error:  # reported by the assertion below
                errors.append(error)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=count) as executor:
            list(executor.map(attempt, range(count)))

        self.assertEqual(errors, [])
        self.assertEqual(LoginIpAttempt.objects.count(), 0)


@tag("mariadb")
class LoginIpAttemptCleanupTests(TestCase):
    def test_cleanup_removes_expired_attempts_and_empty_buckets_only(self):
        current_time = timezone.now()
        expired_bucket = LoginIpBucket.objects.create(digest="a" * 64)
        active_bucket = LoginIpBucket.objects.create(digest="b" * 64)
        LoginIpBucket.objects.create(digest="c" * 64)
        LoginIpAttempt.objects.create(
            bucket=expired_bucket,
            attempted_at=current_time - IP_ATTEMPT_WINDOW - timezone.timedelta(seconds=1),
            pending=False,
        )
        active_attempt = LoginIpAttempt.objects.create(
            bucket=active_bucket,
            attempted_at=current_time,
            pending=False,
        )

        output = StringIO()
        call_command("cleanup_login_ip_attempts", stdout=output)

        self.assertEqual(LoginIpAttempt.objects.count(), 1)
        self.assertTrue(LoginIpAttempt.objects.filter(pk=active_attempt.pk).exists())
        self.assertQuerySetEqual(LoginIpBucket.objects.all(), [active_bucket])
        self.assertIn("Deleted 1 expired login IP attempts", output.getvalue())


@tag("mariadb")
class AuthenticationIpLockoutTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.address = "10.0.0.30"

    def submit_failed_login(self, username):
        request = self.factory.post(
            "/accounts/login/",
            {"username": username, "password": "wrong-password-sentinel", "otp_token": "otp-sentinel"},
        )
        request.META["REMOTE_ADDR"] = self.address
        request.META["HTTP_USER_AGENT"] = "test-agent"
        user = authenticate(request, username=username, password="wrong-password-sentinel")
        return user, request

    def test_unknown_account_with_empty_password_is_a_normal_failure(self):
        username = "unknown@example.org"
        request = self.factory.post(
            "/accounts/login/",
            {"username": username, "password": ""},
        )
        request.META["REMOTE_ADDR"] = self.address

        user = authenticate(request, username=username, password="")

        self.assertIsNone(user)
        self.assertEqual(AccessAttempt.objects.count(), 1)
        self.assertEqual(LoginIpAttempt.objects.count(), 1)

    def test_password_check_keeps_ip_slot_until_full_authentication(self):
        user = User.objects.create_user("person@example.org")
        user.set_password("copper kettle hums beside quiet orchard")
        with user._account_change("status"):
            user.status = User.Status.ACTIVE
            user.save()
        request = self.factory.post(
            "/accounts/login/",
            {"username": user.email, "password": "copper kettle hums beside quiet orchard"},
        )
        request.META["REMOTE_ADDR"] = self.address

        authenticated = authenticate(
            request,
            username=user.email,
            password="copper kettle hums beside quiet orchard",
        )

        self.assertEqual(authenticated.pk, user.pk)
        self.assertEqual(LoginIpAttempt.objects.count(), 1)

        from django.contrib.auth import login
        from django.contrib.sessions.middleware import SessionMiddleware

        SessionMiddleware(lambda _request: None).process_request(request)
        login(request, authenticated)

        self.assertEqual(LoginIpAttempt.objects.count(), 0)

    def test_one_ip_is_locked_across_distinct_usernames_without_logging_secrets(self):
        for attempt in range(LOGIN_FAILURE_LIMIT):
            with self.subTest(attempt=attempt):
                user, request = self.submit_failed_login(f"person{attempt}@example.org")
                self.assertIsNone(user)
                self.assertFalse(getattr(request, "axes_locked_out", False))

        attempts = AccessAttempt.objects.all()
        self.assertEqual(attempts.count(), LOGIN_FAILURE_LIMIT)
        for record in attempts:
            self.assertRegex(record.username, r"\A[0-9a-f]{64}\Z")
            self.assertNotIn("@", record.username)
            self.assertIsNone(record.ip_address)
            self.assertNotIn("wrong-password-sentinel", record.post_data)
            self.assertNotIn("otp-sentinel", record.post_data)

        user, blocked_request = self.submit_failed_login("another@example.org")
        self.assertIsNone(user)
        self.assertTrue(blocked_request.axes_locked_out)
        self.assertTrue(blocked_request.axes_ip_locked_out)
        self.assertEqual(AccessAttempt.objects.count(), LOGIN_FAILURE_LIMIT)
        self.assertEqual(LoginIpAttempt.objects.count(), LOGIN_FAILURE_LIMIT)

    def test_account_lock_is_shared_by_email_case_and_whitespace_variants(self):
        for attempt in range(LOGIN_FAILURE_LIMIT):
            username = " Person@Example.ORG " if attempt % 2 else "person@example.org"
            request = self.factory.post(
                "/accounts/login/",
                {"username": username, "password": "wrong-password-sentinel"},
            )
            request.META["REMOTE_ADDR"] = f"10.0.1.{attempt + 1}"
            self.assertIsNone(
                authenticate(request, username=username, password="wrong-password-sentinel")
            )

        username = "PERSON@example.org"
        request = self.factory.post(
            "/accounts/login/",
            {"username": username, "password": "wrong-password-sentinel"},
        )
        request.META["REMOTE_ADDR"] = "10.0.1.20"

        self.assertIsNone(
            authenticate(request, username=username, password="wrong-password-sentinel")
        )
        self.assertTrue(request.axes_locked_out)
        self.assertFalse(getattr(request, "axes_ip_locked_out", False))