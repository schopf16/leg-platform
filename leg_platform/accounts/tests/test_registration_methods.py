"""Step 2 of the registration (D-106): the choice of the sign-in method and its setup before the account is active.

The account stays unverified until step 3. Everything the setup creates is staged and bound to the link in the
session; step 3 activates it together with the account.
"""

from django.core import mail
from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts import login_methods, passkeys, registration_methods, totp
from leg_platform.accounts.models import (
    AccountIntegrityError,
    AuthenticatorDevice,
    Passkey,
    PasskeyIdentity,
    RecoveryCodeDevice,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    choose_method,
    code_for,
    completion_data,
    password_data,
    verification_path,
)
from leg_platform.accounts.tests.test_passkeys import CeremonyFixture
from leg_platform.accounts.tests.test_registration import register
from leg_platform.accounts.views import REGISTRATION_PASSWORD_SESSION_KEY

METHOD_URL = reverse("accounts:registration_method")
PASSWORD_URL = reverse("accounts:registration_password")
APP_URL = reverse("accounts:registration_app")
APP_QR_URL = reverse("accounts:registration_app_qr")
PASSKEY_URL = reverse("accounts:registration_passkey")
OPTIONS_URL = reverse("accounts:registration_passkey_options")
CREATE_URL = reverse("accounts:registration_passkey_create")
COMPLETE_URL = reverse("accounts:registration_complete")
EMAIL = "new.person@example.org"


@tag("mariadb")
@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
class RegistrationMethodTests(CeremonyFixture, TestCase):
    # Completing the registration reads the address directory (advisory check, D-033).
    databases = {"default", "addresses"}

    def open(self):
        """Register, open the link; ``self.pending`` is the unverified account, ``self.token`` its link."""
        register(self.client)
        self.path = verification_path(mail.outbox[-1])
        self.client.get(self.path)
        self.token = VerificationToken.objects.get(purpose=VerificationToken.Purpose.REGISTRATION)
        self.pending = User.objects.get(email=EMAIL)

    def choose(self, method):
        return choose_method(self.client, method, self.token.pk)

    def staged_passkey(self):
        """Create the passkey through the two JSON entry points, like the script of the page."""
        options = self.json_post(OPTIONS_URL, {"purpose": "register", "mode": "this_device"})
        self.assertEqual(options.status_code, 200)
        options = options.json()
        return self.json_post(
            CREATE_URL, {"ceremony": options["ceremony"], "credential": self.registration(options), "name": "x"}
        )

    # The choice

    def test_the_link_leads_to_the_choice_of_the_method(self):
        register(self.client)
        opened = self.client.get(verification_path(mail.outbox[-1]))
        self.assertRedirects(opened, METHOD_URL, fetch_redirect_response=False)

        page = self.client.get(METHOD_URL)

        self.assertContains(page, "Schritt 2 von 3")
        for key in (login_methods.PASSWORD_ONLY, login_methods.PASSWORD_AND_APP, login_methods.PASSKEY):
            self.assertContains(page, f'value="{key}"')
        # The note of the card ("your password is deleted") speaks of an existing account.
        self.assertNotContains(page, "choice-passkey-note")
        self.assertContains(page, f'name="registration" value="{VerificationToken.objects.get().pk}"')

    def test_the_next_steps_need_a_chosen_method(self):
        self.open()

        for url in (PASSWORD_URL, APP_URL, PASSKEY_URL, COMPLETE_URL):
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), METHOD_URL, fetch_redirect_response=False)
        self.assertRedirects(
            self.client.post(COMPLETE_URL, completion_data(registration=self.token.pk)),
            METHOD_URL, fetch_redirect_response=False,
        )
        self.assertEqual(self.json_post(OPTIONS_URL, {"purpose": "register", "mode": "this_device"}).status_code, 400)
        self.assertEqual(self.json_post(CREATE_URL, {}).status_code, 400)

    def test_an_unoffered_method_is_refused_and_nothing_is_chosen(self):
        self.open()

        for value in ("magic", "", "recovery"):
            with self.subTest(value=value):
                response = self.client.post(METHOD_URL, {"method": value, "registration": self.token.pk})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(registration_methods.SESSION_KEY, self.client.session)

    def test_the_choice_belongs_to_the_link_of_the_form(self):
        self.open()

        response = self.client.post(METHOD_URL, {"method": "password", "registration": self.token.pk + 1})

        self.assertRedirects(response, reverse("accounts:registration_invalid"), fetch_redirect_response=False)
        self.assertNotIn(registration_methods.SESSION_KEY, self.client.session)

    def test_opening_the_link_again_starts_step_two_again_without_deleting_anything(self):
        self.open()
        self.choose("passkey")
        self.assertEqual(self.staged_passkey().status_code, 200)

        again = self.client.get(self.path)

        self.assertRedirects(again, METHOD_URL, fetch_redirect_response=False)
        self.assertNotIn(registration_methods.SESSION_KEY, self.client.session)
        # A mail scanner that opens the link must not wipe what the person staged.
        self.assertEqual(Passkey.objects.filter(user=self.pending).count(), 1)

    def test_step_three_keeps_only_what_belongs_to_the_chosen_method(self):
        # Two tabs of the same link: tab B completes with a password while tab A had confirmed an app and a passkey.
        self.open()
        other = Client()
        other.get(self.path)
        choose_method(other, "password", self.token.pk)
        other.post(PASSWORD_URL, password_data(registration=self.token.pk))
        self.choose("app")
        self.client.post(PASSWORD_URL, password_data(registration=self.token.pk))
        self.client.get(APP_URL)
        device = AuthenticatorDevice.objects.get(user=self.pending)
        self.client.post(APP_URL, {"otp_token": code_for(device), "registration": self.token.pk})
        self.assertTrue(AuthenticatorDevice.objects.get(user=self.pending).confirmed)
        Passkey.objects.create(
            user=self.pending, credential_digest="c" * 64, credential_id=b"c", public_key=b"k", sign_count=0,
            name="Left over", setup_binding="another session", backup_eligible=False, backed_up=False,
        )

        done = other.post(COMPLETE_URL, completion_data(registration=self.token.pk))

        self.assertRedirects(done, reverse("home"), fetch_redirect_response=False)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, User.Status.ACTIVE)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.pending).exists())
        self.assertFalse(Passkey.objects.filter(user=self.pending).exists())
        self.assertEqual(self.pending.authentication_method, User.AuthenticationMethod.PASSWORD)

    def test_a_passkey_account_keeps_no_device_and_no_key_of_another_binding(self):
        self.open()
        self.choose("passkey")
        self.assertEqual(self.staged_passkey().status_code, 200)
        AuthenticatorDevice.objects.create_device(self.pending, confirmed=True)
        Passkey.objects.create(
            user=self.pending, credential_digest="d" * 64, credential_id=b"d", public_key=b"k", sign_count=0,
            name="Foreign", setup_binding="another session", backup_eligible=False, backed_up=False,
        )

        done = self.client.post(COMPLETE_URL, completion_data(registration=self.token.pk))

        self.assertRedirects(done, reverse("home"), fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.pending).exists())
        self.assertEqual(list(Passkey.objects.values_list("name", "active")), [("Mein Passkey", True)])

    def test_a_new_choice_drops_what_the_earlier_one_staged(self):
        self.open()
        self.choose("app")
        self.client.post(PASSWORD_URL, password_data(registration=self.token.pk))
        self.client.get(APP_URL)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.pending).exists())

        self.choose("passkey")

        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.pending).exists())
        self.assertNotIn(REGISTRATION_PASSWORD_SESSION_KEY, self.client.session)
        self.assertEqual(self.staged_passkey().status_code, 200)
        self.assertEqual(Passkey.objects.filter(user=self.pending).count(), 1)

        self.choose("password")

        self.assertFalse(Passkey.objects.exists())

    def test_choosing_the_same_method_again_keeps_the_password(self):
        self.open()
        self.choose("password")
        self.client.post(PASSWORD_URL, password_data(registration=self.token.pk))

        self.choose("password")

        self.assertIn(REGISTRATION_PASSWORD_SESSION_KEY, self.client.session)

    # Password and app

    def test_password_and_app_confirms_the_device_and_activates_it_with_the_account(self):
        self.open()
        self.assertRedirects(self.choose("app"), PASSWORD_URL, fetch_redirect_response=False)
        # The app screen needs the password first.
        self.assertRedirects(self.client.get(APP_URL), PASSWORD_URL, fetch_redirect_response=False)
        self.assertRedirects(
            self.client.post(PASSWORD_URL, password_data(registration=self.token.pk)),
            APP_URL, fetch_redirect_response=False,
        )

        page = self.client.get(APP_URL)

        self.assertContains(page, "Schritt 2 von 3")
        self.assertContains(page, APP_QR_URL)
        device = AuthenticatorDevice.objects.get(user=self.pending)
        self.assertFalse(device.confirmed)
        qr = self.client.get(APP_QR_URL)
        self.assertEqual((qr.status_code, qr["Content-Type"]), (200, "image/svg+xml"))
        # Wrong code: the device stays unconfirmed, the details step stays closed.
        wrong = self.client.post(APP_URL, {"otp_token": "000000", "registration": self.token.pk})
        self.assertEqual(wrong.status_code, 200)
        device.refresh_from_db()
        self.assertFalse(device.confirmed)
        self.assertRedirects(self.client.get(COMPLETE_URL), APP_URL, fetch_redirect_response=False)
        right = self.client.post(APP_URL, {"otp_token": code_for(device), "registration": self.token.pk})
        self.assertRedirects(right, COMPLETE_URL, fetch_redirect_response=False)
        device.refresh_from_db()
        self.assertTrue(device.confirmed)
        # Confirmed, but the account is still not active: no login, no email, no recovery codes yet.
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, User.Status.UNVERIFIED)
        self.assertFalse(RecoveryCodeDevice.objects.filter(user=self.pending).exists())
        self.assertEqual(self.client.get(COMPLETE_URL).status_code, 200)

        done = self.client.post(COMPLETE_URL, completion_data(registration=self.token.pk))

        self.assertEqual(done.status_code, 200)
        self.assertContains(done, "Wiederherstellungscodes")
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, User.Status.ACTIVE)
        self.assertTrue(self.pending.has_usable_password())
        self.assertEqual(self.pending.authentication_method, User.AuthenticationMethod.PASSWORD)
        self.assertEqual(totp.remaining_recovery_codes(self.pending), 10)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.pending.pk))
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)

    def test_going_back_to_the_app_page_after_the_confirmation_changes_nothing(self):
        self.open()
        self.choose("app")
        self.client.post(PASSWORD_URL, password_data(registration=self.token.pk))
        self.client.get(APP_URL)
        device = AuthenticatorDevice.objects.get(user=self.pending)
        self.client.post(APP_URL, {"otp_token": code_for(device), "registration": self.token.pk})

        response = self.client.get(APP_URL)

        self.assertRedirects(response, COMPLETE_URL, fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.get(user=self.pending).confirmed)

    def test_the_qr_code_is_only_for_the_session_of_the_link(self):
        self.open()
        self.choose("app")
        self.client.post(PASSWORD_URL, password_data(registration=self.token.pk))
        self.client.get(APP_URL)

        self.assertEqual(Client().get(APP_QR_URL).status_code, 404)
        self.choose("password")
        self.assertEqual(self.client.get(APP_QR_URL).status_code, 404)

    def test_the_details_step_refuses_an_unconfirmed_device(self):
        self.open()
        self.choose("app")
        self.client.post(PASSWORD_URL, password_data(registration=self.token.pk))
        self.client.get(APP_URL)

        response = self.client.post(COMPLETE_URL, completion_data(registration=self.token.pk))

        self.assertRedirects(response, APP_URL, fetch_redirect_response=False)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, User.Status.UNVERIFIED)

    # Passkey

    def test_a_passkey_is_created_before_the_account_and_switched_on_with_it(self):
        self.open()
        self.assertRedirects(self.choose("passkey"), PASSKEY_URL, fetch_redirect_response=False)
        page = self.client.get(PASSKEY_URL)
        self.assertContains(page, "data-passkey-register")
        self.assertContains(page, OPTIONS_URL)
        self.assertContains(page, "Schritt 2 von 3")
        self.assertRedirects(self.client.get(COMPLETE_URL), PASSKEY_URL, fetch_redirect_response=False)

        created = self.staged_passkey()

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json(), {"redirect": COMPLETE_URL})
        key = Passkey.objects.get(user=self.pending)
        self.assertFalse(key.active)
        self.assertEqual(key.name, "Mein Passkey")
        self.assertEqual(key.setup_binding, passkeys.binding(self.client))
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, User.Status.UNVERIFIED)
        self.assertFalse(self.pending.is_active)
        # The key exists but opens nothing: the passkey login needs an active credential.
        self.assertFalse(Passkey.objects.filter(active=True).exists())
        self.assertRedirects(self.client.get(PASSKEY_URL), COMPLETE_URL, fetch_redirect_response=False)
        self.assertEqual(self.client.get(COMPLETE_URL).status_code, 200)

        done = self.client.post(COMPLETE_URL, completion_data(registration=self.token.pk))

        self.assertRedirects(done, reverse("home"), fetch_redirect_response=False)
        self.pending.refresh_from_db()
        key.refresh_from_db()
        self.assertEqual(self.pending.status, User.Status.ACTIVE)
        self.assertEqual(self.pending.authentication_method, User.AuthenticationMethod.PASSKEY)
        self.assertFalse(self.pending.has_usable_password())
        self.assertIsNotNone(self.pending.customer_number_id)
        self.assertTrue(key.active)
        self.assertEqual(key.setup_binding, "")
        self.assertEqual(self.client.session["_auth_user_id"], str(self.pending.pk))
        self.assertFalse(self.pending.is_staff or self.pending.is_superuser)
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)

    def test_the_passkey_entry_points_need_the_link_and_the_method(self):
        register(self.client)
        self.client.get(verification_path(mail.outbox[-1]))
        token = VerificationToken.objects.get()
        data = {"purpose": "register", "mode": "this_device"}

        # Anonymous, no link in the session.
        anonymous = Client()
        for url in (OPTIONS_URL, CREATE_URL):
            self.assertEqual(self.json_post(url, data, client=anonymous).status_code, 400)
        # The link, but the method password.
        choose_method(self.client, "password", token.pk)
        for url in (OPTIONS_URL, CREATE_URL):
            self.assertEqual(self.json_post(url, data).status_code, 400)
        self.assertFalse(PasskeyIdentity.objects.filter(user__email=EMAIL).exists())

    def test_the_passkey_entry_points_refuse_a_wrong_purpose_and_a_wrong_mode(self):
        self.open()
        self.choose("passkey")

        for data in (
            {"purpose": "login"}, {"purpose": "proof"}, {"purpose": "register"},
            {"purpose": "register", "mode": "any_device"},
        ):
            with self.subTest(data=data):
                self.assertEqual(self.json_post(OPTIONS_URL, data).status_code, 400)
        self.assertFalse(Passkey.objects.exists())

    def test_a_ceremony_of_another_session_is_refused(self):
        self.open()
        self.choose("passkey")
        options = self.json_post(OPTIONS_URL, {"purpose": "register", "mode": "this_device"}).json()
        stranger = Client()
        register(stranger, "other.person@example.org")
        stranger.get(verification_path(mail.outbox[-1]))
        other_token = VerificationToken.objects.get(user__email="other.person@example.org")
        choose_method(stranger, "passkey", other_token.pk)

        response = self.json_post(
            CREATE_URL, {"ceremony": options["ceremony"], "credential": self.registration(options)}, client=stranger
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Passkey.objects.exists())

    def test_the_details_step_needs_a_created_passkey(self):
        self.open()
        self.choose("passkey")

        response = self.client.post(COMPLETE_URL, completion_data(registration=self.token.pk))

        self.assertRedirects(response, PASSKEY_URL, fetch_redirect_response=False)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, User.Status.UNVERIFIED)

    # The account rules

    def test_the_account_rules_for_a_passkey_registration(self):
        self.open()
        user = self.pending

        with self.assertRaises(AccountIntegrityError):
            user.complete_registration(passkey_binding="nothing staged")
        with self.assertRaises(AccountIntegrityError):
            user.complete_registration(PASSWORD, passkey_binding="x")
        with self.assertRaises(AccountIntegrityError):
            user.complete_registration()
        user.refresh_from_db()
        self.assertEqual(
            (user.status, user.authentication_method), (User.Status.UNVERIFIED, User.AuthenticationMethod.PASSWORD)
        )
        # Only the keys of the binding are switched on.
        for binding, digest in (("mine", "a" * 64), ("theirs", "b" * 64)):
            Passkey.objects.create(
                user=user, credential_digest=digest, credential_id=digest.encode(), public_key=b"k", sign_count=0,
                name="k", setup_binding=binding, backup_eligible=False, backed_up=False,
            )

        user.complete_registration(passkey_binding="mine")

        self.assertEqual(
            list(Passkey.objects.order_by("credential_digest").values_list("active", flat=True)), [True, False]
        )
        self.assertEqual(user.authentication_method, User.AuthenticationMethod.PASSKEY)
        self.assertFalse(user.has_usable_password())
