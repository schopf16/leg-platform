"""The passkey wizard and the page "Passkeys" (owner specification KAPITEL-PASSKEY, chapters 8 to 10; D-106).

Real synthetic ES256 ceremonies through HTTP, the same fixtures as ``test_passkeys``. The security rules stay the ones of
``test_passkeys``; these tests add the screens: every step needs the login and the proof, the confirmation comes before the
password is deleted, abort and error change nothing, the last passkey is never offered for removal and removal ends all sessions.
"""

import html
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from leg_platform.accounts import passkey_methods, passkey_views, second_factor
from leg_platform.accounts.models import MailJob, Passkey, SecurityEvent, User
from leg_platform.accounts.tests import test_passkeys
from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.accounts.tests.test_passkeys import CeremonyFixture

SETUP = reverse("accounts:passkey_setup")
MANAGE = reverse("accounts:passkeys")
PROFILE = reverse("accounts:profile")
LOGIN = reverse("accounts:login")


class WizardFixture(CeremonyFixture):
    previous_method = test_passkeys.RecoveryAcceptanceMatrixTests.previous_method
    recover = test_passkeys.RecoveryAcceptanceMatrixTests.recover
    def get(self, path, client=None):
        return (client or self.client).get(path, secure=True)

    def post(self, path, data=None, client=None):
        return (client or self.client).post(path, data or {}, secure=True)

    def english(self):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

    def grant(self, client=None):
        """The proof was given (what ``SetupView`` or the flow "Change sign-in" does after a valid proof)."""
        client = client or self.client
        session = client.session
        session[passkey_methods.SETUP_KEY] = {
            "user": self.user.pk, "hash": self.user.get_session_auth_hash(), "started": time.time(),
        }
        session.save()

    def login(self, passkey=False):
        self.client.force_login(self.user)
        if passkey:
            session = self.client.session
            session[second_factor.PASSKEY_SESSION_KEY] = self.user.pk
            session.save()

    def create_key(self, name="Mein Handy", mode="this_device"):
        """The browser ceremony of the device step (options, then the JSON POST with the name of the hidden field)."""
        self.credential_id = secrets.token_bytes(32)
        options = self.options("register", mode=mode)
        return self.json_post(SETUP, {
            "ceremony": options["ceremony"], "credential": self.registration(options), "name": name,
        })

    def passkey_user(self, count=2):
        """An account with ``count`` active passkeys, set up through ``passkey_methods`` (named by the test)."""
        for index in range(count):
            self.credential_id = secrets.token_bytes(32)
            passkey_methods.allow_setup(self.request)
            self.assertIsNotNone(self.stage())
            self.user = passkey_methods.activate(self.request)
            self.request.user = self.user
        for index, key in enumerate(self.user.passkeys.order_by("pk")):
            key.name = f"Key {index + 1}"
            key.save(update_fields=["name"])
        return list(self.user.passkeys.order_by("pk"))


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class SetupWizardTests(WizardFixture, TestCase):
    def test_every_step_needs_the_login(self):
        anonymous = Client()
        for path in (
            SETUP, f"{SETUP}?step=name", f"{SETUP}?step=device", f"{SETUP}?step=confirm", f"{SETUP}?step=done",
            MANAGE, f"{MANAGE}?remove=1",
        ):
            with self.subTest(path=path):
                response = self.get(path, anonymous)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(LOGIN))
        for path, data in ((SETUP, {"cancel": "yes"}), (SETUP, {"activate": "yes"}), (f"{SETUP}?step=name", {"name": "x"}),
                           (MANAGE, {"credential": 1})):
            with self.subTest(post=path, data=data):
                response = self.post(path, data, anonymous)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(LOGIN))
        self.assertFalse(Passkey.objects.exists())

    def test_without_the_proof_every_step_shows_only_the_proof(self):
        self.login()
        self.english()
        for step in ("name", "device", "confirm", "unknown"):
            with self.subTest(step=step):
                response = self.get(f"{SETUP}?step={step}")
                self.assertContains(response, "Please confirm it is you")
                self.assertNotContains(response, "What should the passkey be called?")
                self.assertNotContains(response, "data-passkey-register")
        # The name is not taken without the proof and the session keeps no name.
        response = self.post(f"{SETUP}?step=name", {"name": "Mein Handy"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(passkey_views.NAME_KEY, self.client.session)
        # An expired proof is no proof.
        session = self.client.session
        session[passkey_methods.SETUP_KEY] = {
            "user": self.user.pk, "hash": self.user.get_session_auth_hash(), "started": time.time() - 301,
        }
        session.save()
        self.assertContains(self.get(f"{SETUP}?step=device"), "Please confirm it is you")
        # The proof of a wrong password opens nothing.
        self.assertEqual(self.post(SETUP, {"current_password": "wrong password entirely"}).status_code, 200)
        self.assertContains(self.get(f"{SETUP}?step=device"), "Please confirm it is you")

    def test_done_without_an_activation_leads_back_without_showing_anything(self):
        self.login()
        self.assertRedirects(self.get(f"{SETUP}?step=done"), PROFILE, fetch_redirect_response=False)
        self.grant()
        self.assertRedirects(self.get(f"{SETUP}?step=done"), PROFILE, fetch_redirect_response=False)

    def test_name_step_validates_and_the_device_step_needs_the_name(self):
        self.login()
        self.grant()
        self.english()
        name = self.get(SETUP)
        self.assertContains(name, "Step 1 of 3: Name")
        self.assertContains(name, "What should the passkey be called?")
        self.assertContains(name, "So you can find the passkey again later.")
        self.assertContains(name, 'placeholder="My phone"')
        self.assertNotContains(name, 'value="My phone"')
        for label in ("Continue", "Back", "Cancel"):
            self.assertContains(name, f">{label}</")
        # No name, no device step.
        self.assertContains(self.get(f"{SETUP}?step=device"), "What should the passkey be called?")
        for bad in ("", "   ", "x" * 81):
            with self.subTest(name=bad):
                response = self.post(f"{SETUP}?step=name", {"name": bad})
                self.assertEqual(response.status_code, 200)
                # Only the inline error at the field, no summary box.
                self.assertNotContains(response, "Please check your entries")
                self.assertNotContains(response, "error-summary")
                self.assertContains(response, 'id="id_name_error"')
                self.assertNotIn(passkey_views.NAME_KEY, self.client.session)
        response = self.post(f"{SETUP}?step=name", {"name": "  Mein Handy  "})
        self.assertRedirects(response, f"{SETUP}?step=device", fetch_redirect_response=False)
        self.assertEqual(self.client.session[passkey_views.NAME_KEY], "Mein Handy")
        device = self.get(f"{SETUP}?step=device")
        self.assertContains(device, "Step 2 of 3: Device")
        self.assertContains(device, "Where should the passkey be stored?")
        self.assertContains(device, "On this device")
        self.assertContains(device, "This computer or this device.")
        self.assertContains(device, "On another device")
        self.assertContains(device, "A QR code is shown to you. Photograph it with your phone.")
        self.assertContains(device, '<input type="hidden" name="name" value="Mein Handy">', html=True)
        self.assertContains(device, 'name="device_choice" id="id_device_this" value="this_device" checked')
        self.assertContains(device, 'value="another_device"')
        self.assertEqual(device.content.decode().count("data-passkey-register"), 1)
        # Coming back to the name keeps it.
        self.assertContains(self.get(f"{SETUP}?step=name"), 'value="Mein Handy"')

    def test_setup_pages_have_no_help_sections_and_no_device_claims(self):
        self.login()
        self.grant()
        self.english()
        self.post(f"{SETUP}?step=name", {"name": "Mein Handy"})
        pages = [self.get(f"{SETUP}?step=name").content.decode(), self.get(f"{SETUP}?step=device").content.decode()]
        self.assertEqual(self.create_key().status_code, 200)
        pages.append(self.get(SETUP).content.decode())
        for page in pages:
            for text in ("<details", "More help", "localhost", "ynchroni", "Credential", "credential", "phone or tablet"):
                self.assertNotIn(text, page)

    def test_first_passkey_path_removes_the_password_only_after_the_confirmation(self):
        other = Client()
        other.force_login(self.user)
        self.login()
        self.grant()
        self.english()
        version = self.user.session_version
        self.post(f"{SETUP}?step=name", {"name": "Mein Handy"})
        self.assertEqual(self.create_key(mode="another_device").status_code, 200)
        # Created, not activated: the password still works, nothing was switched, no notice was sent.
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "password")
        self.assertTrue(self.user.has_usable_password())
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 0)
        self.assertEqual(self.user.passkeys.filter(active=False).count(), 1)
        self.assertEqual(self.user.session_version, version)
        MailJob.objects.all().delete()
        confirm = self.get(SETUP)
        self.assertContains(confirm, "Step 3 of 3: Activate passkey")
        self.assertNotContains(confirm, "Step 3 of 3: Confirm")
        self.assertContains(confirm, "Use the passkey from now on?")
        self.assertContains(confirm, "The passkey “Mein Handy” is created.")
        self.assertContains(confirm, "Your password will be deleted. You do not need it afterwards.")
        self.assertContains(confirm, "All other sign-ins end. You receive a message at all your confirmed addresses.")
        self.assertContains(confirm, "Yes, sign in with the passkey from now on")
        # Going back to an earlier step does not offer a second creation beside the waiting key.
        self.assertContains(self.get(f"{SETUP}?step=device"), "Use the passkey from now on?")
        self.assertContains(self.get(f"{SETUP}?step=name"), "Use the passkey from now on?")
        self.assertEqual(self.post(SETUP, {"activate": "yes"}).status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "passkey")
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        self.assertGreater(self.user.session_version, version)
        self.assertEqual(other.get(PROFILE, secure=True).status_code, 302)
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.SIGN_IN_CHANGED, address=self.user.email).exists())
        done = self.get(f"{SETUP}?step=done")
        self.assertContains(done, "Done. The passkey “Mein Handy” is active.")
        self.assertContains(done, "Your password was deleted. All other sign-ins were ended. You can now sign in with the passkey.")
        self.assertContains(done, "Recommended:")
        self.assertContains(done, "Set up a second passkey on another device.")
        self.assertContains(done, "Set up another passkey")
        self.assertContains(done, "<h1 class=\"start-title\">Set up a passkey</h1>", html=False)
        self.assertContains(done, "Back to profile")
        # The grant is used up: a further setup needs the proof again.
        self.assertContains(self.get(f"{SETUP}?step=name"), "Confirm with your passkey that it is you.")
        self.assertContains(self.get(f"{SETUP}?step=name"), "data-passkey-proof")

    def test_hostile_passkey_names_are_escaped_on_every_screen(self):
        hostile = "<img src=x onerror=alert(1)>"
        escaped = "&lt;img src=x onerror=alert(1)&gt;"
        self.login()
        self.grant()
        self.english()
        self.post(f"{SETUP}?step=name", {"name": hostile})
        device = self.get(f"{SETUP}?step=device").content.decode()
        self.assertNotIn(hostile, device)
        self.assertIn(escaped, device)
        self.assertEqual(self.create_key(name=hostile).status_code, 200)
        confirm = self.get(SETUP).content.decode()
        self.assertNotIn(hostile, confirm)
        self.assertIn(escaped, confirm)
        self.assertEqual(self.post(SETUP, {"activate": "yes"}).status_code, 302)
        done = self.get(f"{SETUP}?step=done").content.decode()
        self.assertNotIn(hostile, done)
        self.assertIn(escaped, done)
        manage = self.get(MANAGE).content.decode()
        self.assertNotIn(hostile, manage)
        self.assertIn(escaped, manage)

    def test_the_confirmation_step_needs_a_created_key(self):
        self.login()
        self.grant()
        self.english()
        for step in ("confirm", "device", "name", "unknown", ""):
            with self.subTest(step=step):
                page = self.get(f"{SETUP}?step={step}")
                self.assertContains(page, "What should the passkey be called?")
                self.assertNotContains(page, "Use the passkey from now on?")

    def test_the_confirmation_of_an_account_with_a_number_code_names_both(self):
        from leg_platform.accounts.models import AuthenticatorDevice

        AuthenticatorDevice.objects.create_device(self.user, confirmed=True)
        self.login()
        self.grant()
        self.english()
        self.post(f"{SETUP}?step=name", {"name": "Mein Handy"})
        self.create_key()
        self.assertContains(
            self.get(SETUP), "Your password and your number code will be deleted. You need neither afterwards.",
        )

    def test_abort_and_error_change_nothing(self):
        self.login()
        self.grant()
        self.english()
        version = self.user.session_version
        self.post(f"{SETUP}?step=name", {"name": "Mein Handy"})
        # An error of the ceremony: nothing is created.
        options = self.options("register", mode="this_device")
        for body in (
            {"ceremony": options["ceremony"], "credential": {"type": "public-key"}, "name": "Mein Handy"},
            {"ceremony": "not-a-ceremony", "credential": {}, "name": "Mein Handy"},
            {"ceremony": options["ceremony"], "name": "Mein Handy"},
        ):
            self.assertEqual(self.json_post(SETUP, body).status_code, 400)
        self.assertFalse(Passkey.objects.filter(user=self.user).exists())
        # Activation without a created key switches nothing and says so.
        response = self.post(SETUP, {"activate": "yes"})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "password")
        self.assertTrue(self.user.has_usable_password())
        # Abort after the key was created: the key never becomes active, the grant and the name are gone.
        self.assertEqual(self.create_key().status_code, 200)
        self.assertEqual(Passkey.objects.filter(user=self.user, active=False).count(), 1)
        response = self.post(SETUP, {"cancel": "yes"})
        self.assertRedirects(response, PROFILE, fetch_redirect_response=False)
        self.assertFalse(Passkey.objects.filter(user=self.user).exists())
        self.assertNotIn(passkey_methods.SETUP_KEY, self.client.session)
        self.assertNotIn(passkey_views.NAME_KEY, self.client.session)
        self.assertEqual(self.post(SETUP, {"activate": "yes"}).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "password")
        self.assertTrue(self.user.has_usable_password())
        self.assertEqual(self.user.session_version, version)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.SIGN_IN_CHANGED).exists())

    def test_cancel_touches_only_the_unactivated_keys_of_this_session(self):
        self.login()
        self.grant()
        self.assertEqual(self.create_key(name="New key").status_code, 200)
        self.assertEqual(self.user.passkeys.filter(active=False).count(), 1)
        other_user = create_active_user("someone.else@example.org")
        foreign = Passkey.objects.create(
            user=other_user, credential_digest="f" * 64, credential_id=b"x", public_key=b"x", name="Foreign",
            setup_binding="not-this-session",
        )
        self.assertRedirects(self.post(SETUP, {"cancel": "yes"}), PROFILE, fetch_redirect_response=False)
        self.assertFalse(self.user.passkeys.exists())
        self.assertTrue(Passkey.objects.filter(pk=foreign.pk).exists())

    def test_further_passkey_is_activated_at_once_after_the_ceremony(self):
        first = self.passkey_user(count=1)[0]
        other = Client()
        other.force_login(self.user)
        session = other.session
        session[second_factor.PASSKEY_SESSION_KEY] = self.user.pk
        session.save()
        self.login(passkey=True)
        self.grant()
        self.english()
        version = self.user.session_version
        MailJob.objects.all().delete()
        page = self.get(SETUP)
        self.assertContains(page, "Set up another passkey")
        self.post(f"{SETUP}?step=name", {"name": "Laptop"})
        device = self.get(f"{SETUP}?step=device")
        # The other device is preselected for a further passkey.
        self.assertContains(device, 'id="id_device_other" value="another_device" checked')
        self.assertNotContains(device, 'value="this_device" checked')
        response = self.create_key(name="Laptop", mode="another_device")
        # The ceremony and the activation are one request: the answer leads to the result screen.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect"], f"{SETUP}?step=done")
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        self.assertEqual(self.user.passkeys.filter(active=False).count(), 0)
        self.assertTrue(self.user.passkeys.filter(pk=first.pk, active=True).exists())
        self.user.refresh_from_db()
        self.assertGreater(self.user.session_version, version)
        # Every other session ended, this one stays; every confirmed address got the notice.
        self.assertEqual(other.get(PROFILE, secure=True).status_code, 302)
        self.assertEqual(self.client.get(PROFILE, secure=True).status_code, 200)
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.SIGN_IN_CHANGED, address=self.user.email).exists())
        self.assertTrue(SecurityEvent.objects.filter(
            event=SecurityEvent.Event.PASSKEYS_CHANGED, account_id=self.user.pk).exists())
        # No confirmation screen: nothing to confirm any more.
        done = self.get(f"{SETUP}?step=done")
        self.assertContains(done, "Set up another passkey")
        self.assertContains(done, "Done. The passkey “Laptop” is active.")
        self.assertContains(done, "All other sign-ins were ended. You can now sign in with the passkey.")
        self.assertNotContains(done, "password was deleted")
        self.assertNotContains(done, "Recommended:")
        self.assertNotContains(done, f'href="{SETUP}"')
        self.assertContains(done, "Back to profile")
        self.assertNotContains(done, "Activate the passkey now?")
        # The grant is used up.
        self.assertContains(self.get(f"{SETUP}?step=name"), "Confirm with your passkey that it is you.")

    def test_a_failed_ceremony_of_a_further_passkey_changes_nothing(self):
        self.passkey_user(count=1)
        other = Client()
        other.force_login(self.user)
        session = other.session
        session[second_factor.PASSKEY_SESSION_KEY] = self.user.pk
        session.save()
        self.login(passkey=True)
        self.grant()
        version = self.user.session_version
        MailJob.objects.all().delete()
        options = self.options("register", mode="another_device")
        for body in (
            {"ceremony": options["ceremony"], "credential": {"type": "public-key"}, "name": "Laptop"},
            {"ceremony": "not-a-ceremony", "credential": {}, "name": "Laptop"},
            {"ceremony": options["ceremony"], "name": "Laptop"},
        ):
            self.assertEqual(self.json_post(SETUP, body).status_code, 400)
        self.assertEqual(self.user.passkeys.count(), 1)
        self.user.refresh_from_db()
        self.assertEqual(self.user.session_version, version)
        self.assertFalse(MailJob.objects.exists())
        self.assertEqual(other.get(PROFILE, secure=True).status_code, 200)

    def test_without_the_grant_nothing_is_created_or_activated(self):
        self.passkey_user(count=1)
        self.login(passkey=True)
        self.credential_id = secrets.token_bytes(32)
        self.assertEqual(self.json_post("/passkeys/options/", {"purpose": "register", "mode": "another_device"}).status_code, 400)
        self.assertEqual(self.json_post(SETUP, {"ceremony": "x", "credential": {}, "name": "Laptop"}).status_code, 400)
        self.assertEqual(self.user.passkeys.count(), 1)

    def test_a_forged_activation_or_a_direct_address_activates_nothing(self):
        first = self.passkey_user(count=1)[0]
        other = Client()
        other.force_login(self.user)
        self.login(passkey=True)
        self.grant()
        self.english()
        version = self.user.session_version
        MailJob.objects.all().delete()
        # No staged key: the forged POST is refused and changes nothing; the direct address is the name step.
        self.assertEqual(self.post(SETUP, {"activate": "yes"}).status_code, 200)
        self.assertContains(self.get(f"{SETUP}?step=confirm"), "What should the passkey be called?")
        self.assertRedirects(self.get(f"{SETUP}?step=done"), PROFILE, fetch_redirect_response=False)
        # A key staged by another session of the same account is not this session's: it is not activated either.
        Passkey.objects.create(
            user=self.user, credential_digest="c" * 64, credential_id=b"w", public_key=b"w", name="Foreign session",
            setup_binding="another-session",
        )
        self.assertEqual(self.post(SETUP, {"activate": "yes"}).status_code, 200)
        self.assertEqual(list(self.user.passkeys.filter(active=True).values_list("pk", flat=True)), [first.pk])
        self.user.refresh_from_db()
        self.assertEqual(self.user.session_version, version)
        self.assertFalse(MailJob.objects.exists())
        self.assertEqual(other.get(PROFILE, secure=True).status_code, 200)

    def test_if_the_activation_fails_the_key_stays_staged_and_the_confirmation_shows_it(self):
        self.passkey_user(count=1)
        self.login(passkey=True)
        self.grant()
        self.english()
        with patch("leg_platform.accounts.passkey_views.passkey_methods.activate", return_value=None):
            response = self.create_key(name="Laptop", mode="another_device")
        self.assertEqual(response.json()["redirect"], SETUP)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        confirm = self.get(SETUP)
        self.assertContains(confirm, "Activate the passkey now?")
        self.assertContains(confirm, "Step 3 of 3: Activate passkey")
        self.assertEqual(self.post(SETUP, {"activate": "yes"}).status_code, 302)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)

    def test_adding_a_passkey_asks_for_a_fresh_passkey_proof_first(self):
        self.passkey_user(count=1)
        self.login(passkey=True)
        self.english()
        for path in (SETUP, f"{SETUP}?step=name", f"{SETUP}?step=device", f"{SETUP}?step=confirm"):
            page = self.get(path)
            self.assertContains(page, "Confirm with your passkey that it is you.")
            self.assertNotContains(page, "Please confirm it is you")
            self.assertNotContains(page, "Confirm this change with your passkey")
            self.assertNotContains(page, "Step 1 of 3")
            self.assertContains(page, "data-passkey-proof")
            self.assertNotContains(page, "data-passkey-register")
        # A proof for another action does not open the setup; the proof of the setup page does.
        options = self.options("proof", action="/profile/email/")
        refused = self.post(SETUP, {"passkey_assertion": json.dumps({
            "ceremony": options["ceremony"], "credential": self.assertion(options)})})
        self.assertEqual(refused.status_code, 200)
        self.assertContains(self.get(f"{SETUP}?step=name"), "Confirm with your passkey that it is you.")
        options = self.options("proof", action=SETUP)
        opened = self.post(SETUP, {"passkey_assertion": json.dumps({
            "ceremony": options["ceremony"], "credential": self.assertion(options)})})
        self.assertRedirects(opened, SETUP, fetch_redirect_response=False)
        self.assertContains(self.get(SETUP), "What should the passkey be called?")

    def test_a_recovering_account_confirms_without_a_password_notice(self):
        self.previous_method("user", "app", "wizard")
        self.recover()
        self.english()
        # The recovery gave the grant: the wizard starts at the name.
        self.assertContains(self.get(SETUP), "What should the passkey be called?")
        self.assertEqual(self.post(f"{SETUP}?step=name", {"name": "Mein Handy"}).status_code, 302)
        self.assertEqual(self.create_key().status_code, 200)
        confirm = self.get(SETUP)
        self.assertContains(confirm, "Activate the passkey now?")
        self.assertContains(confirm, "Your previous sign-in methods no longer work.")
        self.assertNotContains(confirm, "will be deleted")
        self.assertEqual(self.post(SETUP, {"activate": "yes"}).status_code, 302)
        done = self.get(f"{SETUP}?step=done")
        self.assertContains(done, "All other sign-ins were ended.")
        self.assertNotContains(done, "password was deleted")
        self.assertContains(done, "Recommended:")

    def test_a_recovering_account_without_the_grant_is_logged_out_of_the_setup(self):
        for step in ("name", "device", "confirm", "done"):
            with self.subTest(step=step):
                self.previous_method("user", "app", f"nogrant.{step}")
                self.recover()
                session = self.client.session
                session.pop(passkey_methods.SETUP_KEY, None)
                session.save()
                self.assertRedirects(self.get(f"{SETUP}?step={step}"), LOGIN, fetch_redirect_response=False)
                self.assertEqual(self.get(PROFILE).status_code, 302)


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class ManagePasskeysTests(WizardFixture, TestCase):
    def test_overview_shows_cards_with_name_creation_and_last_use_only(self):
        keys = self.passkey_user(count=2)
        Passkey.objects.filter(pk=keys[0].pk).update(last_used_at=keys[0].created_at)
        self.login(passkey=True)
        self.english()
        page = self.get(MANAGE)
        self.assertContains(page, "With a passkey you sign in.")
        self.assertNotContains(page, "sign-in key")
        self.assertContains(page, "You have set up two passkeys.")
        for key in keys:
            self.assertContains(page, f"<h2 id=\"key-{key.pk}\">{key.name}</h2>", html=False)
            self.assertContains(page, f'href="{MANAGE}?remove={key.pk}"')
        self.assertContains(page, "Created on", count=2)
        self.assertContains(page, "Last used", count=2)
        self.assertContains(page, "Never used", count=1)
        # The menu marks "Profile" as the page the person is on.
        self.assertContains(page, 'class="frame-item is-active" href="/profile/" aria-current="page"')
        self.assertContains(page, "Remove “Key 1”")
        self.assertContains(page, "Set up another passkey")
        self.assertContains(page, "Back to profile")
        for text in ("Rename", "Synchroni", "More help", "<details", "<select", "current_password"):
            self.assertNotContains(page, text)
        # The overview changes nothing and offers no proof field.
        self.assertNotContains(page, "data-passkey-proof")
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)

    def test_one_passkey_is_never_offered_for_removal_and_the_page_says_why(self):
        key = self.passkey_user(count=1)[0]
        self.login(passkey=True)
        self.english()
        page = self.get(MANAGE)
        self.assertContains(page, "You have set up one passkey.")
        self.assertContains(page, "This passkey is your only one. Set up another passkey before you remove this one.")
        self.assertNotContains(page, "?remove=")
        self.assertNotContains(page, ">Remove<")
        # The address of the screen of the removal is no way around it.
        page = self.get(f"{MANAGE}?remove={key.pk}")
        self.assertNotContains(page, "Yes, remove and sign out")
        self.assertContains(page, "This passkey is your only one.")

    def test_the_removal_screen_names_the_key_and_opens_only_for_own_active_keys(self):
        keys = self.passkey_user(count=2)
        foreign_owner = create_active_user("foreign.owner@example.org")
        foreign = Passkey.objects.create(
            user=foreign_owner, credential_digest="e" * 64, credential_id=b"y", public_key=b"y", name="Foreign",
            active=True,
        )
        staged = Passkey.objects.create(
            user=self.user, credential_digest="d" * 64, credential_id=b"z", public_key=b"z", name="Staged", active=False,
        )
        self.login(passkey=True)
        self.english()
        page = self.get(f"{MANAGE}?remove={keys[1].pk}")
        self.assertContains(page, "Remove “Key 2”?")
        self.assertContains(page, "You can then no longer sign in with this passkey. All your sessions end.")
        self.assertContains(page, "For your safety, confirm with another passkey that you still have.")
        self.assertContains(page, "Yes, remove and sign out")
        self.assertContains(page, "No, keep it")
        self.assertContains(page, 'class="frame-item is-active" href="/profile/" aria-current="page"')
        self.assertContains(page, "data-passkey-proof")
        self.assertNotContains(page, "Confirm this change with your passkey")
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        for value in (foreign.pk, staged.pk, "abc", "", "0", "-1", "9" * 30, f"{keys[0].pk} OR 1=1"):
            with self.subTest(value=value):
                other = self.get(f"{MANAGE}?remove={value}")
                self.assertNotContains(other, "Yes, remove and sign out")
                self.assertContains(other, "You have set up two passkeys.")

    def test_removal_needs_a_fresh_passkey_proof_and_ends_all_sessions(self):
        keys = self.passkey_user(count=2)
        other = Client()
        other.force_login(self.user)
        session = other.session
        session[second_factor.PASSKEY_SESSION_KEY] = self.user.pk
        session.save()
        self.login(passkey=True)
        self.english()
        MailJob.objects.all().delete()
        # Without a proof, with the proof of another action and with no key: nothing is removed.
        self.assertEqual(self.post(MANAGE, {"credential": keys[0].pk}).status_code, 200)
        options = self.options("proof", action="/profile/email/")
        self.assertEqual(self.post(MANAGE, {"credential": keys[0].pk, "passkey_assertion": json.dumps({
            "ceremony": options["ceremony"], "credential": self.assertion(options)})}).status_code, 200)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        self.assertEqual(other.get(PROFILE, secure=True).status_code, 200)
        # The fresh proof for this address removes it and ends every session, this one included.
        options = self.options("proof", action=MANAGE)
        response = self.post(MANAGE, {"credential": keys[0].pk, "passkey_assertion": json.dumps({
            "ceremony": options["ceremony"], "credential": self.assertion(options)})})
        self.assertRedirects(response, LOGIN, fetch_redirect_response=False)
        # The login page says what happened, once, with the name the person chose (escaped by the template).
        login = self.get(LOGIN)
        self.assertContains(login, "The passkey was removed. You were signed out.")
        self.assertNotContains(login, "Key 1")
        self.assertNotContains(self.get(LOGIN), "was removed")
        self.assertEqual([key.pk for key in self.user.passkeys.filter(active=True)], [keys[1].pk])
        self.assertEqual(other.get(PROFILE, secure=True).status_code, 302)
        self.assertEqual(self.client.get(PROFILE, secure=True).status_code, 302)
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.SIGN_IN_CHANGED, address=self.user.email).exists())

    def test_the_removal_notice_on_the_login_page_names_no_key(self):
        keys = self.passkey_user(count=2)
        hostile = "<img src=x onerror=alert(1)>"
        Passkey.objects.filter(pk=keys[0].pk).update(name=hostile)
        self.login(passkey=True)
        self.english()
        options = self.options("proof", action=MANAGE)
        self.post(MANAGE, {"credential": keys[0].pk, "passkey_assertion": json.dumps({
            "ceremony": options["ceremony"], "credential": self.assertion(options)})})
        login = self.get(LOGIN).content.decode()
        self.assertIn("The passkey was removed. You were signed out.", login)
        self.assertNotIn("img src", login)
        self.assertNotIn("onerror", login)

    def test_the_last_passkey_cannot_be_removed_even_with_a_valid_proof(self):
        key = self.passkey_user(count=1)[0]
        self.login(passkey=True)
        self.english()
        version = self.user.session_version
        options = self.options("proof", action=MANAGE)
        response = self.post(MANAGE, {"credential": key.pk, "passkey_assertion": json.dumps({
            "ceremony": options["ceremony"], "credential": self.assertion(options)})})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This passkey is your only one.")
        self.user.refresh_from_db()
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)
        self.assertEqual(self.user.session_version, version)
        self.assertEqual(self.client.get(PROFILE, secure=True).status_code, 200)

    def test_nobody_removes_the_passkey_of_another_account(self):
        victim_keys = self.passkey_user(count=2)
        victim = self.user
        attacker = create_active_user("attacker@example.org")
        self.user, self.request.user = attacker, attacker
        self.request.session = type(self.request.session)()
        keys = self.passkey_user(count=2)
        self.login(passkey=True)
        options = self.options("proof", action=MANAGE)
        self.post(MANAGE, {"credential": victim_keys[0].pk, "passkey_assertion": json.dumps({
            "ceremony": options["ceremony"], "credential": self.assertion(options)})})
        self.assertEqual(victim.passkeys.filter(active=True).count(), 2)
        self.assertEqual(attacker.passkeys.filter(active=True).count(), 2)
        self.assertTrue(all(Passkey.objects.filter(pk=key.pk).exists() for key in keys))

    def test_a_password_account_has_no_passkey_page(self):
        self.client.force_login(self.user)
        self.assertRedirects(self.get(MANAGE), reverse("accounts:login_method"), fetch_redirect_response=False)
        self.assertRedirects(
            self.post(MANAGE, {"credential": 1}), reverse("accounts:login_method"), fetch_redirect_response=False,
        )

    def test_the_card_of_the_profile_offers_manage_and_one_small_link_only(self):
        self.passkey_user(count=1)
        self.login(passkey=True)
        self.english()
        card = self.get(PROFILE).content.decode().split('id="card-two-factor"')[1].split("</section>")[0]
        self.assertIn(">Passkey<", card.replace("\n", "").replace("  ", ""))
        self.assertIn("You sign in with a passkey.", card)
        self.assertEqual(card.count("btn-primary"), 1)
        self.assertIn(f'<a class="btn btn-primary" href="{MANAGE}">Manage passkeys</a>', card)
        self.assertIn(">Change sign-in</a>", card)
        self.assertIn(f'href="{SETUP}"', card)
        self.assertIn(">Set up another passkey</a>", card)
        for text in ("More help", "<details", "strongly recommend", "localhost", "App", "app ", "recovery codes"):
            self.assertNotIn(text, card)


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class WizardLanguageTests(WizardFixture, TestCase):
    """The texts of the new screens exist in the four languages, and none contains the letter that Swiss German does not use."""

    def test_every_text_of_the_screens_is_translated_and_has_no_sharp_s(self):
        from django.utils.translation import gettext, pgettext

        plain = (
            "So you can find the passkey again later.", "My phone", "Set up another passkey",
            "What should the passkey be called?", "Waiting for your confirmation …", "Did not work. Nothing was changed.",
            "Where should the passkey be stored?", "Device", "On this device", "This computer or this device.",
            "On another device", "A QR code is shown to you. Photograph it with your phone.", "Confirm in the window",
            "Your browser or password manager opens a window. Follow the instructions there.",
            "Activate the passkey now?", "Use the passkey from now on?",
            "Your existing passkeys and this session stay active.", "Yes, activate passkey",
            "Yes, sign in with the passkey from now on",
            "Your password was deleted. All other sign-ins were ended. You can now sign in with the passkey.",
            "All other sign-ins were ended. You can now sign in with the passkey.", "Recommended:",
            "Set up a second passkey on another device. Then you can sign in if a device breaks or is stolen.",
            "With a passkey you sign in.", "You can then no longer sign in with this passkey. All your sessions end.",
            "Activate passkey", "Confirm with your passkey that it is you.",
            "If this device already has a passkey for this portal, choose “On another device”.",
            "You sign in with a passkey.", "Never used", "The passkey was removed. You were signed out.",
            "Number code is set up.", "You have set up two passkeys.",
            "Your browser opens a window. Follow the instructions there.",
            "For your safety, confirm with another passkey that you still have. You do not need the device with this passkey for that.",
            "Yes, remove and sign out", "No, keep it", "Created on",
            "This passkey is your only one. Set up another passkey before you remove this one.", "Remove",
        )
        formatted = (
            "The passkey “%(name)s” is created.", "Done. The passkey “%(name)s” is active.",
            "Remove “%(name)s”?", "Remove “%(name)s”",
        )
        for language in ("de-ch", "fr", "it"):
            with translation.override(language):
                texts = [gettext(source) for source in plain + formatted]
                texts += [pgettext("passkey setup", "Try again"), pgettext("passkey card", "Last used")]
                texts += [gettext("You have set up one passkey."), gettext("You have set up %(counter)s passkeys.")]
                sources = list(plain + formatted) + ["Try again", "Last used", "You have set up one passkey.",
                                                     "You have set up %(counter)s passkeys."]
                for text, source in zip(texts, sources):
                    with self.subTest(language=language, source=source):
                        self.assertNotEqual(text, source)
                        self.assertNotIn("ß", text)
                for source in formatted:
                    self.assertIn("%(name)s", gettext(source))
                self.assertIn("%(counter)s", gettext("You have set up %(counter)s passkeys."))

    def test_the_screens_follow_the_language_of_the_request(self):
        self.passkey_user(count=2)
        self.login(passkey=True)
        expected = {
            "de-ch": ("Passkeys", "Erstellt am", "Entfernen", "Weiteren Passkey einrichten", "Zurück zum Profil"),
            "fr": ("Passkeys", "Créée le", "Supprimer", "Configurer une autre clé d’accès", "Retour au profil"),
            "it": ("Passkeys", "Creata il", "Eliminare", "Configurare un’altra passkey", "Tornare al profilo"),
        }
        for language, texts in expected.items():
            with self.subTest(language=language):
                self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
                page = self.get(MANAGE)
                for text in texts[1:]:
                    self.assertContains(page, text)
                self.assertNotContains(page, "ß")
                self.assertNotContains(page, "Created on")
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "de-ch"
        self.assertContains(self.get(MANAGE), "Mit einem Passkey melden Sie sich an.")
        self.assertNotContains(self.get(MANAGE), "Anmeldeschlüssel")
        self.assertContains(self.get(MANAGE), "Sie haben zwei Passkeys eingerichtet.")


class SetupDisplayScriptTests(SimpleTestCase):
    """``passkey-setup.js`` around the unchanged ceremony of ``passkeys.js`` (headless Chrome, stubbed credentials, no
    real authenticator): the chosen device reaches the registration mode, the window screen shows while the browser is
    asked, a failure shows the screen "Did not work" and nothing is submitted."""

    PAGE = """
<!doctype html><meta charset="utf-8">
<form id="passkey-create" data-passkey-register data-options="/options/" data-pending="Waiting for you"
  data-error="Did not work" data-device-error="Device was not saved">
  <input name="csrfmiddlewaretoken" value="synthetic-csrf">
  <input type="hidden" name="name" value="Synthetic phone">
  <section data-passkey-panel="device"><h2 tabindex="-1">Where</h2>
    <input type="radio" name="device_choice" id="this" value="this_device" checked>
    <input type="radio" name="device_choice" id="other" value="another_device">
    <button type="submit" name="mode" value="this_device">Continue</button></section>
  <section data-passkey-panel="waiting" hidden><h2 tabindex="-1">Waiting</h2><p role="status" data-passkey-status></p></section>
  <section data-passkey-panel="failed" hidden><h2 tabindex="-1">Did not work</h2><p data-passkey-error-text hidden></p></section>
</form><pre id="out"></pre>
<script>
const calls = [], atCreate = [], submissions = [], out = {};
let cancel = true, attachment = null;
window.fetch = async (url, request) => {
  const data = JSON.parse(request.body);
  calls.push({url, mode: data.mode});
  if (url !== "/options/") { submissions.push(data); return {ok: true, json: async () => ({redirect: "#saved"})}; }
  return {ok: true, json: async () => ({ceremony: "synthetic", options: {
    challenge: "AQIDBA", rp: {id: "localhost", name: "Test"},
    user: {id: "AQIDBA", name: "test@example.org", displayName: "Test"},
    pubKeyCredParams: [{type: "public-key", alg: -7}], excludeCredentials: [],
    authenticatorSelection: {residentKey: "required", userVerification: "required"}
  }})};
};
const panels = () => Object.fromEntries([...document.querySelectorAll("[data-passkey-panel]")]
  .map(panel => [panel.dataset.passkeyPanel, !panel.hidden]));
Object.defineProperty(navigator.credentials, "create", {value: async () => {
  atCreate.push(panels());
  if (cancel) throw new DOMException("Synthetic cancellation", "NotAllowedError");
  return {
    id: "AQIDBA", rawId: new Uint8Array([1, 2, 3, 4]).buffer, type: "public-key",
    response: {attestationObject: new ArrayBuffer(0), clientDataJSON: new ArrayBuffer(0)},
    getClientExtensionResults: () => ({}), authenticatorAttachment: attachment
  };
}});
</script>
"""
    SCRIPT = """
<script>
(async () => {
  try {
    const form = document.querySelector("form"), button = form.querySelector("button");
    const wait = () => new Promise(resolve => setTimeout(resolve, 30));
    const detail = form.querySelector("[data-passkey-error-text]");
    const reset = () => form.querySelectorAll("[data-passkey-panel]").forEach(
      panel => { panel.hidden = panel.dataset.passkeyPanel !== "device"; });
    out.initial = panels();
    out.runs = [];
    for (const [choice, cancelled, reported] of [
      ["other", true, null], ["this", true, null], ["other", false, "platform"], ["other", false, "cross-platform"],
    ]) {
      reset();
      cancel = cancelled; attachment = reported;
      document.getElementById(choice).checked = true;
      const before = calls.length, sent = submissions.length;
      form.requestSubmit(button);
      await wait();
      out.runs.push({
        choice, calls: calls.slice(before).map(call => call.mode), submitted: submissions.length - sent,
        panels: panels(), detail: detail.textContent, detailHidden: detail.hidden,
        focus: document.activeElement.closest("[data-passkey-panel]")?.dataset.passkeyPanel ?? null,
        buttonReady: !button.disabled,
      });
    }
    out.atCreate = atCreate;
  } catch (error) { out.failure = String(error); }
  document.getElementById("out").textContent = "RESULT" + JSON.stringify(out) + "END";
})();
</script>
"""

    def test_screens_follow_the_ceremony_and_the_device_choice_reaches_the_mode(self):
        chrome = next((
            found for name in ("chrome", "google-chrome", "chromium", "chromium-browser")
            if (found := shutil.which(name))
        ), None)
        if chrome is None:
            chrome = next((
                path for path in (
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                ) if Path(path).exists()
            ), None)
        if chrome is None:
            self.skipTest("no Chrome or Chromium found")
        static = Path(settings.BASE_DIR) / "leg_platform" / "static"
        page = (
            self.PAGE
            + f'<script src="{(static / "vendor/simplewebauthn/index.umd.min.js").as_uri()}"></script>'
            + f'<script src="{(static / "js/passkeys.js").as_uri()}"></script>'
            + f'<script src="{(static / "js/passkey-setup.js").as_uri()}"></script>'
            + self.SCRIPT
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "setup.html"
            path.write_text(page, encoding="utf-8")
            result = subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", f"--user-data-dir={folder}/profile",
                 "--allow-file-access-from-files", "--virtual-time-budget=10000", "--dump-dom", path.as_uri()],
                capture_output=True, timeout=60, check=False,
            )
        match = re.search(r'<pre id="out">RESULT(.*?)END</pre>', result.stdout.decode("utf-8", "replace"), re.S)
        self.assertIsNotNone(match, f"No browser result (exit {result.returncode}).")
        out = json.loads(html.unescape(match.group(1)))
        self.assertNotIn("failure", out)
        self.assertEqual(out["initial"], {"device": True, "waiting": False, "failed": False})
        one, two, three, four = out["runs"]
        # The radio decides the mode that passkeys.js reads from the button.
        self.assertEqual(one["calls"], ["another_device"])
        self.assertEqual(two["calls"], ["this_device"])
        # Cancelled in the browser window: the screen "Did not work", nothing submitted, the form usable again.
        for run in (one, two):
            self.assertEqual(run["panels"], {"device": False, "waiting": False, "failed": True})
            self.assertEqual(run["submitted"], 0)
            self.assertTrue(run["detailHidden"])
            self.assertEqual(run["focus"], "failed")
            self.assertTrue(run["buttonReady"])
        # A platform result for another device is not submitted and says why.
        self.assertEqual(three["panels"], {"device": False, "waiting": False, "failed": True})
        self.assertEqual(three["submitted"], 0)
        self.assertEqual(three["detail"], "Device was not saved")
        self.assertFalse(three["detailHidden"])
        # A genuine result is submitted; no failure screen appears (the page then follows the redirect).
        self.assertEqual(four["submitted"], 1)
        self.assertEqual(four["panels"], {"device": False, "waiting": True, "failed": False})
        # While the browser was asked, the screen "Confirm in the window" was the one shown.
        self.assertTrue(all(state == {"device": False, "waiting": True, "failed": False} for state in out["atCreate"]))
        self.assertEqual(len(out["atCreate"]), 4)
