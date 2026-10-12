"""Database tests of the independent IP lockout state (D-017, D-023)."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from axes.handlers.database import AxesDatabaseHandler
from axes.helpers import get_client_ip_address
from axes.models import AccessAttempt
from django.contrib.auth import authenticate
from django.core.management import call_command
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