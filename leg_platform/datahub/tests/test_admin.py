"""The administration of the Datahub access (D-139): system administrators only, write-only password, logged changes,
and the connection test. No network: the connection test itself is replaced."""

from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.auth.models import Group, Permission
from django.contrib.messages import get_messages
from django.contrib import admin
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, override

from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.datahub import connection
from leg_platform.datahub.models import DatahubConfiguration

Event = SecurityEvent.Event
Outcome = DatahubConfiguration.TestOutcome

CHANGE_URL = reverse("admin:datahub_datahubconfiguration_change", args=(1,))
CHANGELIST_URL = reverse("admin:datahub_datahubconfiguration_changelist")
ADD_URL = reverse("admin:datahub_datahubconfiguration_add")
DELETE_URL = reverse("admin:datahub_datahubconfiguration_delete", args=(1,))
TEST_URL = reverse("admin:datahub_datahubconfiguration_connection_test")
CHECK = "leg_platform.datahub.admin.connection.check_connection"

# Test values only; not credentials of any server.
USERNAME = "leg-test-user-4711"
PASSWORD = "Datahub-Test-Password-31"
OTHER_PASSWORD = "Another-Test-Password-77"
OWN_EIC = "12X-00000020RL-R"
GRID_EIC = "12X-0000000858-F"


def german(message_id, **values):
    """The Swiss German text of an English message ID, as the users see it (de-ch)."""
    with override(settings.LANGUAGE_CODE):
        text = gettext(message_id)
    return text % values if values else text


def message_texts(response):
    with override(settings.LANGUAGE_CODE):
        return [(message.level, str(message.message)) for message in get_messages(response.wsgi_request)]


def field_errors(response, name):
    with override(settings.LANGUAGE_CODE):
        return [str(error) for error in response.context["adminform"].form.errors.get(name, [])]


def form_data(**overrides):
    """The POST of the change form with the stored values unchanged (default values of the test row)."""
    data = {
        "host": "datahub.example.org",
        "port": "21",
        "username": USERNAME,
        "new_password": "",
        "directory": "",
        "own_name": "LEG Example",
        "own_eic": OWN_EIC,
        "grid_operator_eic": GRID_EIC,
        "_save": "Save",
    }
    data.update(overrides)
    return {name: value for name, value in data.items() if value is not None}


class DatahubAdminBase(TestCase):
    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("datahub.admin@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)
        cls.staff = create_active_user("datahub.staff@example.org")
        cls.staff.set_privileges(is_staff=True, is_superuser=False)
        cls.powerful_staff = create_active_user("datahub.powerful@example.org")
        cls.powerful_staff.set_privileges(is_staff=True, is_superuser=False)
        group = Group.objects.create(name="Test all permissions")
        group.permissions.set(Permission.objects.all())
        cls.powerful_staff.set_access_relations(groups=[group], user_permissions=list(Permission.objects.all()))
        configuration = DatahubConfiguration.load()
        configuration.host = "datahub.example.org"
        configuration.port = 21
        configuration.username = USERNAME
        configuration.own_name = "LEG Example"
        configuration.own_eic = OWN_EIC
        configuration.grid_operator_eic = GRID_EIC
        configuration.set_password(PASSWORD)
        configuration.save()

    def setUp(self):
        self.client.force_login(self.administrator)

    @staticmethod
    def row():
        return DatahubConfiguration.objects.get(pk=1)

    def events(self, event):
        return SecurityEvent.objects.filter(event=event)


class AccessTests(DatahubAdminBase):
    def test_the_system_administrator_sees_the_change_page(self):
        response = self.client.get(CHANGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, TEST_URL)
        self.assertContains(response, 'name="new_password"')
        self.assertContains(response, 'name="clear_password"')
        self.assertNotContains(response, 'name="encrypted_password"')

    def test_the_browser_does_not_fill_in_the_access_data(self):
        form = self.client.get(CHANGE_URL).context["adminform"].form
        for name in ("username", "directory"):
            with self.subTest(field=name):
                html = str(form[name])
                self.assertIn('autocomplete="off"', html)
        password = str(form["new_password"])
        self.assertIn('autocomplete="new-password"', password)
        self.assertNotIn("value=", password)

    def test_the_changelist_redirects_to_the_change_page(self):
        self.assertRedirects(self.client.get(CHANGELIST_URL), CHANGE_URL)

    def test_the_system_administrator_sees_the_module(self):
        self.assertContains(self.client.get(reverse("admin:index")), "/datahub/datahubconfiguration/")

    def test_an_anonymous_visitor_is_sent_to_the_login(self):
        client = Client()
        for url in (CHANGE_URL, CHANGELIST_URL, TEST_URL):
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    response = getattr(client, method)(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith(reverse("admin:login")), response["Location"])

    def test_staff_users_are_refused_everywhere(self):
        for user in (self.staff, self.powerful_staff):
            client = VerifiedClient()
            client.force_login(user)
            with patch(CHECK) as check:
                for method, url, data in (
                    ("get", CHANGE_URL, None),
                    ("post", CHANGE_URL, form_data(username="changed-by-staff")),
                    ("get", CHANGELIST_URL, None),
                    ("get", TEST_URL, None),
                    ("post", TEST_URL, {}),
                    ("get", ADD_URL, None),
                    ("get", DELETE_URL, None),
                    ("post", DELETE_URL, {"post": "yes"}),
                ):
                    with self.subTest(user=user.email, method=method, url=url):
                        self.assertEqual(getattr(client, method)(url, data).status_code, 403)
                index = client.get(reverse("admin:index"))
            check.assert_not_called()
            self.assertEqual(index.status_code, 200)
            self.assertNotContains(index, "/datahub/")
        row = self.row()
        self.assertEqual(row.username, USERNAME)
        self.assertIsNone(row.last_test_at)
        self.assertFalse(self.events(Event.DATAHUB_CONFIGURATION_CHANGED).exists())
        self.assertFalse(self.events(Event.DATAHUB_CONNECTION_TESTED).exists())

    def test_a_post_without_the_csrf_token_is_refused(self):
        client = VerifiedClient(enforce_csrf_checks=True)
        client.force_login(self.administrator)
        with patch(CHECK) as check:
            self.assertEqual(client.post(TEST_URL).status_code, 403)
            self.assertEqual(client.post(CHANGE_URL, form_data(username="changed-without-token")).status_code, 403)
        check.assert_not_called()
        self.assertEqual(self.row().username, USERNAME)
        self.assertIsNone(self.row().last_test_at)

    def test_add_and_delete_are_refused_to_the_system_administrator(self):
        self.assertEqual(self.client.get(ADD_URL).status_code, 403)
        self.assertEqual(self.client.post(ADD_URL, form_data()).status_code, 403)
        self.assertEqual(self.client.get(DELETE_URL).status_code, 403)
        self.assertEqual(self.client.post(DELETE_URL, {"post": "yes"}).status_code, 403)
        self.assertEqual(DatahubConfiguration.objects.count(), 1)

    def test_another_object_id_is_not_found(self):
        response = self.client.get(reverse("admin:datahub_datahubconfiguration_change", args=(2,)))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DatahubConfiguration.objects.count(), 1)


class PasswordTests(DatahubAdminBase):
    def test_a_new_password_is_stored_encrypted_and_never_shown(self):
        old_ciphertext = self.row().encrypted_password
        response = self.client.post(CHANGE_URL, form_data(new_password=OTHER_PASSWORD), follow=True)
        self.assertEqual(response.status_code, 200)
        row = self.row()
        self.assertNotEqual(row.encrypted_password, old_ciphertext)
        self.assertNotIn(OTHER_PASSWORD, row.encrypted_password)
        self.assertEqual(row.password(), OTHER_PASSWORD)
        page = self.client.get(CHANGE_URL).content.decode()
        for content in (response.content.decode(), page):
            self.assertNotIn(OTHER_PASSWORD, content)
            self.assertNotIn(row.encrypted_password, content)
            self.assertNotIn(old_ciphertext, content)

    def test_a_refused_form_does_not_show_the_entered_password(self):
        response = self.client.post(CHANGE_URL, form_data(new_password=OTHER_PASSWORD, port="0"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(field_errors(response, "port"))
        self.assertNotContains(response, OTHER_PASSWORD)
        self.assertNotContains(response, self.row().encrypted_password)
        self.assertEqual(self.row().password(), PASSWORD)

    def test_an_empty_password_field_keeps_the_stored_password(self):
        old_ciphertext = self.row().encrypted_password
        response = self.client.post(CHANGE_URL, form_data(own_name="LEG Other"))
        self.assertRedirects(response, CHANGELIST_URL, fetch_redirect_response=False)
        row = self.row()
        self.assertEqual(row.own_name, "LEG Other")
        self.assertEqual(row.encrypted_password, old_ciphertext)

    def test_clear_password_removes_it(self):
        response = self.client.post(CHANGE_URL, form_data(clear_password="on"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.row().encrypted_password, "")
        self.assertFalse(self.row().has_password)

    def test_a_new_password_and_clear_together_are_refused(self):
        old_ciphertext = self.row().encrypted_password
        response = self.client.post(CHANGE_URL, form_data(new_password=OTHER_PASSWORD, clear_password="on"))
        self.assertEqual(response.status_code, 200)
        with override(settings.LANGUAGE_CODE):
            errors = [str(error) for error in response.context["adminform"].form.non_field_errors()]
        self.assertEqual(errors, [german("Enter a new password or remove the stored one, not both.")])
        self.assertNotContains(response, OTHER_PASSWORD)
        self.assertEqual(self.row().encrypted_password, old_ciphertext)


class ServerChangeTests(DatahubAdminBase):
    """The stored password goes only to the server it was entered for (M-1): a change of the server or the port
    needs the password again in the same save, or its removal."""

    AGAIN = "Enter the password again when you change the server or the port."
    NEW_HOST = "attacker.example.net"

    def assert_refused(self, **overrides):
        before = self.row()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(CHANGE_URL, form_data(**overrides))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(field_errors(response, "new_password"), [german(self.AGAIN)])
        after = self.row()
        for name in ("host", "port", "username", "directory", "encrypted_password", "updated_at"):
            self.assertEqual(getattr(after, name), getattr(before, name))
        self.assertFalse(self.events(Event.DATAHUB_CONFIGURATION_CHANGED).exists())

    def test_a_host_change_without_the_password_is_refused(self):
        self.assert_refused(host=self.NEW_HOST)

    def test_a_port_change_without_the_password_is_refused(self):
        self.assert_refused(port="990")

    def test_a_host_change_with_the_password_is_accepted(self):
        response = self.client.post(CHANGE_URL, form_data(host=self.NEW_HOST, port="2121", new_password=OTHER_PASSWORD))
        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual((row.host, row.port), (self.NEW_HOST, 2121))
        self.assertEqual(row.password(), OTHER_PASSWORD)

    def test_a_host_change_with_the_password_removed_is_accepted(self):
        response = self.client.post(CHANGE_URL, form_data(host=self.NEW_HOST, clear_password="on"))
        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual(row.host, self.NEW_HOST)
        self.assertFalse(row.has_password)

    def test_a_host_change_without_a_stored_password_needs_none(self):
        DatahubConfiguration.objects.filter(pk=1).update(encrypted_password="")
        response = self.client.post(CHANGE_URL, form_data(host=self.NEW_HOST))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.row().host, self.NEW_HOST)

    def test_a_concurrent_server_change_removes_the_stored_password(self):
        """The check under the lock in save_model: the stored row changed after the form was checked."""
        admin_instance = admin.site._registry[DatahubConfiguration]
        form = admin_instance.form(data=form_data(), instance=DatahubConfiguration.load())
        self.assertTrue(form.is_valid(), form.errors)
        DatahubConfiguration.objects.filter(pk=1).update(host=self.NEW_HOST)
        request = RequestFactory().post(CHANGE_URL)
        request.user = self.administrator
        with self.captureOnCommitCallbacks(execute=True):
            admin_instance.save_model(request, form.save(commit=False), form, change=True)
        row = self.row()
        self.assertEqual(row.host, "datahub.example.org")
        self.assertFalse(row.has_password)

    def test_the_old_password_never_goes_to_the_new_host(self):
        with patch(CHECK, return_value=connection.TestResult(Outcome.SUCCEEDED, 0, "")) as check:
            self.client.post(CHANGE_URL, form_data(host=self.NEW_HOST))
            self.client.post(CHANGE_URL, form_data(port="990"))
            self.client.post(TEST_URL)
            DatahubConfiguration.objects.filter(pk=1).update(last_test_at=None)
            self.client.post(CHANGE_URL, form_data(host=self.NEW_HOST, clear_password="on"))
            self.client.post(TEST_URL)
        for call in check.call_args_list:
            tested, password = call.args
            self.assertFalse(
                password == PASSWORD and (tested.host, tested.port) != ("datahub.example.org", 21),
                "the stored password was sent to another server",
            )
        # The first test ran against the stored server; the second had no password and did not run.
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args.args[0].host, "datahub.example.org")


class ValidationTests(DatahubAdminBase):
    def assert_refused(self, field, message_id, **overrides):
        before = self.row()
        response = self.client.post(CHANGE_URL, form_data(**overrides))
        self.assertEqual(response.status_code, 200)
        if message_id is None:
            self.assertTrue(field_errors(response, field))
        else:
            self.assertIn(german(message_id), field_errors(response, field))
        after = self.row()
        for name in ("host", "port", "username", "directory", "own_eic", "encrypted_password"):
            self.assertEqual(getattr(after, name), getattr(before, name))
        return response

    def test_control_characters_are_refused(self):
        control = "The value must not contain line breaks or control characters."
        for value in ("name\r\nDELE a.xml", "name\nUSER x", "name\x01", "name\x7f", "name\x85x", "name\u2028x"):
            for field in ("username", "directory", "new_password"):
                with self.subTest(field=field, value=repr(value)):
                    self.assert_refused(field, control, **{field: value})
        # A NUL character is refused by Django's own validator first.
        for field in ("username", "directory", "new_password"):
            with self.subTest(field=field, value="NUL"):
                self.assert_refused(field, None, **{field: "name\x00x"})

    def test_a_host_with_a_scheme_a_path_or_a_port_is_refused(self):
        message = "Enter the host name only, for example datahub.swisseldex.ch (no ftpes://, no path, no port)."
        for host in ("ftpes://datahub.example.org", "datahub.example.org/inbox", "datahub.example.org:21",
                     "datahub", "-datahub.example.org"):
            with self.subTest(host=host):
                self.assert_refused("host", message, host=host)

    def test_a_port_outside_the_range_is_refused(self):
        for port in ("0", "70000", "-1"):
            with self.subTest(port=port):
                self.assert_refused("port", None, port=port)

    def test_an_invalid_eic_is_refused(self):
        message = "Enter a valid EIC: 16 characters (digits, capital letters, hyphen) with a correct check character."
        for field in ("own_eic", "grid_operator_eic"):
            for value in ("12X-00000020RL-A", "12X-00000020RL", "not an eic"):
                with self.subTest(field=field, value=value):
                    self.assert_refused(field, message, **{field: value})

    def test_a_lowercase_valid_eic_is_stored_in_capitals(self):
        response = self.client.post(
            CHANGE_URL, form_data(own_eic=OWN_EIC.lower(), grid_operator_eic=" " + GRID_EIC.lower() + " ")
        )
        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual(row.own_eic, OWN_EIC)
        self.assertEqual(row.grid_operator_eic, GRID_EIC)


class CollectionSwitchTests(DatahubAdminBase):
    LOCKED = (
        "The collection is locked on this server by the server configuration. Only the one "
        "server that collects the messages may switch it on."
    )
    INCOMPLETE = "Enter the user name, the password and the own EIC before switching the collection on."

    @override_settings(DATAHUB_COLLECTION_ALLOWED=False)
    def test_the_switch_is_refused_where_the_server_configuration_locks_it(self):
        response = self.client.post(CHANGE_URL, form_data(collection_enabled="on"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(field_errors(response, "collection_enabled"), [german(self.LOCKED)])
        self.assertFalse(self.row().collection_enabled)

    @override_settings(DATAHUB_COLLECTION_ALLOWED=True)
    def test_the_switch_is_accepted_with_complete_data_where_allowed(self):
        response = self.client.post(CHANGE_URL, form_data(collection_enabled="on"))
        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertTrue(row.collection_enabled)
        self.assertTrue(row.collection_active)

    @override_settings(DATAHUB_COLLECTION_ALLOWED=True)
    def test_the_switch_is_refused_with_incomplete_data(self):
        for overrides in ({"username": ""}, {"own_eic": ""}, {"clear_password": "on"}):
            with self.subTest(overrides=overrides):
                response = self.client.post(CHANGE_URL, form_data(collection_enabled="on", **overrides))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(field_errors(response, "collection_enabled"), [german(self.INCOMPLETE)])
                row = self.row()
                self.assertFalse(row.collection_enabled)
                self.assertTrue(row.has_password)

    @override_settings(DATAHUB_COLLECTION_ALLOWED=True)
    def test_a_new_password_completes_the_data_for_the_switch(self):
        DatahubConfiguration.objects.filter(pk=1).update(encrypted_password="")
        response = self.client.post(CHANGE_URL, form_data(collection_enabled="on", new_password=OTHER_PASSWORD))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.row().collection_enabled)


class ChangeLogTests(DatahubAdminBase):
    def test_a_save_without_changes_records_no_event(self):
        old = self.row()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(CHANGE_URL, form_data())
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.events(Event.DATAHUB_CONFIGURATION_CHANGED).exists())
        self.assertEqual(self.row().updated_at, old.updated_at)

    def test_a_change_records_one_long_retention_event_with_field_names_only(self):
        old_ciphertext = self.row().encrypted_password
        with (
            self.assertLogs("leg_platform.security", "WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                CHANGE_URL, form_data(username="leg-new-user-0815", directory="/", new_password=OTHER_PASSWORD)
            )
        self.assertEqual(response.status_code, 302)
        events = list(self.events(Event.DATAHUB_CONFIGURATION_CHANGED))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].long_retention)
        self.assertEqual(events[0].actor_id, self.administrator.pk)
        self.assertIsNone(events[0].account_id)
        output = "\n".join(logs.output)
        self.assertIn("fields: username, directory, password", output)
        self.assertIn(f"actor {self.administrator.pk}", output)
        for secret in ("leg-new-user-0815", USERNAME, OTHER_PASSWORD, PASSWORD, old_ciphertext,
                       self.row().encrypted_password):
            self.assertNotIn(secret, output)

    def test_clearing_the_password_is_logged_as_a_password_change(self):
        with (
            self.assertLogs("leg_platform.security", "WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.client.post(CHANGE_URL, form_data(clear_password="on"))
        self.assertEqual(self.events(Event.DATAHUB_CONFIGURATION_CHANGED).count(), 1)
        self.assertIn("fields: password", "\n".join(logs.output))
        # Clearing again changes nothing and records nothing.
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(CHANGE_URL, form_data(clear_password="on"))
        self.assertEqual(self.events(Event.DATAHUB_CONFIGURATION_CHANGED).count(), 1)


class ConnectionTestTests(DatahubAdminBase):
    def last_tested_detail(self, logs):
        lines = [line for line in logs.output if "connection was tested" in line]
        self.assertEqual(len(lines), 1, logs.output)
        return lines[0]

    def test_get_changes_nothing_and_does_not_test(self):
        with patch(CHECK) as check:
            response = self.client.get(TEST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'method="post"')
        check.assert_not_called()
        self.assertIsNone(self.row().last_test_at)
        self.assertFalse(self.events(Event.DATAHUB_CONNECTION_TESTED).exists())

    def test_post_runs_the_test_and_stores_the_result(self):
        before = timezone.now()
        with (
            patch(CHECK, return_value=connection.TestResult(Outcome.SUCCEEDED, 3, "")) as check,
            self.assertLogs("leg_platform.security", "WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(TEST_URL)
        self.assertRedirects(response, CHANGE_URL, fetch_redirect_response=False)
        check.assert_called_once()
        tested, password = check.call_args.args
        self.assertEqual(tested.pk, 1)
        self.assertEqual(password, PASSWORD)
        row = self.row()
        self.assertEqual(row.last_test_outcome, Outcome.SUCCEEDED)
        self.assertEqual(row.last_test_file_count, 3)
        self.assertGreaterEqual(row.last_test_at, before)
        self.assertEqual(self.events(Event.DATAHUB_CONNECTION_TESTED).count(), 1)
        self.assertIn("result succeeded, files 3", self.last_tested_detail(logs))
        self.assertEqual(
            message_texts(response),
            [(django_messages.SUCCESS, german("Connection succeeded. Files in the directory: %(count)d.", count=3))],
        )

    def test_a_second_test_within_sixty_seconds_is_refused(self):
        DatahubConfiguration.objects.filter(pk=1).update(
            last_test_at=timezone.now() - timedelta(seconds=30), last_test_outcome=Outcome.SUCCEEDED
        )
        with (
            patch(CHECK) as check,
            self.assertLogs("leg_platform.security", "WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(TEST_URL)
        check.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertIn("result too_soon", self.last_tested_detail(logs))
        self.assertEqual(
            message_texts(response),
            [(django_messages.ERROR,
              german("The Datahub refuses a second login within 60 seconds. Wait a minute and test again."))],
        )

    def test_two_tests_in_a_row_call_the_server_once(self):
        with patch(CHECK, return_value=connection.TestResult(Outcome.SUCCEEDED, 0, "")) as check:
            self.client.post(TEST_URL)
            self.client.post(TEST_URL)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(self.events(Event.DATAHUB_CONNECTION_TESTED).count(), 2)

    def test_a_test_after_sixty_seconds_runs(self):
        DatahubConfiguration.objects.filter(pk=1).update(last_test_at=timezone.now() - timedelta(seconds=61))
        with patch(CHECK, return_value=connection.TestResult(Outcome.SUCCEEDED, 0, "")) as check:
            self.client.post(TEST_URL)
        check.assert_called_once()

    def test_an_incomplete_configuration_is_not_tested(self):
        DatahubConfiguration.objects.filter(pk=1).update(username="")
        with (
            patch(CHECK) as check,
            self.assertLogs("leg_platform.security", "WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(TEST_URL)
        check.assert_not_called()
        self.assertEqual(self.row().last_test_outcome, Outcome.INCOMPLETE)
        self.assertIsNotNone(self.row().last_test_at)
        self.assertIn("result incomplete", self.last_tested_detail(logs))
        self.assertEqual(
            message_texts(response),
            [(django_messages.ERROR, german("The test needs the server, the port, the user name and the password."))],
        )

    def test_an_unreadable_password_is_not_tested(self):
        DatahubConfiguration.objects.filter(pk=1).update(encrypted_password="not-a-token")
        with (
            patch(CHECK) as check,
            self.assertLogs("leg_platform.security", "WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.client.post(TEST_URL)
        check.assert_not_called()
        self.assertEqual(self.row().last_test_outcome, Outcome.PASSWORD_UNREADABLE)
        self.assertIn("result password_unreadable", self.last_tested_detail(logs))

    def test_a_failure_shows_the_escaped_server_text_but_logs_only_the_code(self):
        server_text = "530 <b>Login</b> incorrect"
        result = connection.TestResult(Outcome.LOGIN_FAILED, None, server_text)
        with (
            patch(CHECK, return_value=result),
            self.assertLogs("leg_platform.datahub.admin", "WARNING") as application,
            self.assertLogs("leg_platform.security", "WARNING") as security,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(TEST_URL, follow=True)
        row = self.row()
        self.assertEqual(row.last_test_outcome, Outcome.LOGIN_FAILED)
        self.assertIsNone(row.last_test_file_count)
        self.assertContains(response, "530 &lt;b&gt;Login&lt;/b&gt; incorrect")
        self.assertNotContains(response, server_text)
        self.assertContains(
            response, german("The login was refused. Check the user name and the password.")
        )
        for output in ("\n".join(application.output), self.last_tested_detail(security)):
            self.assertIn("login_failed", output)
            self.assertNotIn("530", output)
            self.assertNotIn("incorrect", output)
            self.assertNotIn(USERNAME, output)
            self.assertNotIn(PASSWORD, output)

    def test_the_page_shows_the_last_result(self):
        DatahubConfiguration.objects.filter(pk=1).update(
            last_test_at=timezone.now(), last_test_outcome=Outcome.SUCCEEDED, last_test_file_count=7
        )
        response = self.client.get(CHANGE_URL)
        self.assertContains(response, german("Connection succeeded"))

    def test_the_last_result_uses_the_singular_for_one_file(self):
        DatahubConfiguration.objects.filter(pk=1).update(
            last_test_at=timezone.now(), last_test_outcome=Outcome.SUCCEEDED, last_test_file_count=1
        )
        with override(settings.LANGUAGE_CODE):
            response = self.client.get(CHANGE_URL)
        self.assertContains(response, ", 1 Datei<")

    def test_the_page_says_when_no_password_is_stored(self):
        DatahubConfiguration.objects.filter(pk=1).update(encrypted_password="")
        self.assertContains(self.client.get(CHANGE_URL), german("none stored"))
