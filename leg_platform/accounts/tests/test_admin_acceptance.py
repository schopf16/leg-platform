"""The administration after the final acceptance (blocks 5c): the start page with the first steps,
the list of the email texts, readable rights, the portal settings, the lists of blocked logins and
the words of the administration (design package; AGENTS.md, section 6: no predefined roles)."""

import re

from django.contrib.auth.models import Group, Permission
from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import PortalSettings
from leg_platform.accounts import email_texts
from leg_platform.accounts.admin import EMAIL_TEXT_PROCESSES
from leg_platform.accounts.models import (
    EmailText,
    GraphConfiguration,
    PortalSettings,
    SecurityEvent,
)
from leg_platform.accounts.tests.helpers import ProvingClient, VerifiedClient, create_active_user

INDEX_URL = reverse("admin:index")


def staff_client(email, *codenames):
    """A staff account with exactly these rights (no group)."""
    staff = create_active_user(email)
    staff.set_privileges(is_staff=True, is_superuser=False)
    staff.user_permissions.set(Permission.objects.filter(codename__in=codenames))
    client = ProvingClient()
    client.force_login(staff)
    return client


def status_cells(page):
    """The words of the column "Text" of the list of the email texts."""
    return re.findall(r'<td class="field-text_status">([^<]*)</td>', page)


def administrator_client():
    administrator = create_active_user("admin.person@example.org")
    administrator.set_privileges(is_staff=True, is_superuser=True)
    client = ProvingClient()
    client.force_login(administrator)
    return client, administrator


@tag("mariadb")
class FirstStepsTests(TestCase):
    def test_a_system_administrator_sees_the_live_status_with_a_link_each(self):
        client, _administrator = administrator_client()

        page = client.get(INDEX_URL).content.decode()

        self.assertIn("Erste Schritte", page)
        for label in (
            "Adresse der Datenschutzerklärung",
            "Adresse des Impressums",
            "Texte der E-Mails",
            "Versand von E-Mails (Microsoft Graph)",
        ):
            self.assertIn(label, page)
        self.assertEqual(page.count("noch nicht gesetzt"), 2)
        self.assertIn("nicht bereit", page)
        self.assertIn(f"{len(EmailText.Key.values)} Texte, davon 0 angepasst", page)
        self.assertIn(reverse("admin:accounts_portalsettings_change", args=(1,)), page)
        self.assertIn(reverse("admin:accounts_emailtext_changelist"), page)

    def test_the_start_page_has_no_recent_actions_box_and_no_exclamation_mark(self):
        client, _administrator = administrator_client()

        page = client.get(INDEX_URL).content.decode()

        self.assertNotIn("recent-actions-module", page)
        self.assertNotIn("Neueste Aktionen", page)
        self.assertNotIn("portal-state-todo", page)
        self.assertIn('class="status-dot status-muted"', page)

    def test_the_status_follows_the_settings(self):
        client, _administrator = administrator_client()
        PortalSettings.objects.update_or_create(
            pk=1, defaults={"privacy_url": "https://www.example.org/privacy"}
        )
        EmailText.objects.ensure_all()
        EmailText.objects.filter(key=EmailText.Key.PASSWORD_RESET).update(subject="Neuer Betreff")

        page = client.get(INDEX_URL).content.decode()

        self.assertEqual(page.count("noch nicht gesetzt"), 1)
        self.assertIn(f"{len(EmailText.Key.values)} Texte, davon 1 angepasst", page)

    def test_a_ready_delivery_is_shown_as_ready_and_no_secret_is_shown(self):
        client, _administrator = administrator_client()
        row = GraphConfiguration.load()
        row.tenant_id = "11111111-1111-1111-1111-111111111111"
        row.client_id = "22222222-2222-2222-2222-222222222222"
        row.sender_address = "sender.box@example.org"
        row.certificate_pem = "-----BEGIN CERTIFICATE-----\nSECRET-PEM-SENTINEL\n-----END CERTIFICATE-----"
        row.encrypted_private_key = "ENCRYPTED-SENTINEL"
        row.save()

        page = client.get(INDEX_URL).content.decode()

        self.assertNotIn("nicht bereit", page)
        for sentinel in ("SECRET-PEM-SENTINEL", "ENCRYPTED-SENTINEL", "sender.box@example.org", "11111111-1111"):
            self.assertNotIn(sentinel, page)

    def test_the_box_lists_only_what_the_account_may_open(self):
        client = staff_client("texts.person@example.org", "view_emailtext", "change_emailtext")

        page = client.get(INDEX_URL).content.decode()

        self.assertIn("Erste Schritte", page)
        self.assertIn("Texte der E-Mails", page)
        for label in ("Datenschutzerklärung", "Impressums", "Microsoft Graph"):
            self.assertNotIn(label, page)

    def test_an_account_without_rights_gets_no_box_and_the_plain_sentence(self):
        client = staff_client("nobody.person@example.org")

        page = client.get(INDEX_URL).content.decode()

        self.assertNotIn("Erste Schritte", page)
        self.assertNotIn("pflegen Sie unter Inhalte", page)
        self.assertIn(
            "Ihr Konto hat noch keine Rechte in der Verwaltung. Bitte wenden Sie sich an einen "
            "Systemadministrator.",
            page,
        )
        self.assertNotIn("besitzt nicht die nötigen Rechte", page)


@tag("mariadb")
class EmailTextListTests(TestCase):
    def setUp(self):
        self.client, _administrator = administrator_client()
        self.url = reverse("admin:accounts_emailtext_changelist")

    def test_every_email_is_in_exactly_one_process(self):
        keys = [key for _name, group in EMAIL_TEXT_PROCESSES for key in group]

        self.assertEqual(sorted(keys), sorted(EmailText.Key.values))
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_list_follows_the_process_of_a_person(self):
        page = self.client.get(self.url).content.decode()

        positions = [
            page.index(str(EmailText.Key(key).label))
            for _name, group in EMAIL_TEXT_PROCESSES
            for key in group
        ]
        self.assertEqual(positions, sorted(positions))
        for process in ("Registrierung", "Anmeldung und Passwort", "E-Mail-Adressen", "Konto"):
            self.assertIn(process, page)

    def test_the_state_is_a_word_not_a_red_cross(self):
        EmailText.objects.ensure_all()
        EmailText.objects.filter(key=EmailText.Key.PASSWORD_RESET).update(subject="Neuer Betreff")

        page = self.client.get(self.url).content.decode()

        cells = status_cells(page)
        self.assertEqual(cells.count("Angepasst"), 1)
        self.assertEqual(cells.count("Standardtext"), len(EmailText.Key.values) - 1)
        self.assertNotIn("icon-no", page)
        self.assertNotIn("icon-yes", page)

    def test_the_last_change_is_empty_for_a_default_text_and_the_state_returns_after_the_reset(self):
        EmailText.objects.ensure_all()
        row = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)
        row.subject = "Neuer Betreff"
        row.save()
        change = reverse("admin:accounts_emailtext_change", args=(row.pk,))

        with_text = self.client.get(self.url).content.decode()
        self.client.post(
            change, {"subject": "", "body": "", "reset_to_default": "on", "_save": "Speichern"}
        )
        after_reset = self.client.get(self.url).content.decode()

        self.assertEqual(status_cells(with_text).count("Angepasst"), 1)
        self.assertNotIn("Angepasst", status_cells(after_reset))
        self.assertEqual(status_cells(after_reset).count("Standardtext"), len(EmailText.Key.values))
        self.assertEqual(len(re.findall(r'<td class="field-last_changed">-</td>', after_reset)), len(EmailText.Key.values))

    def test_the_list_has_no_table_of_placeholders_and_the_edit_page_keeps_them(self):
        page = self.client.get(self.url).content.decode()
        EmailText.objects.ensure_all()
        row = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)
        edit = self.client.get(reverse("admin:accounts_emailtext_change", args=(row.pk,))).content.decode()

        self.assertNotIn("<code>{", page)
        self.assertIn("<code>{", edit)
        self.assertIn("Jede E-Mail des Portals hat einen Standardtext.", page)

    def test_the_subject_field_has_the_full_width_through_the_stylesheet(self):
        EmailText.objects.ensure_all()
        row = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)

        edit = self.client.get(reverse("admin:accounts_emailtext_change", args=(row.pk,))).content.decode()

        self.assertIn("css/admin-portal.css", edit)
        self.assertIn('class="form-row field-subject"', edit)
        self.assertNotRegex(edit, r"<style\b|\sstyle\s*=")


@tag("mariadb")
class RightsTests(TestCase):
    """No predefined groups or roles; the rights have readable names and a help text."""

    def test_the_portal_has_no_predefined_group(self):
        self.assertEqual(Group.objects.count(), 0)

    def test_the_group_form_shows_the_rights_by_readable_names(self):
        client, _administrator = administrator_client()

        page = client.get(reverse("admin:auth_group_add")).content.decode()

        self.assertIn("Mailtext: ändern", page)
        self.assertIn("Konto: ansehen", page)
        self.assertNotIn("Can change email text", page)
        self.assertNotIn("\u00df", page)

    def test_the_account_page_of_another_person_shows_them_too(self):
        client, _administrator = administrator_client()
        other = create_active_user("other.person@example.org")

        page = client.get(reverse("admin:accounts_user_change", args=(other.pk,))).content.decode()

        self.assertIn("Mailtext: ändern", page)
        self.assertNotIn("Can change email text", page)

    def test_the_group_pages_explain_which_right_is_needed_for_what(self):
        client, _administrator = administrator_client()

        for url in (reverse("admin:auth_group_changelist"), reverse("admin:auth_group_add")):
            with self.subTest(url=url):
                page = client.get(url).content.decode()
                self.assertIn("Welche Rechte brauche ich wofür?", page)
                self.assertIn("Das Portal hat keine vorgefertigten Gruppen.", page)
                self.assertIn("Mailtexte ändern", page)
                self.assertIn("Angaben von Konten ändern", page)
                self.assertIn("kein Recht gibt es", page)

    def test_a_staff_member_gets_neither_the_group_pages_nor_the_help(self):
        client = staff_client("texts.person@example.org", "view_emailtext")

        self.assertEqual(client.get(reverse("admin:auth_group_changelist")).status_code, 403)
        self.assertEqual(client.get(reverse("admin:auth_group_add")).status_code, 403)


@tag("mariadb")
class PortalSettingsPageTests(TestCase):
    def setUp(self):
        self.client, self.administrator = administrator_client()
        self.url = reverse("admin:accounts_portalsettings_change", args=(1,))

    def test_the_fields_have_an_example_a_hint_and_the_width_of_the_page(self):
        page = self.client.get(self.url).content.decode()

        self.assertIn('placeholder="https://www.beispiel.ch/datenschutz"', page)
        self.assertIn('placeholder="https://www.beispiel.ch/impressum"', page)
        self.assertEqual(page.count("Muss mit https:// beginnen. Öffnet sich in einem neuen Tab."), 2)
        self.assertNotIn('size="60"', page)
        self.assertIn("css/admin-portal.css", page)

    def test_saving_says_what_happened(self):
        response = self.client.post(
            self.url,
            {
                "privacy_url": "https://www.example.org/privacy",
                "imprint_url": "https://www.example.org/imprint",
                "_save": "Speichern",
            },
            follow=True,
        )

        self.assertContains(response, "Gespeichert. Die Änderungen sind jetzt auf allen Seiten sichtbar.")
        self.assertNotContains(response, "wurde erfolgreich geändert")
        self.assertEqual(PortalSettings.load().privacy_url, "https://www.example.org/privacy")
        self.assertTrue(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.PORTAL_SETTINGS_CHANGED).exists()
        )


@tag("mariadb")
class BlockedLoginListTests(TestCase):
    def test_the_list_explains_itself_and_says_how_to_unblock(self):
        client, _administrator = administrator_client()

        page = client.get(reverse("admin:failed_logins")).content.decode()

        self.assertIn("Diese Listen zeigen fehlgeschlagene und gesperrte Anmeldungen.", page)
        self.assertIn("ist ein Code und keine Adresse", page)
        self.assertIn("Konto entsperren", page)
        self.assertRegex(page, r"nach \d+ Minuten")


@tag("mariadb")
class WordsOfTheAdministrationTests(TestCase):
    def setUp(self):
        self.client, self.administrator = administrator_client()
        self.other = create_active_user("other.person@example.org")

    def test_the_header_has_the_two_ways_back_into_the_portal_and_no_view_site_link(self):
        # D-113: "Zum Portal" (the old link "View site") is replaced by "Verwaltung" (the management area) and "Mein
        # Bereich" (the start page), both in the same tab.
        page = self.client.get(INDEX_URL).content.decode()

        self.assertRegex(page, r'<a href="/management/">Verwaltung</a>')
        self.assertRegex(page, r'<a href="/">Mein Bereich</a>')
        self.assertNotIn("Zum Portal", page)

    def test_the_path_reads_users_accounts_and_the_history_is_the_change_history(self):
        page = self.client.get(
            reverse("admin:accounts_user_change", args=(self.other.pk,))
        ).content.decode()

        self.assertIn('<li><a href="/admin/">Benutzer</a></li>', page)
        self.assertNotIn('<a href="/admin/accounts/">', page)
        self.assertIn("Änderungsverlauf", page)
        self.assertNotIn("Geschichte", page)

    def test_the_versions_are_earlier_versions_of_the_text(self):
        EmailText.objects.ensure_all()
        row = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)

        page = self.client.get(
            reverse("admin:accounts_emailtext_versions", args=(row.pk,))
        ).content.decode()
        edit = self.client.get(
            reverse("admin:accounts_emailtext_change", args=(row.pk,))
        ).content.decode()

        self.assertIn("Frühere Fassungen", page)
        self.assertIn("Frühere Fassungen", edit)
        self.assertNotIn("Versionen", page + edit)

    def test_the_second_factor_has_one_name_in_the_catalogue(self):
        from pathlib import Path

        catalogue = Path(__file__).resolve().parents[3] / "locale" / "de_CH" / "LC_MESSAGES" / "django.po"

        text = catalogue.read_text(encoding="utf-8")

        self.assertNotIn("Zwei-Faktor-Authentifizierung", text)
        self.assertNotIn("Notfallcode", text)

    def test_the_default_texts_of_every_email_use_the_term_of_the_portal(self):
        for key in email_texts.DEFINITIONS:
            subject, body = email_texts.default_texts(key)
            with self.subTest(key=key):
                self.assertNotIn("Zwei-Faktor-Authentifizierung", subject + body)
