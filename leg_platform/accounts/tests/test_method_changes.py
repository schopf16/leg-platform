"""D-106 lifecycle: real credential fixtures, atomic returns, restricted recovery and narrow access."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import check_password, make_password
from django.conf import settings
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from leg_platform.accounts.tests.helpers import portal_addresses
from leg_platform.accounts import method_changes, passkey_methods, passkeys, second_factor, second_factor_reset
from leg_platform.accounts.mail_queue import enqueue_for_account
from leg_platform.accounts.models import (
    AuthenticatorDevice, MailJob, PendingSignInChange, User, VerificationToken,
)
from leg_platform.accounts.tests import test_passkeys
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD, PASSWORD, VerifiedClient, code_for, create_active_user, reset_request_data, run_mail_queue,
)


@portal_addresses(base_url="https://portal.example.org")
class MethodTransitionTests(TestCase):
    setUp = test_passkeys.PasskeyCeremonyTests.setUp
    client_data = test_passkeys.PasskeyCeremonyTests.client_data
    registration = test_passkeys.PasskeyCeremonyTests.registration
    stage = test_passkeys.PasskeyCeremonyTests.stage
    activate = test_passkeys.PasskeyCeremonyTests.activate
    assertion = test_passkeys.PasskeyCeremonyTests.assertion

    def pending(self, method="password"):
        passkey_methods.allow_setup(self.request)
        return method_changes.start(self.request, make_password(OTHER_PASSWORD), method)

    def test_passkey_setup_and_add_labels_follow_each_request_language(self):
        from leg_platform.accounts.passkey_views import CredentialNameForm, SetupView

        # The same imported field definition must follow subsequent request languages.
        for adding in (False, True):
            if adding:
                self.activate()
            passkey_methods.allow_setup(self.request)
            for language, label in (
                ("de-ch", "Name"),
                ("fr", "Nom"),
                ("it", "Nome"),
                ("en", "Name"),
            ):
                with self.subTest(adding=adding, language=language):
                    with translation.override(language):
                        form = CredentialNameForm()
                        self.assertEqual(str(form.fields["name"].label), label)
                        response = SetupView().page(self.request, form=form)
                        self.assertContains(response, f'>{label}</label>', html=False)

    def test_passkey_error_constant_follows_each_request_language(self):
        from leg_platform.accounts.passkey_views import OptionsView
        from django.test import RequestFactory

        for language, error in (
            ("de-ch", "Der Passkey konnte nicht bestätigt werden. Versuchen Sie es erneut."),
            ("fr", "La clé d’accès n’a pas pu être vérifiée. Veuillez réessayer."),
            ("it", "Non è stato possibile verificare la passkey. Riprovi."),
            ("en", "The passkey could not be verified. Please try again."),
        ):
            with self.subTest(language=language), translation.override(language):
                request = RequestFactory().post("/", {}, content_type="application/json")
                with patch("leg_platform.accounts.passkey_views.limited", return_value=True):
                    response = OptionsView().post(request)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(json.loads(response.content)["error"], error)

    def token(self):
        raw = VerificationToken.objects.issue(
            self.user, VerificationToken.Purpose.METHOD_RETURN, timedelta(hours=24)
        )
        return VerificationToken.objects.find_valid(raw, VerificationToken.Purpose.METHOD_RETURN)

    def login_client(self, user=None):
        user = user or self.user
        self.client.force_login(user)
        session = self.client.session
        if user.authentication_method == User.AuthenticationMethod.PASSKEY:
            session[second_factor.PASSKEY_SESSION_KEY] = user.pk
        session.save()

    def test_password_return_keeps_active_passkeys_until_single_use_confirmation(self):
        self.activate()
        old_version = self.user.session_version
        pending = self.pending()
        self.user.refresh_from_db()
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertNotEqual(pending.password_hash, OTHER_PASSWORD)
        self.assertTrue(check_password(OTHER_PASSWORD, pending.password_hash))
        token = self.token()
        result = method_changes.complete(self.user.pk, token_id=token.pk)
        self.assertIsNotNone(result)
        user, device, codes = result
        self.assertTrue(user.check_password(OTHER_PASSWORD))
        self.assertEqual(user.authentication_method, "password")
        self.assertFalse(user.passkeys.exists())
        self.assertGreater(user.session_version, old_version)
        self.assertIsNone(device)
        self.assertEqual(codes, [])
        self.assertIsNone(method_changes.complete(user.pk, token_id=token.pk))

    def test_privileged_return_requires_app_completion_before_any_switch(self):
        self.user.set_privileges(is_staff=True, is_superuser=True)
        passkey_methods.allow_setup(self.request)
        self.stage()
        import secrets
        self.credential_id = secrets.token_bytes(32)
        self.stage()
        self.user = passkey_methods.activate(self.request)
        self.request.user = self.user
        self.assertIsNone(self.pending("password"))
        pending = self.pending("app")
        self.assertFalse(pending.ready)
        self.assertIsNone(method_changes.complete(self.user.pk, token_id=self.token().pk))
        device = AuthenticatorDevice.objects.get(user=self.user)
        self.assertFalse(device.confirmed)
        self.assertTrue(method_changes.confirm_app(self.request, code_for(device)))
        self.assertFalse(AuthenticatorDevice.objects.get(pk=device.pk).confirmed)
        self.user.refresh_from_db()
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        result = method_changes.complete(self.user.pk, token_id=self.token().pk)
        self.assertIsNotNone(result)
        user, device, codes = result
        self.assertTrue(device.confirmed)
        self.assertEqual(len(codes), 10)
        self.assertTrue(user.check_password(OTHER_PASSWORD))
        self.assertFalse(user.passkeys.exists())

    def test_return_link_get_is_scanner_safe_and_post_needs_csrf(self):
        self.activate()
        self.pending()
        raw = VerificationToken.objects.issue(
            self.user, VerificationToken.Purpose.METHOD_RETURN, timedelta(hours=24)
        )
        strict = Client(enforce_csrf_checks=True)
        response = strict.get(reverse("accounts:method_return_verify", args=[raw]), secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(strict.get(response["Location"], secure=True).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertEqual(strict.post(response["Location"], secure=True).status_code, 403)
        self.assertIsNotNone(VerificationToken.objects.find_valid(raw, VerificationToken.Purpose.METHOD_RETURN))
        self.client.get(reverse("accounts:method_return_verify", args=[raw]), secure=True)
        self.assertEqual(self.client.post(reverse("accounts:method_return_confirm"), secure=True).status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "password")
        self.assertEqual(self.client.post(reverse("accounts:method_return_confirm"), secure=True).status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_return_expiry_and_session_version_change_refuse_activation(self):
        self.activate()
        pending = self.pending()
        token = self.token()
        PendingSignInChange.objects.filter(pk=pending.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(method_changes.complete(self.user.pk, token_id=token.pk))
        self.pending()
        token = self.token()
        self.user.end_all_sessions()
        self.assertIsNone(method_changes.complete(self.user.pk, token_id=token.pk))
        self.assertTrue(self.user.passkeys.filter(active=True).exists())

    def test_scheduled_cleanup_discards_expired_pending_hash_and_return_tokens(self):
        from django.core.management import call_command
        from io import StringIO

        self.activate()
        pending = self.pending()
        token = self.token()
        past = timezone.now() - timedelta(seconds=1)
        PendingSignInChange.objects.filter(pk=pending.pk).update(expires_at=past)
        VerificationToken.objects.filter(pk=token.pk).update(expires_at=past)
        call_command("cleanup_mail_requests", stdout=StringIO())
        self.assertFalse(PendingSignInChange.objects.exists())
        self.assertFalse(VerificationToken.objects.filter(pk=token.pk).exists())
        self.assertTrue(self.user.passkeys.filter(active=True).exists())

    def test_operator_emergency_recovery_never_creates_a_password_bypass_for_passkeys(self):
        from django.core.management import call_command
        from io import StringIO

        self.activate()
        with patch("builtins.input", return_value="yes"):
            call_command(
                "emergency_access", "reset-second-factor", str(self.user.pk), stdout=StringIO()
            )
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertFalse(self.user.has_usable_password())
        self.assertTrue(self.user.passkeys.filter(active=True).exists())
        run_mail_queue()
        token = VerificationToken.objects.get(
            user=self.user, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
        )
        self.assertEqual(second_factor_reset.confirm_link(token).authentication_method, "recovery")

    def test_removing_last_key_refused_and_success_ends_every_session(self):
        self.activate()
        first = self.user.passkeys.get()
        self.assertFalse(passkey_methods.remove(self.user, first.pk))
        import secrets
        self.credential_id = secrets.token_bytes(32)
        passkey_methods.allow_setup(self.request)
        self.stage()
        self.user = passkey_methods.activate(self.request)
        old_version = self.user.session_version
        self.assertTrue(passkey_methods.remove(self.user, first.pk))
        self.user.refresh_from_db()
        self.assertGreater(self.user.session_version, old_version)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)

    def test_administrator_can_remove_a_backup_but_always_retains_one_key(self):
        import secrets
        self.user.set_privileges(is_staff=True, is_superuser=False)
        passkey_methods.allow_setup(self.request)
        self.stage()
        self.credential_id = secrets.token_bytes(32)
        self.stage()
        self.user = passkey_methods.activate(self.request)
        self.request.user = self.user
        first = self.user.passkeys.first()
        self.assertTrue(passkey_methods.remove(self.user, first.pk))
        remaining = self.user.passkeys.get()
        self.assertFalse(passkey_methods.remove(self.user, remaining.pk))
        self.request.user.refresh_from_db()
        self.credential_id = secrets.token_bytes(32)
        passkey_methods.allow_setup(self.request)
        self.stage()
        self.user = passkey_methods.activate(self.request)
        self.assertTrue(passkey_methods.remove(self.user, remaining.pk))
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)

    def test_password_return_outbox_sends_canonical_links_to_every_address(self):
        self.activate()
        self.user.set_secondary_email("second@example.org")
        self.request.user = self.user
        self.pending()
        enqueue_for_account(MailJob.Kind.METHOD_RETURN, self.user)
        run_mail_queue()
        messages = [item for item in mail.outbox if "/sign-in-method/verify/" in item.body]
        self.assertEqual({item.to[0] for item in messages}, {self.user.email, self.user.secondary_email})
        for message in messages:
            self.assertIn("https://portal.example.org/sign-in-method/verify/", message.body)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")

    def test_setup_starts_with_the_name_and_asks_for_no_device_statement(self):
        self.login_client()
        session = self.client.session
        import time
        session[passkey_methods.SETUP_KEY] = {
            "user": self.user.pk, "hash": self.user.get_session_auth_hash(), "started": time.time(),
        }
        session.save()
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        response = self.client.get(reverse("accounts:passkey_setup"))
        self.assertContains(response, "Step 1 of 3: Name")
        self.assertContains(response, "What should the passkey be called?")
        self.assertNotContains(response, 'name="two_devices"')

    def test_existing_key_setup_and_profile_password_card_describe_real_state(self):
        self.activate()
        self.login_client()
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        response = self.client.get(reverse("accounts:passkey_setup"))
        self.assertContains(response, "Set up another passkey")
        self.assertContains(response, "Confirm with your passkey that it is you.")
        session = self.client.session
        import time
        session[passkey_methods.SETUP_KEY] = {
            "user": self.user.pk, "hash": self.user.get_session_auth_hash(), "started": time.time(),
        }
        session.save()
        response = self.client.get(reverse("accounts:passkey_setup"))
        self.assertContains(response, "What should the passkey be called?")
        self.assertNotContains(response, 'name="two_devices"')
        response = self.client.get(reverse("accounts:profile") + "?edit=password")
        self.assertContains(response, "You use passkeys and have no password.")
        self.assertNotContains(response, 'name="new_password1"')
        from django.template.loader import render_to_string
        card = render_to_string("accounts/profile/_password.html", {
            "user": self.user, "edit": "password",
        })
        self.assertNotIn("••••", card)
        self.assertNotIn(reverse("accounts:login_method"), card)

    def test_additional_passkey_guidance_is_localized_and_direct_for_every_access_level(self):
        import secrets

        for role in ("user", "staff", "system_admin"):
            self.user = create_active_user(f"guide.{role}@example.org")
            if role != "user":
                self.user.set_privileges(is_staff=True, is_superuser=role == "system_admin")
            self.request.user = self.user
            self.credential_id = secrets.token_bytes(32)
            self.activate()
            self.login_client()
            for language, manage, further in (
                ("de-ch", "Passkeys verwalten", "Weiteren Passkey einrichten"),
                ("fr", "Gérer les clés d’accès", "Configurer une autre clé d’accès"),
                ("it", "Gestire le passkey", "Configurare un’altra passkey"),
                ("en", "Manage passkeys", "Set up another passkey"),
            ):
                with self.subTest(role=role, language=language):
                    self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
                    profile = self.client.get(reverse("accounts:profile"))
                    self.assertContains(profile, manage)
                    self.assertContains(profile, f'href="{reverse("accounts:passkeys")}"')
                    self.assertNotContains(profile, "More help")
                    setup = self.client.get(reverse("accounts:passkey_setup"))
                    self.assertContains(setup, 'name="passkey_assertion"')
                    self.assertNotContains(setup, 'name="two_devices"')

    def test_method_password_uses_existing_rules_and_accessible_association(self):
        self.activate()
        self.login_client()
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        self.client.post(reverse("accounts:login_method"), {"method": "password"})
        response = self.client.get(reverse("accounts:method_password"))
        self.assertContains(response, 'id="id_new_password1_rules"')
        self.assertContains(response, "At least 15 characters.")
        self.assertIn("id_new_password1_rules", response.context["form"]["new_password1"].build_widget_attrs({})["aria-describedby"])

    def promotion_client(self):
        actor = create_active_user("promotion.administrator@example.org")
        actor.set_privileges(is_staff=True, is_superuser=True)
        client = VerifiedClient()
        client.force_login(actor)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        return client

    def stage_second_active_key(self):
        import secrets

        self.credential_id = secrets.token_bytes(32)
        self.request.user = self.user
        passkey_methods.allow_setup(self.request)
        self.stage()
        self.user = passkey_methods.activate(self.request)
        self.request.user = self.user

    def test_real_admin_promotion_accepts_one_key_without_a_device_statement(self):
        from leg_platform.accounts.tests.test_admin import PROFILE_DATA

        self.activate()
        client = self.promotion_client()
        url = reverse("admin:accounts_user_change", args=[self.user.pk])
        response = client.get(url)
        self.assertNotContains(response, 'name="passkey_devices_confirmed"')
        data = {**PROFILE_DATA, "is_staff": "on", "_save": "Save"}
        response = client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)

    def test_obsolete_device_statement_without_promotion_grants_no_rights(self):
        from leg_platform.accounts.tests.test_admin import PROFILE_DATA

        self.activate()
        self.stage_second_active_key()
        client = self.promotion_client()
        response = client.post(
            reverse("admin:accounts_user_change", args=[self.user.pk]),
            {**PROFILE_DATA, "passkey_devices_confirmed": "on", "_save": "Save"},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)

    def test_promotion_statement_is_absent_for_self_and_ordinary_staff(self):
        from django.test import RequestFactory
        from leg_platform.accounts.admin.users import UserAdmin
        from django.contrib import admin

        self.activate()
        self.stage_second_active_key()
        model_admin = UserAdmin(User, admin.site)
        for actor in (self.user, create_active_user("promotion.staff@example.org")):
            if actor.pk != self.user.pk:
                actor.set_privileges(is_staff=True, is_superuser=False)
            request = RequestFactory().get("/")
            request.user = actor
            form = model_admin.get_form(request, self.user)(instance=self.user)
            self.assertNotIn("passkey_devices_confirmed", form.fields)

    def test_http_return_requires_a_real_current_action_bound_passkey(self):
        self.activate()
        self.login_client()
        self.client.post(reverse("accounts:login_method"), {"method": "password"})
        path = reverse("accounts:method_password")
        data = {"new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD}
        response = self.client.post(path, data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PendingSignInChange.objects.exists())
        options = self.client.post(
            reverse("accounts:passkey_options"), json.dumps({"purpose": "proof", "action": path}),
            content_type="application/json",
        ).json()
        data["passkey_assertion"] = json.dumps({
            "ceremony": options["ceremony"], "credential": self.assertion(options),
        })
        self.assertEqual(self.client.post(path, data).status_code, 200)
        self.assertTrue(PendingSignInChange.objects.get().ready)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")


@portal_addresses(base_url="https://portal.example.org")
class UnifiedRecoveryTests(TestCase):
    def setUp(self):
        self.staff = create_active_user("staff@example.org")
        self.staff.set_privileges(is_staff=True, is_superuser=True)
        AuthenticatorDevice.objects.create_device(self.staff, confirmed=True)
        self.user = create_active_user("member@example.org")
        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)

    def issue(self, target=None):
        target = target or self.user
        second_factor_reset.request_link(self.staff, target)
        run_mail_queue()
        return VerificationToken.objects.get(user=target, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET)

    def test_staff_may_request_own_last_administrator_reset_without_instant_mutation(self):
        token = self.issue(self.staff)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password(PASSWORD))
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.staff, confirmed=True).exists())
        confirmed = second_factor_reset.confirm_link(token)
        self.assertEqual(confirmed.authentication_method, "recovery")
        self.assertFalse(confirmed.has_usable_password())
        self.assertTrue(confirmed.is_superuser)
        self.assertIsNone(second_factor_reset.confirm_link(token))

    def test_active_password_only_and_first_administrator_accounts_are_recoverable(self):
        AuthenticatorDevice.objects.filter(user__in=[self.user, self.staff]).delete()
        for target in (self.user, self.staff):
            with self.subTest(privileged=target.is_superuser):
                token = self.issue(target)
                target.refresh_from_db()
                self.assertTrue(target.check_password(PASSWORD))
                self.assertEqual(second_factor_reset.find_link_by_digest(token.digest).pk, token.pk)
                recovered = second_factor_reset.confirm_link(token)
                self.assertEqual(recovered.authentication_method, "recovery")
                self.assertFalse(recovered.has_usable_password())

    def test_recovery_setup_uses_removed_method_copy(self):
        token = self.issue()
        from leg_platform.accounts.views import TWO_FACTOR_RESET_SESSION_KEY
        session = self.client.session
        session[TWO_FACTOR_RESET_SESSION_KEY] = token.digest
        session.save()
        self.client.post(reverse("accounts:two_factor_reset_confirm"))
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        response = self.client.get(reverse("accounts:passkey_setup"))
        self.assertContains(response, "What should the passkey be called?")
        self.assertNotContains(response, "stays valid until")
        self.assertNotContains(response, 'name="two_devices"')

    def test_ordinary_user_cannot_initiate_reset_for_anyone(self):
        for target in (self.staff, self.user):
            with self.assertRaises(second_factor_reset.ResetRefused):
                second_factor_reset.request_link(self.user, target)
        self.assertFalse(MailJob.objects.exists())

    def test_recovery_session_is_restricted_then_can_choose_password(self):
        token = self.issue()
        client = Client()
        session = client.session
        from leg_platform.accounts.views import TWO_FACTOR_RESET_SESSION_KEY
        session[TWO_FACTOR_RESET_SESSION_KEY] = token.digest
        session.save()
        response = client.post(reverse("accounts:two_factor_reset_confirm"))
        self.assertRedirects(response, reverse("accounts:login_method"), fetch_redirect_response=False)
        self.assertRedirects(client.get(reverse("accounts:profile")), reverse("accounts:login_method"), fetch_redirect_response=False)
        self.assertRedirects(client.get(reverse("home")), reverse("accounts:login_method"), fetch_redirect_response=False)
        choice = client.get(reverse("accounts:login_method"))
        self.assertContains(choice, 'name="method"', count=3)
        self.assertNotContains(choice, "card-personal")
        client.post(reverse("accounts:login_method"), {"method": "password"})
        response = client.post(reverse("accounts:method_password"), {
            "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD,
        })
        self.assertRedirects(response, reverse("accounts:profile"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OTHER_PASSWORD))
        self.assertEqual(self.user.authentication_method, "password")
        self.assertEqual(client.get(reverse("home")).status_code, 200)

    def test_staff_recovery_access_does_not_grant_unrelated_account_editing(self):
        actor = create_active_user("limited@example.org")
        actor.set_privileges(is_staff=True, is_superuser=False)
        client = VerifiedClient()
        client.force_login(actor)
        change = reverse("admin:accounts_user_change", args=[self.staff.pk])
        response = client.get(change)
        self.assertContains(response, reverse("admin:accounts_user_reset_second_factor", args=[self.staff.pk]))
        self.assertNotContains(response, self.staff.email)
        self.assertNotContains(response, 'name="is_superuser"')
        self.assertEqual(client.post(change, {"is_superuser": "on"}).status_code, 403)
        reset = reverse("admin:accounts_user_reset_second_factor", args=[self.staff.pk])
        self.assertEqual(client.post(reset, {"identity_verified": "on"}).status_code, 302)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password(PASSWORD))
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_RESET_LINK, account=self.staff).exists())

    def test_admin_recovery_only_has_one_heading_and_customer_identifier(self):
        actor = create_active_user("limited.recovery@example.org")
        actor.set_privileges(is_staff=True, is_superuser=False)
        client = VerifiedClient()
        client.force_login(actor)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        response = client.get(reverse("admin:accounts_user_change", args=[self.staff.pk]))
        self.assertContains(response, "<h1", count=1)
        self.assertContains(response, self.staff.customer_number_display)
        self.assertContains(response, 'class="button recovery-action"')
        self.assertNotContains(response, 'name="is_superuser"')

    def test_admin_offers_recovery_form_to_active_password_only_account(self):
        AuthenticatorDevice.objects.filter(user=self.user).delete()
        client = VerifiedClient()
        client.force_login(self.staff)
        response = client.get(reverse("admin:accounts_user_reset_second_factor", args=[self.user.pk]))
        self.assertContains(response, 'name="identity_verified"')
        self.assertTrue(response.context["recovery_eligible"])

    def test_recipient_confirmation_names_destructive_post_without_mutating_on_get(self):
        token = self.issue()
        from leg_platform.accounts.views import TWO_FACTOR_RESET_SESSION_KEY
        session = self.client.session
        session[TWO_FACTOR_RESET_SESSION_KEY] = token.digest
        session.save()
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        response = self.client.get(reverse("accounts:two_factor_reset_confirm"))
        self.assertContains(response, "Remove old sign-in methods and continue")
        self.assertContains(response, 'class="btn btn-danger"')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    @override_settings(LANGUAGE_CODE="en")
    def test_recovery_email_subjects_name_whole_method_recovery(self):
        self.issue()
        self.assertTrue(any("Recover access to " in item.subject for item in mail.outbox))
        token = VerificationToken.objects.get(
            user=self.user, purpose=VerificationToken.Purpose.TWO_FACTOR_RESET
        )
        second_factor_reset.confirm_link(token)
        run_mail_queue()
        self.assertTrue(any("Sign-in recovery confirmed for " in item.subject for item in mail.outbox))

    def test_public_guidance_uses_existing_mail_limits_and_generic_response(self):
        with patch("leg_platform.accounts.bot_check.looks_automated", return_value=False):
            response = self.client.post(reverse("accounts:recovery"), reset_request_data("missing@example.org"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.RECOVERY_GUIDANCE).exists())


class LoginHelpPresentationTests(TestCase):
    def test_followup_tile_names_all_three_methods_without_adding_a_destination(self):
        from leg_platform.accounts.tasks import TASKS_BY_KEY

        with translation.override("en"):
            tile = TASKS_BY_KEY["two_factor"]
            self.assertEqual(str(tile.description), "Choose a password, password and app, or passkey.")
            self.assertEqual(tile.status, "optional")
            self.assertEqual(tile.method, "link")

    def test_login_has_one_native_checkbox_and_three_grouped_actions(self):
        with translation.override("en"):
            response = self.client.get(reverse("accounts:login"))
        html = response.content.decode()
        self.assertContains(response, 'type="checkbox"', count=1)
        self.assertContains(response, 'name="remember_me"', count=1)
        self.assertContains(response, 'data-remember-me="id_remember_me"', count=1)
        self.assertNotContains(response, 'name="stay"')
        self.assertNotContains(response, 'No account yet?')
        self.assertNotContains(response, f'href="{reverse("accounts:password_reset")}"')
        self.assertNotContains(response, f'href="{reverse("accounts:recovery")}"')
        self.assertContains(response, f'href="{reverse("accounts:login_help")}"', count=1)
        actions = html[html.index('<div class="login-actions">'):]
        self.assertLess(actions.index('form="login-password"'), actions.index('data-passkey-submit'))
        self.assertLess(actions.index('data-passkey-submit'), actions.index(f'href="{reverse("accounts:registration")}"'))
        self.assertContains(response, 'id="login-password"')
        self.assertFalse(response.context["form"]["remember_me"].value())
        # Disabled until the self-hosted helper detects browser support; password needs no JS.
        self.assertContains(response, "data-passkey-submit disabled")

    def test_checked_preference_survives_password_validation_failure(self):
        response = self.client.post(reverse("accounts:login"), {
            "username": "nobody@example.org", "password": "invalid", "remember_me": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"]["remember_me"].value())
        self.assertContains(response, 'type="checkbox"', count=1)
        self.assertContains(response, 'checked')

    def test_help_is_get_only_navigation_without_an_account_or_mail_job(self):
        response = self.client.get(reverse("accounts:login_help"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("accounts:password_reset")}"')
        self.assertContains(response, f'href="{reverse("accounts:recovery")}"')
        self.assertContains(response, f'href="{reverse("accounts:login")}"')
        self.assertNotContains(response, 'type="email"')
        self.assertNotContains(response, 'name="username"')
        self.assertEqual(self.client.post(reverse("accounts:login_help")).status_code, 405)
        self.assertFalse(User.objects.exists())
        self.assertFalse(MailJob.objects.exists())

    def test_help_and_recovery_use_designer_strings_in_all_four_languages(self):
        for language, title, action in (
            ("en", "Trouble signing in?", "Send recovery instructions"),
            ("de-ch", "Probleme beim Anmelden?", "Anleitung zur Wiederherstellung senden"),
            ("fr", "Des difficultés pour vous connecter ?", "Envoyer les instructions de récupération"),
            ("it", "Problemi di accesso?", "Invia le istruzioni di recupero"),
        ):
            with self.subTest(language=language):
                self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
                self.assertContains(self.client.get(reverse("accounts:login_help")), title)
                self.assertContains(self.client.get(reverse("accounts:recovery")), action)

    def test_recovery_clarifies_password_plus_code_without_promising_a_reset_link(self):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        for name in ("accounts:login_help", "accounts:recovery"):
            response = self.client.get(reverse(name))
            self.assertContains(response, "sign in with your password and enter a recovery code at the next step")
        response = self.client.get(reverse("accounts:recovery"))
        self.assertContains(response, "we send you a link to choose a new sign-in method. Otherwise we send you instructions.")
        self.assertNotContains(response, "We send instructions to the address you enter.")

    @override_settings(LANGUAGE_CODE="en")
    def test_recovery_email_uses_the_same_precise_password_then_code_guidance(self):
        from leg_platform.accounts import emails

        emails.send_recovery_guidance("synthetic@example.org")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(
            "sign in with your password and enter a recovery code at the next step",
            mail.outbox[0].body,
        )

    def test_recovery_bot_controls_are_hidden_once_and_only_email_is_editable(self):
        from html.parser import HTMLParser

        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inputs = []

            def handle_starttag(self, tag, attrs):
                if tag == "input":
                    self.inputs.append(dict(attrs))

        response = self.client.get(reverse("accounts:recovery"))
        parser = Inputs()
        parser.feed(response.content.decode())
        editable = [
            field["name"] for field in parser.inputs
            if field.get("type") != "hidden" and field.get("aria-hidden") != "true"
        ]
        self.assertEqual(editable, ["email"])
        honeypots = [field for field in parser.inputs if field.get("name") == "contact_fax_number"]
        stamps = [field for field in parser.inputs if field.get("name") == "form_started"]
        self.assertEqual(len(honeypots), 1)
        self.assertEqual(honeypots[0]["tabindex"], "-1")
        self.assertEqual(honeypots[0]["aria-hidden"], "true")
        self.assertEqual(honeypots[0]["autocomplete"], "off")
        self.assertEqual(len(stamps), 1)
        self.assertEqual(stamps[0]["type"], "hidden")
        self.assertContains(response, '<div class="hp-field" aria-hidden="true">', count=1)
        self.assertNotContains(response, "Form started")
        self.assertNotContains(response, 'for="id_contact_fax_number"')

    def test_real_recovery_form_stamp_accepts_legitimate_request_without_bypassing_bot_check(self):
        from leg_platform.accounts import bot_check

        started = bot_check.time.time()
        with patch.object(bot_check.time, "time", return_value=started):
            response = self.client.get(reverse("accounts:recovery"))
        stamp = response.context["form"]["form_started"].value()
        with patch.object(bot_check.time, "time", return_value=started + 3):
            response = self.client.post(reverse("accounts:recovery"), {
                "email": "synthetic@example.org",
                "contact_fax_number": "",
                "form_started": stamp,
            })
        self.assertRedirects(
            response, reverse("accounts:recovery_sent"), fetch_redirect_response=False
        )
        self.assertEqual(
            MailJob.objects.filter(kind=MailJob.Kind.RECOVERY_GUIDANCE).count(), 1
        )
