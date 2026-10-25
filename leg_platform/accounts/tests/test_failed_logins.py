"""The one entry "Failed logins" (owner decision): superusers see it with both sections, the old lists redirect there,
nobody can add or delete a row, staff with any right see nothing of it."""

from datetime import timedelta

from axes.models import AccessAttempt, AccessFailureLog
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.login_protection import LOGIN_COOLOFF, LOGIN_FAILURE_LIMIT
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user

BLOCKED = "a" * 60 + "1234"
SINGLE = "b" * 60 + "5678"


class FailedLoginsTests(TestCase):
    def setUp(self):
        self.superuser = create_active_user("failed.logins.superuser@example.org")
        self.superuser.set_privileges(is_staff=True, is_superuser=True)
        self.staff = create_active_user("failed.logins.staff@example.org")
        self.staff.set_privileges(is_staff=True, is_superuser=False)
        self.staff.user_permissions.add(Permission.objects.get(codename="request_directory_import"))

    def client_for(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client

    def test_a_superuser_sees_the_one_entry_and_both_sections(self):
        client = self.client_for(self.superuser)
        index = client.get(reverse("admin:index"))
        self.assertContains(index, reverse("admin:failed_logins"))
        page = client.get(reverse("admin:failed_logins"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="failed-logins-locked"')
        self.assertContains(page, 'id="failed-logins-log"')
        self.assertLess(page.content.index(b'id="failed-logins-locked"'), page.content.index(b'id="failed-logins-log"'))

    def test_staff_with_any_right_does_not_see_it(self):
        client = self.client_for(self.staff)
        self.assertNotContains(client.get(reverse("admin:index")), reverse("admin:failed_logins"))
        self.assertEqual(client.get(reverse("admin:failed_logins")).status_code, 403)

    def test_the_old_lists_redirect_to_the_new_page_for_superusers(self):
        client = self.client_for(self.superuser)
        for name in ("admin:axes_accessattempt_changelist", "admin:axes_accessfailurelog_changelist"):
            with self.subTest(name=name):
                response = client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse("admin:failed_logins"))

    def test_no_add_and_no_delete(self):
        client = self.client_for(self.superuser)
        self.assertEqual(client.get(reverse("admin:axes_accessattempt_add")).status_code, 403)
        self.assertEqual(client.get(reverse("admin:axes_accessfailurelog_add")).status_code, 403)
        self.assertEqual(client.get(reverse("admin:axes_accessattempt_delete", args=(1,))).status_code, 403)
        page = client.get(reverse("admin:failed_logins"))
        self.assertNotContains(page, reverse("admin:axes_accessattempt_add"))
        self.assertNotContains(page, 'name="action"')


class LockoutListTests(TestCase):
    """Design review A: "Locked" shows only current blocks; the identifier is shortened in both sections."""

    def setUp(self):
        self.superuser = create_active_user("failed.lockout.superuser@example.org")
        self.superuser.set_privileges(is_staff=True, is_superuser=True)
        self.client_ = VerifiedClient()
        self.client_.force_login(self.superuser)

    def attempt(self, username, failures, when=None):
        when = when or timezone.now()
        attempt = AccessAttempt.objects.create(
            username=username, ip_address="203.0.113.7", user_agent="test", path_info="/accounts/login/",
            http_accept="text/html", failures_since_start=failures,
        )
        # The time is set by axes on create (auto_now_add): it is moved afterwards for the test.
        AccessAttempt.objects.filter(pk=attempt.pk).update(attempt_time=when)
        log = AccessFailureLog.objects.create(
            username=username, ip_address="203.0.113.7", user_agent="test", path_info="/accounts/login/",
            http_accept="text/html", locked_out=failures >= LOGIN_FAILURE_LIMIT,
        )
        AccessFailureLog.objects.filter(pk=log.pk).update(attempt_time=when)

    def sections(self):
        html = self.client_.get(reverse("admin:failed_logins")).content.decode()
        locked = html[html.index('id="failed-logins-locked"'):html.index('id="failed-logins-log"')]
        log = html[html.index('id="failed-logins-log"'):]
        return locked, log

    def test_the_locked_list_holds_only_the_current_blocks(self):
        self.attempt(BLOCKED, LOGIN_FAILURE_LIMIT)
        self.attempt(SINGLE, 1)
        locked, log = self.sections()
        self.assertIn(BLOCKED[:8], locked)
        self.assertNotIn(SINGLE[:8], locked)
        self.assertIn(SINGLE[:8], log)

    def test_an_old_block_is_no_longer_locked(self):
        self.attempt(BLOCKED, LOGIN_FAILURE_LIMIT, when=timezone.now() - LOGIN_COOLOFF - timedelta(minutes=1))
        locked, _log = self.sections()
        self.assertNotIn(BLOCKED[:8], locked)

    def test_each_cell_of_the_tables_has_its_label(self):
        self.attempt(BLOCKED, LOGIN_FAILURE_LIMIT)
        locked, log = self.sections()
        self.assertIn('class="phone-table"', locked)
        self.assertIn('<td data-label="Kennung, gekürzt">', locked)
        self.assertNotIn("<table", log)

    def test_the_filters_and_the_line_count_have_plain_labels(self):
        html = self.client_.get(reverse("admin:failed_logins")).content.decode()
        self.assertIn("Nach Zeitpunkt", html)
        self.assertNotIn("Nach Nach", html)
        self.assertNotIn("Nach Pfad", html)
        self.assertIn("Kennung, gekürzt", html)
        self.assertIn("Anzahl anzeigen", html)
        self.assertIn(">Alle</a>", html)

    def test_the_address_filter_is_shown_only_when_there_are_rows_of_that_kind(self):
        html = self.client_.get(reverse("admin:failed_logins")).content.decode()
        self.assertNotIn("Nach Adresse", html)
        self.attempt(BLOCKED, 1)
        html = self.client_.get(reverse("admin:failed_logins")).content.decode()
        self.assertIn("Nach Adresse", html)

    def test_the_identifier_is_shortened_in_both_sections(self):
        self.attempt(BLOCKED, LOGIN_FAILURE_LIMIT)
        locked, log = self.sections()
        for section in (locked, log):
            self.assertIn(BLOCKED[:8] + "…" + BLOCKED[-4:], section)
            self.assertNotIn(BLOCKED, section)


class BreadcrumbPatternTests(TestCase):
    """Design review D: one breadcrumb pattern (an ordered list) on the administration pages, no separator glyph."""

    def setUp(self):
        self.superuser = create_active_user("failed.crumbs.superuser@example.org")
        self.superuser.set_privileges(is_staff=True, is_superuser=True)
        self.client_ = VerifiedClient()
        self.client_.force_login(self.superuser)

    def test_the_failed_logins_page_has_the_list_and_no_arrow_bar(self):
        page = self.client_.get(reverse("admin:failed_logins")).content.decode()
        self.assertIn('<ol class="breadcrumbs">', page)
        self.assertNotIn("&rsaquo;", page)
        self.assertNotIn("›", page)
        self.assertNotIn('<div class="breadcrumbs">', page)

    def test_the_other_pages_use_the_same_list(self):
        for name in ("admin:logmenu", "admin:addresses_housekeepingstatus_changelist", "admin:logs"):
            with self.subTest(name=name):
                page = self.client_.get(reverse(name)).content.decode()
                self.assertIn('<ol class="breadcrumbs">', page)
                self.assertNotIn("&rsaquo;", page)

