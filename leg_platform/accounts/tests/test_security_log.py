"""The security log: append-only storage, retention, view and recorded events
(docs/SECURITY.md, sections 10 and 11; D-018, D-041)."""

from datetime import timedelta
from pathlib import Path
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import security_log
from leg_platform.accounts.login_protection import LOGIN_FAILURE_LIMIT
from leg_platform.accounts.models import (
    AccountIntegrityError,
    AuthenticatorDevice,
    SecurityEvent,
    User,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    VerifiedClient,
    create_active_user,
)

Event = SecurityEvent.Event


def administrator():
    user = create_active_user("admin@example.org")
    user.set_privileges(is_staff=True, is_superuser=True)
    return user


@tag("mariadb")
class SecurityEventStorageTests(TestCase):
    def test_record_stores_the_internal_numbers_and_logs_without_personal_data(self):
        user = create_active_user()
        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                security_log.record(Event.PASSWORD_CHANGED, account=user, actor=user.pk)
        entry = SecurityEvent.objects.get()
        self.assertEqual(
            (entry.event, entry.account_id, entry.actor_id),
            ("password_changed", user.pk, user.pk),
        )
        self.assertEqual(len(logs.output), 1)
        self.assertIn(f"account {user.pk}", logs.output[0])
        self.assertNotIn(user.email, logs.output[0])

    def test_actor_is_logged_by_internal_number_only(self):
        account = create_active_user()
        actor = create_active_user("second.admin@example.org")
        with self.assertLogs("leg_platform.security", "WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                security_log.record(Event.PASSWORD_CHANGED, account=account, actor=actor)
        self.assertIn(f"account {account.pk}, actor {actor.pk}", logs.output[0])
        self.assertNotIn(account.email, logs.output[0])
        self.assertNotIn(actor.email, logs.output[0])

    def test_a_rolled_back_change_leaves_no_stored_event_and_no_log_line(self):
        user = create_active_user()
        with mock.patch.object(security_log.logger, "warning") as warning:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        security_log.record(Event.PASSWORD_CHANGED, account=user)
                        raise RuntimeError("the change failed")
        self.assertFalse(SecurityEvent.objects.filter(event="password_changed").exists())
        self.assertEqual(callbacks, [])
        warning.assert_not_called()

    def test_a_committed_change_logs_once_after_the_commit(self):
        user = create_active_user()
        with mock.patch.object(security_log.logger, "warning") as warning:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                with transaction.atomic():
                    security_log.record(Event.PASSWORD_CHANGED, account=user)
                warning.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            for callback in callbacks:
                callback()
        warning.assert_called_once()

    def test_long_retention_follows_the_event_or_the_caller(self):
        for event in Event:
            security_log.record(event)
        long_events = set(
            SecurityEvent.objects.filter(long_retention=True).values_list("event", flat=True)
        )
        self.assertEqual(long_events, set(SecurityEvent.LONG_RETENTION))
        security_log.record(Event.TWO_FACTOR_RESET, long_retention=True)
        self.assertTrue(SecurityEvent.objects.filter(event="two_factor_reset", long_retention=True).exists())

    def test_every_event_has_a_log_message(self):
        self.assertEqual(set(security_log.MESSAGES), set(Event))

    def test_entries_cannot_be_changed_or_deleted_by_the_application(self):
        security_log.record(Event.PASSWORD_CHANGED, account=1)
        entry = SecurityEvent.objects.get()
        with self.assertRaises(AccountIntegrityError):
            SecurityEvent.objects.update(event="password_reset")
        with self.assertRaises(AccountIntegrityError):
            SecurityEvent.objects.all().delete()
        with self.assertRaises(AccountIntegrityError):
            SecurityEvent.objects.bulk_create([SecurityEvent(event="password_reset")])
        with self.assertRaises(AccountIntegrityError):
            entry.delete()
        entry.event = "password_reset"
        with self.assertRaises(AccountIntegrityError):
            entry.save()
        with self.assertRaises(AccountIntegrityError):
            entry.save(update_fields=["event"])
        self.assertEqual(SecurityEvent.objects.get().event, "password_changed")

    def test_the_entries_of_a_deleted_account_stay_without_reference(self):
        user = create_active_user()
        security_log.record(Event.PASSWORD_CHANGED, account=user)
        user.delete()
        self.assertEqual(SecurityEvent.objects.count(), 1)


@tag("mariadb")
class SecurityLogRetentionTests(TestCase):
    def make(self, event, age_days):
        entry = SecurityEvent.objects.record(event)
        # The time is set directly; the application never changes entries.
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE accounts_securityevent SET occurred_at = %s WHERE id = %s",
                [timezone.now() - timedelta(days=age_days), entry.pk],
            )
        return entry.pk

    def test_usual_entries_expire_after_thirty_days_and_long_ones_after_a_year(self):
        old_short = self.make(Event.PASSWORD_CHANGED, 31)
        young_short = self.make(Event.PASSWORD_CHANGED, 29)
        old_long = self.make(Event.PRIVILEGES_CHANGED, 366)
        young_long = self.make(Event.PRIVILEGES_CHANGED, 364)
        middle_long = self.make(Event.PRIVILEGES_CHANGED, 100)
        deleted = SecurityEvent.objects.delete_expired()
        self.assertEqual(deleted, 2)
        self.assertEqual(
            set(SecurityEvent.objects.values_list("pk", flat=True)),
            {young_short, young_long, middle_long},
        )
        self.assertNotIn(old_short, SecurityEvent.objects.values_list("pk", flat=True))
        self.assertNotIn(old_long, SecurityEvent.objects.values_list("pk", flat=True))

    @override_settings(SECURITY_LOG_RETENTION_DAYS=60, SECURITY_LOG_LONG_RETENTION_DAYS=400)
    def test_the_retention_of_the_instance_applies(self):
        kept = self.make(Event.PASSWORD_CHANGED, 45)
        kept_long = self.make(Event.PRIVILEGES_CHANGED, 380)
        gone = self.make(Event.PASSWORD_CHANGED, 61)
        self.assertEqual(SecurityEvent.objects.delete_expired(), 1)
        self.assertEqual(
            set(SecurityEvent.objects.values_list("pk", flat=True)), {kept, kept_long}
        )
        self.assertNotIn(gone, SecurityEvent.objects.values_list("pk", flat=True))

    def test_the_cleanup_command_deletes_expired_entries(self):
        self.make(Event.PASSWORD_CHANGED, 40)
        self.make(Event.PASSWORD_CHANGED, 1)
        out = StringIO()
        call_command("cleanup_security_log", stdout=out)
        self.assertIn("Deleted 1 security log entries", out.getvalue())
        self.assertEqual(SecurityEvent.objects.count(), 1)


@tag("mariadb")
class SecurityLogAdminTests(TestCase):
    client_class = VerifiedClient

    def setUp(self):
        self.administrator = administrator()
        self.staff_user = create_active_user("staff@example.org")
        self.staff_user.set_privileges(is_staff=True, is_superuser=False)
        # Even every permission of the log does not open it for a staff user.
        self.staff_user.user_permissions.set(
            Permission.objects.filter(content_type__app_label="accounts")
        )
        security_log.record(Event.PASSWORD_CHANGED, account=self.staff_user)

    def test_system_administrator_sees_the_entries(self):
        self.client.force_login(self.administrator)
        response = self.client.get(reverse("admin:accounts_securityevent_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.staff_user.pk))
        # No actions, not even the site-wide "delete selected".
        self.assertIsNone(response.context["action_form"])
        entry = SecurityEvent.objects.get()
        detail = self.client.get(reverse("admin:accounts_securityevent_change", args=(entry.pk,)))
        self.assertEqual(detail.status_code, 200)
        # Django 6.1 also offers actions on the change page; 6.0 has no such context entry.
        self.assertIsNone(detail.context.get("action_form"))

    def test_staff_user_cannot_see_the_log(self):
        entry = SecurityEvent.objects.get()
        self.client.force_login(self.staff_user)
        for url in (
            reverse("admin:accounts_securityevent_changelist"),
            reverse("admin:accounts_securityevent_change", args=(entry.pk,)),
        ):
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (302, 403))
        index = self.client.get(reverse("admin:index"))
        self.assertNotContains(index, "securityevent")

    def test_nobody_can_add_change_or_delete_entries(self):
        entry = SecurityEvent.objects.get()
        self.client.force_login(self.administrator)
        add = self.client.get(reverse("admin:accounts_securityevent_add"))
        self.assertEqual(add.status_code, 403)
        change = reverse("admin:accounts_securityevent_change", args=(entry.pk,))
        self.assertEqual(self.client.post(change, {"event": "password_reset"}).status_code, 403)
        delete = reverse("admin:accounts_securityevent_delete", args=(entry.pk,))
        self.assertEqual(self.client.post(delete, {"post": "yes"}).status_code, 403)
        action = self.client.post(
            reverse("admin:accounts_securityevent_changelist"),
            {"action": "delete_selected", "_selected_action": [entry.pk], "post": "yes"},
        )
        self.assertIn(action.status_code, (200, 302))
        self.assertEqual(SecurityEvent.objects.count(), 1)
        self.assertEqual(SecurityEvent.objects.get().event, "password_changed")


@tag("mariadb")
class RecordedEventsTests(TestCase):
    client_class = VerifiedClient

    def test_password_change_is_recorded(self):
        user = create_active_user()
        self.client.force_login(user)
        response = self.client.post(
            reverse("accounts:password_change"),
            {
                "current_password": PASSWORD,
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302)
        entry = SecurityEvent.objects.get()
        self.assertEqual((entry.event, entry.account_id), ("password_changed", user.pk))

    def test_the_lockout_is_recorded_once_and_without_the_address(self):
        user = create_active_user()
        for number in range(LOGIN_FAILURE_LIMIT + 3):
            self.client.post(
                reverse("accounts:login"),
                {"username": user.email, "password": "wrong password " * 2},
                REMOTE_ADDR=f"10.2.0.{number + 1}",
            )
        entries = SecurityEvent.objects.filter(event="login_lockout")
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.get().account_id, user.pk)

    def test_changes_of_privileges_and_access_are_recorded_with_the_actor(self):
        admin_user = administrator()
        account = create_active_user("member@example.org")
        # Rights in the administration need a second factor first (D-042).
        AuthenticatorDevice.objects.create_device(account, confirmed=True)
        group = Group.objects.create(name="Log test group")
        self.client.force_login(admin_user)
        data = {
            "first_name": "Anna",
            "last_name": "Example",
            "town": "Bern",
            "street": "Examplestrasse",
            "house_number": "12a",
            "postal_code": "3000",
            "country": "CH",
        }
        url = reverse("admin:accounts_user_change", args=(account.pk,))
        # Nothing changed: nothing recorded.
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertFalse(SecurityEvent.objects.exists())
        self.client.post(url, {**data, "is_staff": "on", "groups": [str(group.pk)]})
        recorded = {
            entry.event: entry
            for entry in SecurityEvent.objects.all()
        }
        self.assertEqual(set(recorded), {"privileges_changed", "access_relations_changed"})
        for entry in recorded.values():
            self.assertEqual((entry.account_id, entry.actor_id), (account.pk, admin_user.pk))
            self.assertTrue(entry.long_retention)
        self.assertTrue(User.objects.get(pk=account.pk).is_staff)


@tag("mariadb")
class SecurityLogLayoutTests(TestCase):
    """Design review E: the event types as one grouped select (not a list of links), plain column names, the table named
    "Security events" in the sidebar. Nothing here changes or deletes an entry."""

    client_class = VerifiedClient

    def setUp(self):
        self.administrator = administrator()
        self.client.force_login(self.administrator)
        security_log.record(Event.PASSWORD_CHANGED, account=self.administrator)
        security_log.record(Event.LOG_NOTIFICATION_CHANGED, account=self.administrator)

    def page(self, query=None):
        return self.client.get(reverse("admin:accounts_securityevent_changelist"), query or {})

    def test_the_event_types_are_one_grouped_select_and_no_list_of_links(self):
        response = self.page()
        html = response.content.decode()
        self.assertIn('<select id="event-type-filter" name="event__exact">', html)
        self.assertGreaterEqual(html.count("<optgroup"), 2)
        self.assertNotIn("?event__exact=", html)

    def test_the_select_filters_one_event_type(self):
        response = self.page({"event__exact": "password_changed"})
        self.assertEqual(response.status_code, 200)
        rows = [entry.event for entry in response.context["cl"].result_list]
        self.assertEqual(rows, ["password_changed"])
        self.assertIn('value="password_changed" selected', response.content.decode())

    def test_the_columns_have_short_plain_headers(self):
        html = self.page().content.decode()
        self.assertIn(">Konto<", html)
        self.assertIn(">Von<", html)
        self.assertNotIn("interne Nummer", html)

    def test_the_sidebar_names_the_table_security_events(self):
        html = self.page().content.decode()
        self.assertIn(">Sicherheitsereignisse</a>", html)

    def test_the_page_is_named_security_events_with_plain_filters(self):
        html = self.page().content.decode()
        self.assertIn("<h1>Sicherheitsereignisse</h1>", html)
        self.assertIn("Einen Eintrag zum Ansehen wählen.", html)
        self.assertNotIn("Lange aufbewahrt", html)
        self.assertIn("Nach Zeitpunkt", html)
        self.assertNotIn("Anzahl anzeigen", html)

    def test_the_filters_are_stacked_below_the_table_by_the_stylesheet(self):
        # The filter column is placed by the stylesheet (order 2, full width); no layout is checked in a browser here.
        css = (Path(settings.BASE_DIR) / "leg_platform" / "static" / "css" / "admin-portal.css").read_text(encoding="utf-8")
        self.assertIn(".model-securityevent #changelist-filter {", css)
        self.assertIn("order: 2;", css)

