"""The choice "Sign-in method" (D-106, "Guidance"): which methods an account is offered, the page in the profile card,
the start-page tile, the routing into the existing flows and the server-side validation of the posted method.

All methods are implemented; unsupported values and privileged password-only choices are refused.
"""

from unittest import mock

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import translation

from leg_platform.accounts import login_methods, second_factor
from leg_platform.accounts.forms import LoginMethodForm
from leg_platform.accounts.models import AuthenticatorDevice, HiddenTask, SessionCodeFailure, User
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    VerifiedClient,
    age_authentication,
    create_active_user,
)
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp, promote

CHOOSE_URL = reverse("accounts:login_method")
CARD_CHOOSE_URL = f"{reverse('accounts:profile')}?edit=two_factor&step=choose"
PROFILE_URL = reverse("accounts:profile")
HOME_URL = reverse("home")
SETUP_URL = reverse("accounts:two_factor_setup")
CONFIRM_URL = reverse("accounts:two_factor_confirm")
TWO_FACTOR_URL = reverse("accounts:two_factor")
LOGIN_URL = reverse("accounts:login")
PASSWORD_CHANGE_URL = reverse("accounts:password_change")
ENDED = "Aus Sicherheitsgründen wurden Sie ausgeloggt"

APP = login_methods.PASSWORD_AND_APP
PASSWORD_ONLY = login_methods.PASSWORD_ONLY
PASSKEY = login_methods.PASSKEY


def keys(user):
    return [method.key for method in login_methods.available(user)]


class AvailableMethodsTests(SimpleTestCase):
    """``available(user)`` decides the options; the templates and the form only follow it."""

    def test_a_user_may_choose_password_app_or_passkey(self):
        user = User(email="user@example.org")

        self.assertEqual(keys(user), [PASSWORD_ONLY, APP, PASSKEY])
        self.assertTrue(login_methods.has_choice(user))
        self.assertEqual(login_methods.recommended(user), APP)

    def test_staff_and_system_administrators_choose_app_or_passkey(self):
        for field in ("is_staff", "is_superuser"):
            with self.subTest(field=field):
                administrator = User(email="admin@example.org", **{field: True})

                self.assertEqual(keys(administrator), [APP, PASSKEY])
                self.assertTrue(login_methods.has_choice(administrator))

    def test_passkeys_are_offered_last_for_every_access_level(self):
        self.assertEqual(keys(User(email="user@example.org")), [PASSWORD_ONLY, APP, PASSKEY])
        administrator = User(email="admin@example.org", is_staff=True)
        self.assertEqual(keys(administrator), [APP, PASSKEY])
        self.assertTrue(login_methods.has_choice(administrator))

    def test_the_recommendation_is_the_app_for_users_and_the_passkey_for_administrators(self):
        self.assertEqual(login_methods.recommended(User(email="user@example.org")), APP)
        self.assertEqual(login_methods.recommended(User(email="admin@example.org", is_superuser=True)), PASSKEY)

    def test_the_administrator_card_names_the_two_devices(self):
        administrator = User(email="admin@example.org", is_staff=True)

        with translation.override("en"):
            notes = {method.key: str(method.note) for method in login_methods.available(administrator)}

        self.assertIn("two passkeys on two devices", notes[PASSKEY])

class LoginMethodFormTests(SimpleTestCase):
    def valid(self, method, user):
        return LoginMethodForm({"method": method}, user=user).is_valid()

    def test_only_a_method_that_is_offered_is_valid(self):
        user = User(email="user@example.org")

        self.assertTrue(self.valid(APP, user))
        self.assertTrue(self.valid(PASSWORD_ONLY, user))
        self.assertTrue(self.valid(PASSKEY, user))
        for forged in ("", "Passkey", "app ", "<script>", "x" * 500):
            with self.subTest(forged=forged):
                self.assertFalse(self.valid(forged, user))

    def test_an_administrator_cannot_choose_password_only(self):
        administrator = User(email="admin@example.org", is_staff=True)

        self.assertTrue(self.valid(APP, administrator))
        self.assertFalse(self.valid(PASSWORD_ONLY, administrator))

    def test_a_passkey_is_valid_only_while_the_setting_is_on(self):
        self.assertTrue(self.valid(PASSKEY, User(email="user@example.org")))
        self.assertFalse(self.valid(PASSWORD_ONLY, User(email="admin@example.org", is_staff=True)))

    def test_a_missing_choice_has_one_short_message(self):
        form = LoginMethodForm({}, user=User(email="user@example.org"))

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["method"], [str(LoginMethodForm.choose_message)])


@tag("mariadb")
class LoginMethodPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("active.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def page(self):
        response = self.client.get(CARD_CHOOSE_URL)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_card_offers_password_only_and_password_and_app_with_the_app_recommended(self):
        html = self.page()

        self.assertIn("Anmeldemethode", html)
        self.assertIn("Wie möchten Sie sich anmelden?", html)
        self.assertIn("Nur Passwort", html)
        self.assertIn("Passwort und Code aus einer App", html)
        self.assertEqual(html.count('name="method"'), 3)
        self.assertEqual(html.count("Empfohlen"), 1)
        self.assertIn("Unsicher? Wir empfehlen: Passwort und Code aus einer App.", html)
        self.assertIn("Was passt zu mir?", html)
        self.assertEqual(html.count('<li>Ich '), 2)

    def test_the_current_method_is_marked_and_preselected(self):
        html = self.page()

        self.assertRegex(html, r'value="password" checked')
        self.assertNotRegex(html, r'value="app" checked')
        self.assertEqual(html.count(">Aktuell</span>"), 1)

    def test_the_explainer_is_a_native_details_without_brands_and_without_passkeys(self):
        html = self.page()

        self.assertIn('<details class="help-details">', html)
        self.assertIn("<summary>Was ist eine Authenticator-App? Was ist ein Passkey?</summary>", html)
        self.assertIn("kostenlose App aus dem App-Store Ihres Smartphones", html)
        for brand in ("Google", "Microsoft", "Aegis"):
            self.assertNotIn(brand, html)

    def test_the_cards_are_one_fieldset_with_a_legend_and_native_radios_in_labels(self):
        html = self.page()

        self.assertIn('<fieldset class="choice-group">', html)
        self.assertIn('<legend class="choice-legend">Wie möchten Sie sich anmelden?</legend>', html)
        self.assertEqual(html.count('<label class="choice-card"'), 3)
        self.assertRegex(html, r'type="radio" name="method" id="id_method_0" value="password"')
        self.assertIn('aria-describedby="choice-app-benefit choice-app-need choice-app-lose"', html)

    def test_choice_pages_do_not_mutate_credentials(self):
        pages = {
            "choose": self.client.get(CARD_CHOOSE_URL),
            "profile": self.client.get(PROFILE_URL),
            "home": self.client.get(HOME_URL),
            "setup": self.client.get(f"{PROFILE_URL}?edit=two_factor&step=setup"),
            "empty": self.client.post(CHOOSE_URL, {}),
        }
        for name, response in pages.items():
            with self.subTest(page=name):
                self.assertEqual(response.status_code, 200)
        self.assertFalse(self.user.passkeys.exists())

    def test_the_other_languages_show_the_card_too(self):
        for code, text in (("fr", "Méthode de connexion"), ("it", "Metodo di accesso"), ("en", "Sign-in method")):
            with self.subTest(language=code):
                self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = code
                self.assertIn(text, self.client.get(CARD_CHOOSE_URL).content.decode())

    def test_the_view_state_leads_to_the_choice_instead_of_posting_the_setup(self):
        html = self.client.get(PROFILE_URL).content.decode()

        self.assertIn("Anmeldemethode", html)
        self.assertNotIn("Zwei-Faktor-Anmeldung (2FA)", html)
        self.assertRegex(html, r'href="[^"]*step=choose#card-two-factor">Ändern</a>')
        self.assertNotIn('action="/profile/two-factor/setup/"', html)

    def test_anonymous_visitors_are_sent_to_the_login(self):
        anonymous = Client()

        for response in (anonymous.get(CHOOSE_URL), anonymous.post(CHOOSE_URL, {"method": APP})):
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response["Location"].startswith(LOGIN_URL))
        self.assertTrue(anonymous.get(CARD_CHOOSE_URL)["Location"].startswith(LOGIN_URL))

    def test_the_post_needs_the_csrf_token(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)

        self.assertEqual(strict.post(CHOOSE_URL, {"method": APP}).status_code, 403)
        self.assertFalse(AuthenticatorDevice.objects.exists())


@tag("mariadb")
class LoginMethodRoutingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("active.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def test_choosing_the_app_starts_the_existing_setup(self):
        response = self.client.post(CHOOSE_URL, {"method": APP})

        self.assertRedirects(response, CONFIRM_URL, fetch_redirect_response=False)
        device = AuthenticatorDevice.objects.get()
        self.assertFalse(device.confirmed)
        self.assertEqual(device.user_id, self.user.pk)

    def test_choosing_the_app_after_the_five_minutes_asks_for_the_password_first(self):
        age_authentication(self.client)

        response = self.client.post(CHOOSE_URL, {"method": APP})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="current_password"')
        self.assertFalse(AuthenticatorDevice.objects.exists())

    def test_choosing_password_only_while_it_is_current_changes_nothing(self):
        response = self.client.post(CHOOSE_URL, {"method": PASSWORD_ONLY})

        self.assertRedirects(response, f"{PROFILE_URL}#card-two-factor", fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.exists())
        self.assertContains(self.client.get(PROFILE_URL), "Ihre Anmeldemethode bleibt unverändert.")

    def test_choosing_password_only_with_the_app_on_leads_to_the_existing_turn_off_step(self):
        user, device = create_user_with_totp("holder.person@example.org")
        client = VerifiedClient()
        client.force_login(user)

        response = client.post(CHOOSE_URL, {"method": PASSWORD_ONLY})

        self.assertRedirects(
            response, f"{PROFILE_URL}?edit=two_factor&step=disable#card-two-factor", fetch_redirect_response=False
        )
        # Nothing is turned off by the choice: the step asks for the proof.
        self.assertTrue(AuthenticatorDevice.objects.filter(pk=device.pk, confirmed=True).exists())

    def test_choosing_the_app_while_it_is_on_changes_nothing(self):
        user, device = create_user_with_totp("holder.person@example.org")
        client = VerifiedClient()
        client.force_login(user)

        response = client.post(CHOOSE_URL, {"method": APP})

        self.assertRedirects(response, f"{PROFILE_URL}#card-two-factor", fetch_redirect_response=False)
        self.assertEqual(AuthenticatorDevice.objects.filter(user=user).count(), 1)

    def test_a_forged_method_is_refused_with_the_error_summary_and_changes_nothing(self):
        for forged in ("Passkey", "", "unknown", "app\x00"):
            with self.subTest(forged=forged):
                response = self.client.post(CHOOSE_URL, {"method": forged})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Bitte prüfen Sie Ihre Angaben")
                self.assertContains(response, "Wählen Sie, wie Sie sich anmelden möchten.")
        self.assertFalse(AuthenticatorDevice.objects.exists())

    def test_a_get_with_the_step_in_the_address_shows_the_same_choice(self):
        self.assertContains(self.client.get(CHOOSE_URL), 'name="method"', count=3)

    def test_the_other_flows_stay_as_they_were(self):
        # The old button posted to the setup address: it still works (the card's setup state and the forced setup).
        response = self.client.post(SETUP_URL, {})

        self.assertRedirects(response, CONFIRM_URL, fetch_redirect_response=False)


@tag("mariadb")
class AdministratorLoginMethodTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator, cls.device = create_user_with_totp("staff.person@example.org")
        promote(cls.administrator)

    def verified(self):
        client = VerifiedClient()
        client.force_login(self.administrator)
        return client

    def test_privileged_accounts_have_a_choice_page(self):
        client = self.verified()

        self.assertContains(client.get(CHOOSE_URL), 'name="method"', count=2)
        self.assertRedirects(client.post(CHOOSE_URL, {"method": APP}), f"{PROFILE_URL}#card-two-factor", fetch_redirect_response=False)
        html = client.get(CARD_CHOOSE_URL).content.decode()
        self.assertIn('value="passkey"', html)
        self.assertNotIn('value="password"', html)

    def test_a_forged_password_only_does_not_turn_the_app_off(self):
        client = self.verified()

        response = client.post(CHOOSE_URL, {"method": PASSWORD_ONLY})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["choose_form"].errors)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.administrator, confirmed=True).exists())

    def test_the_card_shows_a_change_button_for_privileged_accounts(self):
        html = self.verified().get(PROFILE_URL).content.decode()

        self.assertIn(">Ändern</a>", html.split('id="card-two-factor"')[1].split("</section>")[0])

    def test_a_restricted_session_cannot_reach_the_choice_and_is_sent_to_the_setup(self):
        newcomer = create_active_user("new.staff@example.org")
        promote(newcomer)
        restricted = Client()
        restricted.force_login(newcomer)

        self.assertContains(restricted.get(CHOOSE_URL), 'value="passkey"')
        self.assertRedirects(restricted.get(CARD_CHOOSE_URL), CHOOSE_URL, fetch_redirect_response=False)
        self.assertRedirects(restricted.post(CHOOSE_URL, {"method": APP}), CONFIRM_URL, fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.get(user=newcomer).confirmed)

    def test_the_restricted_setup_shows_the_app_without_a_choice_or_a_passkey(self):
        newcomer = create_active_user("new.staff@example.org")
        promote(newcomer)
        restricted = Client()
        restricted.force_login(newcomer)

        html = restricted.get(SETUP_URL).content.decode()

        self.assertNotIn('name="method"', html)
        self.assertNotIn('name="passkey_assertion"', html)

    def test_with_passkeys_on_an_administrator_sees_the_app_and_the_passkey_but_not_password_only(self):
        client = self.verified()

        html = client.get(CARD_CHOOSE_URL).content.decode()

        self.assertEqual(html.count('name="method"'), 2)
        self.assertIn('value="app"', html)
        self.assertIn('value="passkey"', html)
        self.assertNotIn('value="password"', html)
        self.assertIn("Unsicher? Wir empfehlen einen Passkey auf zwei Geräten.", html)
        self.assertIn("Richten Sie zuerst zwei Passkeys auf zwei Geräten ein.", html)
        # A forged "password only" is refused; the app stays on.
        forged = client.post(CHOOSE_URL, {"method": PASSWORD_ONLY})
        self.assertEqual(forged.status_code, 200)
        self.assertContains(forged, "Bitte prüfen Sie Ihre Angaben")
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.administrator, confirmed=True).exists())


@tag("mariadb")
class PasskeysOnTests(TestCase):
    """The choice explains passkeys and routes into setup without itself changing credentials."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("active.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def test_a_user_sees_three_cards_and_the_passkey_explainer(self):
        html = self.client.get(CARD_CHOOSE_URL).content.decode()

        self.assertEqual(html.count('name="method"'), 3)
        self.assertLess(html.index('value="password"'), html.index('value="app"'))
        self.assertLess(html.index('value="app"'), html.index('value="passkey"'))
        self.assertIn("Was ist eine Authenticator-App? Was ist ein Passkey?", html)
        self.assertIn("Ihr Passwort und Ihre App werden gelöscht.", html)
        self.assertEqual(html.count("Empfohlen"), 1)

    def test_choosing_a_passkey_without_a_flow_is_not_found_and_changes_nothing(self):
        response = self.client.post(CHOOSE_URL, {"method": PASSKEY})

        self.assertRedirects(response, reverse("accounts:passkey_setup"), fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.exists())


@tag("mariadb")
class ForgedRequestTests(TestCase):
    """L-1, L-3: the choose URL adds no way around the rules of the flows it leads into."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("active.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def failures(self):
        return SessionCodeFailure.objects.get(account=self.user).failures

    def test_the_error_summary_links_to_an_existing_field(self):
        html = self.client.post(CHOOSE_URL, {"method": "unknown"}).content.decode()

        self.assertIn('<a href="#id_method_0">', html)
        self.assertIn('id="id_method_0"', html)
        self.assertNotIn('<a href="#">', html)

    def test_a_forged_password_on_the_choose_url_is_counted_and_changes_nothing(self):
        age_authentication(self.client)

        for expected in (1, 2):
            response = self.client.post(
                CHOOSE_URL, {"method": APP, "current_password": "not the password"}, REMOTE_ADDR="10.94.1.1"
            )

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Das Passwort stimmt nicht.")
            self.assertEqual(self.failures(), expected)
        self.assertFalse(AuthenticatorDevice.objects.exists())

    def test_the_right_password_on_the_choose_url_starts_the_setup_after_the_five_minutes(self):
        age_authentication(self.client)

        response = self.client.post(CHOOSE_URL, {"method": APP, "current_password": PASSWORD})

        self.assertRedirects(response, CONFIRM_URL, fetch_redirect_response=False)

    def test_five_wrong_passwords_on_the_choose_url_end_the_session_with_one_message(self):
        age_authentication(self.client)
        forged = {"method": APP, "current_password": "not the password"}
        for _attempt in range(second_factor.SESSION_CODE_LIMIT - 1):
            self.assertEqual(self.client.post(CHOOSE_URL, forged, REMOTE_ADDR="10.94.1.2").status_code, 200)

        last = self.client.post(CHOOSE_URL, forged, REMOTE_ADDR="10.94.1.2")

        self.assertRedirects(last, LOGIN_URL, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        # The notice is added once, although the choice runs the setup view on the same request.
        self.assertContains(self.client.get(LOGIN_URL), ENDED, count=1)
        self.assertNotContains(self.client.get(LOGIN_URL), ENDED)
        self.assertFalse(AuthenticatorDevice.objects.exists())

    def test_a_password_that_must_be_replaced_cannot_reach_the_choice(self):
        self.user.password_change_required = True
        self.user.save()

        for response in (
            self.client.get(CHOOSE_URL),
            self.client.post(CHOOSE_URL, {"method": APP}),
            self.client.get(CARD_CHOOSE_URL),
        ):
            self.assertRedirects(response, PASSWORD_CHANGE_URL, fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.exists())

    def test_a_blocked_or_deactivated_account_has_no_session_for_the_choice(self):
        for status in (User.Status.BLOCKED, User.Status.DEACTIVATED):
            with self.subTest(status=status):
                self.user.set_status(User.Status.ACTIVE)
                self.client.force_login(self.user)
                self.user.set_status(status)

                for response in (
                    self.client.get(CHOOSE_URL),
                    self.client.post(CHOOSE_URL, {"method": APP}),
                    self.client.get(CARD_CHOOSE_URL),
                ):
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith(LOGIN_URL))
                self.assertFalse(AuthenticatorDevice.objects.exists())


@tag("mariadb")
class PrivilegedWithoutSecondFactorTests(TestCase):
    """L-3: a staff user or system administrator without a second factor is never offered a way around the setup,
    with or without passkeys."""

    @classmethod
    def setUpTestData(cls):
        cls.newcomer = create_active_user("new.staff@example.org")
        promote(cls.newcomer)

    def setUp(self):
        self.restricted = Client()
        self.restricted.force_login(self.newcomer)

    def test_with_passkeys_on_the_choose_url_and_the_card_lead_to_the_setup(self):
        self.assertContains(self.restricted.get(CHOOSE_URL), 'name="method"', count=2)
        self.assertEqual(self.restricted.post(CHOOSE_URL, {"method": PASSWORD_ONLY}).status_code, 200)
        self.assertRedirects(self.restricted.post(CHOOSE_URL, {"method": PASSKEY}), reverse("accounts:passkey_setup"), fetch_redirect_response=False)
        self.assertRedirects(self.restricted.get(CARD_CHOOSE_URL), CHOOSE_URL, fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.newcomer).exists())

    def test_the_card_state_never_shows_the_choice_without_a_second_factor(self):
        from leg_platform.accounts.profile_views import two_factor_state

        self.assertEqual(two_factor_state(self.newcomer, "two_factor", "choose", {}), "choose")


@tag("mariadb")
class SignInTileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("active.person@example.org")

    def setUp(self):
        # The tile "Register building" would come first; this test is about the sign-in tile.
        HiddenTask.objects.create(user=self.user, task_key="register_building")
        self.client.force_login(self.user)

    def test_the_tile_is_an_optional_link_to_the_choice(self):
        html = self.client.get(HOME_URL).content.decode()
        tile = html.split('<article class="task-tile"')[1].split("</article>")[0]

        self.assertIn("Anmeldemethode wählen", tile)
        self.assertIn("Optional", tile)
        self.assertNotIn("status-open", tile)
        self.assertRegex(tile, r'<a class="btn btn-secondary-action" href="/profile/\?edit=two_factor&amp;step=choose#card-two-factor">Wählen</a>')
        self.assertNotIn("/profile/two-factor/setup/", tile)
        self.assertNotIn("Zwei-Faktor-Anmeldung", html)

    def test_the_tile_can_still_be_hidden_and_the_link_leads_to_the_choice(self):
        response = self.client.post(reverse("accounts:task_hide", args=("two_factor",)))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(HiddenTask.objects.filter(user=self.user, task_key="two_factor").exists())
        self.assertNotIn("Anmeldemethode wählen", self.client.get(HOME_URL).content.decode())
        self.assertEqual(self.client.get(CARD_CHOOSE_URL).status_code, 200)

    def test_the_tile_disappears_once_a_second_factor_exists(self):
        user, _device = create_user_with_totp("holder.person@example.org")
        client = VerifiedClient()
        client.force_login(user)

        self.assertNotIn("Anmeldemethode wählen", client.get(HOME_URL).content.decode())
