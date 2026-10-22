"""D-106 ceremonies with real ES256 signatures and synthetic credentials, never real devices."""

import hashlib
import json
import secrets
import struct
from datetime import timedelta
from unittest.mock import patch

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings, tag
from django.utils import timezone
from webauthn.helpers import bytes_to_base64url

from leg_platform.accounts import passkey_methods, passkeys, password_reset, second_factor
from leg_platform.accounts.forms import EmailChangeForm, ReauthenticationForm
from leg_platform.accounts.models import (
    AccountIntegrityError, AuthenticatorDevice, MailJob, Passkey, PasskeyChallenge,
    RecoveryCodeDevice, User, VerificationToken,
)
from leg_platform.accounts.tests.helpers import create_active_user


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class PasskeyCeremonyTests(TestCase):
    def setUp(self):
        self.user = create_active_user()
        self.request = RequestFactory().post("/profile/email/")
        self.request.user = self.user
        self.request.session = SessionStore()
        self.request.session[second_factor.SESSION_COUNTER_KEY] = secrets.token_urlsafe(32)
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)

    def client_data(self, options, kind, **changes):
        return json.dumps({
            "type": kind, "challenge": options["options"]["challenge"],
            "origin": "https://portal.example.org", "crossOrigin": False, **changes,
        }).encode()

    def registration(self, options, **changes):
        public = self.private_key.public_key().public_numbers()
        cose = cbor2.dumps({
            1: 2, 3: -7, -1: 1,
            -2: public.x.to_bytes(32, "big"), -3: public.y.to_bytes(32, "big"),
        })
        auth_data = (
            hashlib.sha256(b"portal.example.org").digest()
            + b"\x45" + struct.pack(">I", 0) + b"\0" * 16
            + struct.pack(">H", len(self.credential_id)) + self.credential_id + cose
        )
        client = self.client_data(options, "webauthn.create", **changes)
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id), "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client),
                "attestationObject": bytes_to_base64url(cbor2.dumps({
                    "fmt": "none", "attStmt": {}, "authData": auth_data,
                })),
            },
            "clientExtensionResults": {"credProps": {"rk": True}},
        }

    def stage(self):
        options = passkeys.begin(self.request, "register", user=self.user)
        return passkeys.register(self.request, options["ceremony"], self.registration(options), "Synthetic key")

    def activate(self):
        passkey_methods.allow_setup(self.request)
        self.assertIsNotNone(self.stage())
        self.user = passkey_methods.activate(self.request)
        self.request.user = self.user
        self.assertIsNotNone(self.user)

    def assertion(self, options, *, count=1, handle=None, flags=b"\x05", **changes):
        client = self.client_data(options, "webauthn.get", **changes)
        auth_data = hashlib.sha256(b"portal.example.org").digest() + flags + struct.pack(">I", count)
        signature = self.private_key.sign(
            auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id), "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(
                    handle if handle is not None else bytes(self.user.passkey_identity.handle)
                ),
            },
        }

    def test_real_registration_stages_but_cannot_sign_in(self):
        key = self.stage()
        self.assertIsNotNone(key)
        self.assertFalse(key.active)
        options = passkeys.begin(self.request, "login")
        self.assertEqual(options["options"].get("allowCredentials", []), [])
        self.assertIsNone(passkeys.authenticate(self.request, options["ceremony"], self.assertion(options)))

    def test_real_username_free_authentication_consumes_challenge_and_counter(self):
        self.activate()
        options = passkeys.begin(self.request, "login")
        assertion = self.assertion(options)
        self.assertEqual(passkeys.authenticate(self.request, options["ceremony"], assertion), self.user)
        self.assertEqual(Passkey.objects.get(user=self.user).sign_count, 1)
        self.assertIsNone(passkeys.authenticate(self.request, options["ceremony"], assertion))

    def test_bad_origin_and_cross_origin_are_refused_and_consumed(self):
        self.activate()
        for changes in (
            {"origin": "https://attacker.example.org"}, {"crossOrigin": True},
            {"topOrigin": "https://portal.example.org"},
        ):
            with self.subTest(changes=changes):
                options = passkeys.begin(self.request, "login")
                self.assertIsNone(passkeys.authenticate(
                    self.request, options["ceremony"], self.assertion(options, **changes)
                ))
                self.assertFalse(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())

    def test_user_verification_and_user_handle_are_required(self):
        self.activate()
        for fields in ({"flags": b"\x01"}, {"handle": b"wrong"}):
            options = passkeys.begin(self.request, "login")
            self.assertIsNone(passkeys.authenticate(
                self.request, options["ceremony"], self.assertion(options, **fields)
            ))

    def test_counter_replay_is_refused(self):
        self.activate()
        options = passkeys.begin(self.request, "login")
        self.assertIsNotNone(passkeys.authenticate(self.request, options["ceremony"], self.assertion(options)))
        options = passkeys.begin(self.request, "login")
        self.assertIsNone(passkeys.authenticate(self.request, options["ceremony"], self.assertion(options)))

    def test_current_proof_is_action_bound(self):
        self.activate()
        options = passkeys.begin(self.request, "proof", user=self.user, action="/profile/email/")
        self.assertIsNone(passkeys.authenticate(
            self.request, options["ceremony"], self.assertion(options),
            user=self.user, action="/profile/delete/",
        ))

    def test_expired_challenge_and_foreign_session_are_refused(self):
        self.activate()
        options = passkeys.begin(self.request, "login")
        other = RequestFactory().post("/passkeys/login/")
        other.session = SessionStore()
        self.assertIsNone(passkeys.authenticate(other, options["ceremony"], self.assertion(options)))
        self.assertTrue(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())
        PasskeyChallenge.objects.filter(pk=options["ceremony"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertIsNone(passkeys.authenticate(self.request, options["ceremony"], self.assertion(options)))

    def test_method_switch_removes_password_app_codes_links_and_other_sessions(self):
        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        old_version = self.user.session_version
        VerificationToken.objects.issue(
            self.user, VerificationToken.Purpose.PASSWORD_RESET, timedelta(hours=24)
        )
        self.activate()
        self.assertFalse(self.user.has_usable_password())
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertFalse(RecoveryCodeDevice.objects.filter(user=self.user).exists())
        self.assertFalse(VerificationToken.objects.filter(user=self.user).exists())
        self.assertGreater(self.user.session_version, old_version)
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.SIGN_IN_CHANGED, address=self.user.email).exists())

    def test_password_reset_does_not_issue_a_passkey_bypass_link(self):
        self.activate()
        self.assertIsNone(password_reset.process_reset_request(self.user.email))
        self.assertFalse(VerificationToken.objects.filter(user=self.user).exists())

    def test_promotion_accepts_one_active_credential_without_device_confirmation(self):
        self.activate()
        self.user.set_privileges(is_staff=True, is_superuser=False)
        self.assertTrue(second_factor.has_second_factor(self.user))
        self.user.set_privileges(is_staff=True, is_superuser=True)
        self.assertTrue(second_factor.has_second_factor(self.user))

    def test_public_handle_is_opaque_and_stable(self):
        first = passkeys.begin(self.request, "register", user=self.user)
        second = passkeys.begin(self.request, "register", user=self.user)
        self.assertEqual(first["options"]["user"]["id"], second["options"]["user"]["id"])
        self.assertNotIn(self.user.email, first["options"]["user"]["id"])

    def test_registration_requires_discoverable_credential_and_verification(self):
        options = passkeys.begin(self.request, "register", user=self.user)
        selection = options["options"]["authenticatorSelection"]
        self.assertEqual(selection["residentKey"], "required")
        self.assertEqual(selection["userVerification"], "required")

    def test_rp_uses_canonical_configuration_not_request_headers(self):
        self.request.META["HTTP_HOST"] = "attacker.example.org"
        self.assertEqual(passkeys.relying_party(), ("portal.example.org", "https://portal.example.org"))

    def test_current_passkey_replaces_password_and_app_in_sensitive_forms(self):
        self.activate()
        options = passkeys.begin(
            self.request, "proof", user=self.user, action=self.request.path
        )
        form = ReauthenticationForm({
            "passkey_assertion": json.dumps({
                "ceremony": options["ceremony"], "credential": self.assertion(options),
            }),
            "current_password": "ignored",
            "otp_token": "000000",
        }, request=self.request)
        self.assertEqual(form.proof, "passkey")
        self.assertNotIn("current_password", form.fields)
        self.assertNotIn("otp_token", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        replay = ReauthenticationForm(form.data, request=self.request)
        self.assertFalse(replay.is_valid())

    def test_passkey_confirmation_settles_the_ip_slot_on_success_and_failure(self):
        self.activate()
        with patch(
            "leg_platform.accounts.second_factor.settle_client_ip_attempt",
            wraps=second_factor.settle_client_ip_attempt,
        ) as settle:
            for count, flags, valid in ((1, b"\x05", True), (2, b"\x01", False)):
                options = passkeys.begin(
                    self.request, "proof", user=self.user, action=self.request.path
                )
                assertion = json.dumps({
                    "ceremony": options["ceremony"],
                    "credential": self.assertion(options, count=count, flags=flags),
                })
                self.assertEqual(second_factor.verify_passkey(self.request, self.user, assertion), valid)
                settle.assert_called_with(self.request, succeeded=True)
            self.assertEqual(settle.call_count, 2)

    def test_non_object_client_data_is_a_consumed_verification_failure(self):
        self.activate()
        options = passkeys.begin(self.request, "login")
        response = self.assertion(options)
        response["response"]["clientDataJSON"] = bytes_to_base64url(b"[]")
        self.assertIsNone(passkeys.authenticate(self.request, options["ceremony"], response))
        self.assertFalse(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())

    def test_malformed_challenge_identifier_is_a_controlled_failure(self):
        self.activate()
        options = passkeys.begin(self.request, "login")
        self.assertIsNone(passkeys.authenticate(self.request, "not-a-uuid", self.assertion(options)))

    def test_registration_cleans_abandoned_staging_but_not_active_credentials(self):
        self.activate()
        active = self.user.passkeys.get()
        self.credential_id = secrets.token_bytes(32)
        stale = self.stage()
        Passkey.objects.filter(pk__in=[active.pk, stale.pk]).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        passkeys.begin(self.request, "register", user=self.user)
        self.assertFalse(Passkey.objects.filter(pk=stale.pk).exists())
        self.assertTrue(Passkey.objects.filter(pk=active.pk, active=True).exists())

    def test_staff_can_activate_exactly_one_credential(self):
        self.user.set_privileges(is_staff=True, is_superuser=False)
        passkey_methods.allow_setup(self.request)
        self.assertIsNotNone(self.stage())
        self.assertIsNotNone(passkey_methods.activate(self.request))
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, User.AuthenticationMethod.PASSKEY)
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        self.assertTrue(second_factor.has_second_factor(self.user))

    def test_one_key_activation_atomically_removes_app_password_and_real_recovery_codes(self):
        from leg_platform.accounts import totp
        from leg_platform.accounts.models import RecoveryCode

        self.user.set_privileges(is_staff=True, is_superuser=True)
        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        totp._replace_recovery_codes(self.user)
        self.assertEqual(RecoveryCode.objects.filter(device__user=self.user).count(), 10)
        passkey_methods.allow_setup(self.request)
        staged = self.stage()
        with patch.object(User, "end_all_sessions", side_effect=RuntimeError("Synthetic rollback")):
            with self.assertRaisesMessage(RuntimeError, "Synthetic rollback"):
                passkey_methods.activate(self.request)
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_usable_password())
        self.assertEqual(self.user.authentication_method, "password")
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user, confirmed=True).exists())
        self.assertEqual(RecoveryCode.objects.filter(device__user=self.user).count(), 10)
        staged.refresh_from_db()
        self.assertFalse(staged.active)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.SIGN_IN_CHANGED).exists())
        self.user = passkey_methods.activate(self.request)
        self.assertIsNotNone(self.user)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        self.assertFalse(self.user.has_usable_password())
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertFalse(RecoveryCodeDevice.objects.filter(user=self.user).exists())
        self.assertFalse(RecoveryCode.objects.filter(device__user=self.user).exists())

    def test_fresh_setup_grant_does_not_activate_abandoned_staging(self):
        staged = self.stage()
        Passkey.objects.filter(pk=staged.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        passkey_methods.allow_setup(self.request)
        self.assertIsNone(passkey_methods.activate(self.request))
        self.assertFalse(Passkey.objects.filter(pk=staged.pk).exists())
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, User.AuthenticationMethod.PASSWORD)

    def test_passkey_promotion_refuses_staged_only_credentials(self):
        passkey_methods.allow_setup(self.request)
        self.assertIsNotNone(self.stage())
        # The active-method invariant refuses a passkey account without an active key.
        with self.assertRaises(AccountIntegrityError):
            self.user._change(authentication_method=User.AuthenticationMethod.PASSKEY)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)
        self.assertFalse(second_factor.has_second_factor(self.user))

    def test_internal_models_offer_no_role_permissions(self):
        from leg_platform.accounts.models import PasskeyIdentity

        for model in (Passkey, PasskeyChallenge, PasskeyIdentity):
            self.assertEqual(model._meta.default_permissions, ())

    def test_http_username_free_login_marks_a_verified_session(self):
        self.activate()
        response = self.client.post(
            "/passkeys/options/", data=json.dumps({"purpose": "login"}),
            content_type="application/json", secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        options = response.json()
        response = self.client.post(
            "/passkeys/login/", data=json.dumps({
                "ceremony": options["ceremony"], "credential": self.assertion(options),
                "username": "not-the-credential-owner@example.org", "stay": True,
            }), content_type="application/json", secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertEqual(self.client.session[second_factor.PASSKEY_SESSION_KEY], self.user.pk)

    def test_http_passkey_login_persists_only_the_explicit_stay_choice(self):
        from leg_platform.accounts import session_timeout

        self.activate()
        for count, stay in enumerate((False, True), start=1):
            with self.subTest(stay=stay):
                client = Client()
                options = client.post(
                    "/passkeys/options/", data=json.dumps({"purpose": "login"}),
                    content_type="application/json", secure=True,
                ).json()
                response = client.post(
                    "/passkeys/login/", data=json.dumps({
                        "ceremony": options["ceremony"],
                        "credential": self.assertion(options, count=count), "stay": stay,
                    }), content_type="application/json", secure=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(client.session.get(session_timeout.STAYING_KEY, False), stay)

    def test_http_anonymous_enrollment_and_proof_are_refused_without_account_disclosure(self):
        for purpose in ("register", "proof"):
            response = self.client.post(
                "/passkeys/options/", data=json.dumps({"purpose": purpose}),
                content_type="application/json", secure=True,
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("no-store", response["Cache-Control"])
            self.assertNotIn(self.user.email, response.content.decode())
        self.assertFalse(PasskeyChallenge.objects.exists())

    def test_http_csrf_is_required_even_for_anonymous_login_options(self):
        response = Client(enforce_csrf_checks=True).post(
            "/passkeys/options/", data=json.dumps({"purpose": "login"}),
            content_type="application/json", secure=True,
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("no-store", response["Cache-Control"])

    @override_settings(AXES_FAILURE_LIMIT=100)
    def test_http_malformed_authentication_fields_fail_closed_and_consume_challenge(self):
        self.activate()
        case = 0
        for field in ("signature", "authenticatorData", "clientDataJSON", "userHandle"):
            for invalid in (None, [], {}, 12):
                with self.subTest(field=field, invalid=invalid):
                    # Isolate malformed-input handling from the separately tested IP lockout.
                    case += 1
                    self.client.defaults["REMOTE_ADDR"] = f"192.0.2.{case}"
                    options = self.client.post(
                        "/passkeys/options/", data=json.dumps({"purpose": "login"}),
                        content_type="application/json", secure=True,
                    ).json()
                    credential = self.assertion(options)
                    credential["response"][field] = invalid
                    response = self.client.post(
                        "/passkeys/login/", data=json.dumps({
                            "ceremony": options["ceremony"], "credential": credential,
                        }), content_type="application/json", secure=True,
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())
                    self.assertNotIn("_auth_user_id", self.client.session)

    def test_http_non_object_payload_and_oversized_body_are_refused(self):
        for data, status in (("[]", 400), ("x" * 32769, 413)):
            response = self.client.post(
                "/passkeys/options/", data=data, content_type="application/json", secure=True,
            )
            self.assertEqual(response.status_code, status)
            self.assertIn("no-store", response["Cache-Control"])

    def test_email_form_can_render_without_a_password_or_app(self):
        self.activate()
        form = EmailChangeForm(request=self.request)
        self.assertIn("passkey_assertion", form.fields)
        self.assertNotIn("current_password", form.fields)
        self.assertNotIn("otp_token", form.fields)

    def test_wrong_method_and_missing_challenge_fail_closed(self):
        self.activate()
        options = passkeys.begin(self.request, "proof", user=self.user, action=self.request.path)
        self.assertIsNone(passkeys.authenticate(self.request, options["ceremony"], self.assertion(options)))
        self.assertFalse(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())

    def test_duplicate_registration_does_not_overwrite_public_key(self):
        first = self.stage()
        options = passkeys.begin(self.request, "register", user=self.user)
        self.assertIsNone(passkeys.register(
            self.request, options["ceremony"], self.registration(options), "Duplicate"
        ))
        self.assertEqual(Passkey.objects.get(user=self.user).public_key, first.public_key)

    @override_settings(INSTANCE_BASE_URL="http://localhost:8001")
    def test_canonical_localhost_origin_is_allowed(self):
        self.assertEqual(passkeys.relying_party(), ("localhost", "http://localhost:8001"))


class CeremonyFixture:
    """Reuse real synthetic ES256 fixtures without inheriting another class's tests."""

    setUp = PasskeyCeremonyTests.setUp
    client_data = PasskeyCeremonyTests.client_data
    registration = PasskeyCeremonyTests.registration
    stage = PasskeyCeremonyTests.stage
    activate = PasskeyCeremonyTests.activate
    assertion = PasskeyCeremonyTests.assertion

    def json_post(self, path, data, client=None):
        return (client or self.client).post(
            path, json.dumps(data), content_type="application/json", secure=True
        )

    def options(self, purpose="login", **extra):
        response = self.json_post("/passkeys/options/", {"purpose": purpose, **extra})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def signed_in(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[second_factor.PASSKEY_SESSION_KEY] = self.user.pk
        session.save()

    def privileged(self, *, system=False):
        self.user.set_privileges(is_staff=True, is_superuser=system)
        passkey_methods.allow_setup(self.request)
        self.stage()
        self.credential_id = secrets.token_bytes(32)
        self.stage()
        self.user = passkey_methods.activate(self.request)
        self.request.user = self.user


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class PasskeyEndpointAcceptanceTests(CeremonyFixture, TestCase):
    """D-106: real signature verification through HTTP, not a mocked verifier."""

    def test_add_key_from_profile_requires_current_proof_and_preserves_disclosed_session_policy(self):
        from django.conf import settings
        from django.urls import reverse
        from leg_platform.accounts.models import SecurityEvent

        self.user.set_privileges(is_staff=True, is_superuser=True)
        self.activate()
        self.signed_in()
        other_session = Client()
        other_session.force_login(self.user)
        session = other_session.session
        session[second_factor.PASSKEY_SESSION_KEY] = self.user.pk
        session.save()
        original = self.user.passkeys.get()
        version = self.user.session_version
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        path = reverse("accounts:passkey_setup")
        self.assertContains(self.client.get(reverse("accounts:profile"), secure=True), f'href="{path}"')
        self.assertContains(self.client.get(reverse("accounts:passkeys"), secure=True), "Keep at least one active passkey.")
        self.assertContains(self.client.get(path, secure=True), "Add a passkey")
        self.assertEqual(self.json_post("/passkeys/options/", {"purpose": "register"}).status_code, 400)
        self.assertEqual(self.client.post(path, {"activate": "yes"}, secure=True).status_code, 200)
        options = self.options("proof", action=path)
        self.assertEqual(self.client.post(path, {
            "passkey_assertion": json.dumps({
                "ceremony": options["ceremony"], "credential": self.assertion(options),
            }),
        }, secure=True).status_code, 302)
        setup = self.client.get(path, secure=True)
        self.assertContains(setup, "Other sessions end and you receive a security notice.")
        self.assertContains(setup, "We strongly recommend")
        self.assertNotContains(setup, 'name="two_devices"')
        self.credential_id = secrets.token_bytes(32)
        options = self.options("register")
        self.assertEqual(self.json_post(path, {
            "ceremony": options["ceremony"], "credential": self.registration(options),
            "name": "Synthetic additional computer",
        }).status_code, 200)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        MailJob.objects.all().delete()
        self.assertEqual(self.client.post(path, {"activate": "yes"}, secure=True).status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        self.assertTrue(self.user.passkeys.filter(pk=original.pk, active=True).exists())
        self.assertGreater(self.user.session_version, version)
        self.assertEqual(self.client.get(reverse("accounts:profile"), secure=True).status_code, 200)
        self.assertEqual(other_session.get(reverse("accounts:profile"), secure=True).status_code, 302)
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.SIGN_IN_CHANGED, address=self.user.email).exists())
        self.assertTrue(SecurityEvent.objects.filter(
            event=SecurityEvent.Event.PASSKEYS_CHANGED, account_id=self.user.pk, long_retention=True,
        ).exists())

    def test_obsolete_setup_statement_is_ignored_and_promotion_needs_no_statement(self):
        from django.urls import reverse
        from leg_platform.accounts.tests.helpers import PASSWORD, VerifiedClient
        from leg_platform.accounts.tests.test_admin import PROFILE_DATA

        self.client.force_login(self.user)
        path = reverse("accounts:passkey_setup")
        self.assertEqual(self.client.post(
            path, {"current_password": PASSWORD}, secure=True
        ).status_code, 302)
        for _ in range(2):
            self.credential_id = secrets.token_bytes(32)
            options = self.options("register")
            response = self.json_post(path, {
                "ceremony": options["ceremony"], "credential": self.registration(options),
                "name": "Synthetic forged-statement key",
            })
            self.assertEqual(response.status_code, 200)
        response = self.client.post(path, {"activate": "yes", "two_devices": "on"}, secure=True)
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        self.assertFalse(self.user.passkey_identity.two_devices_confirmed)
        actor = create_active_user("foreign.confirmation.administrator@example.org")
        actor.set_privileges(is_staff=True, is_superuser=True)
        admin_client = VerifiedClient()
        admin_client.force_login(actor)
        url = reverse("admin:accounts_user_change", args=[self.user.pk])
        self.assertNotContains(admin_client.get(url, secure=True), 'name="passkey_devices_confirmed"')
        response = admin_client.post(
            url, {**PROFILE_DATA, "is_staff": "on", "_save": "Save"}, secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)

    def test_corrupted_signature_is_refused_without_session_or_counter_change(self):
        self.activate()
        options = self.options()
        credential = self.assertion(options)
        credential["response"]["signature"] = bytes_to_base64url(b"not-an-es256-signature")
        response = self.json_post("/passkeys/login/", {
            "ceremony": options["ceremony"], "credential": credential,
        })
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.user.passkeys.get().sign_count, 0)
        self.assertFalse(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())

    def test_valid_signature_for_wrong_rp_hash_is_refused(self):
        self.activate()
        options = self.options()
        credential = self.assertion(options)
        client_data = self.client_data(options, "webauthn.get")
        auth_data = hashlib.sha256(b"attacker.example.org").digest() + b"\x05" + struct.pack(">I", 1)
        signature = self.private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        credential["response"]["authenticatorData"] = bytes_to_base64url(auth_data)
        credential["response"]["signature"] = bytes_to_base64url(signature)
        response = self.json_post("/passkeys/login/", {
            "ceremony": options["ceremony"], "credential": credential,
        })
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_foreign_browser_cannot_use_or_consume_the_owner_login_challenge(self):
        self.activate()
        options = self.options()
        payload = {"ceremony": options["ceremony"], "credential": self.assertion(options)}
        other = Client()
        self.assertEqual(self.json_post("/passkeys/login/", payload, client=other).status_code, 400)
        self.assertNotIn("_auth_user_id", other.session)
        self.assertTrue(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())
        self.assertEqual(self.json_post("/passkeys/login/", payload).status_code, 200)

    def test_replaced_options_refuse_the_previous_signed_response(self):
        self.activate()
        old = self.options()
        current = self.options()
        self.assertFalse(PasskeyChallenge.objects.filter(pk=old["ceremony"]).exists())
        self.assertEqual(self.json_post("/passkeys/login/", {
            "ceremony": old["ceremony"], "credential": self.assertion(old),
        }).status_code, 400)
        self.assertEqual(self.json_post("/passkeys/login/", {
            "ceremony": current["ceremony"], "credential": self.assertion(current),
        }).status_code, 200)

    def test_blocking_after_options_refuses_a_genuine_signed_assertion(self):
        self.activate()
        options = self.options()
        self.user.set_status(User.Status.BLOCKED)
        self.assertEqual(self.json_post("/passkeys/login/", {
            "ceremony": options["ceremony"], "credential": self.assertion(options),
        }).status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.user.passkeys.get().sign_count, 0)

    def test_anonymous_cannot_request_registration_or_current_proof(self):
        for purpose in ("register", "proof"):
            with self.subTest(purpose=purpose):
                self.assertEqual(self.json_post("/passkeys/options/", {
                    "purpose": purpose, "action": "/profile/email/",
                }).status_code, 400)
        self.assertFalse(PasskeyChallenge.objects.exists())

    def test_registration_endpoint_requires_a_current_proof_grant(self):
        self.client.force_login(self.user)
        self.assertEqual(self.json_post("/passkeys/options/", {"purpose": "register"}).status_code, 400)
        self.assertFalse(PasskeyChallenge.objects.exists())
        self.assertFalse(self.user.passkeys.exists())

    def test_login_endpoint_requires_csrf_even_with_a_genuine_assertion(self):
        self.activate()
        options = self.options()
        strict = Client(enforce_csrf_checks=True)
        response = self.json_post("/passkeys/login/", {
            "ceremony": options["ceremony"], "credential": self.assertion(options),
        }, client=strict)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())
        self.assertNotIn("_auth_user_id", strict.session)

    def test_real_current_proof_for_another_action_cannot_remove_a_key(self):
        self.activate()
        passkey_methods.allow_setup(self.request)
        self.credential_id = secrets.token_bytes(32)
        self.stage()
        self.user = passkey_methods.activate(self.request)
        self.request.user = self.user
        self.signed_in()
        options = self.options("proof", action="/profile/email/")
        keys = set(self.user.passkeys.values_list("pk", flat=True))
        from django.urls import reverse
        response = self.client.post(reverse("accounts:passkeys"), {
            "credential": min(keys),
            "passkey_assertion": json.dumps({
                "ceremony": options["ceremony"], "credential": self.assertion(options),
            }),
        }, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(self.user.passkeys.values_list("pk", flat=True)), keys)
        self.assertFalse(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())

    def test_setup_grant_expires_exactly_at_five_minutes(self):
        self.activate()
        with patch("leg_platform.accounts.passkey_methods.time.time", return_value=1000):
            passkey_methods.allow_setup(self.request)
        with patch("leg_platform.accounts.passkey_methods.time.time", return_value=1299.999):
            self.assertTrue(passkey_methods.setup_allowed(self.request))
        with patch("leg_platform.accounts.passkey_methods.time.time", return_value=1300):
            self.assertFalse(passkey_methods.setup_allowed(self.request))

    def test_session_revocation_invalidates_a_signed_current_proof(self):
        self.activate()
        options = passkeys.begin(self.request, "proof", user=self.user, action=self.request.path)
        credential = self.assertion(options)
        self.user.end_all_sessions()
        self.user.refresh_from_db()
        self.assertIsNone(passkeys.authenticate(
            self.request, options["ceremony"], credential,
            user=self.user, action=self.request.path,
        ))
        self.assertFalse(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())

    def test_privileged_removal_endpoint_cannot_remove_the_last_active_key(self):
        self.privileged(system=True)
        self.assertTrue(passkey_methods.remove(self.user, self.user.passkeys.first().pk))
        self.user.refresh_from_db()
        self.signed_in()
        from django.urls import reverse

        path = reverse("accounts:passkeys")
        options = self.options("proof", action=path)
        version = self.user.session_version
        response = self.client.post(path, {
            "credential": self.user.passkeys.first().pk,
            "passkey_assertion": json.dumps({
                "ceremony": options["ceremony"], "credential": self.assertion(options),
            }),
        }, secure=True)
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        self.assertEqual(self.user.session_version, version)

    def test_registration_without_user_verification_cannot_stage_a_key(self):
        options = passkeys.begin(self.request, "register", user=self.user)
        credential = self.registration(options)
        from webauthn.helpers import base64url_to_bytes

        attestation = cbor2.loads(base64url_to_bytes(credential["response"]["attestationObject"]))
        auth_data = bytearray(attestation["authData"])
        auth_data[32] &= ~0x04
        attestation["authData"] = bytes(auth_data)
        credential["response"]["attestationObject"] = bytes_to_base64url(cbor2.dumps(attestation))
        self.assertIsNone(passkeys.register(
            self.request, options["ceremony"], credential, "Unverified synthetic key"
        ))
        self.assertFalse(self.user.passkeys.exists())
        self.assertFalse(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())

    def test_another_accounts_genuine_credential_cannot_prove_the_current_account(self):
        self.activate()
        owner = self.user
        other = create_active_user("foreign.credential@example.org")
        self.user = other
        self.request.user = other
        self.credential_id = secrets.token_bytes(32)
        self.activate()
        self.request.user = owner
        options = passkeys.begin(self.request, "proof", user=owner, action=self.request.path)
        # The assertion is genuinely signed and contains the other account's
        # correct opaque handle; it is not proof for the requested account.
        credential = self.assertion(options)
        self.assertIsNone(passkeys.authenticate(
            self.request, options["ceremony"], credential, user=owner, action=self.request.path
        ))
        self.assertEqual(self.user.passkeys.get().sign_count, 0)
        self.assertFalse(PasskeyChallenge.objects.filter(pk=options["ceremony"]).exists())

    def test_native_login_form_remains_submittable_without_javascript(self):
        from html.parser import HTMLParser
        from django.urls import reverse

        class Controls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.elements = []

            def handle_starttag(self, tag, attrs):
                self.elements.append((tag, dict(attrs)))

        response = self.client.get(reverse("accounts:login"))
        parser = Controls()
        parser.feed(response.content.decode())
        forms = [attrs for tag, attrs in parser.elements if tag == "form"]
        password = next(attrs for attrs in forms if attrs.get("id") == "login-password")
        self.assertEqual(password.get("method"), "post")
        submit = [
            attrs for tag, attrs in parser.elements
            if tag == "button" and attrs.get("form") == "login-password"
        ]
        self.assertEqual(len(submit), 1)
        self.assertEqual(submit[0].get("type"), "submit")
        self.assertNotIn("disabled", submit[0])
        remember = [
            attrs for tag, attrs in parser.elements
            if tag == "input" and attrs.get("name") == "remember_me"
        ]
        self.assertEqual(len(remember), 1)
        self.assertEqual(remember[0].get("type"), "checkbox")
        passkey = next(attrs for attrs in forms if "data-passkey-login" in attrs)
        self.assertEqual(passkey["data-remember-me"], remember[0]["id"])
        self.assertEqual(passkey["action"], reverse("accounts:passkey_login"))


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class MethodReturnAcceptanceTests(CeremonyFixture, TestCase):
    """Pending switch boundaries, competing links and notification recipients."""

    def pending(self, method="password"):
        from django.contrib.auth.hashers import make_password
        from leg_platform.accounts import method_changes
        from leg_platform.accounts.tests.helpers import OTHER_PASSWORD

        passkey_methods.allow_setup(self.request)
        return method_changes.start(self.request, make_password(OTHER_PASSWORD), method)

    def token(self, user=None):
        raw = VerificationToken.objects.issue(
            user or self.user, VerificationToken.Purpose.METHOD_RETURN, timedelta(hours=24)
        )
        return VerificationToken.objects.find_valid(raw, VerificationToken.Purpose.METHOD_RETURN)

    def test_replacing_pending_switch_invalidates_old_token_and_old_password_hash(self):
        from django.contrib.auth.hashers import make_password
        from leg_platform.accounts import method_changes

        self.activate()
        self.pending()
        old = self.token()
        replacement_password = "synthetic replacement password for newer request"
        method_changes.start(self.request, make_password(replacement_password), "password")
        self.assertIsNone(method_changes.complete(self.user.pk, token_id=old.pk))
        self.assertTrue(self.user.passkeys.filter(active=True).exists())
        result = method_changes.complete(self.user.pk, token_id=self.token().pk)
        self.assertIsNotNone(result)
        self.assertTrue(result[0].check_password(replacement_password))

    def test_foreign_account_token_cannot_complete_the_pending_switch(self):
        from leg_platform.accounts import method_changes

        self.activate()
        self.pending()
        other = create_active_user("other.return@example.org")
        self.assertIsNone(method_changes.complete(self.user.pk, token_id=self.token(other).pk))
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertFalse(self.user.has_usable_password())

    def test_token_and_pending_expire_at_the_exact_boundary(self):
        from leg_platform.accounts import method_changes
        from leg_platform.accounts.models import PendingSignInChange

        self.activate()
        for expired in ("token", "pending"):
            with self.subTest(expired=expired):
                pending = self.pending()
                token = self.token()
                boundary = timezone.now()
                if expired == "token":
                    VerificationToken.objects.filter(pk=token.pk).update(expires_at=boundary)
                else:
                    PendingSignInChange.objects.filter(pk=pending.pk).update(expires_at=boundary)
                with patch("django.utils.timezone.now", return_value=boundary):
                    self.assertIsNone(method_changes.complete(self.user.pk, token_id=token.pk))
                self.assertTrue(self.user.passkeys.filter(active=True).exists())

    def test_second_address_link_is_single_use_and_notifies_every_address(self):
        import re
        from urllib.parse import urlsplit
        from leg_platform.accounts.mail_queue import enqueue_for_account
        from leg_platform.accounts.tests.helpers import run_mail_queue

        self.activate()
        self.user.set_secondary_email("backup.return@example.org")
        self.request.user = self.user
        self.pending()
        enqueue_for_account(MailJob.Kind.METHOD_RETURN, self.user)
        run_mail_queue()
        from django.core import mail
        messages = [item for item in mail.outbox if "/sign-in-method/verify/" in item.body]
        self.assertEqual({item.to[0] for item in messages}, set(self.user.notification_addresses()))
        paths = {}
        for message in messages:
            match = re.search(r"https://portal\.example\.org/sign-in-method/verify/\S+", message.body)
            self.assertIsNotNone(match)
            paths[message.to[0]] = urlsplit(match.group()).path
        from django.urls import reverse
        self.client.get(paths[self.user.secondary_email], secure=True)
        response = self.client.post(reverse("accounts:method_return_confirm"), secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(VerificationToken.objects.filter(user=self.user).exists())
        self.assertNotEqual(
            Client().get(paths[self.user.email], secure=True)["Location"],
            reverse("accounts:method_return_confirm"),
        )
        notifications = MailJob.objects.filter(kind=MailJob.Kind.SIGN_IN_CHANGED)
        self.assertEqual(set(notifications.values_list("address", flat=True)), set(self.user.notification_addresses()))
        self.assertFalse(notifications.exclude(account=None).exists())

    def test_app_confirmation_is_bound_to_the_initiating_session(self):
        from leg_platform.accounts import method_changes
        from leg_platform.accounts.tests.helpers import code_for

        self.activate()
        self.pending("app")
        device = AuthenticatorDevice.objects.get(user=self.user)
        foreign = RequestFactory().post("/sign-in-method/app/")
        foreign.user = self.user
        foreign.session = SessionStore()
        foreign.session["method_app_device"] = device.pk
        code = code_for(device)
        self.assertFalse(method_changes.confirm_app(foreign, code))
        self.assertFalse(device.confirmed)
        self.assertTrue(method_changes.confirm_app(self.request, code))
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")

    def test_blocking_target_after_link_issue_prevents_switch(self):
        from leg_platform.accounts import method_changes

        self.activate()
        self.pending()
        token = self.token()
        self.user.set_status(User.Status.BLOCKED)
        self.assertIsNone(method_changes.complete(self.user.pk, token_id=token.pk))
        self.assertTrue(self.user.passkeys.filter(active=True).exists())

    def test_password_return_http_matrix_keeps_old_keys_until_email_confirmed_completion(self):
        from django.urls import reverse
        from leg_platform.accounts.tests.helpers import OTHER_PASSWORD, code_for

        for role, chosen in (("user", "password"), ("user", "app"), ("staff", "app"), ("system_admin", "app")):
            with self.subTest(role=role, chosen=chosen):
                self.user = create_active_user(f"return.http.{role}.{chosen}@example.org")
                self.request.user = self.user
                self.request.session = SessionStore()
                self.request.session[second_factor.SESSION_COUNTER_KEY] = secrets.token_urlsafe(32)
                self.credential_id = secrets.token_bytes(32)
                if role == "user":
                    self.activate()
                else:
                    self.privileged(system=role == "system_admin")
                self.client = Client()
                # Each independent scenario has its own reserved documentation IP.
                # Keep the real protection enabled instead of sharing one budget across
                # the whole combination matrix or clearing global rate-limit records.
                self.client.defaults["REMOTE_ADDR"] = f"192.0.2.{self.user.pk % 250 + 1}"
                self.signed_in()
                old_keys = set(self.user.passkeys.values_list("pk", flat=True))
                self.client.post(reverse("accounts:login_method"), {"method": chosen}, secure=True)
                path = reverse("accounts:method_password")
                options = self.options("proof", action=path)
                response = self.client.post(path, {
                    "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD,
                    "passkey_assertion": json.dumps({
                        "ceremony": options["ceremony"], "credential": self.assertion(options),
                    }),
                }, secure=True)
                if chosen == "app":
                    self.assertRedirects(
                        response, reverse("accounts:method_app"), fetch_redirect_response=False
                    )
                    device = AuthenticatorDevice.objects.get(user=self.user)
                    self.assertFalse(device.confirmed)
                    response = self.client.post(reverse("accounts:method_app"), {
                        "otp_token": code_for(device),
                    }, secure=True)
                self.assertEqual(response.status_code, 200)
                self.user.refresh_from_db()
                self.assertEqual(self.user.authentication_method, "passkey")
                self.assertFalse(self.user.has_usable_password())
                self.assertEqual(set(self.user.passkeys.values_list("pk", flat=True)), old_keys)
                raw = VerificationToken.objects.issue(
                    self.user, VerificationToken.Purpose.METHOD_RETURN, timedelta(hours=24)
                )
                link_client = Client()
                link_client.get(reverse("accounts:method_return_verify", args=[raw]), secure=True)
                self.user.refresh_from_db()
                self.assertEqual(self.user.authentication_method, "passkey")
                response = link_client.post(reverse("accounts:method_return_confirm"), secure=True)
                self.assertEqual(response.status_code, 200 if chosen == "app" else 302)
                self.assertNotIn("_auth_user_id", link_client.session)
                self.user.refresh_from_db()
                self.assertTrue(self.user.check_password(OTHER_PASSWORD))
                self.assertEqual(self.user.authentication_method, "password")
                self.assertFalse(self.user.passkeys.exists())
                self.assertEqual(
                    AuthenticatorDevice.objects.filter(user=self.user, confirmed=True).exists(),
                    chosen == "app",
                )
                if chosen == "app":
                    self.assertEqual(RecoveryCodeDevice.objects.get(user=self.user).codes.count(), 10)
                self.assertEqual(
                    self.client.get(reverse("accounts:profile"), secure=True).status_code, 302
                )
                self.assertEqual(
                    link_client.post(reverse("accounts:method_return_confirm"), secure=True).status_code, 302
                )


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class RecoveryAcceptanceMatrixTests(CeremonyFixture, TestCase):
    """Owner amendment: same recovery for app/passkey and every access level."""

    def previous_method(self, role, previous, suffix):
        self.user = create_active_user(f"matrix.{suffix}.{role}.{previous}@example.org")
        self.request = RequestFactory().post("/profile/email/")
        self.request.user = self.user
        self.request.session = SessionStore()
        self.request.session[second_factor.SESSION_COUNTER_KEY] = secrets.token_urlsafe(32)
        self.credential_id = secrets.token_bytes(32)
        self.client = Client()
        # Independent matrix scenarios must not exhaust each other's real IP budget.
        self.client.defaults["REMOTE_ADDR"] = f"192.0.2.{self.user.pk % 250 + 1}"
        if previous == "passkey":
            if role == "user":
                self.activate()
            else:
                self.privileged(system=role == "system_admin")
        else:
            if previous == "app":
                AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
            if role != "user":
                self.user.set_privileges(is_staff=True, is_superuser=role == "system_admin")

    def recover(self):
        from django.urls import reverse
        from leg_platform.accounts import second_factor_reset
        from leg_platform.accounts.views import TWO_FACTOR_RESET_SESSION_KEY

        raw = VerificationToken.objects.issue(
            self.user, VerificationToken.Purpose.TWO_FACTOR_RESET, timedelta(hours=24),
            target_email=self.user.normalized_email,
        )
        token = second_factor_reset.find_link(raw)
        self.assertIsNotNone(token)
        session = self.client.session
        session[TWO_FACTOR_RESET_SESSION_KEY] = token.digest
        session.save()
        response = self.client.post(reverse("accounts:two_factor_reset_confirm"), secure=True)
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.request.user = self.user
        self.assertEqual(self.user.authentication_method, "recovery")
        return response

    def test_recovery_choice_matrix_for_app_and_passkey_at_every_access_level(self):
        from django.urls import reverse

        for role in ("user", "staff", "system_admin"):
            for previous in ("password", "app", "passkey"):
                with self.subTest(role=role, previous=previous):
                    self.previous_method(role, previous, "choice")
                    self.recover()
                    response = self.client.get(reverse("accounts:login_method"), secure=True)
                    self.assertContains(response, 'value="app"')
                    self.assertContains(response, 'value="passkey"')
                    if role == "user":
                        self.assertContains(response, 'value="password"')
                    else:
                        self.assertNotContains(response, 'value="password"')
                        refused = self.client.post(
                            reverse("accounts:login_method"), {"method": "password"}, secure=True
                        )
                        self.assertEqual(refused.status_code, 200)
                    for path in (reverse("home"), reverse("accounts:profile"), "/admin/", "/management/"):
                        self.assertRedirects(
                            self.client.get(path, secure=True), reverse("accounts:login_method"),
                            fetch_redirect_response=False,
                        )

    def test_recovery_completion_matrix_uses_real_app_codes_and_passkey_ceremonies(self):
        from django.urls import reverse
        from leg_platform.accounts.tests.helpers import OTHER_PASSWORD, code_for

        for role in ("user", "staff", "system_admin"):
            for previous in ("password", "app", "passkey"):
                for chosen in (("password", "app", "passkey") if role == "user" else ("app", "passkey")):
                    with self.subTest(role=role, previous=previous, chosen=chosen):
                        self.previous_method(role, previous, f"complete.{chosen}")
                        old_version = self.user.session_version
                        self.recover()
                        self.client.post(
                            reverse("accounts:login_method"), {"method": chosen}, secure=True
                        )
                        if chosen == "passkey":
                            for index in range(1):
                                self.credential_id = secrets.token_bytes(32)
                                options = self.options("register")
                                response = self.json_post(reverse("accounts:passkey_setup"), {
                                    "ceremony": options["ceremony"],
                                    "credential": self.registration(options),
                                    "name": f"Synthetic recovery key {index}",
                                })
                                self.assertEqual(response.status_code, 200)
                            data = {"activate": "yes"}
                            self.client.post(reverse("accounts:passkey_setup"), data, secure=True)
                        else:
                            response = self.client.post(reverse("accounts:method_password"), {
                                "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD,
                            }, secure=True)
                            if chosen == "app":
                                self.assertRedirects(
                                    response, reverse("accounts:method_app"), fetch_redirect_response=False
                                )
                                self.user.refresh_from_db()
                                self.assertEqual(self.user.authentication_method, "recovery")
                                device = AuthenticatorDevice.objects.get(user=self.user)
                                self.assertFalse(device.confirmed)
                                self.client.post(reverse("accounts:method_app"), {
                                    "otp_token": code_for(device),
                                }, secure=True)
                        self.user.refresh_from_db()
                        self.assertEqual(
                            self.user.authentication_method, "passkey" if chosen == "passkey" else "password"
                        )
                        self.assertGreater(self.user.session_version, old_version)
                        self.assertEqual(self.user.is_staff, role != "user")
                        self.assertEqual(self.user.is_superuser, role == "system_admin")
                        self.assertEqual(
                            AuthenticatorDevice.objects.filter(user=self.user, confirmed=True).exists(),
                            chosen == "app",
                        )
                        self.assertEqual(self.user.has_usable_password(), chosen != "passkey")
                        if chosen != "passkey":
                            self.assertTrue(self.user.check_password(OTHER_PASSWORD))
                            self.assertFalse(self.user.passkeys.exists())
                        else:
                            self.assertEqual(
                                self.user.passkeys.filter(active=True).count(), 1
                            )
                        self.assertEqual(
                            RecoveryCodeDevice.objects.filter(user=self.user).exists(), chosen == "app"
                        )
                        self.assertEqual(
                            self.client.get(reverse("accounts:profile"), secure=True).status_code, 200
                        )

    def test_real_recovery_registration_finishes_as_passkey_without_password(self):
        from django.urls import reverse

        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        self.recover()
        self.client.post(reverse("accounts:login_method"), {"method": "passkey"}, secure=True)
        options = self.options("register")
        registered = self.json_post(reverse("accounts:passkey_setup"), {
            "ceremony": options["ceremony"], "credential": self.registration(options),
            "name": "Synthetic recovery credential",
        })
        self.assertEqual(registered.status_code, 200)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 0)
        self.client.post(reverse("accounts:passkey_setup"), {"activate": "yes"}, secure=True)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(self.client.session[second_factor.PASSKEY_SESSION_KEY], self.user.pk)
        self.assertEqual(self.client.get(reverse("accounts:profile"), secure=True).status_code, 200)

    def test_password_and_app_switch_matrix_requires_real_current_proof_then_real_registration(self):
        from django.urls import reverse
        from leg_platform.accounts.tests.helpers import PASSWORD, VerifiedClient, code_for

        for role, previous in (("user", "password"), ("user", "app"), ("staff", "app"), ("system_admin", "app")):
            with self.subTest(role=role, previous=previous):
                self.previous_method(role, "app", f"switch.{previous}")
                if previous == "password":
                    AuthenticatorDevice.objects.filter(user=self.user).delete()
                self.client = VerifiedClient()
                self.client.force_login(self.user)
                self.client.post(reverse("accounts:login_method"), {"method": "passkey"}, secure=True)
                path = reverse("accounts:passkey_setup")
                denied = self.json_post("/passkeys/options/", {"purpose": "register"})
                self.assertEqual(denied.status_code, 400)
                if previous == "app":
                    proof = {"otp_token": code_for(AuthenticatorDevice.objects.get(user=self.user))}
                else:
                    proof = {"current_password": PASSWORD}
                self.assertEqual(self.client.post(path, proof, secure=True).status_code, 302)
                for index in range(1):
                    self.credential_id = secrets.token_bytes(32)
                    options = self.options("register")
                    response = self.json_post(path, {
                        "ceremony": options["ceremony"], "credential": self.registration(options),
                        "name": f"Synthetic switch key {index}",
                    })
                    self.assertEqual(response.status_code, 200)
                    self.user.refresh_from_db()
                    self.assertTrue(self.user.check_password(PASSWORD))
                    self.assertEqual(self.user.authentication_method, "password")
                data = {"activate": "yes"}
                self.assertEqual(self.client.post(path, data, secure=True).status_code, 302)
                self.user.refresh_from_db()
                self.assertEqual(self.user.authentication_method, "passkey")
                self.assertFalse(self.user.has_usable_password())
                self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())
                self.assertEqual(self.client.session[second_factor.PASSKEY_SESSION_KEY], self.user.pk)
                self.assertEqual(self.client.get(reverse("accounts:profile"), secure=True).status_code, 200)

    def test_ordinary_account_recovery_cannot_use_privileged_setup_pages(self):
        from django.urls import reverse

        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        self.recover()
        for path in ("/admin/auth/group/", "/admin/accounts/user/", "/management/settings/"):
            response = self.client.post(path, {"is_superuser": "on"}, secure=True)
            self.assertRedirects(response, reverse("accounts:login_method"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)

    def test_demoted_stale_staff_actor_cannot_request_recovery(self):
        from leg_platform.accounts import second_factor_reset

        actor = create_active_user("demoted.actor@example.org")
        actor.set_privileges(is_staff=True, is_superuser=False)
        stale_actor = User.objects.get(pk=actor.pk)
        actor.set_privileges(is_staff=False, is_superuser=False)
        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        with self.assertRaises(second_factor_reset.ResetRefused):
            second_factor_reset.request_link(stale_actor, self.user)
        self.assertFalse(MailJob.objects.exists())

    def test_staff_can_request_recovery_for_password_only_and_first_login_admin_targets(self):
        from leg_platform.accounts import second_factor_reset

        actor = create_active_user("active.recovery.staff@example.org")
        actor.set_privileges(is_staff=True, is_superuser=False)
        # Target eligibility includes an active password-only account. Actor
        # session verification remains a separate HTTP middleware requirement.
        second_factor_reset.request_link(actor, self.user)
        self.assertTrue(MailJob.objects.filter(
            kind=MailJob.Kind.TWO_FACTOR_RESET_LINK, account=self.user
        ).exists())
        self.user.set_privileges(is_staff=True, is_superuser=True)
        self.user.password_change_required = True
        self.user.save(update_fields=["password_change_required"])
        second_factor_reset.request_link(actor, self.user)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertFalse(self.user.passkeys.exists())
        self.assertEqual(MailJob.objects.filter(
            kind=MailJob.Kind.TWO_FACTOR_RESET_LINK, account=self.user
        ).count(), 2)

    def test_blocked_actor_and_target_are_rechecked_not_trusted_from_stale_instances(self):
        from leg_platform.accounts import second_factor_reset

        actor = create_active_user("blocked.actor@example.org")
        actor.set_privileges(is_staff=True, is_superuser=False)
        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        stale_actor = User.objects.get(pk=actor.pk)
        actor.set_status(User.Status.BLOCKED)
        with self.assertRaises(second_factor_reset.ResetRefused):
            second_factor_reset.request_link(stale_actor, self.user)
        actor.set_status(User.Status.ACTIVE)
        stale_target = User.objects.get(pk=self.user.pk)
        self.user.set_status(User.Status.BLOCKED)
        with self.assertRaises(second_factor_reset.ResetRefused):
            second_factor_reset.request_link(actor, stale_target)
        self.assertFalse(MailJob.objects.exists())

    def test_staff_without_permissions_can_recover_last_passkey_admin_but_csrf_is_required(self):
        from django.urls import reverse
        from leg_platform.accounts.tests.helpers import VerifiedClient

        self.privileged(system=True)
        self.assertEqual(User.objects.filter(is_superuser=True, is_active=True).count(), 1)
        actor = create_active_user("unprivileged.staff@example.org")
        actor.set_privileges(is_staff=True, is_superuser=False)
        self.assertFalse(actor.user_permissions.exists())
        strict = VerifiedClient(enforce_csrf_checks=True)
        strict.force_login(actor)
        path = reverse("admin:accounts_user_reset_second_factor", args=[self.user.pk])
        old_version = self.user.session_version
        self.assertEqual(strict.post(path, {"identity_verified": "on"}, secure=True).status_code, 403)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_RESET_LINK).exists())
        response = strict.get(path, secure=True)
        self.assertEqual(response.status_code, 200)
        from django.conf import settings
        csrf = strict.cookies[settings.CSRF_COOKIE_NAME].value
        response = strict.post(
            path, {"identity_verified": "on"}, secure=True,
            HTTP_X_CSRFTOKEN=csrf, HTTP_REFERER=f"https://testserver{path}",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_RESET_LINK, account=self.user).count(), 1
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        self.assertEqual(self.user.session_version, old_version)
        change = reverse("admin:accounts_user_change", args=[self.user.pk])
        self.assertEqual(strict.post(
            change, {"is_superuser": ""}, secure=True,
            HTTP_X_CSRFTOKEN=csrf, HTTP_REFERER=f"https://testserver{change}",
        ).status_code, 403)

    def test_staff_without_verified_second_factor_cannot_initiate_recovery(self):
        from django.urls import reverse

        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        actor = create_active_user("unverified.staff@example.org")
        actor.set_privileges(is_staff=True, is_superuser=False)
        AuthenticatorDevice.objects.create_device(actor, confirmed=True)
        self.client.force_login(actor)
        response = self.client.post(
            reverse("admin:accounts_user_reset_second_factor", args=[self.user.pk]),
            {"identity_verified": "on"}, secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MailJob.objects.exists())
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user, confirmed=True).exists())

    def test_expired_recovery_setup_proof_cannot_finish_password_or_passkey(self):
        from django.urls import reverse
        from leg_platform.accounts.tests.helpers import OTHER_PASSWORD

        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        self.recover()
        session = self.client.session
        grant = session[passkey_methods.SETUP_KEY]
        grant["started"] -= passkeys.LIFETIME.total_seconds()
        session[passkey_methods.SETUP_KEY] = grant
        session["chosen_password_method"] = "password"
        session.save()
        self.assertEqual(self.json_post("/passkeys/options/", {"purpose": "register"}).status_code, 400)
        response = self.client.post(reverse("accounts:method_password"), {
            "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD,
        }, secure=True)
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "recovery")
        self.assertFalse(self.user.has_usable_password())
        self.assertFalse(self.user.passkeys.exists())


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class PasskeyPromotionHttpAcceptanceTests(CeremonyFixture, TestCase):
    """One active passkey satisfies promotion; existing authorization remains mandatory."""

    def setUp(self):
        CeremonyFixture.setUp(self)
        from leg_platform.accounts.tests.helpers import VerifiedClient

        self.actor = create_active_user("promotion.administrator@example.org")
        self.actor.set_privileges(is_staff=True, is_superuser=True)
        self.client = VerifiedClient()
        self.client.force_login(self.actor)

    def target_keys(self, count):
        passkey_methods.allow_setup(self.request)
        for _ in range(count):
            self.credential_id = secrets.token_bytes(32)
            self.assertIsNotNone(self.stage())
        self.user = passkey_methods.activate(self.request)
        self.request.user = self.user
        self.assertEqual(self.user.passkeys.filter(active=True).count(), count)

    def change_path(self, user=None):
        from django.urls import reverse

        return reverse("admin:accounts_user_change", args=[(user or self.user).pk])

    def assert_no_statement_field(self):
        from django import forms

        response = self.client.get(self.change_path(), secure=True)
        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        model_fields = {field.name for field in User._meta.get_fields()}
        statements = [
            name for name, field in form.fields.items()
            if isinstance(field, forms.BooleanField) and name not in model_fields
        ]
        self.assertEqual(statements, [])

    def post_rights(self, **extra):
        data = {
            "first_name": self.user.first_name,
            "last_name": self.user.last_name,
            "salutation": self.user.salutation,
            "street": self.user.street,
            "house_number": self.user.house_number,
            "postal_code": self.user.postal_code_display,
            "town": self.user.town,
            "country": str(self.user.country),
            "is_staff": "on",
            "is_superuser": "on",
            **extra,
        }
        return self.client.post(self.change_path(), data, secure=True)

    def test_one_active_key_without_statement_grants_rights(self):
        self.target_keys(1)
        self.assert_no_statement_field()
        response = self.post_rights()
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff and self.user.is_superuser)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)

    def test_no_active_key_is_a_form_error_not_a_server_error(self):
        self.target_keys(1)
        self.user.passkeys.update(active=False)
        self.assert_no_statement_field()
        response = self.post_rights()
        self.assertEqual(response.status_code, 200)
        self.assertIn("is_staff", response.context["adminform"].form.errors)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff or self.user.is_superuser)

    def test_locked_save_rechecks_the_active_key_after_form_validation(self):
        from django.contrib import admin
        from django.core.exceptions import PermissionDenied
        from leg_platform.accounts.admin.users import UserAdmin

        self.target_keys(1)
        request = RequestFactory().get(self.change_path())
        request.user = self.actor
        model_admin = UserAdmin(User, admin.site)
        form = model_admin.get_form(request, self.user)(data={
            "first_name": self.user.first_name, "last_name": self.user.last_name,
            "salutation": self.user.salutation, "street": self.user.street,
            "house_number": self.user.house_number, "postal_code": self.user.postal_code_display,
            "town": self.user.town, "country": str(self.user.country),
            "is_staff": "on", "is_superuser": "on",
        }, instance=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        self.user.passkeys.update(active=False)
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(request, form.instance, form, change=True)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff or self.user.is_superuser)

    def test_posted_obsolete_identity_flags_are_ignored(self):
        self.target_keys(1)
        self.assert_no_statement_field()
        response = self.post_rights(
            passkey_devices_confirmed="on",
            two_devices_confirmed="on",
            **{"passkey_identity.two_devices_confirmed": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff and self.user.is_superuser)
        self.assertFalse(self.user.passkey_identity.two_devices_confirmed)

    def test_legacy_true_device_flag_does_not_gate_or_change_on_promotion(self):
        from leg_platform.accounts.models import PasskeyIdentity

        self.target_keys(1)
        PasskeyIdentity.objects.filter(user=self.user).update(two_devices_confirmed=True)
        self.assert_no_statement_field()
        self.assertEqual(self.post_rights().status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff and self.user.is_superuser)
        self.assertTrue(self.user.passkey_identity.two_devices_confirmed)

    def test_promotion_with_one_key_is_logged_and_ends_existing_sessions(self):
        from leg_platform.accounts.models import SecurityEvent

        self.target_keys(1)
        version = self.user.session_version
        self.assert_no_statement_field()
        response = self.post_rights()
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff and self.user.is_superuser)
        self.assertGreater(self.user.session_version, version)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        self.assertTrue(SecurityEvent.objects.filter(
            account_id=self.user.pk, actor_id=self.actor.pk,
        ).exists())

    def test_foreign_credentials_cannot_satisfy_the_target_active_key_requirement(self):
        self.target_keys(1)
        self.user.passkeys.update(active=False)
        self.assert_no_statement_field()
        target = self.user
        other = create_active_user("foreign.promotion.keys@example.org")
        self.user = other
        self.request.user = other
        self.target_keys(2)
        self.user = target
        self.request.user = target
        response = self.post_rights()
        self.assertEqual(response.status_code, 200)
        target.refresh_from_db()
        self.assertFalse(target.is_staff or target.is_superuser)

    def test_staff_forged_statement_cannot_promote_a_customer(self):
        from django.contrib.auth.models import Permission

        self.target_keys(2)
        self.assert_no_statement_field()
        staff = create_active_user("forging.promotion.staff@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        staff.user_permissions.set(Permission.objects.filter(
            content_type__app_label="accounts", codename__in=("view_user", "change_user")
        ))
        self.client.force_login(staff)
        response = self.post_rights(passkey_devices_confirmed="on")
        self.assertIn(response.status_code, (200, 302))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff or self.user.is_superuser)

    def test_staff_forged_statement_cannot_promote_their_own_account(self):
        self.target_keys(2)
        self.assert_no_statement_field()
        from django.contrib.auth.models import Permission

        staff = create_active_user("self.promotion.staff@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        staff.user_permissions.set(Permission.objects.filter(
            content_type__app_label="accounts", codename__in=("view_user", "change_user")
        ))
        self.client.force_login(staff)
        response = self.client.post(self.change_path(staff), {
            "is_staff": "on", "is_superuser": "on", "passkey_devices_confirmed": "on",
        }, secure=True)
        self.assertEqual(response.status_code, 403)
        staff.refresh_from_db()
        self.assertFalse(staff.is_superuser)


@tag("mariadb")
@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class MethodReturnRaceAcceptanceTests(CeremonyFixture, TransactionTestCase):
    """Two valid address links must never complete the same transition twice."""

    def test_delayed_old_link_issuance_serializes_with_replacement_and_old_link_is_invalid(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.contrib.auth.hashers import make_password
        from django.db import connection, connections
        from leg_platform.accounts import method_changes
        from leg_platform.accounts.tests.helpers import OTHER_PASSWORD

        if connection.vendor != "mysql":
            self.skipTest("Real row-lock race requires MariaDB.")
        self.activate()
        passkey_methods.allow_setup(self.request)
        method_changes.start(self.request, make_password(OTHER_PASSWORD), "password")
        replacement_hash = make_password("synthetic newer pending password value")
        user_id, address = self.user.pk, self.user.email
        issuing, replacement_query, replaced = Event(), Event(), Event()
        real_issue = VerificationToken.objects.issue
        observed = {}

        def pause_issuance(user, purpose, lifetime, **kwargs):
            # This hook pauses the worker only. Replacement runs through an
            # independent MariaDB connection, never a reentrant transaction.
            issuing.set()
            if not replacement_query.wait(timeout=10):
                raise AssertionError("Replacement did not reach its account lock query.")
            observed["replacement_finished_before_issuance"] = replaced.wait(timeout=1)
            observed["raw_token"] = real_issue(user, purpose, lifetime, **kwargs)
            return observed["raw_token"]

        def worker():
            connections.close_all()
            try:
                with patch.object(VerificationToken.objects, "issue", side_effect=pause_issuance):
                    return method_changes.process_return_link(user_id, address) is not None
            finally:
                connections.close_all()

        def replacement():
            connections.close_all()
            try:
                if not issuing.wait(timeout=10):
                    raise AssertionError("Worker did not reach token issuance.")
                user = User.objects.get(pk=user_id)
                request = RequestFactory().post("/sign-in-method/password/")
                request.user = user
                request.session = SessionStore()
                request.session[second_factor.SESSION_COUNTER_KEY] = secrets.token_urlsafe(32)

                def record_account_lock(execute, sql, params, many, context):
                    if "FOR UPDATE" in sql.upper() and User._meta.db_table in sql:
                        replacement_query.set()
                    return execute(sql, params, many, context)

                with connections["default"].execute_wrapper(record_account_lock):
                    pending = method_changes.start(request, replacement_hash, "password")
                replaced.set()
                return pending is not None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            worker_future = executor.submit(worker)
            replacement_future = executor.submit(replacement)
            self.assertTrue(worker_future.result(timeout=20))
            self.assertTrue(replacement_future.result(timeout=20))
        self.assertFalse(observed["replacement_finished_before_issuance"])
        self.assertIsNone(VerificationToken.objects.find_valid(
            observed["raw_token"], VerificationToken.Purpose.METHOD_RETURN
        ))
        from leg_platform.accounts.models import PendingSignInChange
        self.assertEqual(PendingSignInChange.objects.get(user=self.user).password_hash, replacement_hash)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertFalse(self.user.has_usable_password())
        self.assertTrue(self.user.passkeys.filter(active=True).exists())

    def test_competing_address_confirmations_have_exactly_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.contrib.auth.hashers import make_password
        from django.db import connection, connections
        from leg_platform.accounts import method_changes
        from leg_platform.accounts.tests.helpers import OTHER_PASSWORD

        if connection.vendor != "mysql":
            self.skipTest("Real row-lock race requires MariaDB.")
        self.activate()
        # Activation queues one notice; count only the transition under test.
        MailJob.objects.all().delete()
        passkey_methods.allow_setup(self.request)
        method_changes.start(self.request, make_password(OTHER_PASSWORD), "password")
        token_ids = []
        for _ in range(2):
            raw = VerificationToken.objects.issue(
                self.user, VerificationToken.Purpose.METHOD_RETURN, timedelta(hours=24)
            )
            token_ids.append(
                VerificationToken.objects.find_valid(raw, VerificationToken.Purpose.METHOD_RETURN).pk
            )
        barrier = Barrier(2)
        user_id = self.user.pk

        def complete(token_id):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                return method_changes.complete(user_id, token_id=token_id) is not None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(complete, token_ids))
        self.assertEqual(sorted(results), [False, True])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OTHER_PASSWORD))
        self.assertFalse(self.user.passkeys.exists())
        self.assertFalse(VerificationToken.objects.filter(user=self.user).exists())
        self.assertEqual(
            MailJob.objects.filter(
                kind=MailJob.Kind.SIGN_IN_CHANGED, address=self.user.email, account=None
            ).count(), 1
        )

    def test_competing_administrator_key_removals_preserve_one_active_credential(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connection, connections

        if connection.vendor != "mysql":
            self.skipTest("Real row-lock race requires MariaDB.")
        self.privileged(system=True)
        keys = list(self.user.passkeys.values_list("pk", flat=True))
        self.assertEqual(len(keys), 2)
        barrier = Barrier(2)
        user_id = self.user.pk

        def remove(key_id):
            connections.close_all()
            try:
                user = User.objects.get(pk=user_id)
                barrier.wait(timeout=10)
                return passkey_methods.remove(user, key_id)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(remove, keys[:2]))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)

    def test_first_admin_changes_password_before_one_real_passkey_unlocks_normal_access(self):
        from django.urls import reverse
        from leg_platform.accounts.tests.helpers import PASSWORD, OTHER_PASSWORD

        self.user = User.objects.create_superuser(
            "first.admin.ceremony@example.org", PASSWORD, password_change_required=True
        )
        self.request.user = self.user
        self.client.post(reverse("accounts:login"), {
            "username": self.user.email, "password": PASSWORD,
        }, secure=True)
        for path in (reverse("accounts:login_method"), reverse("accounts:passkey_setup"), "/admin/"):
            self.assertRedirects(
                self.client.get(path, secure=True), reverse("accounts:password_change"),
                fetch_redirect_response=False,
            )
        self.assertEqual(self.client.post(reverse("accounts:password_change"), {
            "current_password": PASSWORD,
            "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD,
        }, secure=True).status_code, 302)
        self.assertEqual(
            self.client.get("/admin/", secure=True)["Location"], reverse("accounts:login_method")
        )
        self.client.post(reverse("accounts:login_method"), {"method": "passkey"}, secure=True)
        path = reverse("accounts:passkey_setup")
        self.assertEqual(self.client.post(path, {
            "current_password": OTHER_PASSWORD,
        }, secure=True).status_code, 302)
        for index in range(1):
            self.credential_id = secrets.token_bytes(32)
            options = self.options("register")
            self.assertEqual(self.json_post(path, {
                "ceremony": options["ceremony"], "credential": self.registration(options),
                "name": f"First admin synthetic key {index}",
            }).status_code, 200)
        self.assertEqual(self.client.post(path, {
            "activate": "yes",
        }, secure=True).status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.password_change_required)
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        self.assertEqual(self.client.get("/admin/", secure=True).status_code, 200)
