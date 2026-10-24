"""The last spot check of the administration (block 6): security delta L-3 to L-5 and the design
points 1 to 10 (rights offered to a group, breadcrumbs, help sizes, messages, history)."""

import re
from datetime import timedelta
from pathlib import Path

from axes.models import AccessAttempt
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, Permission
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from leg_platform.accounts.models import PortalSettings
from leg_platform.accounts.models import EmailText, GraphConfiguration, PortalSettings
from leg_platform.accounts.tests.test_admin_acceptance import (
    INDEX_URL,
    administrator_client,
    staff_client,
)

STATIC_CSS = Path(__file__).resolve().parents[2] / "static" / "css" / "admin-portal.css"
GROUP_ADD = reverse("admin:auth_group_add")


@tag("mariadb")
class GraphStepTests(TestCase):
    """L-3: an expired certificate is not "ready"."""

    def setUp(self):
        self.client, _administrator = administrator_client()
        row = GraphConfiguration.load()
        row.tenant_id = "11111111-1111-1111-1111-111111111111"
        row.client_id = "22222222-2222-2222-2222-222222222222"
        row.sender_address = "sender.box@example.org"
        row.certificate_pem = "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----"
        row.encrypted_private_key = "ENCRYPTED"
        row.certificate_expires_on = timezone.now() + timedelta(days=200)
        row.save()
        self.row = row

    def test_a_valid_certificate_is_ready(self):
        page = self.client.get(INDEX_URL).content.decode()

        self.assertRegex(page, r"portal-state portal-state-ok\">bereit<")
        self.assertNotIn("Zertifikat abgelaufen", page)

    def test_an_expired_certificate_is_not_ready(self):
        self.row.certificate_expires_on = timezone.now() - timedelta(days=1)
        self.row.save()

        page = self.client.get(INDEX_URL).content.decode()

        self.assertRegex(page, r"portal-state portal-state-todo\">Zertifikat abgelaufen<")
        self.assertNotRegex(page, r">bereit<")


@tag("mariadb")
class RightNamesAreOnlyDisplayTests(TestCase):
    """L-4: the German names are the display; the stored rights are untouched."""

    def test_the_stored_names_and_codenames_do_not_change_when_the_names_are_shown(self):
        before = list(Permission.objects.order_by("pk").values_list("pk", "codename", "name"))
        client, _administrator = administrator_client()

        page = client.get(GROUP_ADD).content.decode()

        self.assertIn("Mailtext: ändern", page)
        after = list(Permission.objects.order_by("pk").values_list("pk", "codename", "name"))
        self.assertEqual(before, after)
        change = Permission.objects.get(codename="change_emailtext")
        self.assertEqual(change.name, "Can change email text")
        # The value of the option is the key of the right, not a name.
        self.assertRegex(page, rf'<option value="{change.pk}"[^>]*>Mailtext: ändern</option>')


@tag("mariadb")
class GroupRightsTests(TestCase):
    """Design 1: a group is offered the documented rights only."""

    def setUp(self):
        self.client, self.administrator = administrator_client()

    def test_only_the_documented_rights_are_offered(self):
        page = self.client.get(GROUP_ADD).content.decode()

        options = re.findall(r'<option value="\d+"[^>]*>([^<]+)</option>', page)
        self.assertEqual(
            sorted(options),
            sorted(
                [
                    "Konto: ansehen",
                    "Konto: ändern",
                    "Mailtext: ansehen",
                    "Mailtext: ändern",
                    "Portal-Einstellungen: ansehen",
                    "Portal-Einstellungen: ändern",
                    # The plain tables of the management data: view only (D-091).
                    "Einstellungen der Verwaltung: ansehen",
                    "LEG-Zuordnung: ansehen",
                    "LEG: ansehen",
                    "Messpunkt: ansehen",
                    "Person: ansehen",
                    "Personenzuordnung: ansehen",
                    "Standort: ansehen",
                    "Trafokreis: ansehen",
                    "Verlaufseintrag: ansehen",
                    "Änderung der Anteile: ansehen",
                ]
            ),
        )

    def test_the_script_of_the_widget_is_german_and_has_no_sharp_s(self):
        import json

        page = self.client.get(GROUP_ADD).content.decode()
        script = self.client.get(reverse("admin:jsi18n")).content.decode()

        self.assertIn(reverse("admin:jsi18n"), page)
        for text in (
            "Alle auswählen (%s)",
            "Alle entfernen (%s)",
            "Markierte auswählen (%s)",
            "Markierte entfernen (%s)",
            "Markieren Sie die gewünschten Einträge (%s) und klicken Sie dann auf den Pfeil «Wählen».",
        ):
            with self.subTest(text=text):
                self.assertIn(json.dumps(text), script)
        self.assertNotIn("\\u00df", script)

    def test_another_right_cannot_be_given_by_a_forged_post(self):
        forbidden = Permission.objects.get(codename="change_group")

        response = self.client.post(
            GROUP_ADD, {"name": "Forged group", "permissions": [forbidden.pk], "_save": "x"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Group.objects.filter(name="Forged group").exists())

    def test_a_right_that_the_group_holds_stays_in_the_list_and_is_kept_when_saving(self):
        held = Permission.objects.get(codename="change_group")
        keep = Permission.objects.get(codename="view_user")
        group = Group.objects.create(name="Existing group")
        group.permissions.set([held, keep])
        url = reverse("admin:auth_group_change", args=(group.pk,))

        page = self.client.get(url).content.decode()
        response = self.client.post(
            url, {"name": "Existing group", "permissions": [held.pk, keep.pk], "_save": "x"}
        )

        self.assertIn("Gruppe: ändern", page)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(group.permissions.values_list("pk", flat=True)), {held.pk, keep.pk})


@tag("mariadb")
class BreadcrumbTests(TestCase):
    """Design 3: the second step of the path is the group of the start page."""

    def setUp(self):
        self.client, _administrator = administrator_client()
        EmailText.objects.ensure_all()
        self.text = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)

    def crumbs(self, url):
        page = self.client.get(url).content.decode()
        block = re.search(r'<ol class="breadcrumbs">(.*?)</ol>', page, re.S).group(1)
        return re.findall(r"<li[^>]*>(?:<a [^>]*>)?([^<]+)", block)

    def test_the_pages_of_the_content_group(self):
        self.assertEqual(
            self.crumbs(reverse("admin:accounts_portalsettings_change", args=(1,)))[:3],
            ["Startseite", "Inhalte", "Portal-Einstellungen"],
        )
        self.assertEqual(
            self.crumbs(reverse("admin:accounts_emailtext_changelist")), ["Startseite", "Inhalte", "Mailtexte"]
        )
        change = reverse("admin:accounts_emailtext_change", args=(self.text.pk,))
        self.assertEqual(self.crumbs(change)[:3], ["Startseite", "Inhalte", "Mailtexte"])

    def test_the_earlier_versions_page_has_the_same_pattern(self):
        crumbs = self.crumbs(reverse("admin:accounts_emailtext_versions", args=(self.text.pk,)))

        self.assertEqual(crumbs[:3], ["Startseite", "Inhalte", "Mailtexte"])
        self.assertEqual(crumbs[-1], "Frühere Fassungen")

    def test_groups_and_accounts_are_under_users(self):
        self.assertEqual(
            self.crumbs(reverse("admin:auth_group_changelist")), ["Startseite", "Benutzer", "Gruppen"]
        )
        self.assertEqual(
            self.crumbs(reverse("admin:accounts_user_changelist")), ["Startseite", "Benutzer", "Konten"]
        )

    def test_the_history_page_follows_the_pattern(self):
        url = reverse("admin:accounts_emailtext_history", args=(self.text.pk,))

        self.assertEqual(self.crumbs(url)[:3], ["Startseite", "Inhalte", "Mailtexte"])


@tag("mariadb")
class MessagesAndHintsTests(TestCase):
    def setUp(self):
        self.client, self.administrator = administrator_client()
        self.settings_url = reverse("admin:accounts_portalsettings_change", args=(1,))

    def test_the_same_message_after_save_and_continue(self):
        response = self.client.post(
            self.settings_url,
            {
                "privacy_url": "https://www.example.org/privacy",
                "imprint_url": "",
                "_continue": "Sichern und weiter bearbeiten",
            },
            follow=True,
        )

        self.assertContains(response, "Gespeichert. Die Änderungen sind jetzt auf allen Seiten sichtbar.")
        self.assertNotContains(response, "wurde erfolgreich geändert")
        self.assertEqual(response.redirect_chain[-1][0], self.settings_url)

    def test_empty_addresses_say_that_the_footer_has_no_link(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"privacy_url": "https://www.example.org/p"})

        response = self.client.post(
            self.settings_url, {"privacy_url": "", "imprint_url": "", "_save": "x"}, follow=True
        )

        self.assertContains(
            response,
            "Die Adressen wurden entfernt. Die Fusszeile zeigt Datenschutz und Impressum ohne Link.",
        )
        self.assertNotContains(response, "Gespeichert. Die Änderungen sind")

    def test_the_history_says_that_the_default_text_was_restored(self):
        EmailText.objects.ensure_all()
        row = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)
        row.subject = "Neuer Betreff"
        row.save()

        self.client.post(
            reverse("admin:accounts_emailtext_change", args=(row.pk,)),
            {"subject": "", "body": "", "reset_to_default": "on", "_save": "x"},
        )

        entry = LogEntry.objects.filter(object_id=str(row.pk)).latest("id")
        self.assertEqual(entry.change_message, "Standardtext wiederhergestellt.")
        history = self.client.get(reverse("admin:accounts_emailtext_history", args=(row.pk,)))
        self.assertContains(history, "Standardtext wiederhergestellt.")

    def test_restoring_the_default_text_from_the_earlier_versions_says_the_same(self):
        EmailText.objects.ensure_all()
        row = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)
        row.subject = "Neuer Betreff"
        row.save()

        self.client.post(reverse("admin:accounts_emailtext_restore", args=(row.pk, 0)))

        entry = LogEntry.objects.filter(object_id=str(row.pk)).latest("id")
        self.assertEqual(entry.change_message, "Standardtext wiederhergestellt.")

    def test_the_email_text_page_explains_earlier_versions_and_the_history_in_one_line(self):
        EmailText.objects.ensure_all()
        row = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)
        url = reverse("admin:accounts_emailtext_change", args=(row.pk,))

        admin_page = self.client.get(url).content.decode()
        staff = staff_client("texts.person@example.org", "view_emailtext", "change_emailtext")
        staff_page = staff.get(url).content.decode()

        line = "Frühere Fassungen zeigen den Wortlaut früherer Texte."
        history = "Der Änderungsverlauf zeigt, wer wann was geändert hat."
        self.assertIn(f"{line} {history}", admin_page)
        # The history is for system administrators only, so a staff member is not pointed to it.
        self.assertIn(line, staff_page)
        self.assertNotIn(history, staff_page)

    def test_the_start_page_names_name_logo_and_colours_as_a_plain_fact(self):
        page = self.client.get(INDEX_URL).content.decode()

        # D-073: no longer "set by the operator at the installation"; an item of the box instead.
        self.assertNotIn("legt der Betrieb bei der Installation fest", page)
        self.assertIn("Name, Logo und Farben", page)
        self.assertIn("Serverkonfiguration", page)
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Beispiel"})
        self.assertIn("eigene Einstellungen", self.client.get(INDEX_URL).content.decode())

    def test_the_code_message_is_the_same_in_the_profile_and_asks_to_wait(self):
        from leg_platform.accounts.forms import CodeForm, SetupCodeForm

        with translation.override("de-ch"):
            self.assertEqual(str(SetupCodeForm.invalid_code_message), str(CodeForm.invalid_code_message))
            self.assertIn("Warten Sie auf den nächsten", str(CodeForm.invalid_code_message))


@tag("mariadb")
class BlockedLoginDetailTests(TestCase):
    """L-5: no browser data of a failed login in the administration."""

    def test_the_detail_page_shows_no_user_agent_and_no_accept_header(self):
        client, _administrator = administrator_client()
        attempt = AccessAttempt.objects.create(
            username="pseudonym-digest",
            ip_address="10.9.9.9",
            user_agent="UA-SENTINEL-1",
            http_accept="ACCEPT-SENTINEL-1",
            path_info="/login/",
            failures_since_start=1,
        )

        page = client.get(reverse("admin:axes_accessattempt_change", args=(attempt.pk,))).content.decode()

        self.assertIn("pseudonym-digest", page)
        for sentinel in ("UA-SENTINEL-1", "ACCEPT-SENTINEL-1", "10.9.9.9"):
            self.assertNotIn(sentinel, page)


class AdminStyleSheetTests(SimpleTestCase):
    def setUp(self):
        self.css = re.sub(r"/\*.*?\*/", "", STATIC_CSS.read_text(encoding="utf-8"), flags=re.S)

    def declarations(self, selector):
        for match in re.finditer(r"([^{}]+)\{([^}]*)\}", self.css):
            if selector in [part.strip() for part in match.group(1).split(",")]:
                return match.group(2)
        raise AssertionError(f"No rule for {selector}.")

    def test_help_texts_are_at_least_14_px(self):
        for selector in (".help", "p.help", "form .aligned p.help", ".helptext", "div.help li"):
            with self.subTest(selector=selector):
                self.assertIn("font-size: 0.875rem", self.declarations(selector))

    def test_the_text_of_an_email_has_the_width_of_the_subject(self):
        self.assertIn("width: 100%", self.declarations(".field-body textarea"))
        self.assertIn("width: 100%", self.declarations('.field-subject input[type="text"]'))

    def test_the_status_of_the_first_steps_has_a_fixed_column(self):
        self.assertIn("grid-template-columns: 18em", self.declarations(".portal-steps li"))
