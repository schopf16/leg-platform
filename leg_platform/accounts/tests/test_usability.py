"""Usability batch of the designer's review (D-054): wording, markup, forms and flows of the
member pages. Texts are asserted in Swiss German, the default language of the portal."""

import re

from django.contrib.auth import password_validation
from django.core import mail
from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import translation

from leg_platform.accounts import emails, totp
from leg_platform.accounts.login_protection import LOGIN_COOLOFF, LOGIN_FAILURE_LIMIT
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    EmailText,
    RecoveryCode,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    VerifiedClient,
    choose_method,
    code_for,
    complete_second_factor,
    completion_data,
    create_active_user,
    password_data,
    reset_request_data,
    run_mail_queue,
    verification_path,
)
from leg_platform.accounts.tests.test_registration import register
from leg_platform.accounts.tests.test_second_factor import (
    CODE_URL,
    RECOVERY_CODES,
    create_user_with_totp,
    log_in,
    promote,
    send_code,
)

LOGIN_URL = reverse("accounts:login")
PROFILE_URL = reverse("accounts:profile")
HOME_URL = reverse("home")
COMPLETE_URL = reverse("accounts:registration_complete")
PROFILE_EDIT_URL = reverse("accounts:profile_edit")
SETUP_URL = reverse("accounts:two_factor_setup")
CONFIRM_URL = reverse("accounts:two_factor_confirm")
ADDRESS_DATABASES = {"default", "addresses"}
SHARP_S = "\u00df"


def element(html, field_id):
    """The start tag of the form control with this id."""
    match = re.search(rf'<(?:input|select|textarea)\b[^>]*\bid="{field_id}"[^>]*>', html)
    assert match, f"No control with the id {field_id}."
    return match.group(0)


def label_of(html, field_id):
    match = re.search(rf'<label[^>]*\bfor="{field_id}"[^>]*>(.*?)</label>', html, re.S)
    assert match, f"No label for {field_id}."
    return " ".join(match.group(1).split())


@tag("mariadb")
class CompletionFormTests(TestCase):
    """The password step and the details step of the registration (D-058)."""

    # Completing the registration reads the address directory (advisory check, D-033).
    databases = ADDRESS_DATABASES

    def setUp(self):
        register(self.client)
        self.client.get(verification_path(mail.outbox[0]))
        self.token = VerificationToken.objects.get()
        choose_method(self.client, "password", self.token.pk)
        self.password_url = reverse("accounts:registration_password")
        self.password_page = self.client.get(self.password_url)
        self.client.post(self.password_url, password_data(registration=self.token.pk))
        self.page = self.client.get(COMPLETE_URL)
        self.html = self.page.content.decode()

    def test_the_details_step_has_the_fields_of_the_package_side_by_side(self):
        for label, field_id in (
            ("Vorname", "id_first_name"),
            ("Nachname", "id_last_name"),
            ("Strasse", "id_street"),
            ("Hausnummer", "id_house_number"),
            ("PLZ", "id_postal_code"),
            ("Ort", "id_town"),
        ):
            with self.subTest(field=field_id):
                self.assertEqual(label_of(self.html, field_id), label)
        # Pairs, also on a phone: the first two fields, the street and the house number, the
        # postal code and the town.
        self.assertEqual(self.html.count('class="field-pair'), 3)
        self.assertIn("field-pair-street", self.html)
        self.assertIn("field-pair-locality", self.html)
        self.assertIn('inputmode="numeric"', element(self.html, "id_postal_code"))
        self.assertIn('autocomplete="off"', element(self.html, "id_house_number"))
        # The fields that only the profile has are not asked at the registration (K15).
        for field_id in ("id_salutation", "id_organisation_name", "id_address_line_1", "id_phone_number"):
            self.assertNotIn(f'id="{field_id}"', self.html)
        # The address suggestions are offered here too (D-066): once, under the street input.
        self.assertEqual(self.html.count("data-address-directory"), 1)
        self.assertLess(self.html.index(' id="id_street"'), self.html.index("data-address-directory"))
        self.assertLess(self.html.index("data-address-directory"), self.html.index(' id="id_house_number"'))
        self.assertLess(self.html.index(' id="id_town"'), self.html.index("data-address-notice"))

    def test_the_password_step_has_the_username_the_rules_and_buttons_that_show_the_password(self):
        html = self.password_page.content.decode()

        self.assertRegex(element(html, "id_username"), r'autocomplete="username"')
        self.assertIn("E-Mail-Adresse (Benutzername)", html)
        self.assertRegex(element(html, "id_username"), r"\breadonly\b")
        # The rules are a list of the policy; Django's list of the validators is not shown.
        self.assertIn("Mindestens 15 Zeichen.", html)
        with translation.override("de-ch"):
            for text in password_validation.password_validators_help_texts():
                self.assertNotIn(text, html)
        # The buttons are hidden until the script shows them, so the fields work without it.
        for field_id in ("id_password1", "id_password2"):
            self.assertRegex(
                html,
                rf'<button type="button"[^>]*data-password-toggle hidden[^>]*aria-controls="{field_id}"',
            )
            self.assertIn('type="password"', element(html, field_id))
        self.assertIn("js/password-toggle.js", html)
        self.assertIn('aria-label="Passwort anzeigen"', html)

    def test_an_invalid_password_step_has_an_error_summary_and_specific_messages(self):
        data = password_data("", registration=self.token.pk, password2="")
        self.client.get(self.password_url)

        response = self.client.post(self.password_url, data)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        for field_id in ("id_password1", "id_password2"):
            self.assertIn(f'href="#{field_id}"', html)
        for text in ("Geben Sie ein Passwort ein.", "Geben Sie das Passwort noch einmal ein."):
            self.assertIn(text, html)
        self.assertNotIn("Dieses Feld ist zwingend erforderlich", html)

    def test_an_invalid_form_has_an_error_summary_and_specific_messages(self):
        data = completion_data(registration=self.token.pk, town="", postal_code="")

        response = self.client.post(COMPLETE_URL, data)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertRegex(html, r'<div class="alert alert-danger error-summary" role="alert" tabindex="-1" autofocus>')
        self.assertIn("Bitte prüfen Sie Ihre Angaben", html)
        for field_id in ("id_town", "id_postal_code"):
            self.assertIn(f'href="#{field_id}"', html)
        for text in ("Geben Sie einen Ort ein.", "Geben Sie eine Postleitzahl ein."):
            self.assertIn(text, html)
        self.assertNotIn("Dieses Feld ist zwingend erforderlich", html)
        # The error is tied to its field for assistive technology.
        self.assertIn('aria-describedby="id_town_error"', element(html, "id_town"))
        self.assertIn('id="id_town_error"', html)
        self.assertIn('aria-invalid="true"', element(html, "id_town"))

    def test_the_names_and_the_street_are_required(self):
        data = completion_data(
            registration=self.token.pk, first_name="", last_name="", street=""
        )

        html = self.client.post(COMPLETE_URL, data).content.decode()

        for text in (
            "Geben Sie Ihren Vornamen ein.",
            "Geben Sie Ihren Nachnamen ein.",
            "Geben Sie Ihre Strasse ein.",
        ):
            self.assertIn(text, html)
        self.assertEqual(User.objects.get().status, User.Status.UNVERIFIED)

    def test_the_house_number_may_stay_empty(self):
        data = completion_data(registration=self.token.pk, house_number="")

        self.assertEqual(self.client.post(COMPLETE_URL, data).status_code, 302)

    def test_the_completion_leads_to_the_start_page_without_the_old_login_message(self):
        response = self.client.post(COMPLETE_URL, completion_data(registration=self.token.pk), follow=True)

        # The person is logged in by the completion (D-055): no "log in now" message.
        self.assertEqual(response.redirect_chain[-1][0], reverse("home"))
        self.assertNotContains(response, "Melden Sie sich mit Ihrer E-Mail-Adresse und Ihrem Passwort an.")
        self.assertNotContains(response, "Ihr Konto ist aktiv.")
        self.assertContains(response, "Guten Tag, Anna Example")

    def test_the_page_has_no_inline_code_and_no_data_image(self):
        self.assertNotRegex(self.html, r"(?i)\sstyle\s*=")
        self.assertNotIn("data:image", self.html)


@tag("mariadb")
class ProfileFormTests(TestCase):
    databases = ADDRESS_DATABASES

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user(phone_number="+41311234567")

    def setUp(self):
        self.client.force_login(self.user)

    def test_the_card_marks_optional_fields_and_keeps_the_pairs_and_the_directory(self):
        html = self.client.get(PROFILE_EDIT_URL).content.decode()

        for field_id in (
            "id_salutation",
            "id_organisation_name",
            "id_address_line_1",
            "id_post_office_box_number",
            "id_phone_number",
        ):
            with self.subTest(optional=field_id):
                self.assertIn("(freiwillig)", label_of(html, field_id))
        for field_id in ("id_first_name", "id_last_name", "id_street", "id_postal_code", "id_town"):
            with self.subTest(required=field_id):
                self.assertNotIn("(freiwillig)", label_of(html, field_id))
        for field_id in ("id_town", "id_postal_code", "id_country"):
            with self.subTest(html_required=field_id):
                self.assertRegex(element(html, field_id), r"\brequired\b")
        # The further details are in their own group (K15); the pairs sit side by side.
        self.assertIn("Weitere Angaben (optional)", html)
        self.assertIn("field-pair field-pair-street", html)
        self.assertIn("field-pair field-pair-locality", html)
        # The suggestions sit directly under the street input, before the house number; the notice
        # follows the postal code and town (D-065). A leading space: the widget also carries
        # data-...-id="id_..." attributes.
        order = [
            html.index(' id="id_street"'),
            html.index("data-address-directory"),
            html.index(' id="id_house_number"'),
            html.index(' id="id_postal_code"'),
            html.index(' id="id_town"'),
            html.index("data-address-notice"),
            html.index(' id="id_post_office_box_number"'),
            html.index(' id="id_phone_number"'),
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn("Nur, wenn Sie keine Strasse angeben.", html)
        box = element(html, "id_post_office_box_number")
        self.assertIn('type="text"', box)
        self.assertIn('inputmode="numeric"', box)
        self.assertIn('inputmode="numeric"', element(html, "id_postal_code"))
        self.assertIn("js/postal-code-input.js", html)
        self.assertIn("js/address-directory.js", html)
        # The card has no password fields.
        self.assertNotIn("id_password1", html)

    def test_the_phone_number_is_shown_as_a_person_writes_it(self):
        html = self.client.get(PROFILE_EDIT_URL).content.decode()

        self.assertIn('value="031 123 45 67"', element(html, "id_phone_number"))
        self.assertNotIn("+41311234567", html)

    def test_the_phone_number_is_stored_in_the_international_format(self):
        data = {**completion_data(), "phone_number": "031 123 45 67"}

        response = self.client.post(PROFILE_EDIT_URL, data)

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number.as_e164, "+41311234567")

    def test_an_invalid_form_has_an_error_summary_with_links_to_the_fields(self):
        data = completion_data(town="", street="", post_office_box_number="")

        html = self.client.post(PROFILE_EDIT_URL, data).content.decode()

        self.assertIn("Bitte prüfen Sie Ihre Angaben", html)
        self.assertIn('href="#id_town"', html)
        self.assertIn('href="#id_street"', html)
        self.assertIn("Geben Sie einen Ort ein.", html)

    def test_the_postal_code_keyboard_follows_the_country(self):
        self.user.country = "DE"
        self.user.swiss_postal_code = None
        self.user.foreign_postal_code = "10115"
        self.user.save()

        html = self.client.get(PROFILE_EDIT_URL).content.decode()

        self.assertIn('inputmode="text"', element(html, "id_postal_code"))


@tag("mariadb")
class PhoneNumberDisplayTests(TestCase):
    def test_a_swiss_number_is_shown_in_the_national_format_and_others_internationally(self):
        cases = {
            "+41311234567": "031 123 45 67",
            "+41212345678": "021 234 56 78",
            "+49 30 901820": "+49 30 901820",
            "+423 232 1234": "+423 232 12 34",
            "": "",
        }
        for stored, shown in cases.items():
            with self.subTest(stored=stored):
                user = User(phone_number=stored)
                self.assertEqual(user.phone_number_display, shown)

    def test_the_profile_shows_the_display_format_and_the_database_the_international_one(self):
        user = create_active_user(phone_number="+41311234567")
        self.client.force_login(user)

        response = self.client.get(PROFILE_URL)

        self.assertContains(response, "031 123 45 67")
        self.assertNotContains(response, "+41311234567")
        self.assertEqual(User.objects.values_list("phone_number", flat=True).get(pk=user.pk), "+41311234567")


@tag("mariadb")
class InputTypeTests(TestCase):
    def test_the_login_field_asks_for_an_email_keyboard_but_the_browser_does_not_validate(self):
        html = self.client.get(LOGIN_URL).content.decode()

        field = element(html, "id_username")
        for attribute in ('type="email"', 'inputmode="email"', 'autocapitalize="none"', 'spellcheck="false"'):
            self.assertIn(attribute, field)
        from html.parser import HTMLParser

        class Forms(HTMLParser):
            def __init__(self):
                super().__init__()
                self.forms = []

            def handle_starttag(self, tag, attrs):
                if tag == "form":
                    self.forms.append(dict(attrs))

        parser = Forms()
        parser.feed(html)
        password_form = next(form for form in parser.forms if form.get("id") == "login-password")
        self.assertEqual(password_form["method"], "post")
        self.assertIn("novalidate", password_form)

    def test_the_login_still_accepts_what_the_browser_would_not(self):
        user = create_active_user("active.person@example.org")

        response = self.client.post(
            LOGIN_URL, {"username": f"  {user.email.upper()} ", "password": PASSWORD}
        )

        self.assertRedirects(response, HOME_URL)

    def test_the_login_has_the_show_password_button_and_a_specific_message(self):
        html = self.client.get(LOGIN_URL).content.decode()
        self.assertIn("data-password-toggle", html)

        failed = self.client.post(LOGIN_URL, {"username": "", "password": ""}, REMOTE_ADDR="10.9.9.9")

        self.assertContains(failed, "Geben Sie Ihre E-Mail-Adresse ein.")
        self.assertContains(failed, "Geben Sie Ihr Passwort ein.")
        self.assertNotContains(failed, "zwingend erforderlich")

    def test_no_code_field_asks_for_a_numeric_keyboard_or_a_length(self):
        user, device = create_user_with_totp()
        # The second step of the login accepts the codes of the app and the recovery codes.
        stepper = Client()
        log_in(stepper, user.email)
        step = element(stepper.get(CODE_URL).content.decode(), "id_otp_token")
        self.assertNotIn("inputmode", step)
        self.assertIn('autocomplete="one-time-code"', step)
        # So do the forms that ask for the code again.
        member = VerifiedClient()
        member.force_login(user)
        complete_second_factor(member, user)
        again = element(member.get(reverse("accounts:two_factor_disable")).content.decode(), "id_otp_token")
        self.assertNotIn("inputmode", again)
        # The first code of the setup is a plain text field too (owner, block 9): a pasted
        # "123 456" is accepted by the check, and the browser neither limits nor restricts it.
        enrolling = create_active_user("enrolling.person@example.org")
        client = Client()
        client.force_login(enrolling)
        client.post(SETUP_URL, {"current_password": PASSWORD}, REMOTE_ADDR="10.9.8.1")
        field = element(client.get(CONFIRM_URL).content.decode(), "id_otp_token")
        for attribute in ("inputmode", "maxlength", "pattern"):
            self.assertNotIn(attribute, field)
        self.assertIn('autocomplete="one-time-code"', field)
        self.assertIn('placeholder="000000"', field)


@tag("mariadb")
class LogoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("active.person@example.org")

    def setUp(self):
        self.client.force_login(self.user)

    def test_the_menu_has_a_logout_button_and_the_profile_entry(self):
        html = self.client.get(PROFILE_URL).content.decode()

        menu = re.search(r'<nav class="frame-menu.*?</nav>', html, re.S).group(0)
        self.assertIn(reverse("accounts:profile"), menu)
        self.assertRegex(menu, r'<form method="post" action="/logout/"')
        self.assertIn("csrfmiddlewaretoken", menu)
        self.assertIn("Ausloggen", menu)
        # The menu for small screens is the same menu in a drawer.
        self.assertIn('id="frame-drawer"', html)

    def test_an_account_that_must_change_its_password_can_still_log_out(self):
        self.user.password_change_required = True
        self.user.save()

        html = self.client.get(reverse("accounts:password_change")).content.decode()

        header = re.search(r'<header class="frame-header.*?</header>', html, re.S).group(0)
        self.assertIn('action="/logout/"', header)
        self.assertNotIn(reverse("accounts:profile"), html.split("</header>")[0])

    def test_the_logout_still_needs_the_csrf_token_and_says_so_afterwards(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        page = browser.get(PROFILE_URL)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)

        with self.assertLogs("django.security.csrf", "WARNING"):
            refused = browser.post(reverse("accounts:logout"), headers={"origin": "http://testserver"})
        self.assertEqual(refused.status_code, 403)
        response = browser.post(
            reverse("accounts:logout"),
            {"csrfmiddlewaretoken": token},
            headers={"origin": "http://testserver"},
            follow=True,
        )

        self.assertRedirects(response, LOGIN_URL)
        self.assertContains(response, "Sie wurden ausgeloggt.")
        self.assertNotIn("_auth_user_id", browser.session)


@tag("mariadb")
class TwoFactorPageTests(TestCase):
    """The card of two-factor authentication in the profile (D-059): wording and structure."""

    def test_the_card_without_a_second_factor_explains_it_in_plain_words(self):
        self.client.force_login(create_active_user())

        response = self.client.get(PROFILE_URL)

        self.assertContains(response, ">Anmeldemethode</h2>")
        self.assertContains(response, "Nur Passwort")
        self.assertContains(
            response,
            "Schützen Sie Ihr Konto zusätzlich mit einem Zahlencode vom Handy. "
            "Oder ersetzen Sie das Passwort durch einen Passkey.",
        )
        self.assertContains(response, "Ändern</a>")

    def test_the_card_with_a_second_factor_counts_the_recovery_codes_and_changes_in_the_flow(self):
        user, _device = create_user_with_totp()
        client = VerifiedClient()
        client.force_login(user)
        complete_second_factor(client, user)

        response = client.get(PROFILE_URL)
        html = response.content.decode()

        self.assertContains(response, "Sie haben 3 unbenutzte Wiederherstellungscodes.")
        self.assertContains(response, "Neue Codes erzeugen")
        # The turn-off is the step "Zahlencode abschalten" of the flow: no red button on the card.
        self.assertNotIn("2FA deaktivieren", html)
        self.assertNotIn("step=disable", html)
        RecoveryCode.objects.filter(device__user=user).exclude(pk=RecoveryCode.objects.first().pk).delete()
        self.assertContains(client.get(PROFILE_URL), "Sie haben 1 unbenutzten Wiederherstellungscode.")
        RecoveryCode.objects.all().delete()
        self.assertContains(
            client.get(PROFILE_URL),
            "Sie haben keine unbenutzten Wiederherstellungscodes mehr. Erstellen Sie neue.",
        )
        self.assertNotContains(client.get(PROFILE_URL), "0 unbenutzte")

    def test_the_wording_is_turned_off_everywhere(self):
        user, device = create_user_with_totp()
        client = VerifiedClient()
        client.force_login(user)
        complete_second_factor(client, user)

        response = client.post(
            reverse("accounts:two_factor_disable"),
            {"current_password": PASSWORD, "otp_token": code_for(device)},
            REMOTE_ADDR="10.9.7.1",
            follow=True,
        )

        self.assertContains(response, "Der Zahlencode ist abgeschaltet.")
        self.assertNotContains(response, "wurde entfernt")

    def test_the_setup_has_two_steps_one_form_each_and_the_manual_key_as_text(self):
        user = create_active_user("enrolling.person@example.org")
        client = Client()
        client.force_login(user)

        first = client.get(SETUP_URL).content.decode()
        # The session logged in just now: the button starts the setup without a password (D-055).
        self.assertNotIn('name="current_password"', first)
        self.assertIn("2FA einrichten", first)
        self.assertNotIn("Für Ihre Rolle ist die Zwei-Faktor-Anmeldung (2FA) Pflicht.", first)

        client.post(SETUP_URL, {})
        second = client.get(CONFIRM_URL).content.decode()
        device = AuthenticatorDevice.objects.get(user=user)

        self.assertIn("Schritt 1 von 2", second)
        self.assertIn("Scannen Sie diesen Code mit Ihrer Authenticator-App.", second)
        self.assertIn("Kein Scannen möglich? Geben Sie diesen Schlüssel in der App ein:", second)
        self.assertIn(totp.secret_for_display(device), second)
        self.assertEqual(second.count('name="otp_token"'), 1)

    def test_the_wrong_code_message_explains_the_rotation_of_the_codes(self):
        user = create_active_user("enrolling.person@example.org")
        client = Client()
        client.force_login(user)
        client.post(SETUP_URL, {"current_password": PASSWORD}, REMOTE_ADDR="10.9.6.2")

        response = client.post(CONFIRM_URL, {"otp_token": "000000"})

        self.assertContains(
            response, "Die App zeigt alle 30 Sekunden einen neuen Code an: Warten Sie auf den nächsten"
        )

    def test_a_required_setup_explains_why_and_has_no_cancel_link(self):
        staff = promote(create_active_user("staff.person@example.org"))
        client = Client()
        log_in(client, staff.email, address="10.9.5.1")

        setup = client.get(SETUP_URL).content.decode()
        self.assertIn("Für Ihre Rolle ist die Zwei-Faktor-Anmeldung (2FA) Pflicht.", setup)
        self.assertNotIn("Abbrechen", setup)
        client.post(SETUP_URL, {"current_password": PASSWORD}, REMOTE_ADDR="10.9.5.1")
        confirm = client.get(CONFIRM_URL).content.decode()
        self.assertNotIn("Abbrechen", confirm)

    def test_the_recovery_codes_have_a_list_a_copy_button_without_script_and_one_done_button(self):
        user = create_active_user("enrolling.person@example.org")
        client = Client()
        client.force_login(user)
        client.post(SETUP_URL, {"current_password": PASSWORD}, REMOTE_ADDR="10.9.4.1")
        device = AuthenticatorDevice.objects.get(user=user)

        response = client.post(CONFIRM_URL, {"otp_token": code_for(device)})
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Wiederherstellungscodes", html)
        self.assertEqual(html.count('type="checkbox"'), 1)
        self.assertEqual(html.count(">Fertig</button>"), 1)
        self.assertRegex(html, r'<ol class="recovery-codes" id="recovery-codes-list">')
        self.assertEqual(len(re.findall(r"<li>[a-z2-7]{4}-[a-z2-7]{4}</li>", html)), 10)
        # The copy button is hidden until the script shows it; the page works without a script.
        self.assertRegex(html, r'data-codes-copy hidden aria-controls="recovery-codes-list"')
        self.assertIn("js/recovery-codes.js", html)
        self.assertNotIn("Notfallcode", html)
        self.assertIn("no-store", response["Cache-Control"])

    def test_no_page_of_the_profile_says_notfallcode(self):
        user, _device = create_user_with_totp()
        client = VerifiedClient()
        client.force_login(user)
        complete_second_factor(client, user)
        pages = [
            client.get(PROFILE_URL),
            client.get(reverse("accounts:recovery_codes")),
            client.get(reverse("accounts:two_factor_disable")),
        ]
        stepper = Client()
        log_in(stepper, user.email, address="10.9.3.1")
        pages.append(stepper.get(CODE_URL))

        for response in pages:
            self.assertNotContains(response, "Notfallcode")
            self.assertNotContains(response, "mergency code")
        self.assertContains(pages[0], "Wiederherstellungscodes")
        self.assertContains(pages[-1], "Wiederherstellungscode")


@tag("mariadb")
class RecoveryCodeLoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp()

    def log_in_with(self, code, address):
        client = Client()
        log_in(client, self.user.email, address=address)
        return client, send_code(client, code, address=address)

    def test_a_login_with_a_recovery_code_says_how_many_codes_are_left(self):
        client, response = self.log_in_with("abcd-2345", "10.8.1.1")

        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        page = client.get(PROFILE_URL)
        self.assertContains(
            page,
            "Sie haben einen Wiederherstellungscode verwendet. Es sind noch 2 Codes übrig. "
            "Neue Codes können Sie in Ihrem Profil erstellen.",
        )
        # The message is shown once.
        self.assertNotContains(client.get(PROFILE_URL), "Sie haben einen Wiederherstellungscode verwendet")

    def test_the_wording_follows_the_number_of_codes_that_are_left(self):
        self.log_in_with("abcd2345", "10.8.2.1")
        one, _ = self.log_in_with("efgh6723", "10.8.2.2")
        zero, _ = self.log_in_with("ijkl4567", "10.8.2.3")

        self.assertContains(one.get(PROFILE_URL), "Es ist noch 1 Code übrig.")
        self.assertContains(
            zero.get(PROFILE_URL), "Sie haben keine Wiederherstellungscodes mehr. Erstellen Sie in Ihrem Profil neue Codes."
        )

    def test_a_login_with_the_code_of_the_app_says_nothing_about_recovery_codes(self):
        client = Client()
        log_in(client, self.user.email, address="10.8.3.1")
        response = send_code(client, code_for(self.device), address="10.8.3.1")

        self.assertRedirects(response, HOME_URL)
        self.assertNotContains(client.get(PROFILE_URL), "Wiederherstellungscode verwendet")

    def test_a_wrong_code_says_the_same_as_before_and_shows_no_count(self):
        client = Client()
        log_in(client, self.user.email, address="10.8.4.1")

        response = send_code(client, "wrong-code", address="10.8.4.1")

        self.assertContains(response, "Der Code ist falsch, abgelaufen oder wurde schon verwendet.")
        self.assertNotContains(response, "übrig")
        self.assertEqual(len(RECOVERY_CODES), 3)


@tag("mariadb")
class SentAndInvalidPageTests(TestCase):
    def test_the_registration_page_names_the_address_of_this_browser_and_offers_a_new_mail(self):
        register(self.client, "someone.else@example.org")

        response = self.client.get(reverse("accounts:registration_sent"))

        self.assertContains(response, "Prüfen Sie Ihre E-Mails")
        self.assertContains(response, "someone.else@example.org")
        self.assertContains(response, "Keine E-Mail erhalten? Ein Blick in den Spam-Ordner lohnt sich.")
        self.assertContains(response, "E-Mail erneut senden")
        self.assertContains(response, "Erneut senden ist nach")
        self.assertContains(response, "Falsche Adresse?")
        self.assertContains(
            response, f'href="{reverse("accounts:registration")}">Andere E-Mail-Adresse verwenden'
        )
        self.assertContains(response, "Schritt 1 von 3")
        # The page does not say whether the address has an account.
        self.assertNotContains(response, "besteht bereits")

    def test_the_password_reset_page_is_the_same_pattern_without_the_step_bar(self):
        client = Client()
        client.post(reverse("accounts:password_reset"), reset_request_data("someone@example.org"), REMOTE_ADDR="10.7.9.1")

        response = client.get(reverse("accounts:password_reset_sent"))

        self.assertContains(response, "Prüfen Sie Ihre E-Mails")
        self.assertContains(response, "someone@example.org")
        self.assertContains(
            response, f'href="{reverse("accounts:password_reset")}">Andere E-Mail-Adresse verwenden'
        )
        self.assertNotContains(response, "Schritt 1 von 3")
        self.assertNotContains(response, "Erneut registrieren")

    def test_the_pages_for_an_unknown_and_a_known_address_are_identical(self):
        create_active_user("known.person@example.org")
        pages = []
        for address in ("known.person@example.org", "unknown.person@example.org"):
            client = Client()
            client.post(reverse("accounts:password_reset"), reset_request_data(address), REMOTE_ADDR="10.7.1.1")
            content = client.get(reverse("accounts:password_reset_sent")).content
            content = re.sub(rb'name="csrfmiddlewaretoken" value="[^"]+"', b"", content)
            # The page names the address that the person entered; everything else is equal.
            pages.append(content.replace(address.encode(), b"ADDRESS"))

        self.assertEqual(pages[0], pages[1])

    def test_the_email_change_card_says_what_happens_next(self):
        user = create_active_user()
        client = VerifiedClient()
        client.force_login(user)

        response = client.post(
            reverse("accounts:email_change_request"),
            {"email": "new.address@example.org", "secondary_email": "", "current_password": PASSWORD},
            REMOTE_ADDR="10.7.2.1",
            follow=True,
        )

        self.assertContains(
            response, "Wir haben einen Bestätigungslink an die neue Adresse gesendet. Er ist 24 Stunden gültig."
        )
        self.assertContains(response, "Ausstehend")
        self.assertContains(response, "new.address@example.org")

    def test_invalid_link_pages_ask_for_the_address_again_and_have_a_login_button(self):
        registration = self.client.get(reverse("accounts:registration_verify", args=["x"]), follow=True)
        reset = self.client.get(reverse("accounts:password_reset_verify", args=["x"]), follow=True)

        for response, target, lead in (
            (
                registration,
                "accounts:registration",
                "Der Link ist abgelaufen oder wurde schon verwendet. Falls Sie sich bereits registriert haben, "
                "melden Sie sich an. Andernfalls fordern Sie einen neuen Link an.",
            ),
            (
                reset,
                "accounts:password_reset",
                "Der Link ist abgelaufen oder wurde schon verwendet. Falls Sie bereits ein neues Passwort "
                "gewählt haben, melden Sie sich an. Andernfalls fordern Sie einen neuen Link an.",
            ),
        ):
            self.assertContains(response, "Link nicht mehr gültig")
            self.assertContains(response, lead)
            self.assertContains(response, "Neuen Link senden")
            self.assertContains(response, f'<form method="post" action="{reverse(target)}"')
            self.assertContains(response, f'href="{LOGIN_URL}">Zur Anmeldung</a>')
            self.assertNotContains(response, "Dieser Link ist ungültig")

    def test_the_lockout_page_names_the_real_duration_and_links_to_the_password_reset(self):
        for attempt in range(LOGIN_FAILURE_LIMIT):
            self.client.post(
                LOGIN_URL, {"username": f"person{attempt}@example.org", "password": "x"}, REMOTE_ADDR="10.7.2.1"
            )

        response = self.client.post(
            LOGIN_URL, {"username": "person@example.org", "password": "x"}, REMOTE_ADDR="10.7.2.1"
        )

        minutes = int(LOGIN_COOLOFF.total_seconds() // 60)
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Anmeldung vorübergehend gesperrt", status_code=429)
        self.assertContains(response, f"Bitte warten Sie etwa {minutes} Minuten", status_code=429)
        self.assertContains(
            response, "Wenn Sie Ihr Passwort vergessen haben, können Sie ein neues wählen.", status_code=429
        )
        self.assertContains(response, f'href="{reverse("accounts:password_reset")}"', status_code=429)
        self.assertNotContains(response, "person@example.org", status_code=429)
        # The page is rendered in the entry layout like the login: two columns, the panel, the
        # corner buttons and the footer row (designer, phase 3).
        html = response.content.decode()
        for marker in ('class="entry-split"', 'class="entry-warm"', 'class="corner', 'class="alert alert-danger"'):
            self.assertIn(marker, html)
        self.assertEqual(html.count("<h1"), 1)
        # A page that says "blocked" is never stored by a browser or a proxy (D-067).
        for directive in ("no-store", "no-cache", "private", "max-age=0"):
            self.assertIn(directive, response["Cache-Control"])
        self.assertIn("Expires", response)

    def lock_the_account(self, username):
        # Every failure comes from another address, so that only the lockout of the account applies.
        for attempt in range(LOGIN_FAILURE_LIMIT):
            self.client.post(
                LOGIN_URL, {"username": username, "password": "x"}, REMOTE_ADDR=f"10.7.3.{attempt + 1}"
            )

    def test_the_lockout_of_an_account_is_not_stored_either(self):
        # The same answer as the lockout of an address (security review of unit A, L-4).
        self.lock_the_account("person@example.org")

        response = self.client.post(
            LOGIN_URL, {"username": "person@example.org", "password": "x"}, REMOTE_ADDR="10.7.3.99"
        )

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Anmeldung vorübergehend gesperrt", status_code=429)
        for directive in ("no-store", "no-cache", "private", "max-age=0"):
            self.assertIn(directive, response["Cache-Control"])
        self.assertIn("Expires", response)

    def test_a_lockout_answers_a_script_with_the_page_and_without_cors_headers(self):
        # Axes would answer ``X-Requested-With: XMLHttpRequest`` with JSON that echoes the
        # submitted username and sets ``Access-Control-Allow-Origin`` (``*`` by default). That
        # answer is not used any more (D-067, hardening; security review of unit A, L-4).
        self.lock_the_account("person@example.org")

        response = self.client.post(
            LOGIN_URL,
            {"username": "person@example.org", "password": "x"},
            REMOTE_ADDR="10.7.3.99",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ORIGIN="https://other.example.net",
        )

        self.assertEqual(response.status_code, 429)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertContains(response, "Anmeldung vorübergehend gesperrt", status_code=429)
        self.assertNotContains(response, "person@example.org", status_code=429)
        self.assertNotIn("Access-Control-Allow-Origin", response)
        self.assertIn("no-store", response["Cache-Control"])


@tag("mariadb")
class AlertAndMessageMarkupTests(TestCase):
    def test_messages_keep_their_role_and_the_classes_of_the_alerts(self):
        user = create_active_user()
        self.client.force_login(user)

        response = self.client.post(
            reverse("accounts:password_change"),
            {"current_password": PASSWORD, "new_password1": "silver lantern rests under quiet rain",
             "new_password2": "silver lantern rests under quiet rain"},
            REMOTE_ADDR="10.6.1.1",
            follow=True,
        )
        html = response.content.decode()

        # On the profile the message sits inside the changed card (designer acceptance, D4).
        self.assertRegex(html, r'<div class="alert alert-success profile-message" role="status"')

    def test_a_form_error_alert_keeps_role_alert(self):
        response = self.client.post(LOGIN_URL, {"username": "a@example.org", "password": "x"}, REMOTE_ADDR="10.6.2.1")

        self.assertContains(response, '<div class="alert alert-danger" role="alert">')


@tag("mariadb")
class EmailWebsiteLineAndWordingTests(TestCase):
    WEBSITE = "https://www.example.org/"

    def send_all_link_mails(self):
        register(Client(), "new.person@example.org")
        user = create_active_user("active.person@example.org")
        Client().post(reverse("accounts:password_reset"), reset_request_data(user.email), REMOTE_ADDR="10.5.1.1")
        run_mail_queue()
        return [message for message in mail.outbox]

    @override_settings(INSTANCE_WEBSITE_URL=WEBSITE)
    def test_the_link_emails_name_the_website_where_one_is_configured(self):
        for message in self.send_all_link_mails():
            with self.subTest(subject=message.subject):
                self.assertIn(f"Fragen oder Probleme: {self.WEBSITE}", message.body)
                # The emails still invite no reply and contain only the links of the application.
                self.assertEqual(message.extra_headers["Auto-Submitted"], "auto-generated")

    @override_settings(INSTANCE_WEBSITE_URL="")
    def test_the_line_is_left_out_without_a_website(self):
        for message in self.send_all_link_mails():
            with self.subTest(subject=message.subject):
                self.assertNotIn("Fragen oder Probleme", message.body)

    def test_the_emails_about_the_second_factor_say_recovery_code(self):
        emails.send_recovery_code_used("a@example.org")
        emails.send_recovery_codes_renewed("a@example.org")
        emails.send_two_factor_enabled("a@example.org")
        emails.send_two_factor_disabled("a@example.org")

        text = "\n".join(m.subject + "\n" + m.body for m in mail.outbox)

        self.assertIn("Wiederherstellungscode", text)
        self.assertIn("ausgeschaltet", text)
        self.assertNotIn("Notfallcode", text)
        self.assertNotIn(SHARP_S, text)

    def test_the_labels_of_the_editable_texts_and_the_security_log_say_recovery_code(self):
        with translation.override("de-ch"):
            labels = [str(label) for _value, label in EmailText._meta.get_field("key").flatchoices]
            labels += [str(label) for _value, label in SecurityEvent._meta.get_field("event").flatchoices]

        self.assertIn("Hinweis: Wiederherstellungscode verwendet", labels)
        self.assertIn("Wiederherstellungscodes erneuert", labels)
        self.assertFalse([label for label in labels if "Notfallcode" in label])
