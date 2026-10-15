"""Editable email texts (D-039): validation, rendering, sending and the administration."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import translation

from leg_platform.accounts import email_texts, emails
from leg_platform.accounts.models import EmailText, EmailTextVersion
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user

Key = EmailText.Key
BODY_WITH_LINK = "Guten Tag\n\nHier: {verification_url}\n\nGültig: {lifetime_hours} Stunden."


def messages(validate, key, text):
    try:
        validate(key, text)
    except ValidationError as error:
        return error.messages
    return []


class ValidationTests(SimpleTestCase):
    def test_an_empty_text_is_the_default_and_always_valid(self):
        self.assertEqual(messages(email_texts.validate_subject, Key.PASSWORD_RESET, ""), [])
        self.assertEqual(messages(email_texts.validate_body, Key.PASSWORD_RESET, ""), [])

    def test_a_valid_body_passes(self):
        self.assertEqual(
            messages(email_texts.validate_body, Key.REGISTRATION_VERIFICATION, BODY_WITH_LINK), []
        )

    def test_an_unknown_placeholder_is_rejected_by_name(self):
        errors = messages(
            email_texts.validate_body, Key.PASSWORD_RESET, "{reset_url} {nonsense}"
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("{nonsense}", errors[0])

    def test_a_placeholder_of_another_email_is_rejected(self):
        errors = messages(
            email_texts.validate_body, Key.PASSWORD_RESET, "{reset_url} {verification_url}"
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("{verification_url}", errors[0])

    def test_the_personal_link_is_required(self):
        errors = messages(email_texts.validate_body, Key.PASSWORD_RESET, "Hallo {instance_name}")

        self.assertEqual(len(errors), 1)
        self.assertIn("{reset_url}", errors[0])

    def test_emails_without_a_personal_link_need_no_placeholder(self):
        for key in (Key.REGISTRATION_EXISTING_ACCOUNT, Key.PASSWORD_CHANGED):
            with self.subTest(key=key):
                self.assertEqual(messages(email_texts.validate_body, key, "Hallo"), [])

    def test_typed_addresses_are_rejected(self):
        for text in (
            "https://evil.example/login",
            "http://evil.example",
            "ftp://evil.example",
            "javascript://x",
            "Siehe www.evil.example",
            "mailto:evil@example.org",
            "HTTPS://EVIL.EXAMPLE",
        ):
            with self.subTest(text=text):
                errors = messages(
                    email_texts.validate_body, Key.PASSWORD_CHANGED, f"Hallo {text} Ende"
                )
                self.assertTrue(errors)

    def test_template_syntax_is_not_a_placeholder(self):
        for text in ("{{ 7 * 7 }}", "{% load i18n %}", "{{ settings.SECRET_KEY }}", "{0.__class__}"):
            with self.subTest(text=text):
                self.assertTrue(
                    messages(email_texts.validate_body, Key.PASSWORD_CHANGED, text)
                )

    def test_unbalanced_brackets_are_rejected(self):
        for text in ("a { b", "a } b", "{login_url", "login_url}"):
            with self.subTest(text=text):
                self.assertTrue(messages(email_texts.validate_body, Key.PASSWORD_CHANGED, text))

    def test_control_characters_and_overlong_texts_are_rejected(self):
        self.assertTrue(
            messages(email_texts.validate_body, Key.PASSWORD_CHANGED, "a\x00b")
        )
        self.assertTrue(
            messages(email_texts.validate_body, Key.PASSWORD_CHANGED, "x" * 5001)
        )
        self.assertEqual(
            messages(email_texts.validate_body, Key.PASSWORD_CHANGED, "x\ty\nz" * 10), []
        )

    def test_disguised_addresses_are_rejected(self):
        for text in (
            "portal-sicherheit.example/login",
            "bit.ly/x",
            "//evil.example",
            "support@portal.example.org",
            "tel:+41790000000",
            "sms:123",
            "javascript:alert(1)",
            "evil\u3002example",
            "\uff48\uff54\uff54\uff50\uff53\uff1a\uff0f\uff0fevil.example",
            "https\u200b://evil.example",
            "WWW.EVIL.ORG",
            "c:\\\\share",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    messages(email_texts.validate_body, Key.PASSWORD_CHANGED, f"Hallo {text} Ende")
                )
                self.assertTrue(
                    messages(email_texts.validate_subject, Key.PASSWORD_CHANGED, f"Hallo {text}")
                )

    def test_ordinary_german_text_is_accepted(self):
        text = (
            "Guten Tag, z.B. um 9.30 Uhr, d.h. bald; und/oder später. Hinweis: bitte prüfen.\n"
            "Link:{login_url} Preis 12.50 Franken (inkl. MwSt.)"
        )

        self.assertEqual(messages(email_texts.validate_body, Key.PASSWORD_CHANGED, text), [])

    def test_format_and_separator_characters_are_rejected(self):
        for char in ("\u2028", "\u200b", "\u202e", "\x7f", "\x85", "\u00ad"):
            with self.subTest(char=repr(char)):
                self.assertTrue(
                    messages(email_texts.validate_body, Key.PASSWORD_CHANGED, f"a{char}b")
                )
                self.assertTrue(
                    messages(email_texts.validate_subject, Key.PASSWORD_CHANGED, f"a{char}b")
                )

    def test_the_subject_is_one_line_with_the_portal_name_only(self):
        validate = email_texts.validate_subject

        self.assertEqual(messages(validate, Key.PASSWORD_RESET, "Neu bei {instance_name}"), [])
        self.assertTrue(messages(validate, Key.PASSWORD_RESET, "Zeile\nzwei"))
        self.assertTrue(messages(validate, Key.PASSWORD_RESET, "{reset_url}"))
        self.assertTrue(messages(validate, Key.PASSWORD_RESET, "{login_url}"))


@tag("mariadb")
class RenderTests(TestCase):
    VALUES = {
        "instance_name": "Portal",
        "contact_url": "",
        "privacy_url": "",
        "login_url": "https://portal.example.org/login/",
        "verification_url": "https://portal.example.org/register/verify/abc/",
        "lifetime_hours": "24",
    }

    def store(self, key, **fields):
        EmailText.objects.update_or_create(key=key, defaults=fields)

    def test_without_a_stored_text_the_default_is_used(self):
        with translation.override("de-ch"):
            subject, body = email_texts.render(Key.REGISTRATION_VERIFICATION, self.VALUES)

        self.assertEqual(subject, "Ihre Registrierung beim Portal")
        self.assertIn(self.VALUES["verification_url"], body)
        self.assertIn("24 Stunden", body)

    def test_a_stored_text_replaces_the_placeholders(self):
        self.store(Key.REGISTRATION_VERIFICATION, subject="Willkommen bei {instance_name}", body=BODY_WITH_LINK)

        subject, body = email_texts.render(Key.REGISTRATION_VERIFICATION, self.VALUES)

        self.assertEqual(subject, "Willkommen bei Portal")
        self.assertEqual(
            body,
            "Guten Tag\n\nHier: https://portal.example.org/register/verify/abc/\n\n"
            "Gültig: 24 Stunden.\n",
        )

    def test_subject_and_body_are_independent(self):
        self.store(Key.REGISTRATION_VERIFICATION, subject="", body=BODY_WITH_LINK)

        with translation.override("de-ch"):
            subject, body = email_texts.render(Key.REGISTRATION_VERIFICATION, self.VALUES)

        self.assertEqual(subject, "Ihre Registrierung beim Portal")
        self.assertTrue(body.startswith("Guten Tag\n\nHier:"))

    def test_values_are_not_searched_for_further_placeholders(self):
        self.store(Key.REGISTRATION_VERIFICATION, body=BODY_WITH_LINK)
        values = {**self.VALUES, "lifetime_hours": "{verification_url}"}

        _subject, body = email_texts.render(Key.REGISTRATION_VERIFICATION, values)

        self.assertIn("Gültig: {verification_url} Stunden.", body)

    def test_an_invalid_stored_text_falls_back_to_the_default_and_is_logged(self):
        # Saved before a placeholder disappeared: the link is missing now.
        self.store(Key.REGISTRATION_VERIFICATION, subject="{reset_url}", body="Kein Link hier")

        with self.assertLogs("leg_platform.accounts.email_texts", "WARNING") as logs:
            subject, body = email_texts.render(Key.REGISTRATION_VERIFICATION, self.VALUES)

        self.assertNotIn("{", subject)
        self.assertIn(self.VALUES["verification_url"], body)
        self.assertNotIn("Kein Link hier", "\n".join(logs.output))

    def test_template_syntax_in_a_stored_text_is_not_executed(self):
        # Written around the validation, e.g. directly in the database.
        self.store(Key.PASSWORD_CHANGED, body="{{ 7 * 7 }} {% now 'Y' %}")

        _subject, body = email_texts.render(Key.PASSWORD_CHANGED, self.VALUES)

        self.assertNotIn("49", body)
        self.assertNotIn("{%", body)

    def test_every_default_uses_the_markers_of_its_placeholders(self):
        for key in Key.values:
            with self.subTest(key=key):
                subject, body = email_texts.default_texts(key)
                self.assertIn("{instance_name}", subject + body)
                for placeholder in email_texts.required_placeholders(key):
                    self.assertIn("{%s}" % placeholder.name, body)
                # The default must itself pass the validation.
                self.assertEqual(messages(email_texts.validate_subject, key, subject), [])
                self.assertEqual(messages(email_texts.validate_body, key, body), [])

    def test_the_default_has_no_contact_branch_without_a_website(self):
        _subject, without = email_texts.default_texts(Key.PASSWORD_CHANGED)
        with override_settings(INSTANCE_WEBSITE_URL="https://www.example.org/"):
            _subject, with_website = email_texts.default_texts(Key.PASSWORD_CHANGED)

        self.assertNotIn("{contact_url}", without)
        self.assertIn("{contact_url}", with_website)

    @override_settings(INSTANCE_NAME="Strom Ittigen", INSTANCE_WEBSITE_URL="https://www.example.org/")
    def test_the_sent_email_uses_the_stored_text(self):
        self.store(
            Key.PASSWORD_RESET,
            subject="Neues Passwort für {instance_name}",
            body="Link: {reset_url}\nGültig {lifetime_hours} h\nKontakt: {contact_url}\n{login_url}",
        )

        emails.send_password_reset("person@example.org", "T" * 43, timedelta(hours=24))

        message = mail.outbox[0]
        self.assertEqual(message.subject, "Neues Passwort für Strom Ittigen")
        self.assertIn("/password-reset/verify/" + "T" * 43 + "/", message.body)
        self.assertIn("Gültig 24 h", message.body)
        self.assertIn("Kontakt: https://www.example.org/", message.body)
        self.assertEqual(message.to, ["person@example.org"])
        self.assertEqual(message.extra_headers["Auto-Submitted"], "auto-generated")

    def test_the_sent_email_keeps_the_default_without_a_stored_text(self):
        emails.send_password_changed("person@example.org")

        self.assertIn("Passwort", mail.outbox[0].body)


@tag("mariadb")
class AdminTests(TestCase):
    client_class = VerifiedClient

    def setUp(self):
        self.administrator = create_active_user("admin@example.org")
        self.administrator.set_privileges(is_staff=True, is_superuser=True)
        self.editor = create_active_user("editor@example.org")
        self.editor.set_privileges(is_staff=True, is_superuser=False)
        self.editor.user_permissions.add(
            Permission.objects.get(content_type__app_label="accounts", codename="change_emailtext"),
            Permission.objects.get(content_type__app_label="accounts", codename="view_emailtext"),
        )
        self.plain_staff = create_active_user("plain@example.org")
        self.plain_staff.set_privileges(is_staff=True, is_superuser=False)
        self.list_url = reverse("admin:accounts_emailtext_changelist")

    def change_url(self, key):
        EmailText.objects.ensure_all()
        return reverse("admin:accounts_emailtext_change", args=(EmailText.objects.get(key=key).pk,))

    def post(self, key, **data):
        return self.client.post(self.change_url(key), data)

    def test_the_list_shows_every_email_and_no_placeholder_table(self):
        self.client.force_login(self.administrator)
        with translation.override("de-ch"):
            response = self.client.get(self.list_url)

        content = response.content.decode()
        self.assertEqual(EmailText.objects.count(), len(Key.values))
        for key in Key:
            self.assertIn(str(key.label), content)
        # The placeholders are explained on the page of each email, not on the list (acceptance, P1-2).
        self.assertNotIn("<code>{", content)
        self.assertIn("Mailtexte", content)
        self.assertNotIn("\u00df", content)

    def test_the_form_shows_the_default_text_and_only_the_placeholders_of_the_email(self):
        self.client.force_login(self.administrator)
        with translation.override("de-ch"):
            response = self.client.get(self.change_url(Key.PASSWORD_RESET))

        content = response.content.decode()
        self.assertIn("{reset_url}", content)
        self.assertIn("{lifetime_hours}", content)
        self.assertNotIn("{verification_url}</code>", content)
        self.assertIn("Neues Passwort für {instance_name} wählen", content)

    def test_saving_the_unchanged_default_stores_nothing(self):
        self.client.force_login(self.administrator)
        subject, body = email_texts.default_texts(Key.PASSWORD_RESET)

        response = self.post(Key.PASSWORD_RESET, subject=subject, body=body)

        self.assertEqual(response.status_code, 302)
        stored = EmailText.objects.get(key=Key.PASSWORD_RESET)
        self.assertEqual((stored.subject, stored.body), ("", ""))

    def test_a_written_text_is_saved_and_used(self):
        self.client.force_login(self.administrator)

        response = self.post(
            Key.PASSWORD_RESET,
            subject="Neues Passwort",
            body="Hallo\r\n\r\nLink: {reset_url}\r\n",
        )

        self.assertEqual(response.status_code, 302)
        stored = EmailText.objects.get(key=Key.PASSWORD_RESET)
        self.assertEqual(stored.subject, "Neues Passwort")
        self.assertEqual(stored.body, "Hallo\n\nLink: {reset_url}")

    def test_an_invalid_text_is_rejected_with_swiss_german_messages(self):
        self.client.force_login(self.administrator)
        with translation.override("de-ch"):
            response = self.post(
                Key.PASSWORD_RESET, subject="Test", body="Ohne Link {unbekannt} http://evil.example"
            )

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Der Platzhalter {unbekannt} existiert nicht", content)
        self.assertIn("Der Platzhalter {reset_url} ist Pflicht", content)
        self.assertIn("Geben Sie keine Web- oder E-Mail-Adressen ein", content)
        self.assertNotIn("\u00df", content)
        stored = EmailText.objects.get(key=Key.PASSWORD_RESET)
        self.assertEqual((stored.subject, stored.body), ("", ""))

    def test_the_subject_cannot_use_other_placeholders(self):
        self.client.force_login(self.administrator)

        response = self.post(Key.PASSWORD_RESET, subject="{reset_url}", body="{reset_url}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmailText.objects.get(key=Key.PASSWORD_RESET).subject, "")

    def test_the_default_can_be_restored(self):
        self.client.force_login(self.administrator)
        self.post(Key.PASSWORD_RESET, subject="Neues Passwort", body="Link: {reset_url}")

        response = self.post(
            Key.PASSWORD_RESET, subject="egal", body="egal", reset_to_default="on"
        )

        self.assertEqual(response.status_code, 302)
        stored = EmailText.objects.get(key=Key.PASSWORD_RESET)
        self.assertEqual((stored.subject, stored.body), ("", ""))

    def test_a_staff_user_with_the_permission_can_edit(self):
        self.client.force_login(self.editor)

        listing = self.client.get(self.list_url)
        response = self.post(Key.PASSWORD_RESET, subject="Neu", body="Link: {reset_url}")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EmailText.objects.get(key=Key.PASSWORD_RESET).subject, "Neu")

    def test_a_staff_user_without_the_permission_has_no_access(self):
        EmailText.objects.ensure_all()
        pk = EmailText.objects.get(key=Key.PASSWORD_RESET).pk
        self.client.force_login(self.plain_staff)

        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        change_url = reverse("admin:accounts_emailtext_change", args=(pk,))
        self.assertEqual(self.client.get(change_url).status_code, 403)
        response = self.client.post(change_url, {"subject": "Neu", "body": "{reset_url}"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(EmailText.objects.get(pk=pk).subject, "")

    def test_a_view_only_user_cannot_change_texts(self):
        EmailText.objects.ensure_all()
        viewer = create_active_user("viewer@example.org")
        viewer.set_privileges(is_staff=True, is_superuser=False)
        viewer.user_permissions.add(
            Permission.objects.get(content_type__app_label="accounts", codename="view_emailtext")
        )
        self.client.force_login(viewer)
        pk = EmailText.objects.get(key=Key.PASSWORD_RESET).pk

        response = self.client.post(
            reverse("admin:accounts_emailtext_change", args=(pk,)),
            {"subject": "Neu", "body": "{reset_url}"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(EmailText.objects.get(pk=pk).subject, "")

    def test_texts_can_neither_be_added_nor_deleted(self):
        self.client.force_login(self.administrator)
        url = self.change_url(Key.PASSWORD_RESET)

        self.assertEqual(self.client.get(reverse("admin:accounts_emailtext_add")).status_code, 403)
        self.assertEqual(self.client.get(url.replace("change/", "delete/")).status_code, 403)

    def test_the_history_is_for_system_administrators_only(self):
        url = self.change_url(Key.PASSWORD_RESET).replace("change/", "history/")
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.administrator)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_log_entry_names_the_email_but_not_the_text(self):
        self.client.force_login(self.administrator)

        self.post(Key.PASSWORD_RESET, subject="Geheimer Betreff", body="Geheimer Text {reset_url}")

        entry = LogEntry.objects.get()
        stored = f"{entry.object_repr}{entry.change_message}"
        self.assertNotIn("Geheim", stored)
        self.assertNotIn("@", stored)

    def test_an_anonymous_visitor_is_sent_to_the_login(self):
        EmailText.objects.ensure_all()

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_the_list_creates_rows_only_for_permitted_users(self):
        self.client.force_login(self.plain_staff)

        self.client.get(self.list_url)

        self.assertEqual(EmailText.objects.count(), 0)


@tag("mariadb")
class PreviewAndVersionTests(TestCase):
    client_class = VerifiedClient

    def setUp(self):
        self.administrator = create_active_user("admin@example.org")
        self.administrator.set_privileges(is_staff=True, is_superuser=True)
        self.editor = create_active_user("editor@example.org")
        self.editor.set_privileges(is_staff=True, is_superuser=False)
        self.editor.user_permissions.add(
            Permission.objects.get(content_type__app_label="accounts", codename="change_emailtext"),
            Permission.objects.get(content_type__app_label="accounts", codename="view_emailtext"),
        )
        self.viewer = create_active_user("viewer@example.org")
        self.viewer.set_privileges(is_staff=True, is_superuser=False)
        self.viewer.user_permissions.add(
            Permission.objects.get(content_type__app_label="accounts", codename="view_emailtext")
        )
        self.plain_staff = create_active_user("plain@example.org")
        self.plain_staff.set_privileges(is_staff=True, is_superuser=False)
        EmailText.objects.ensure_all()
        self.text = EmailText.objects.get(key=Key.PASSWORD_RESET)
        self.change_url = reverse("admin:accounts_emailtext_change", args=(self.text.pk,))
        self.versions_url = reverse("admin:accounts_emailtext_versions", args=(self.text.pk,))

    def restore_url(self, version_id):
        return reverse("admin:accounts_emailtext_restore", args=(self.text.pk, version_id))

    def save(self, subject, body):
        return self.client.post(self.change_url, {"subject": subject, "body": body})

    def test_the_example_values_use_a_link_that_does_not_exist(self):
        subject, body = emails.preview(Key.PASSWORD_RESET, "Neu bei {instance_name}", "{reset_url}")

        self.assertEqual(subject, "Neu bei LEG Platform")
        self.assertIn("/password-reset/verify/" + "x" * 43 + "/", body)

    def test_the_preview_shows_the_typed_text_and_stores_nothing(self):
        self.client.force_login(self.administrator)

        with translation.override("de-ch"):
            response = self.client.post(
                self.change_url,
                {"subject": "Neu bei {instance_name}", "body": "Link: {reset_url}", "_preview": "1"},
            )

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Vorschau", content)
        self.assertIn("Neu bei LEG Platform", content)
        self.assertIn("/password-reset/verify/" + "x" * 43 + "/", content)
        self.assertIn("Es wurde noch nichts gespeichert", content)
        self.assertEqual(len(mail.outbox), 0)
        self.text.refresh_from_db()
        self.assertEqual((self.text.subject, self.text.body), ("", ""))
        self.assertEqual(self.text.versions.count(), 0)
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_the_preview_of_an_invalid_text_shows_the_errors_instead(self):
        self.client.force_login(self.administrator)

        with translation.override("de-ch"):
            response = self.client.post(
                self.change_url, {"subject": "", "body": "Ohne Link", "_preview": "1"}
            )

        content = response.content.decode()
        self.assertIn("Der Platzhalter {reset_url} ist Pflicht", content)
        self.assertNotIn("Es wurde noch nichts gespeichert", content)

    def test_the_preview_of_the_default_shows_the_default_text(self):
        self.client.force_login(self.administrator)

        with translation.override("de-ch"):
            response = self.client.post(
                self.change_url, {"subject": "", "body": "", "_preview": "1"}
            )

        content = response.content.decode()
        self.assertIn("Neues Passwort für LEG Platform wählen", content)
        self.assertIn("/password-reset/verify/", content)

    def test_the_test_email_goes_to_the_address_of_the_administrator_only(self):
        self.client.force_login(self.administrator)

        with translation.override("de-ch"):
            response = self.client.post(
                self.change_url,
                {
                    "subject": "Neu bei {instance_name}",
                    "body": "Link: {reset_url}",
                    "_send_test": "1",
                    "to": "other@example.org",
                },
            )

        self.assertEqual(response.status_code, 200)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["admin@example.org"])
        self.assertEqual(message.subject, "[Test] Neu bei LEG Platform")
        self.assertTrue(message.body.startswith("Dies ist eine Testmail."))
        self.assertIn("/password-reset/verify/" + "x" * 43 + "/", message.body)
        self.assertEqual(message.extra_headers["Auto-Submitted"], "auto-generated")
        self.assertIn("Eine Testmail wurde an admin@example.org gesendet.", response.content.decode())
        self.text.refresh_from_db()
        self.assertEqual((self.text.subject, self.text.body), ("", ""))
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_a_failing_test_email_is_reported_without_details(self):
        self.client.force_login(self.administrator)

        with translation.override("de-ch"):
            with patch("leg_platform.accounts.emails.EmailMessage.send", side_effect=OSError("x")):
                with self.assertLogs("leg_platform.accounts.admin", "ERROR") as logs:
                    response = self.client.post(
                        self.change_url,
                        {"subject": "", "body": "", "_send_test": "1"},
                    )

        self.assertIn("Die Testmail konnte nicht gesendet werden.", response.content.decode())
        self.assertNotIn("admin@example.org", "\n".join(logs.output))
        self.assertIn("OSError", "\n".join(logs.output))

    def test_preview_and_test_email_need_the_change_permission(self):
        for user in (self.viewer, self.plain_staff):
            self.client.force_login(user)
            for action in ("_preview", "_send_test"):
                with self.subTest(user=user.email, action=action):
                    response = self.client.post(
                        self.change_url, {"subject": "", "body": "", action: "1"}
                    )
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_preview_button_is_offered_to_editors_only(self):
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(self.change_url), 'name="_preview"')
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(self.change_url), 'name="_preview"')

    def test_every_save_that_changes_the_text_adds_a_version(self):
        self.client.force_login(self.editor)

        self.save("Eins", "Link: {reset_url}")
        self.save("Eins", "Link: {reset_url}")  # unchanged
        self.save("Zwei", "Link: {reset_url}")

        versions = list(self.text.versions.order_by("id"))
        self.assertEqual([v.subject for v in versions], ["Eins", "Zwei"])
        self.assertEqual({v.changed_by_id for v in versions}, {self.editor.pk})

    def test_using_the_default_again_is_a_version_too(self):
        self.client.force_login(self.editor)
        self.save("Eins", "Link: {reset_url}")

        self.client.post(self.change_url, {"subject": "x", "body": "x", "reset_to_default": "on"})

        last = self.text.versions.first()
        self.assertEqual((last.subject, last.body), ("", ""))

    def test_the_versions_page_lists_the_texts_and_marks_the_current_one(self):
        self.client.force_login(self.administrator)
        self.save("Eins", "Link: {reset_url}")
        self.save("Zwei", "Link: {reset_url}")
        self.client.force_login(self.editor)

        with translation.override("de-ch"):
            content = self.client.get(self.versions_url).content.decode()

        self.assertIn("Eins", content)
        self.assertIn("Zwei", content)
        self.assertEqual(content.count("Aktuell"), 1)
        self.assertIn("Standardtext", content)
        # The account that wrote a version is not shown.
        self.assertNotIn("admin@example.org", content)
        self.assertNotIn("\u00df", content)

    def test_an_earlier_version_can_be_restored(self):
        self.client.force_login(self.editor)
        self.save("Eins", "Link: {reset_url}")
        self.save("Zwei", "Link: {reset_url} zwei")
        first = self.text.versions.order_by("id").first()

        response = self.client.post(self.restore_url(first.pk))

        self.assertRedirects(response, self.versions_url)
        self.text.refresh_from_db()
        self.assertEqual((self.text.subject, self.text.body), ("Eins", "Link: {reset_url}"))
        self.assertEqual(self.text.versions.count(), 3)
        self.assertEqual(LogEntry.objects.filter(object_id=self.text.pk).count(), 3)

    def test_the_default_can_be_restored_from_the_versions(self):
        self.client.force_login(self.editor)
        self.save("Eins", "Link: {reset_url}")

        self.client.post(self.restore_url(0))

        self.text.refresh_from_db()
        self.assertEqual((self.text.subject, self.text.body), ("", ""))

    def test_a_version_that_is_no_longer_valid_is_not_restored(self):
        self.client.force_login(self.editor)
        old = EmailTextVersion.objects.create(
            email_text=self.text, subject="", body="Kein Link {unbekannt}"
        )
        self.save("Eins", "Link: {reset_url}")

        with translation.override("de-ch"):
            response = self.client.post(self.restore_url(old.pk), follow=True)

        self.assertIn("nicht mehr gültig", response.content.decode())
        self.text.refresh_from_db()
        self.assertEqual(self.text.subject, "Eins")

    def test_a_restore_is_logged_even_if_a_preview_field_is_posted(self):
        self.client.force_login(self.editor)
        self.save("Eins", "Link: {reset_url}")
        self.save("Zwei", "Link: {reset_url}")
        first = self.text.versions.order_by("id").first()
        LogEntry.objects.all().delete()

        self.client.post(self.restore_url(first.pk), {"_preview": "1", "_send_test": "1"})

        self.text.refresh_from_db()
        self.assertEqual(self.text.subject, "Eins")
        self.assertEqual(LogEntry.objects.filter(object_id=self.text.pk).count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_number_of_versions_is_limited_and_the_oldest_go(self):
        self.client.force_login(self.editor)

        with patch("leg_platform.accounts.admin.MAX_EMAIL_TEXT_VERSIONS", 3):
            for number in range(5):
                self.save(f"Betreff {number}", "Link: {reset_url}")

        self.assertEqual(
            list(self.text.versions.values_list("subject", flat=True)),
            ["Betreff 4", "Betreff 3", "Betreff 2"],
        )

    def test_restoring_needs_post_and_the_change_permission(self):
        self.client.force_login(self.editor)
        self.save("Eins", "Link: {reset_url}")
        version = self.text.versions.get()

        self.assertEqual(self.client.get(self.restore_url(version.pk)).status_code, 405)
        for user in (self.viewer, self.plain_staff):
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.restore_url(0)).status_code, 403)
        self.text.refresh_from_db()
        self.assertEqual(self.text.subject, "Eins")

    def test_the_versions_can_be_read_with_the_view_permission_only(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(self.versions_url).status_code, 200)
        self.assertNotContains(self.client.get(self.versions_url), "Restore this version")
        self.client.force_login(self.plain_staff)
        self.assertEqual(self.client.get(self.versions_url).status_code, 403)

    def test_an_unknown_email_or_version_is_not_found(self):
        self.client.force_login(self.administrator)

        self.assertEqual(
            self.client.get(reverse("admin:accounts_emailtext_versions", args=(999999,))).status_code,
            404,
        )
        self.assertEqual(self.client.post(self.restore_url(999999)).status_code, 404)

    def test_the_account_of_a_version_is_cleared_when_it_is_deleted(self):
        self.client.force_login(self.editor)
        self.save("Eins", "Link: {reset_url}")
        version = self.text.versions.get()

        self.editor.delete()

        version.refresh_from_db()
        self.assertIsNone(version.changed_by_id)