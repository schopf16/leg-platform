"""Independent verification tests of the final state (commit 0f8b5dd): the areas changed last.

Covered: open email address change requests end with a change of the hold on the account (own
account only), the stop link of a notice is dead after a confirmation, cancellation or replacement
and then ends no session, the first-steps box of the administration start page (no secrets, only
for permitted accounts), the status words of the list of email texts, the portal settings form
(system administrators only), the readable rights without the letter sharp s, and no groups that
the migrations create.

Expectations come from docs/PROJECT.md ("Changing the Email Address"), docs/SECURITY.md (section 3
and 11), D-016, D-056, D-059, D-060 and AGENTS.md (section 6: no predefined roles; section 2:
Swiss German without the letter sharp s). Groups and permissions are created in the tests.
"""

import re
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts import second_factor_reset
from leg_platform.accounts.models import (
    EmailText,
    GraphConfiguration,
    PendingEmailChange,
    PortalSettings,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.permission_names import permission_label
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    VerifiedClient,
    create_active_user,
    run_mail_queue,
)
from leg_platform.accounts.tests.test_password_reset import (
    NEW_PASSWORD,
    SET_URL,
    request_reset,
    reset_path,
)
from leg_platform.accounts.tests.test_profile import link_in, mails_to
from leg_platform.accounts.tests.test_second_factor_reset import (
    administrator,
    with_second_factor,
)

SHARP_S = "\u00df"
Purpose = VerificationToken.Purpose
REQUEST_PURPOSES = (Purpose.EMAIL_CHANGE, Purpose.SECONDARY_EMAIL, Purpose.EMAIL_CHANGE_STOP)
PROFILE_URL = reverse("accounts:profile")
LOGIN_URL = reverse("accounts:login")
REQUEST_URL = reverse("accounts:email_change_request")
STOP_CONFIRM_URL = reverse("accounts:email_change_stop_confirm")
INVALID_LINK_TEXT = "Link nicht mehr gültig"
ADMIN_INDEX = reverse("admin:index")
EMAIL_TEXT_LIST = reverse("admin:accounts_emailtext_changelist")
SETTINGS_URL = reverse("admin:accounts_portalsettings_change", args=(1,))


def request_change(client, user, *, email=None, secondary="", remote_addr):
    """Ask for new addresses through the profile form, then let the worker send the mails."""
    response = client.post(
        REQUEST_URL,
        {
            "email": email if email is not None else user.email,
            "secondary_email": secondary,
            "current_password": PASSWORD,
        },
        REMOTE_ADDR=remote_addr,
    )
    assert response.status_code == 302, "The request for a new address was not accepted."
    run_mail_queue()


def counts(user):
    """The open requests and the links that belong to them."""
    return (
        PendingEmailChange.objects.filter(user=user).count(),
        VerificationToken.objects.filter(user=user, purpose__in=REQUEST_PURPOSES).count(),
    )


def staff_client(email, *codenames, superuser=False):
    """A staff account with exactly these rights (no group) and a completed second factor."""
    staff = create_active_user(email)
    staff.set_privileges(is_staff=True, is_superuser=superuser)
    staff.user_permissions.set(Permission.objects.filter(codename__in=codenames))
    client = VerifiedClient()
    client.force_login(staff)
    return client, staff


@tag("mariadb")
class OpenChangesEndWithTheHoldOnTheAccountTests(TestCase):
    """A request for a new address made before a new password, a block or a reset of the second
    factor cannot be confirmed afterwards; the account of another person is not touched."""

    def setUp(self):
        self.holder = create_active_user("holder.person@example.org")
        self.bystander = create_active_user("bystander.person@example.org")
        for number, (user, new, second) in enumerate(
            (
                (self.holder, "holder.new@example.org", "holder.second@example.org"),
                (self.bystander, "bystander.new@example.org", "bystander.second@example.org"),
            ),
            start=1,
        ):
            client = Client()
            client.force_login(user)
            request_change(client, user, email=new, secondary=second, remote_addr=f"10.90.0.{number}")
        self.assertEqual(counts(self.holder), (2, 3))
        self.assertEqual(counts(self.bystander), (2, 3))
        mail.outbox.clear()

    def assert_only_the_holder_is_cleared(self):
        self.assertEqual(counts(self.holder), (0, 0))
        self.assertEqual(counts(self.bystander), (2, 3))

    def test_a_confirmed_password_reset_clears_the_requests(self):
        request_reset(self.holder.email)
        client = Client()
        client.get(reset_path(mail.outbox[0]))

        response = client.post(
            SET_URL, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
        )

        self.assertEqual(response.status_code, 302)
        self.assert_only_the_holder_is_cleared()

    def test_a_refused_password_reset_keeps_the_requests(self):
        request_reset(self.holder.email)
        client = Client()
        client.get(reset_path(mail.outbox[0]))

        client.post(SET_URL, {"new_password1": "short", "new_password2": "short"})

        self.assertEqual(counts(self.holder), (2, 3))

    def test_a_password_change_clears_the_requests(self):
        client = Client()
        client.force_login(self.holder)

        response = client.post(
            reverse("accounts:password_change"),
            {
                "current_password": PASSWORD,
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assert_only_the_holder_is_cleared()

    def test_a_password_change_with_a_wrong_current_password_keeps_the_requests(self):
        client = Client()
        client.force_login(self.holder)

        client.post(
            reverse("accounts:password_change"),
            {
                "current_password": "not the password",
                "new_password1": OTHER_PASSWORD,
                "new_password2": OTHER_PASSWORD,
            },
        )

        self.assertEqual(counts(self.holder), (2, 3))

    def test_every_status_but_active_clears_the_requests(self):
        for status in (User.Status.BLOCKED, User.Status.DEACTIVATED):
            with self.subTest(status=status):
                self.holder.set_status(User.Status.ACTIVE)
                PendingEmailChange.objects.filter(user=self.holder).delete()
                self.holder.set_status(status)
                # Nothing is left of the holder, nothing was taken from the bystander.
                self.assertEqual(counts(self.holder), (0, 0))
                self.assertEqual(counts(self.bystander), (2, 3))

    def test_the_block_clears_requests_that_were_open(self):
        self.holder.set_status(User.Status.BLOCKED)

        self.assert_only_the_holder_is_cleared()

    def test_the_reset_of_a_staff_second_factor_clears_the_requests(self):
        self.holder.set_privileges(is_staff=True, is_superuser=False)
        with_second_factor(self.holder)

        second_factor_reset.reset_privileged(administrator(), self.holder)
        # D-106: requesting never clears anything; explicit owner confirmation does.
        from leg_platform.accounts.tests.helpers import run_mail_queue

        run_mail_queue()
        token = VerificationToken.objects.get(
            user=self.holder, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
        )
        self.assertIsNotNone(second_factor_reset.confirm_link(token))

        self.assert_only_the_holder_is_cleared()

    def test_the_emergency_reset_of_the_second_factor_clears_the_requests(self):
        with_second_factor(self.holder)

        with mock.patch("builtins.input", return_value="yes"):
            call_command(
                "emergency_access", "reset-second-factor", str(self.holder.pk), stdout=StringIO()
            )

        self.assert_only_the_holder_is_cleared()


@tag("mariadb")
class StopLinkIsDeadAfterwardsTests(TestCase):
    """The link of a notice that stops a change has a meaning only while the change is open. Once
    the change was confirmed, cancelled or replaced it does nothing, in particular it ends no
    session and keeps a newer request."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("holder.person@example.org")

    def setUp(self):
        self.owner = Client()
        self.owner.force_login(self.user)

    def stop_path(self, address):
        return link_in(mails_to(address)[-1], "email-change/stop")

    def use_the_link(self, path):
        """Open the link; a link that is valid would lead to the confirmation page, and its button
        is pressed as well. Every page that is returned is followed to its end."""
        visitor = Client()
        opened = visitor.get(path, follow=True)
        pages = [opened]
        if opened.redirect_chain and opened.redirect_chain[-1][0] == STOP_CONFIRM_URL:
            pages.append(visitor.post(STOP_CONFIRM_URL, {}, follow=True))
        return pages

    def assert_invalid_and_harmless(self, path, *, session_client):
        for response in self.use_the_link(path):
            self.assertContains(response, INVALID_LINK_TEXT)
        direct = Client().post(STOP_CONFIRM_URL, {}, follow=True)
        self.assertContains(direct, INVALID_LINK_TEXT)
        self.assertEqual(session_client.get(PROFILE_URL).status_code, 200)
        self.assertFalse(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.EMAIL_CHANGE_STOPPED).exists()
        )

    def test_after_the_person_cancelled_the_request(self):
        request_change(self.owner, self.user, email="new.login@example.org", remote_addr="10.91.0.1")
        path = self.stop_path(self.user.email)
        self.owner.post(reverse("accounts:email_change_cancel", args=["login"]))
        # No stop link of a closed change stays in the database (second layer of the protection).
        self.assertFalse(
            VerificationToken.objects.filter(user=self.user, purpose=Purpose.EMAIL_CHANGE_STOP).exists()
        )

        self.assert_invalid_and_harmless(path, session_client=self.owner)

    def test_after_the_confirmation_of_a_second_address(self):
        request_change(
            self.owner, self.user, secondary="second.address@example.org", remote_addr="10.91.0.2"
        )
        path = self.stop_path(self.user.email)
        confirmer = Client()
        confirmer.get(link_in(mails_to("second.address@example.org")[-1], "email-change/verify"))
        confirmer.post(reverse("accounts:email_change_confirm"), {})
        self.user.refresh_from_db()
        self.assertEqual(self.user.secondary_email, "second.address@example.org")

        # A second address does not end the sessions, so the old session still works.
        self.assert_invalid_and_harmless(path, session_client=self.owner)

    def test_after_the_confirmation_of_the_login_address(self):
        request_change(self.owner, self.user, email="new.login@example.org", remote_addr="10.91.0.3")
        path = self.stop_path(self.user.email)
        confirmer = Client()
        confirmer.get(link_in(mails_to("new.login@example.org")[-1], "email-change/verify"))
        confirmer.post(reverse("accounts:email_change_confirm"), {})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new.login@example.org")
        # The confirmation ended the old sessions (D-016); a new login is the session under test.
        fresh = Client()
        fresh.force_login(self.user)

        self.assert_invalid_and_harmless(path, session_client=fresh)

    def test_after_a_newer_request_replaced_the_old_one(self):
        request_change(self.owner, self.user, email="first.login@example.org", remote_addr="10.91.0.4")
        old_path = self.stop_path(self.user.email)
        request_change(self.owner, self.user, email="second.login@example.org", remote_addr="10.91.0.5")
        new_path = self.stop_path(self.user.email)
        self.assertNotEqual(old_path, new_path)

        for response in self.use_the_link(old_path):
            self.assertContains(response, INVALID_LINK_TEXT)

        # The old link neither stopped the newer request nor ended the session.
        self.assertEqual(
            list(PendingEmailChange.objects.filter(user=self.user).values_list("address", flat=True)),
            ["second.login@example.org"],
        )
        self.assertEqual(self.owner.get(PROFILE_URL).status_code, 200)
        self.assertFalse(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.EMAIL_CHANGE_STOPPED).exists()
        )

    def test_the_current_link_still_stops_the_current_request_and_ends_the_session(self):
        request_change(self.owner, self.user, email="first.login@example.org", remote_addr="10.91.0.6")
        request_change(self.owner, self.user, email="second.login@example.org", remote_addr="10.91.0.7")
        new_path = self.stop_path(self.user.email)
        visitor = Client()
        visitor.get(new_path)

        done = visitor.post(STOP_CONFIRM_URL, {})

        self.assertContains(done, "Änderung gestoppt")
        self.assertEqual(counts(self.user), (0, 0))
        self.assertRedirects(
            self.owner.get(PROFILE_URL), f"{LOGIN_URL}?next={PROFILE_URL}", fetch_redirect_response=False
        )

    def test_a_link_of_another_account_does_not_reach_this_account(self):
        other = create_active_user("other.person@example.org")
        other_client = Client()
        other_client.force_login(other)
        request_change(other_client, other, email="other.new@example.org", remote_addr="10.91.0.8")
        other_path = self.stop_path(other.email)
        request_change(self.owner, self.user, email="new.login@example.org", remote_addr="10.91.0.9")

        visitor = Client()
        visitor.get(other_path)
        visitor.post(STOP_CONFIRM_URL, {})

        self.assertEqual(counts(other), (0, 0))
        self.assertEqual(PendingEmailChange.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self.owner.get(PROFILE_URL).status_code, 200)


@tag("mariadb")
class FirstStepsBoxTests(TestCase):
    """The box "Erste Schritte" on the start page of the administration (D-060)."""

    def test_a_system_administrator_sees_the_box_and_no_secret(self):
        client, _staff = staff_client("admin.person@example.org", superuser=True)
        row = GraphConfiguration.load()
        row.tenant_id = "11111111-aaaa-bbbb-cccc-111111111111"
        row.client_id = "22222222-aaaa-bbbb-cccc-222222222222"
        row.sender_address = "sender.box@example.org"
        row.certificate_pem = "-----BEGIN CERTIFICATE-----\nPEM-SENTINEL-X\n-----END CERTIFICATE-----"
        row.encrypted_private_key = "ENCRYPTED-SENTINEL-X"
        row.save()

        page = client.get(ADMIN_INDEX).content.decode()

        self.assertIn("Erste Schritte", page)
        for secret in (
            "PEM-SENTINEL-X",
            "ENCRYPTED-SENTINEL-X",
            "sender.box@example.org",
            "11111111-aaaa",
            "22222222-aaaa",
            settings.SECRET_KEY,
        ):
            self.assertNotIn(secret, page)
        self.assertNotIn(SHARP_S, page)

    def test_the_box_is_missing_without_any_right(self):
        client, _staff = staff_client("nobody.person@example.org")

        response = client.get(ADMIN_INDEX)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Erste Schritte", response.content.decode())

    def test_the_box_lists_only_the_items_of_the_rights_of_the_account(self):
        client, _staff = staff_client("texts.person@example.org", "view_emailtext")

        page = client.get(ADMIN_INDEX).content.decode()

        self.assertIn("Erste Schritte", page)
        self.assertIn("Texte der E-Mails", page)
        for foreign in ("Versand von E-Mails", "Microsoft Graph", "Datenschutzerklärung", "Impressum"):
            self.assertNotIn(foreign, page)

    def test_a_right_for_the_delivery_settings_gives_no_item_to_a_staff_member(self):
        # The delivery settings are for system administrators only (the model admin refuses a
        # staff member whatever right is held), so neither the box nor the item appears.
        client, _staff = staff_client("graph.person@example.org", "view_graphconfiguration")

        page = client.get(ADMIN_INDEX).content.decode()

        self.assertNotIn("Erste Schritte", page)
        self.assertNotIn("Microsoft Graph", page)

    def test_a_right_for_the_portal_settings_gives_the_items_to_a_staff_member(self):
        # D-073: the rights to see and to change the portal settings are ordinary rights, so
        # the box shows the items and the link for an account that holds one of them.
        client, _staff = staff_client(
            "settings.person@example.org", "view_portalsettings", "change_portalsettings"
        )

        page = client.get(ADMIN_INDEX).content.decode()

        self.assertIn("Name, Logo und Farben", page)
        self.assertIn("Datenschutzerklärung", page)
        self.assertIn(SETTINGS_URL, page)

    def test_the_box_is_not_shown_to_those_who_may_not_open_the_administration(self):
        anonymous = Client().get(ADMIN_INDEX)
        ordinary = Client()
        ordinary.force_login(create_active_user("ordinary.person@example.org"))
        without_code = Client()
        staff = create_active_user("staff.person@example.org")
        staff.set_privileges(is_staff=True, is_superuser=True)
        without_code.force_login(staff)  # no completed second factor

        for client in (Client(), ordinary, without_code):
            with self.subTest(client=client):
                response = client.get(ADMIN_INDEX)
                self.assertNotEqual(response.status_code, 200)
                self.assertNotIn("Erste Schritte", response.content.decode())
        self.assertEqual(anonymous.status_code, 302)


def status_cells(page):
    return re.findall(r'<td class="field-text_status">([^<]*)</td>', page)


@tag("mariadb")
class EmailTextListTests(TestCase):
    """The list of the email texts shows the state as a word (D-060)."""

    def setUp(self):
        self.client, _staff = staff_client("admin.person@example.org", superuser=True)

    def test_a_new_portal_shows_only_default_texts(self):
        cells = status_cells(self.client.get(EMAIL_TEXT_LIST).content.decode())

        self.assertEqual(len(cells), len(EmailText.Key.values))
        self.assertEqual(set(cells), {"Standardtext"})

    def test_a_subject_or_a_body_makes_the_text_customised_and_the_reset_returns_it(self):
        EmailText.objects.ensure_all()
        EmailText.objects.filter(key=EmailText.Key.PASSWORD_RESET).update(subject="Neuer Betreff")
        EmailText.objects.filter(key=EmailText.Key.REGISTRATION_VERIFICATION).update(body="Neuer Text")

        cells = status_cells(self.client.get(EMAIL_TEXT_LIST).content.decode())

        self.assertEqual(cells.count("Angepasst"), 2)
        self.assertEqual(cells.count("Standardtext"), len(EmailText.Key.values) - 2)
        row = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)
        self.client.post(
            reverse("admin:accounts_emailtext_change", args=(row.pk,)),
            {"subject": "", "body": "", "reset_to_default": "on", "_save": "Speichern"},
        )
        cells = status_cells(self.client.get(EMAIL_TEXT_LIST).content.decode())
        self.assertEqual(cells.count("Angepasst"), 1)

    def test_the_list_is_only_for_accounts_with_the_right_to_see_it(self):
        without, _staff = staff_client("without.person@example.org", "view_user")
        reader, _other = staff_client("reader.person@example.org", "view_emailtext")

        self.assertEqual(without.get(EMAIL_TEXT_LIST).status_code, 403)
        page = reader.get(EMAIL_TEXT_LIST)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(status_cells(page.content.decode())), len(EmailText.Key.values))
        self.assertNotIn(SHARP_S, page.content.decode())

    def test_a_reader_cannot_change_a_text(self):
        EmailText.objects.ensure_all()
        row = EmailText.objects.get(key=EmailText.Key.PASSWORD_RESET)
        reader, _other = staff_client("reader.person@example.org", "view_emailtext")

        response = reader.post(
            reverse("admin:accounts_emailtext_change", args=(row.pk,)),
            {"subject": "Hijacked", "body": "", "_save": "Speichern"},
        )

        self.assertIn(response.status_code, (302, 403))
        row.refresh_from_db()
        self.assertEqual(row.subject, "")


@tag("mariadb")
class PortalSettingsFormTests(TestCase):
    """The portal settings are ordinary rights (D-073): view and change, assigned at runtime. The
    positive path of a system administrator and the refusal of add and delete are in
    ``test_portal_settings.PortalSettingsAdminTests``; the rest of the negative cases in
    ``test_portal_branding``."""

    def test_a_staff_member_without_a_right_is_refused_and_nothing_is_changed(self):
        client, _staff = staff_client("settings.person@example.org", "view_user")

        get = client.get(SETTINGS_URL)
        post = client.post(
            SETTINGS_URL,
            {"privacy_url": "https://www.example.org/hijacked", "imprint_url": "", "_save": "x"},
        )
        listing = client.get(reverse("admin:accounts_portalsettings_changelist"))

        for response in (get, post, listing):
            self.assertEqual(response.status_code, 403)
        self.assertEqual(PortalSettings.load().privacy_url, "")

    def test_a_right_to_add_or_delete_does_not_allow_adding_or_deleting_the_row(self):
        client, _staff = staff_client(
            "settings.person@example.org",
            "view_portalsettings",
            "change_portalsettings",
            "add_portalsettings",
            "delete_portalsettings",
        )

        self.assertEqual(client.get(SETTINGS_URL).status_code, 200)
        self.assertEqual(client.get(reverse("admin:accounts_portalsettings_add")).status_code, 403)
        delete = client.post(reverse("admin:accounts_portalsettings_delete", args=(1,)), {"post": "yes"})
        self.assertEqual(delete.status_code, 403)
        self.assertTrue(PortalSettings.objects.filter(pk=1).exists())

    def test_a_group_with_the_rights_opens_the_form(self):
        group = Group.objects.create(name="settings group")
        group.permissions.set(
            Permission.objects.filter(
                content_type__app_label="accounts",
                content_type__model="portalsettings",
                codename__in=("view_portalsettings", "change_portalsettings"),
            )
        )
        client, staff = staff_client("member.person@example.org")
        staff.groups.add(group)

        self.assertEqual(client.get(SETTINGS_URL).status_code, 200)

    def test_an_ordinary_account_and_a_visitor_do_not_reach_the_form(self):
        ordinary = Client()
        ordinary.force_login(create_active_user("ordinary.person@example.org"))

        for client in (Client(), ordinary):
            response = client.get(SETTINGS_URL)
            self.assertIn(response.status_code, (302, 403))
            self.assertNotEqual(response.status_code, 200)


@tag("mariadb")
class ReadableRightsAndNoGroupsTests(TestCase):
    """The rights have German names without the letter sharp s; the migrations create no group."""

    def test_no_group_exists_after_the_migrations(self):
        self.assertEqual(Group.objects.count(), 0)

    def test_the_creation_of_accounts_creates_no_group(self):
        create_active_user("first.person@example.org")
        staff_client("staff.person@example.org", superuser=True)

        self.assertEqual(Group.objects.count(), 0)

    def test_no_label_of_a_right_has_the_letter_sharp_s(self):
        for permission in Permission.objects.select_related("content_type"):
            with self.subTest(permission=permission.codename):
                self.assertNotIn(SHARP_S, permission_label(permission))

    def test_the_labels_of_the_rights_of_the_portal_are_german(self):
        labels = {
            permission.codename: permission_label(permission)
            for permission in Permission.objects.select_related("content_type").filter(
                content_type__app_label="accounts"
            )
        }

        self.assertEqual(labels["change_emailtext"], "Mailtext: ändern")
        self.assertEqual(labels["view_user"], "Konto: ansehen")
        self.assertEqual(labels["view_emailtext"], "Mailtext: ansehen")
        self.assertEqual(labels["delete_user"], "Konto: löschen")
        self.assertEqual(labels["add_user"], "Konto: hinzufügen")

    def test_the_group_form_lists_no_english_standard_name_of_a_right_of_the_portal(self):
        client, _staff = staff_client("admin.person@example.org", superuser=True)

        page = client.get(reverse("admin:auth_group_add")).content.decode()

        self.assertNotRegex(page, r"accounts \| [^<]*\| Can ")
        self.assertNotIn("Can change email text", page)
        self.assertNotIn(SHARP_S, page)

    # The group form offers only the four documented rights since block 6, all in German; the
    # expectedFailure decorator was removed by the developer (the test passes).
    def test_the_group_form_shows_no_english_words_for_any_right(self):
        client, _staff = staff_client("admin.person@example.org", superuser=True)

        page = client.get(reverse("admin:auth_group_add")).content.decode()

        for english in (
            "Pending email change",
            "Verification token",
            "Mail job",
            "Rate limit",
            "Login ip",
            "Access attempt expiration",
            "Can request an address directory import",
        ):
            with self.subTest(english=english):
                self.assertNotIn(english, page)


@tag("mariadb")
class NoSharpSTests(TestCase):
    """The letter sharp s is used neither in the catalogue nor in the rendered pages (AGENTS.md,
    section 2)."""

    def test_the_catalogue_has_none(self):
        catalogue = Path(settings.BASE_DIR) / "locale" / "de_CH" / "LC_MESSAGES" / "django.po"

        self.assertNotIn(SHARP_S, catalogue.read_text(encoding="utf-8"))

    def test_the_rendered_pages_have_none(self):
        user = create_active_user("page.person@example.org")
        client = Client()
        client.force_login(user)
        admin_client, _staff = staff_client("admin.person@example.org", superuser=True)
        pages = {
            "login": Client().get(LOGIN_URL),
            "registration": Client().get(reverse("accounts:registration")),
            "password reset": Client().get(reverse("accounts:password_reset")),
            "profile": client.get(PROFILE_URL),
            "admin dashboard": admin_client.get(ADMIN_INDEX),
        }

        for name, response in pages.items():
            with self.subTest(page=name):
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(SHARP_S, response.content.decode())
