"""The addresses of the privacy notice and the legal notice (D-056; LEG-Design-Vorgaben/legal/
VORGABEN.md, section 8): the portal settings, their fallback, the footer links and the admin."""

from unittest import mock

from django.contrib.admin.models import CHANGE, LogEntry
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.test import RequestFactory, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts.admin import PortalSettingsAdmin
from leg_platform.accounts.models import PortalSettings, SecurityEvent, validate_https_address
from leg_platform.accounts.portal_settings import legal_links
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user

PRIVACY = "https://www.example.org/privacy"
IMPRINT = "https://www.example.org/imprint"
WEBSITE = "https://www.example.org/"
LOGIN_URL = reverse("accounts:login")


@tag("mariadb")
class AddressValidationTests(TestCase):
    def test_only_complete_https_addresses_are_accepted(self):
        for value in (
            PRIVACY,
            "https://example.org",
            "https://example.org:8443/path?query=1",
        ):
            with self.subTest(value=value):
                validate_https_address(value)

    def test_other_schemes_credentials_and_white_space_are_refused(self):
        for value in (
            "http://www.example.org/privacy",
            "javascript:alert(1)",
            "ftp://www.example.org/",
            "//www.example.org/privacy",
            "www.example.org/privacy",
            "https://user:secret@www.example.org/",
            "https://www.example.org/pri vacy",
            "https://www.example.org/\nprivacy",
            "https:///privacy",
            "https://www.example.org:99999/",
            # An unclosed bracket makes urlsplit() itself raise ValueError (security review L4).
            "https://[::1",
            "https://[::1/privacy",
            # DEL and other control characters.
            "https://www.example.org/pri\x7fvacy",
            "https://www.example.org/\x00",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_https_address(value)

    def test_the_model_refuses_an_address_with_the_wrong_scheme_and_allows_empty(self):
        row = PortalSettings(privacy_url="http://www.example.org/", imprint_url="")
        with self.assertRaises(ValidationError) as caught:
            row.full_clean()
        self.assertIn("privacy_url", caught.exception.message_dict)
        self.assertNotIn("imprint_url", caught.exception.message_dict)
        PortalSettings(privacy_url="", imprint_url="").full_clean()

    def test_the_row_is_a_singleton(self):
        PortalSettings.load()
        self.assertEqual(PortalSettings.load().pk, 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PortalSettings.objects.create(id=2)


@tag("mariadb")
class LegalLinksTests(TestCase):
    @override_settings(INSTANCE_PRIVACY_URL="https://env.example.org/privacy")
    def test_without_a_setting_the_privacy_address_falls_back_to_the_instance_configuration(self):
        links = legal_links()
        self.assertEqual(links.privacy_url, "https://env.example.org/privacy")
        self.assertEqual(links.imprint_url, "")

    @override_settings(INSTANCE_PRIVACY_URL="https://env.example.org/privacy")
    def test_the_setting_wins_over_the_instance_configuration(self):
        PortalSettings.objects.create(id=1, privacy_url=PRIVACY, imprint_url=IMPRINT)
        links = legal_links()
        self.assertEqual((links.privacy_url, links.imprint_url), (PRIVACY, IMPRINT))

    @override_settings(INSTANCE_PRIVACY_URL="")
    def test_without_any_address_there_is_no_link(self):
        self.assertEqual(tuple(legal_links()), ("", ""))

    def test_the_database_is_read_once_per_request(self):
        PortalSettings.objects.create(id=1, privacy_url=PRIVACY)
        request = RequestFactory().get("/")
        with self.assertNumQueries(1):
            first = legal_links(request)
            second = legal_links(request)
        self.assertEqual(first, second)

    @override_settings(INSTANCE_PRIVACY_URL="https://env.example.org/privacy")
    def test_a_database_error_fails_safe_without_an_exception(self):
        PortalSettings.objects.create(id=1, privacy_url=PRIVACY, imprint_url=IMPRINT)
        with mock.patch.object(
            PortalSettings.objects, "filter", side_effect=OperationalError("down")
        ), self.assertLogs("leg_platform.accounts.portal_settings", level="WARNING"):
            links = legal_links()
        self.assertEqual(links.privacy_url, "https://env.example.org/privacy")
        self.assertEqual(links.imprint_url, "")

    @override_settings(INSTANCE_WEBSITE_URL=WEBSITE)
    def test_the_login_footer_has_three_items_and_the_links_open_in_a_new_tab(self):
        PortalSettings.objects.create(id=1, privacy_url=PRIVACY, imprint_url=IMPRINT)
        html = self.client.get(LOGIN_URL).content.decode()
        footer = html[html.index('<div class="legal-links">') :]
        footer = footer[: footer.index("</div>")]
        self.assertLess(footer.index("Datenschutz"), footer.index("Impressum"))
        self.assertLess(footer.index("Impressum"), footer.index("www.example.org</a>"))
        for address in (PRIVACY, IMPRINT, WEBSITE):
            self.assertIn(f'href="{address}" target="_blank" rel="noopener noreferrer"', footer)

    @override_settings(INSTANCE_PRIVACY_URL="", INSTANCE_WEBSITE_URL="")
    def test_without_addresses_the_items_are_plain_text(self):
        html = self.client.get(LOGIN_URL).content.decode()
        footer = html[html.index('<div class="legal-links">') :]
        footer = footer[: footer.index("</div>")]
        self.assertNotIn("<a ", footer)
        self.assertIn("<span", footer)
        self.assertIn("Datenschutz", footer)
        self.assertIn("Impressum", footer)

    @override_settings(INSTANCE_WEBSITE_URL=WEBSITE)
    def test_the_footer_text_of_the_website_is_the_host_only(self):
        html = self.client.get(LOGIN_URL).content.decode()
        self.assertIn(">www.example.org</a>", html)
        self.assertNotIn("To the website", html)


@tag("mariadb")
class PortalSettingsAdminTests(TestCase):
    client_class = VerifiedClient

    def setUp(self):
        self.administrator = create_active_user("administrator@example.org")
        self.administrator.set_privileges(is_staff=True, is_superuser=True)
        self.staff = create_active_user("staff@example.org")
        self.staff.set_privileges(is_staff=True, is_superuser=False)
        self.url = reverse("admin:accounts_portalsettings_change", args=(1,))

    def test_the_administrator_edits_the_addresses_with_german_labels(self):
        self.client.force_login(self.administrator)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for text in (
            "Portal-Einstellungen",
            "Adresse der Datenschutzerklärung",
            "Adresse des Impressums",
            "Die Texte pflegen Sie auf Ihrer Website. Hier tragen Sie nur die Adressen ein.",
        ):
            self.assertContains(response, text)
        response = self.client.post(
            self.url, {"privacy_url": PRIVACY, "imprint_url": IMPRINT, "_save": "Sichern"}
        )
        self.assertEqual(response.status_code, 302)
        row = PortalSettings.objects.get(pk=1)
        self.assertEqual((row.privacy_url, row.imprint_url), (PRIVACY, IMPRINT))
        # The change is in the history of the administration.
        self.assertTrue(
            LogEntry.objects.filter(
                user=self.administrator, object_id="1", action_flag=CHANGE
            ).exists()
        )

    def test_a_change_of_an_address_is_a_security_event_with_the_hosts_only(self):
        self.client.force_login(self.administrator)
        self.client.get(self.url)
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(
            execute=True
        ):
            self.client.post(
                self.url,
                {
                    "privacy_url": "https://old.example.org/secret-path?token=1",
                    "imprint_url": "",
                    "_save": "Sichern",
                },
            )
            self.client.post(
                self.url,
                {
                    "privacy_url": "https://new.example.org/other",
                    "imprint_url": IMPRINT,
                    "_save": "Sichern",
                },
            )
        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.PORTAL_SETTINGS_CHANGED)
        self.assertEqual(events.count(), 2)
        self.assertTrue(all(event.long_retention for event in events))
        self.assertTrue(all(event.actor_id == self.administrator.pk for event in events))
        text = "\n".join(logs.output)
        self.assertIn("privacy_url changed, host - -> old.example.org", text)
        self.assertIn("privacy_url changed, host old.example.org -> new.example.org", text)
        self.assertIn("imprint_url changed, host - -> www.example.org", text)
        # Paths, queries and the whole address are not in the log.
        self.assertNotIn("secret-path", text)
        self.assertNotIn("token=1", text)

    def test_saving_without_a_change_records_no_event(self):
        self.client.force_login(self.administrator)
        self.client.get(self.url)
        self.client.post(self.url, {"privacy_url": "", "imprint_url": "", "_save": "Sichern"})
        self.assertFalse(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.PORTAL_SETTINGS_CHANGED).exists()
        )

    def test_an_address_without_https_is_refused_with_a_german_message(self):
        self.client.force_login(self.administrator)
        response = self.client.post(
            self.url, {"privacy_url": "http://www.example.org/", "imprint_url": "", "_save": "Sichern"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bitte geben Sie eine vollständige Adresse mit https:// ein.")
        self.assertFalse(PortalSettings.objects.exists() and PortalSettings.objects.get(pk=1).privacy_url)

    def test_a_staff_user_has_no_access_and_nothing_is_changed(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        response = self.client.post(self.url, {"privacy_url": PRIVACY, "imprint_url": IMPRINT})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PortalSettings.objects.exists())
        index = self.client.get(reverse("admin:index"))
        self.assertNotContains(index, "Portal-Einstellungen")

    def test_nobody_can_add_or_delete_the_row(self):
        self.client.force_login(self.administrator)
        self.assertEqual(self.client.get(reverse("admin:accounts_portalsettings_add")).status_code, 403)
        PortalSettings.load()
        delete = reverse("admin:accounts_portalsettings_delete", args=(1,))
        self.assertEqual(self.client.post(delete, {"post": "yes"}).status_code, 403)
        self.assertTrue(PortalSettings.objects.filter(pk=1).exists())

    def test_the_list_leads_to_the_one_row_and_the_model_needs_a_login(self):
        self.client.force_login(self.administrator)
        response = self.client.get(reverse("admin:accounts_portalsettings_changelist"))
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_the_permissions_are_checked_for_the_system_administrator_only(self):
        admin = PortalSettingsAdmin(PortalSettings, mock.Mock())
        request = RequestFactory().get("/")
        request.user = self.staff
        self.assertFalse(admin.has_module_permission(request))
        self.assertFalse(admin.has_change_permission(request))
        request.user = self.administrator
        self.assertTrue(admin.has_change_permission(request))
        self.assertFalse(admin.has_add_permission(request))
        self.assertFalse(admin.has_delete_permission(request))
