"""The flow "Change sign-in" of the profile (owner specification, profile chapters 1 to 4; D-106, D-062).

Card states, the proof (one field, never a recovery code), the choice (texts by state, server decides), and every path of
step 3: no change, number code on (the existing enrolment), number code off, passkey. Wrong or missing proof changes
nothing; later steps need the grant of the proof.
"""

import re

from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts import login_methods, passkey_methods, sign_in_change
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    MailJob,
    RecoveryCode,
    RecoveryCodeDevice,
    User,
)
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    VerifiedClient,
    code_for,
    create_active_user,
)
from leg_platform.accounts.tests.test_passkeys import CeremonyFixture
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp, promote, recovery_codes_of

FLOW = reverse("accounts:login_method")
PROFILE = reverse("accounts:profile")
PASSKEY_SETUP = reverse("accounts:passkey_setup")
CHOOSE = f"{FLOW}?step=choose"
SAME = f"{FLOW}?step=same"
DISABLE = f"{FLOW}?step=disable"
SCAN = f"{FLOW}?step=scan"
NUMBER = f"{FLOW}?step=number"
CANCEL = f"{FLOW}?step=cancel"
PROOF_STEP = f"{FLOW}?step=proof"
CARD = f"{PROFILE}#card-two-factor"


def prove(client, user=None, device=None):
    """Give the proof of step 1 (the code if the account has a device, else the password)."""
    data = {"otp_token": code_for(device)} if device is not None else {"current_password": PASSWORD}
    return client.post(FLOW, data)


def choose(client, method):
    return client.post(CHOOSE, {"method": method})


def page(response):
    return response.content.decode()


@tag("mariadb")
class CardStateTests(TestCase):
    """Chapter 1: heading, status, one sentence, one button; the state comes from the account."""

    @classmethod
    def setUpTestData(cls):
        cls.password_user = create_active_user("password.person@example.org")
        cls.code_user, cls.device = create_user_with_totp("code.person@example.org")

    def card(self, user):
        client = Client()
        client.force_login(user)
        html = page(client.get(PROFILE))
        return html.split('id="card-two-factor"')[1].split("</section>")[0]

    def test_a_password_account_shows_the_status_the_sentence_and_one_button(self):
        card = self.card(self.password_user)

        self.assertIn(">Anmeldung</h2>", card)
        self.assertIn("Nur Passwort", card)
        self.assertIn(
            "Schützen Sie Ihr Konto zusätzlich mit einem Zahlencode vom Handy. "
            "Oder ersetzen Sie das Passwort durch einen Passkey.",
            card,
        )
        self.assertRegex(card, rf'<a class="btn btn-primary" href="{FLOW}">Anmeldung ändern</a>')
        self.assertEqual(card.count("<a "), 1)

    def test_a_number_code_account_shows_its_own_status_and_text(self):
        card = self.card(self.code_user)

        self.assertIn("Passwort und Zahlencode", card)
        self.assertIn("Sie melden sich mit Ihrem Passwort und einem Zahlencode vom Handy an.", card)
        self.assertRegex(card, rf'href="{FLOW}">Anmeldung ändern</a>')
        self.assertNotIn("Nur Passwort", card)
        # The turn-off is a step of the flow now: no extra button on the card.
        self.assertNotIn("2FA deaktivieren", card)
        self.assertNotIn("step=disable", card)

    def test_the_old_choice_address_leads_into_the_flow_for_a_password_account(self):
        client = Client()
        client.force_login(self.password_user)

        response = client.get(f"{PROFILE}?edit=two_factor&step=choose")

        self.assertRedirects(response, FLOW, fetch_redirect_response=False)


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
@tag("mariadb")
class PasskeyCardTests(CeremonyFixture, TestCase):
    def test_a_passkey_account_shows_the_status_passkey_and_keeps_its_buttons(self):
        self.activate()
        self.signed_in()

        html = page(self.client.get(PROFILE))
        card = html.split('id="card-two-factor"')[1].split("</section>")[0]

        self.assertIn(">Anmeldung</h2>", card)
        self.assertRegex(
            card, r'profile-status"><span class="status-dot status-active" aria-hidden="true"></span>Passkey</span>'
        )
        self.assertNotIn("Nur Passwort", card)
        self.assertIn(reverse("accounts:passkeys"), card)
        # One sentence, one primary button, one small link named like the one of the password card.
        self.assertIn("Sie melden sich mit einem Passkey an.", card)
        self.assertEqual(card.count("btn-primary"), 1)
        self.assertIn(">Passkeys verwalten</a>", card)
        self.assertRegex(card, r'class="profile-link" href="[^"]*sign-in-method/">Anmeldung ändern</a>')
        for text in ("App", "Wiederherstellungscodes", "Weitere Hilfe", "Synchron"):
            self.assertNotIn(text, card)
        # The secondary button: further passkeys directly from the card (one primary, one secondary, one small link).
        self.assertIn(">Weiteren Passkey einrichten</a>", card)
        self.assertEqual(card.count("btn-secondary-action"), 1)
        # An account without a password has no button "Anmeldung ändern" in its password card.
        self.assertNotIn("Anmeldung ändern", html.split('id="card-password"')[1].split("</section>")[0])
        self.assertFalse(sign_in_change.uses_flow(self.user))
        self.assertFalse(sign_in_change.uses_flow(self.user))
        # The earlier choice page serves this account (not part of this flow).
        choice = page(self.client.get(FLOW))
        self.assertNotIn("Bitte bestätigen Sie sich", choice)
        self.assertIn('name="method"', choice)


@tag("mariadb")
class ProofStepTests(TestCase):
    """Chapter 2: only one field; a wrong, missing or recovery-code proof changes nothing and opens nothing."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("password.person@example.org")
        cls.code_user, cls.device = create_user_with_totp("code.person@example.org")

    def logged_in(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_a_password_account_sees_the_progress_the_heading_and_only_the_password_field(self):
        html = page(self.logged_in(self.user).get(FLOW))

        self.assertIn("Schritt 1 von 3: Passwort bestätigen", html)
        self.assertIn('<progress class="path-bar" max="3" value="1"', html)
        self.assertIn("Bitte bestätigen Sie sich", html)
        self.assertIn("Geben Sie zur Sicherheit noch einmal Ihr Passwort ein, bevor Sie die Anmeldung ändern.", html)
        self.assertIn('name="current_password"', html)
        self.assertNotIn('name="otp_token"', html)
        self.assertNotIn("Handy verloren?", html)
        self.assertIn(">Weiter</button>", html)
        self.assertIn(">Abbrechen</button>", html)
        self.assertNotIn(">Zurück</a>", html)

    def test_a_number_code_account_sees_only_the_code_field(self):
        html = page(self.logged_in(self.code_user).get(FLOW))

        self.assertIn("Schritt 1 von 3: Zahlencode bestätigen", html)
        self.assertIn("Geben Sie zur Sicherheit den Zahlencode ein, den Ihr Handy gerade anzeigt.", html)
        self.assertIn('name="otp_token"', html)
        self.assertIn('placeholder="000 000"', html)
        self.assertIn("Zahlencode vom Handy", html)
        self.assertNotIn('name="current_password"', html)
        # A recovery code is no proof here, so the lost phone has its own way.
        self.assertIn("Handy verloren?", html)

    def test_a_wrong_password_is_refused_and_opens_nothing(self):
        client = self.logged_in(self.user)

        response = client.post(FLOW, {"current_password": "not the password at all"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Das Passwort stimmt nicht.")
        body = response.content.decode()
        # The error stands inline at the field (described by it, focus on it), without a summary box.
        self.assertNotIn("error-summary", body)
        field = re.search(r'<input[^>]*name="current_password"[^>]*>', body).group(0)
        self.assertIn("id_current_password_error", field)
        self.assertIn("autofocus", field)
        self.assertNotIn(sign_in_change.SESSION_KEY, client.session)
        self.assertRedirects(client.get(CHOOSE), FLOW, fetch_redirect_response=False)

    def test_a_missing_proof_is_refused(self):
        client = self.logged_in(self.user)

        for data in ({}, {"current_password": ""}):
            with self.subTest(data=data):
                response = client.post(FLOW, data)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(sign_in_change.SESSION_KEY, client.session)

    def test_the_password_does_not_count_for_a_number_code_account(self):
        client = self.logged_in(self.code_user)

        response = client.post(FLOW, {"current_password": PASSWORD})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(sign_in_change.SESSION_KEY, client.session)

    def test_a_wrong_code_and_a_recovery_code_are_refused(self):
        client = self.logged_in(self.code_user)
        recovery = RecoveryCodeDevice.objects.get(user=self.code_user)
        unused = RecoveryCode.objects.filter(device=recovery).count()
        real_recovery_code = recovery_codes_of("code.person@example.org")[0]

        for token in ("000000", real_recovery_code, "abcd-efgh"):
            with self.subTest(token=token):
                response = client.post(FLOW, {"otp_token": token})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(sign_in_change.SESSION_KEY, client.session)
        # A recovery code is neither accepted nor used up here.
        self.assertEqual(RecoveryCode.objects.filter(device=recovery).count(), unused)

    def test_a_correct_proof_opens_the_choice(self):
        client = self.logged_in(self.user)

        response = prove(client)

        self.assertRedirects(response, CHOOSE, fetch_redirect_response=False)
        self.assertTrue(client.session[sign_in_change.SESSION_KEY])
        self.assertEqual(client.get(CHOOSE).status_code, 200)

    def test_a_correct_code_opens_the_choice(self):
        client = self.logged_in(self.code_user)

        response = prove(client, device=self.device)

        self.assertRedirects(response, CHOOSE, fetch_redirect_response=False)

    def test_every_later_step_needs_the_proof(self):
        client = self.logged_in(self.code_user)

        for url in (CHOOSE, SAME, DISABLE):
            with self.subTest(url=url):
                self.assertRedirects(client.get(url), FLOW, fetch_redirect_response=False)
        for url in (CHOOSE, DISABLE):
            with self.subTest(post=url):
                response = client.post(url, {"method": login_methods.PASSWORD_ONLY})
                self.assertRedirects(response, FLOW, fetch_redirect_response=False)
        # The choice "Passkey" has no screen of its own any more: without the proof it hands out no setup grant either.
        response = client.post(CHOOSE, {"method": login_methods.PASSKEY})
        self.assertRedirects(response, FLOW, fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.code_user, confirmed=True).exists())
        self.assertNotIn(passkey_methods.SETUP_KEY, client.session)

    def test_an_old_proof_does_not_count(self):
        client = self.logged_in(self.user)
        prove(client)
        session = client.session
        session[sign_in_change.SESSION_KEY]["at"] -= 3600
        session.save()

        response = client.get(CHOOSE)

        self.assertRedirects(response, FLOW, fetch_redirect_response=False)
        self.assertContains(client.get(FLOW), "Bitte bestätigen Sie sich noch einmal.")

    def test_a_proof_of_a_changed_account_does_not_count(self):
        client = self.logged_in(self.user)
        prove(client)
        session = client.session
        session[sign_in_change.SESSION_KEY]["hash"] = "something else"
        session.save()

        self.assertRedirects(client.get(CHOOSE), FLOW, fetch_redirect_response=False)

    def test_opening_the_first_step_again_ends_the_proof(self):
        client = self.logged_in(self.user)
        prove(client)

        client.get(FLOW)

        self.assertNotIn(sign_in_change.SESSION_KEY, client.session)

    def test_anonymous_visitors_and_a_missing_csrf_token_are_refused(self):
        anonymous = Client()
        self.assertTrue(anonymous.get(FLOW)["Location"].startswith(reverse("accounts:login")))
        self.assertTrue(anonymous.post(FLOW, {"current_password": PASSWORD})["Location"].startswith(reverse("accounts:login")))
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        for url in (FLOW, CHOOSE, DISABLE, NUMBER, CANCEL):
            with self.subTest(url=url):
                self.assertEqual(strict.post(url, {"current_password": PASSWORD}).status_code, 403)


@tag("mariadb")
class ChoiceStepTests(TestCase):
    """Chapter 3: the options come from the server; the texts depend on the state; nothing the spec leaves open shows."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("password.person@example.org")
        cls.code_user, cls.device = create_user_with_totp("code.person@example.org")

    def choice_page(self, user, device=None):
        client = Client()
        client.force_login(user)
        prove(client, device=device)
        return client, page(client.get(CHOOSE))

    def test_a_password_account_sees_three_options_with_the_two_questions(self):
        _, html = self.choice_page(self.user)

        self.assertIn("Schritt 2 von 3: Anmeldung wählen", html)
        self.assertIn('<progress class="path-bar" max="3" value="2"', html)
        self.assertIn("Wie möchten Sie sich anmelden?", html)
        self.assertEqual(html.count('name="method"'), 3)
        self.assertEqual(html.count("Was ist das?"), 3)
        self.assertEqual(html.count("Was passiert?"), 3)
        self.assertIn("Ein Passwort ist ein Geheimwort, das nur Sie kennen.", html)
        self.assertIn(
            "Sie geben bei jeder Anmeldung nur Ihr Passwort ein. Wer Ihr Passwort kennt, kann sich anmelden.", html
        )
        self.assertNotIn("Der Zahlencode wird abgeschaltet.", html)
        self.assertIn("Ihr Passwort wird dabei gelöscht. Sie brauchen es danach nicht mehr.", html)
        self.assertRegex(html, r'value="password" checked')
        self.assertEqual(html.count(">Aktuell</span>"), 1)
        self.assertIn(">Zurück</a>", html)
        self.assertIn(">Abbrechen</button>", html)

    def test_a_number_code_account_sees_the_texts_of_its_state(self):
        _, html = self.choice_page(self.code_user, self.device)

        self.assertRegex(html, r'value="app" checked')
        self.assertIn("Der Zahlencode wird abgeschaltet.", html)
        self.assertIn("Ihr Passwort und Ihr Zahlencode werden dabei gelöscht. Sie brauchen beide danach nicht mehr.", html)
        self.assertEqual(html.count(">Aktuell</span>"), 1)

    def test_the_titles_describe_what_the_person_has(self):
        _, html = self.choice_page(self.user)

        for title in ("Passwort", "Passwort und Zahlencode vom Handy", "Passkey"):
            self.assertIn(f'<span class="choice-title">{title}</span>', html)
        self.assertNotIn("Nur Passwort", html)

    def test_the_open_decisions_are_not_shown(self):
        _, html = self.choice_page(self.user)
        main = html.split('<div class="wizard">')[1]

        for text in ("Google", "Microsoft", "Sicherheit:", "tief", "hoch", "verlässt das Gerät", "nie auf"):
            self.assertNotIn(text, main)
        self.assertNotIn("ß", main)
        self.assertIn("Solche Programme heissen Authenticator-Apps.", main)
        self.assertIn("Ein Passkey, auch Anmeldeschlüssel genannt", main)

    def test_the_choice_shows_no_recommendation_tag(self):
        _, html = self.choice_page(self.user)

        self.assertNotIn("Empfohlen", html)
        self.assertNotIn("choice-tag-recommended", html)

    def test_back_from_the_choice_goes_to_the_profile_and_asks_for_no_new_proof(self):
        client, html = self.choice_page(self.user)

        main = html.split('<div class="wizard">')[1]
        self.assertIn(f'<a class="btn btn-secondary-action" href="{CARD}">Zurück</a>', main)
        self.assertEqual(client.get(CARD).status_code, 200)
        # The proof is still valid for the next step of this session: nothing was weakened or asked twice.
        self.assertEqual(client.get(CHOOSE).status_code, 200)

    def test_an_administrator_is_not_offered_the_password(self):
        administrator, device = create_user_with_totp("staff.person@example.org")
        promote(administrator)
        client = VerifiedClient()
        client.force_login(administrator)
        prove(client, device=device)

        html = page(client.get(CHOOSE))

        self.assertEqual(html.count('name="method"'), 2)
        self.assertNotIn('value="password"', html)
        self.assertRegex(html, r'value="app" checked')

    def test_a_forged_method_is_refused_and_changes_nothing(self):
        client, _ = self.choice_page(self.user)

        for forged in ("", "Passkey", "unknown", "app\x00", "recovery"):
            with self.subTest(forged=forged):
                response = client.post(CHOOSE, {"method": forged})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Bitte prüfen Sie Ihre Angaben")
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())

    def test_an_administrator_cannot_force_password_only(self):
        administrator, device = create_user_with_totp("staff.person@example.org")
        promote(administrator)
        client = VerifiedClient()
        client.force_login(administrator)
        prove(client, device=device)

        response = client.post(CHOOSE, {"method": login_methods.PASSWORD_ONLY})
        self.assertEqual(response.status_code, 200)
        client.post(DISABLE)

        self.assertTrue(AuthenticatorDevice.objects.filter(user=administrator, confirmed=True).exists())


@tag("mariadb")
class PathsOfStepThreeTests(TestCase):
    """Chapter 4: each choice (4a to 4e) and its effects."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("password.person@example.org")
        cls.code_user, cls.device = create_user_with_totp("code.person@example.org")

    def proved(self, user, device=None):
        client = Client()
        client.force_login(user)
        prove(client, device=device)
        return client

    # 4a and 4d: no change
    def test_4a_the_password_while_it_is_current_changes_nothing(self):
        client = self.proved(self.user)

        response = choose(client, login_methods.PASSWORD_ONLY)

        self.assertRedirects(response, SAME, fetch_redirect_response=False)
        html = page(client.get(SAME))
        self.assertIn("Keine Änderung", html)
        self.assertIn("Sie bleiben bei Ihrem Passwort. Nichts wird geändert.", html)
        self.assertIn(">Zurück zum Profil</button>", html)
        self.assertNotIn('class="progress-block"', html)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertFalse(MailJob.objects.exists())

    def test_4d_the_number_code_while_it_is_on_changes_nothing(self):
        client = self.proved(self.code_user, self.device)

        response = choose(client, login_methods.PASSWORD_AND_APP)

        self.assertRedirects(response, SAME, fetch_redirect_response=False)
        html = page(client.get(SAME))
        self.assertIn("Keine Änderung", html)
        self.assertIn("Sie melden sich weiter mit Ihrem Passwort und dem Zahlencode vom Handy an.", html)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.code_user, confirmed=True).exists())
        self.assertFalse(MailJob.objects.exists())

    def test_back_to_profile_ends_the_proof_and_leads_to_the_card(self):
        client = self.proved(self.user)
        choose(client, login_methods.PASSWORD_ONLY)

        response = client.post(CANCEL)

        self.assertRedirects(response, CARD, fetch_redirect_response=False)
        self.assertNotIn(sign_in_change.SESSION_KEY, client.session)
        self.assertRedirects(client.get(CHOOSE), FLOW, fetch_redirect_response=False)

    # 4b: the number code on
    def test_4b_the_number_code_is_set_up_in_two_screens_with_the_existing_enrolment(self):
        client = self.proved(self.user)

        response = choose(client, login_methods.PASSWORD_AND_APP)

        self.assertRedirects(response, SCAN, fetch_redirect_response=False)
        device = AuthenticatorDevice.objects.get(user=self.user)
        self.assertFalse(device.confirmed)
        scan = page(client.get(SCAN))
        self.assertIn("Zahlencode einrichten, Schritt 1 von 2", scan)
        self.assertIn('<progress class="path-bar" max="2" value="1"', scan)
        self.assertIn("Programm auf dem Handy einrichten", scan)
        self.assertIn("Scannen Sie mit dem Programm diesen Code:", scan)
        self.assertIn(f'src="{reverse("accounts:two_factor_qr")}"', scan)
        self.assertNotIn("Google", scan)
        self.assertNotIn("Microsoft", scan)
        self.assertEqual(client.get(reverse("accounts:two_factor_qr")).status_code, 200)
        number = page(client.get(NUMBER))
        self.assertIn("Zahlencode einrichten, Schritt 2 von 2", number)
        self.assertIn('<progress class="path-bar" max="2" value="2"', number)
        self.assertIn("Zahl eingeben", number)
        self.assertIn("Geben Sie die Zahl ein, die das Programm gerade anzeigt.", number)
        self.assertIn('placeholder="000 000"', number)
        self.assertIn(">Bestätigen</button>", number)
        self.assertIn(">Zurück</a>", number)

    def test_4b_a_wrong_number_changes_nothing_and_the_right_one_turns_the_code_on(self):
        client = self.proved(self.user)
        choose(client, login_methods.PASSWORD_AND_APP)
        device = AuthenticatorDevice.objects.get(user=self.user)

        for token in ("", "123", "000000"):
            with self.subTest(token=token):
                response = client.post(NUMBER, {"otp_token": token})
                self.assertEqual(response.status_code, 200)
                body = response.content.decode()
                # The error stands at the field (described by it, focus on it), without a summary box.
                self.assertNotIn("error-summary", body)
                self.assertIn('id="id_otp_token_error"', body)
                field = re.search(r'<input[^>]*name="otp_token"[^>]*>', body).group(0)
                self.assertIn("id_otp_token_error", field)
                self.assertIn("autofocus", field)
                device.refresh_from_db()
                self.assertFalse(device.confirmed)

        response = client.post(NUMBER, {"otp_token": code_for(device)})

        self.assertEqual(response.status_code, 200)
        device.refresh_from_db()
        self.assertTrue(device.confirmed)
        self.assertEqual(RecoveryCode.objects.filter(device__user=self.user).count(), 10)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_ENABLED).count(), 1)
        self.assertNotIn(sign_in_change.SESSION_KEY, client.session)
        # The recovery codes: a screen of the flow (kicker, heading of the flow, a step card), shown once, one "Fertig".
        html = page(response)
        main = html.split('<div class="wizard">')[1]
        self.assertIn("Mein Konto · Profil", main)
        self.assertIn("Anmeldung ändern", main)
        self.assertIn('class="step-card"', main)
        self.assertIn("Diese Codes brauchen Sie, wenn Sie keinen Zugang zu Ihrem Zahlencode vom Handy haben.", main)
        self.assertNotIn("Zugang zur App", main)
        self.assertNotIn("Schritt 2 von 2", main)
        self.assertNotIn("profile-card", main)
        self.assertEqual(main.count(">Fertig</button>"), 1)
        self.assertEqual(main.count('class="recovery-codes"'), 1)
        self.assertEqual(len(re.findall(r"<li>[a-z2-7]{4}-[a-z2-7]{4}</li>", main)), 10)
        # Shown only in this answer: the address of the flow has no such screen.
        self.assertRedirects(client.get(NUMBER), CHOOSE, fetch_redirect_response=False)
        unknown = client.get(FLOW + "?step=codes")
        self.assertContains(unknown, "Bitte bestätigen Sie sich")
        self.assertNotContains(unknown, "Diese Codes brauchen Sie")

    def test_4b_the_setup_screens_need_a_started_setup(self):
        client = self.proved(self.user)

        for url in (SCAN, NUMBER):
            with self.subTest(url=url):
                self.assertRedirects(client.get(url), CHOOSE, fetch_redirect_response=False)
        self.assertEqual(client.post(NUMBER, {"otp_token": "000000"}).status_code, 302)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())

    def test_4b_cancelling_leaves_nothing_active(self):
        client = self.proved(self.user)
        choose(client, login_methods.PASSWORD_AND_APP)

        response = client.post(CANCEL)

        self.assertRedirects(response, CARD, fetch_redirect_response=False)
        self.assertRedirects(client.get(SCAN), CHOOSE, fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user, confirmed=True).exists())

    # 4c: the number code off
    def test_4c_the_number_code_is_turned_off_only_after_the_explicit_yes(self):
        client = self.proved(self.code_user, self.device)
        other = Client()
        other.force_login(self.code_user)

        response = choose(client, login_methods.PASSWORD_ONLY)

        self.assertRedirects(response, DISABLE, fetch_redirect_response=False)
        html = page(client.get(DISABLE))
        self.assertIn("Zahlencode abschalten", html)
        self.assertIn("Danach melden Sie sich nur noch mit Ihrem Passwort an. Wir informieren alle Ihre bestätigten Adressen.", html)
        self.assertIn(">Ja, Zahlencode abschalten</button>", html)
        self.assertIn(">Zurück</a>", html)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.code_user, confirmed=True).exists())

        response = client.post(DISABLE)

        self.assertRedirects(response, CARD, fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.code_user).exists())
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_DISABLED).count(), 1)
        self.assertContains(client.get(PROFILE), "Der Zahlencode ist abgeschaltet.")
        self.assertNotContains(client.get(PROFILE), "2FA")
        # The other session ended, this one stays.
        self.assertEqual(client.get(PROFILE).status_code, 200)
        self.assertEqual(other.get(PROFILE).status_code, 302)

    def test_4c_without_the_proof_nothing_is_turned_off(self):
        client = Client()
        client.force_login(self.code_user)

        response = client.post(DISABLE)

        self.assertRedirects(response, FLOW, fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.code_user, confirmed=True).exists())
        self.assertFalse(MailJob.objects.exists())

    def test_4c_the_step_is_not_offered_to_an_account_without_a_number_code(self):
        client = self.proved(self.user)

        self.assertRedirects(client.get(DISABLE), CHOOSE, fetch_redirect_response=False)

    def test_4c_an_administrator_keeps_the_mandatory_code_even_with_the_proof(self):
        administrator, device = create_user_with_totp("staff.person@example.org")
        promote(administrator)
        client = VerifiedClient()
        client.force_login(administrator)
        prove(client, device=device)

        self.assertRedirects(client.get(DISABLE), CHOOSE, fetch_redirect_response=False)
        client.post(DISABLE)

        self.assertTrue(AuthenticatorDevice.objects.filter(user=administrator, confirmed=True).exists())
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_DISABLED).exists())

    # 4e: the passkey (D-112: no screen of its own; "Continue" goes straight to the passkey setup)
    def test_4e_the_passkey_goes_straight_to_the_setup_with_its_grant_and_changes_nothing_yet(self):
        client = self.proved(self.code_user, self.device)

        response = choose(client, login_methods.PASSKEY)

        self.assertRedirects(response, PASSKEY_SETUP, fetch_redirect_response=False)
        self.assertIn(passkey_methods.SETUP_KEY, client.session)
        self.assertNotIn(sign_in_change.SESSION_KEY, client.session)
        setup = client.get(PASSKEY_SETUP)
        self.assertEqual(setup.status_code, 200)
        self.assertTrue(setup.context["allowed"])
        self.assertContains(setup, "Wie soll der Passkey heissen?")
        user = User.objects.get(pk=self.code_user.pk)
        self.assertEqual(user.authentication_method, User.AuthenticationMethod.PASSWORD)
        self.assertTrue(user.has_usable_password())
        self.assertTrue(AuthenticatorDevice.objects.filter(user=user, confirmed=True).exists())
        self.assertFalse(MailJob.objects.exists())

    def test_4e_has_no_screen_any_more(self):
        client = self.proved(self.user)

        html = page(client.get(f"{FLOW}?step=passkey"))

        # An unknown step is the first step: the proof, nothing from the old warning screen.
        self.assertIn("Bitte bestätigen Sie sich", html)
        self.assertNotIn("Ja, Passkey einrichten", html)
        self.assertNotIn("Ihr Passwort wird gelöscht", html)
        self.assertNotIn(passkey_methods.SETUP_KEY, client.session)

    def test_4e_without_the_proof_there_is_no_setup_grant(self):
        client = Client()
        client.force_login(self.user)

        response = client.post(CHOOSE, {"method": login_methods.PASSKEY})

        self.assertRedirects(response, FLOW, fetch_redirect_response=False)
        self.assertNotIn(passkey_methods.SETUP_KEY, client.session)
        # The setup page itself still asks for the proof.
        self.assertFalse(client.get(PASSKEY_SETUP).context["allowed"])

    def test_the_proof_is_used_up_by_the_step_that_acts(self):
        client = self.proved(self.user)
        choose(client, login_methods.PASSKEY)

        self.assertRedirects(
            client.post(CHOOSE, {"method": login_methods.PASSKEY}), FLOW, fetch_redirect_response=False
        )


@tag("mariadb")
class FlowServesPasswordAccountsTests(TestCase):
    def test_the_flow_serves_password_accounts_only(self):
        user = create_active_user("password.person@example.org")

        self.assertTrue(sign_in_change.uses_flow(user))
        privileged = create_active_user("staff.person@example.org")
        promote(privileged)
        # A mandatory second factor that is not set up yet keeps the forced setup.
        self.assertFalse(sign_in_change.uses_flow(User.objects.get(pk=privileged.pk)))


@tag("mariadb")
class TextsTests(TestCase):
    """German texts, Swiss spelling, other languages."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("password.person@example.org")

    def test_no_screen_of_the_flow_contains_the_letter_sharp_s(self):
        client = Client()
        client.force_login(self.user)
        pages = [page(client.get(FLOW))]
        prove(client)
        pages.append(page(client.get(CHOOSE)))
        choose(client, login_methods.PASSWORD_AND_APP)
        pages += [page(client.get(SCAN)), page(client.get(NUMBER))]
        pages.append(page(client.get(PROFILE)))

        for html in pages:
            self.assertNotIn("ß", html)

    def test_the_other_languages_have_the_texts(self):
        from django.conf import settings

        client = Client()
        client.force_login(self.user)
        for code, text in (("fr", "Veuillez confirmer votre identité"), ("it", "Confermi la Sua identità"),
                           ("en", "Please confirm it is you")):
            with self.subTest(language=code):
                client.cookies[settings.LANGUAGE_COOKIE_NAME] = code
                self.assertIn(text, page(client.get(FLOW)))
