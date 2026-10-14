"""Usability batch of the designer's review (D-054): wording, markup, forms and flows of the
member pages. Texts are asserted in Swiss German, the default language of the portal."""

import re

from django.contrib.auth import password_validation
from django.core import mail
from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import translation

from leg_platform.accounts import emails
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
    code_for,
    complete_second_factor,
    completion_data,
    create_active_user,
    registration_data,
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
ADDRESS_DATABASES = {"default", "addresses", "address_import"}
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


class ProfileFieldsMarkupMixin:
    """The markup that the completion of the registration and the profile form share."""

    optional_fields = (
        "id_salutation",
        "id_organisation_name",
        "id_address_line_1",
        "id_house_number",
        "id_post_office_box_number",
        "id_phone_number",
    )
    required_fields = ("id_first_name", "id_last_name", "id_street", "id_postal_code", "id_town")

    def check_markup(self, html):
        self.assertIn(
            "Felder mit dem Hinweis «freiwillig» dürfen leer bleiben. "
            "Alle anderen Felder müssen ausgefüllt werden.",
            html,
        )
        for field_id in self.optional_fields:
            with self.subTest(optional=field_id):
                self.assertIn("(freiwillig)", label_of(html, field_id))
        for field_id in self.required_fields:
            with self.subTest(required=field_id):
                self.assertNotIn("(freiwillig)", label_of(html, field_id))
        # The HTML attribute is set where a field is always required. The names and the street
        # are required only under a condition, which the text states, so they carry none.
        for field_id in ("id_town", "id_postal_code", "id_country"):
            with self.subTest(html_required=field_id):
                self.assertRegex(element(html, field_id), r"\brequired\b")
        for field_id in ("id_first_name", "id_last_name", "id_street", "id_organisation_name"):
            with self.subTest(not_html_required=field_id):
                self.assertNotRegex(element(html, field_id), r"\brequired\b")
        self.assertIn("Vorname und Nachname sind erforderlich, ausser Sie geben unten einen Firmennamen ein.", html)
        for legend in ("Name", "Firma", "Postadresse", "Telefon"):
            self.assertRegex(html, rf"<legend>{legend}</legend>")
        # Street and house number, and postal code and town, side by side from 576 px.
        self.assertRegex(html, r'field-row-street">\s*<div>\s*<div class="mb-3">\s*<label[^>]*for="id_street"')
        self.assertRegex(html, r'</div>\s*<div>\s*<div class="mb-3">\s*<label[^>]*for="id_house_number"')
        self.assertRegex(html, r'field-row-locality">\s*<div>\s*<div class="mb-3">\s*<label[^>]*for="id_postal_code"')
        self.assertRegex(html, r'</div>\s*<div>\s*<div class="mb-3">\s*<label[^>]*for="id_town"')
        # The mount point of the address suggestions: after the street and the house number and
        # before the postal code and the town; the post office box after the country.
        order = [
            html.index('id="id_street"'),
            html.index('id="id_house_number"'),
            html.index("data-address-directory"),
            html.index('id="id_postal_code"'),
            html.index('id="id_town"'),
            html.index('id="id_country"'),
            html.index('id="id_post_office_box_number"'),
            html.index('id="id_phone_number"'),
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn("Nur, wenn Sie keine Strasse angeben.", html)
        box = element(html, "id_post_office_box_number")
        self.assertIn('type="text"', box)
        self.assertIn('inputmode="numeric"', box)
        self.assertIn('inputmode="numeric"', element(html, "id_postal_code"))
        self.assertIn("js/postal-code-input.js", html)
        self.assertIn("js/address-directory.js", html)


@tag("mariadb")
class CompletionFormTests(ProfileFieldsMarkupMixin, TestCase):
    # Completing the registration reads the address directory (advisory check, D-033).
    databases = ADDRESS_DATABASES

    def setUp(self):
        register(self.client)
        self.client.get(verification_path(mail.outbox[0]))
        self.token = VerificationToken.objects.get()
        self.page = self.client.get(COMPLETE_URL)
        self.html = self.page.content.decode()

    def test_optional_fields_are_marked_and_the_rule_and_the_groups_are_stated(self):
        self.check_markup(self.html)

    def test_the_password_field_has_the_advice_and_a_button_that_shows_it(self):
        minimum = next(
            v.min_length
            for v in password_validation.get_default_password_validators()
            if isinstance(v, password_validation.MinimumLengthValidator)
        )
        self.assertIn(f"Wählen Sie mindestens {minimum} Zeichen.", self.html)
        self.assertIn("Meine Katze trinkt jeden Morgen Milch", self.html)
        # Django's list of the validator rules is not shown.
        with translation.override("de-ch"):
            for text in password_validation.password_validators_help_texts():
                self.assertNotIn(text, self.html)
        # The buttons are hidden until the script shows them, so the fields work without it.
        for field_id in ("id_password1", "id_password2"):
            self.assertRegex(
                self.html,
                rf'<button type="button"[^>]*data-password-toggle hidden[^>]*aria-controls="{field_id}"',
            )
            self.assertIn('type="password"', element(self.html, field_id))
        self.assertIn("js/password-toggle.js", self.html)
        self.assertIn('aria-label="Passwort anzeigen"', self.html)

    def test_an_invalid_form_has_an_error_summary_and_specific_messages(self):
        data = completion_data(
            registration=self.token.pk, town="", postal_code="", password1="", password2=""
        )

        response = self.client.post(COMPLETE_URL, data)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertRegex(html, r'<div class="alert alert-danger error-summary" role="alert" tabindex="-1" autofocus>')
        self.assertIn("Bitte prüfen Sie Ihre Angaben", html)
        for field_id in ("id_town", "id_postal_code", "id_password1", "id_password2"):
            self.assertIn(f'href="#{field_id}"', html)
        for text in (
            "Geben Sie einen Ort ein.",
            "Geben Sie eine Postleitzahl ein.",
            "Geben Sie ein Passwort ein.",
            "Geben Sie das Passwort noch einmal ein.",
        ):
            self.assertIn(text, html)
        self.assertNotIn("Dieses Feld ist zwingend erforderlich", html)
        # The error is tied to its field for assistive technology.
        self.assertIn('aria-describedby="id_town_error"', element(html, "id_town"))
        self.assertIn('id="id_town_error"', html)
        self.assertIn('aria-invalid="true"', element(html, "id_town"))

    def test_the_names_are_required_unless_there_is_a_company_name(self):
        data = completion_data(registration=self.token.pk, first_name="", last_name="")

        html = self.client.post(COMPLETE_URL, data).content.decode()

        self.assertIn("Geben Sie einen Vornamen oder einen Firmennamen ein.", html)
        company = completion_data(
            registration=self.token.pk, first_name="", last_name="", organisation_name="Example AG"
        )
        self.assertEqual(self.client.post(COMPLETE_URL, company).status_code, 302)

    def test_the_completion_leads_to_the_welcome_page_without_the_old_login_message(self):
        response = self.client.post(COMPLETE_URL, completion_data(registration=self.token.pk), follow=True)

        # The person is logged in by the completion (D-055): no "log in now" message.
        self.assertEqual(response.redirect_chain[-1][0], reverse("accounts:welcome"))
        self.assertNotContains(response, "Melden Sie sich mit Ihrer E-Mail-Adresse und Ihrem Passwort an.")
        self.assertNotContains(response, "Ihr Konto ist aktiv.")
        self.assertContains(response, "Ihr Konto ist bereit")

    def test_the_page_has_no_inline_code_and_no_data_image(self):
        self.assertNotRegex(self.html, r"(?i)\sstyle\s*=")
        self.assertNotIn("data:image", self.html)


@tag("mariadb")
class ProfileFormTests(ProfileFieldsMarkupMixin, TestCase):
    databases = ADDRESS_DATABASES

    def setUp(self):
        self.user = create_active_user(phone_number="+41311234567")
        self.client.force_login(self.user)

    def test_optional_fields_are_marked_and_the_rule_and_the_groups_are_stated(self):
        html = self.client.get(PROFILE_EDIT_URL).content.decode()

        self.check_markup(html)
        # The profile form has no password fields.
        self.assertNotIn("id_password1", html)

    def test_the_phone_number_is_shown_as_a_person_writes_it(self):
        html = self.client.get(PROFILE_EDIT_URL).content.decode()

        self.assertIn('value="031 123 45 67"', element(html, "id_phone_number"))
        self.assertNotIn("+41311234567", html)

    def test_the_phone_number_is_stored_in_the_international_format(self):
        data = {**completion_data(), "phone_number": "031 123 45 67"}
        data.pop("password1"), data.pop("password2")

        response = self.client.post(PROFILE_EDIT_URL, data)

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number.as_e164, "+41311234567")

    def test_an_invalid_form_has_an_error_summary_with_links_to_the_fields(self):
        data = completion_data(town="", street="", post_office_box_number="")
        data.pop("password1"), data.pop("password2")

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
        self.assertRegex(html, r"<form method=\"post\" novalidate>")

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

    def test_only_the_setup_code_field_asks_for_a_numeric_keyboard(self):
        user, device = create_user_with_totp()
        # The second step of the login accepts the codes of the app and the emergency codes.
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
        # Only the first code of the setup is always six digits.
        enrolling = create_active_user("enrolling.person@example.org")
        client = Client()
        client.force_login(enrolling)
        client.post(SETUP_URL, {"current_password": PASSWORD}, REMOTE_ADDR="10.9.8.1")
        field = element(client.get(CONFIRM_URL).content.decode(), "id_otp_token")
        self.assertIn('inputmode="numeric"', field)
        self.assertIn('autocomplete="one-time-code"', field)
        # A pasted "123 456" is still accepted by the check, so the field is not limited to six
        # characters (the limit of 32 characters is the one of every code field).
        self.assertNotIn('maxlength="6"', field)


@tag("mariadb")
class LogoutTests(TestCase):
    def setUp(self):
        self.user = create_active_user("active.person@example.org")
        self.client.force_login(self.user)

    def test_the_menu_has_a_logout_button_and_the_profile_entry(self):
        html = self.client.get(PROFILE_URL).content.decode()

        menu = re.search(r'<nav class="frame-menu.*?</nav>', html, re.S).group(0)
        self.assertIn(reverse("accounts:profile"), menu)
        self.assertRegex(menu, r'<form method="post" action="/logout/"')
        self.assertIn("csrfmiddlewaretoken", menu)
        self.assertIn("Abmelden", menu)
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
        self.assertContains(response, "Sie wurden abgemeldet.")
        self.assertNotIn("_auth_user_id", browser.session)


@tag("mariadb")
class TwoFactorPageTests(TestCase):
    def test_the_overview_without_a_second_factor_explains_it_in_plain_words(self):
        user = create_active_user()
        self.client.force_login(user)

        response = self.client.get(reverse("accounts:two_factor"))

        self.assertContains(response, "Zusätzlicher Schutz bei der Anmeldung")
        self.assertContains(response, "Zurzeit melden Sie sich nur mit Ihrem Passwort an.")
        self.assertContains(response, "Das ist freiwillig.")
        self.assertContains(response, "<details")
        self.assertContains(response, "Was ist eine Authenticator-App?")
        self.assertContains(response, "App Store oder bei Google Play")
        self.assertContains(response, "Zusätzlichen Schutz einrichten")

    def test_the_overview_with_a_second_factor_counts_the_emergency_codes_and_turns_off_in_red(self):
        user, _device = create_user_with_totp()
        client = VerifiedClient()
        client.force_login(user)
        complete_second_factor(client, user)

        response = client.get(reverse("accounts:two_factor"))
        html = response.content.decode()

        self.assertContains(response, "Sie haben 3 unbenutzte Notfallcodes.")
        self.assertContains(response, "Neue Notfallcodes erstellen")
        self.assertRegex(
            html, r'class="btn btn-outline-danger" href="/profile/two-factor/disable/">Zusätzlichen Schutz ausschalten'
        )
        RecoveryCode.objects.filter(device__user=user).exclude(pk=RecoveryCode.objects.first().pk).delete()
        self.assertContains(client.get(reverse("accounts:two_factor")), "Sie haben 1 unbenutzten Notfallcode.")
        RecoveryCode.objects.all().delete()
        self.assertContains(
            client.get(reverse("accounts:two_factor")),
            "Sie haben keine unbenutzten Notfallcodes mehr. Erstellen Sie neue.",
        )
        self.assertNotContains(client.get(reverse("accounts:two_factor")), "0 unbenutzte")

    def test_the_wording_is_turned_off_everywhere_and_the_button_is_red(self):
        user, device = create_user_with_totp()
        client = VerifiedClient()
        client.force_login(user)
        complete_second_factor(client, user)
        page = client.get(reverse("accounts:two_factor_disable")).content.decode()
        self.assertIn("Zusätzlichen Schutz ausschalten", page)
        self.assertRegex(page, r'<button type="submit" class="btn btn-outline-danger w-100">Ausschalten</button>')

        response = client.post(
            reverse("accounts:two_factor_disable"),
            {"current_password": PASSWORD, "otp_token": code_for(device)},
            REMOTE_ADDR="10.9.7.1",
            follow=True,
        )

        self.assertContains(response, "Die Zwei-Faktor-Authentifizierung wurde ausgeschaltet.")
        self.assertNotContains(response, "wurde entfernt")

    def test_the_setup_pages_have_a_step_indicator_one_list_and_the_manual_key_in_a_details_element(self):
        user = create_active_user("enrolling.person@example.org")
        client = Client()
        client.force_login(user)

        first = client.get(SETUP_URL).content.decode()
        # The session logged in just now: the button starts the setup without a password (D-055).
        self.assertNotIn('name="current_password"', first)
        self.assertIn("Zusätzlichen Schutz jetzt einrichten (etwa 2 Minuten)", first)
        self.assertIn("Das ist freiwillig und dauert etwa 2 Minuten.", first)
        self.assertIn(">Abbrechen</a>", first)
        self.assertNotIn("Für Ihre Rolle ist die Zwei-Faktor-Authentifizierung Pflicht.", first)

        client.post(SETUP_URL, {})
        second = client.get(CONFIRM_URL).content.decode()
        secret = AuthenticatorDevice.objects.get(user=user)

        # Two steps in all: the app (QR code and code on one page), then the emergency codes.
        self.assertIn("Schritt 1 von 2", second)
        self.assertRegex(second, r'<li aria-current="step" class="is-current">App verbinden</li>')
        # One ordered list with the three steps; the QR code is on a tile of its own.
        self.assertEqual(second.count('<ol class="setup-instructions">'), 1)
        self.assertIn("Installieren Sie eine Authenticator-App, falls Sie noch keine haben.", second)
        self.assertIn("Geben Sie den Code ein, den die App jetzt anzeigt.", second)
        self.assertIn('<div class="qr-tile">', second)
        self.assertIn("Scannen Sie den Code mit der Kamera in Ihrer Authenticator-App.", second)
        self.assertRegex(second, r"<details[^>]*>\s*<summary>Scannen nicht möglich\?")
        self.assertIn("Geben Sie stattdessen diesen Schlüssel ein", second)
        from leg_platform.accounts import totp

        self.assertIn(totp.secret_for_display(secret), second)

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
        self.assertIn("Für Ihre Rolle ist die Zwei-Faktor-Authentifizierung Pflicht.", setup)
        self.assertNotIn("Abbrechen", setup)
        self.assertNotIn("Das ist freiwillig", setup)
        client.post(SETUP_URL, {"current_password": PASSWORD}, REMOTE_ADDR="10.9.5.1")
        confirm = client.get(CONFIRM_URL).content.decode()
        self.assertNotIn("Abbrechen", confirm)

    def test_the_emergency_codes_page_has_one_button_a_list_and_print_and_copy_without_script(self):
        user = create_active_user("enrolling.person@example.org")
        client = Client()
        client.force_login(user)
        client.post(SETUP_URL, {"current_password": PASSWORD}, REMOTE_ADDR="10.9.4.1")
        device = AuthenticatorDevice.objects.get(user=user)

        response = client.post(CONFIRM_URL, {"otp_token": code_for(device)})
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Schritt 2 von 2", html)
        self.assertIn("Der zusätzliche Schutz ist eingeschaltet.", html)
        self.assertIn("Ihre Notfallcodes", html)
        self.assertNotIn('type="checkbox"', html)
        self.assertEqual(html.count("Ich habe die Codes gespeichert – weiter"), 1)
        self.assertRegex(html, r'<ol class="recovery-codes" id="emergency-codes">')
        self.assertEqual(len(re.findall(r"<li>[a-z2-7]{4}-[a-z2-7]{4}</li>", html)), 10)
        self.assertRegex(html, r'data-codes-print hidden>Seite drucken</button>')
        self.assertRegex(html, r'data-codes-copy hidden aria-controls="emergency-codes"')
        self.assertIn("js/recovery-codes.js", html)
        self.assertNotIn("Wiederherstellungscode", html)
        self.assertIn("no-store", response["Cache-Control"])

    def test_no_page_calls_the_emergency_codes_recovery_codes_any_more(self):
        user, device = create_user_with_totp()
        client = VerifiedClient()
        client.force_login(user)
        complete_second_factor(client, user)
        pages = [
            client.get(reverse("accounts:two_factor")),
            client.get(reverse("accounts:recovery_codes")),
            client.get(reverse("accounts:two_factor_disable")),
        ]
        stepper = Client()
        log_in(stepper, user.email, address="10.9.3.1")
        pages.append(stepper.get(CODE_URL))

        for response in pages:
            self.assertNotContains(response, "Wiederherstellungscode")
            self.assertNotContains(response, "ecovery code")
        self.assertContains(pages[-1], "Notfallcodes")


@tag("mariadb")
class EmergencyCodeLoginTests(TestCase):
    def setUp(self):
        self.user, self.device = create_user_with_totp()

    def log_in_with(self, code, address):
        client = Client()
        log_in(client, self.user.email, address=address)
        return client, send_code(client, code, address=address)

    def test_a_login_with_an_emergency_code_says_how_many_codes_are_left(self):
        client, response = self.log_in_with("abcd-2345", "10.8.1.1")

        self.assertRedirects(response, HOME_URL, fetch_redirect_response=False)
        page = client.get(PROFILE_URL)
        self.assertContains(
            page,
            "Sie haben einen Notfallcode verwendet. Es sind noch 2 Codes übrig. "
            "Neue Codes können Sie in Ihrem Profil erstellen.",
        )
        # The message is shown once.
        self.assertNotContains(client.get(PROFILE_URL), "Sie haben einen Notfallcode verwendet")

    def test_the_wording_follows_the_number_of_codes_that_are_left(self):
        self.log_in_with("abcd2345", "10.8.2.1")
        one, _ = self.log_in_with("efgh6723", "10.8.2.2")
        zero, _ = self.log_in_with("ijkl4567", "10.8.2.3")

        self.assertContains(one.get(PROFILE_URL), "Es ist noch 1 Code übrig.")
        self.assertContains(
            zero.get(PROFILE_URL), "Sie haben keine Notfallcodes mehr. Erstellen Sie in Ihrem Profil neue Codes."
        )

    def test_a_login_with_the_code_of_the_app_says_nothing_about_emergency_codes(self):
        client = Client()
        log_in(client, self.user.email, address="10.8.3.1")
        response = send_code(client, code_for(self.device), address="10.8.3.1")

        self.assertRedirects(response, HOME_URL)
        self.assertNotContains(client.get(PROFILE_URL), "Notfallcode verwendet")

    def test_a_wrong_code_says_the_same_as_before_and_shows_no_count(self):
        client = Client()
        log_in(client, self.user.email, address="10.8.4.1")

        response = send_code(client, "wrong-code", address="10.8.4.1")

        self.assertContains(response, "Der Code ist falsch oder wurde bereits verwendet.")
        self.assertNotContains(response, "übrig")
        self.assertEqual(len(RECOVERY_CODES), 3)


@tag("mariadb")
class SentAndInvalidPageTests(TestCase):
    def test_the_registration_page_says_what_to_expect_without_naming_the_address(self):
        register(self.client, "someone.else@example.org")

        response = self.client.get(reverse("accounts:registration_sent"))

        self.assertContains(
            response,
            "Wenn wir die angegebene Adresse verwenden können, haben wir eine Nachricht an diese Adresse gesendet.",
        )
        self.assertContains(response, "Sie sollte innerhalb weniger Minuten ankommen.")
        self.assertContains(response, "Spam-Ordner")
        self.assertContains(response, "Der Link ist 24 Stunden gültig.")
        self.assertContains(response, "Kommt nichts an, prüfen Sie die Schreibweise der Adresse")
        self.assertContains(response, f'href="{reverse("accounts:registration")}">Erneut registrieren')
        self.assertNotContains(response, "someone.else@example.org")
        self.assertNotContains(response, " ihr ")

    def test_the_password_reset_page_offers_a_new_link(self):
        response = self.client.get(reverse("accounts:password_reset_sent"))

        self.assertContains(response, "Der Link ist 24 Stunden gültig.")
        self.assertContains(response, f'href="{reverse("accounts:password_reset")}">Neuen Link anfordern')
        self.assertNotContains(response, "Erneut registrieren")

    def test_the_pages_for_an_unknown_and_a_known_address_are_identical(self):
        create_active_user("known.person@example.org")
        pages = []
        for address in ("known.person@example.org", "unknown.person@example.org"):
            client = Client()
            client.post(reverse("accounts:password_reset"), {"email": address}, REMOTE_ADDR="10.7.1.1")
            content = client.get(reverse("accounts:password_reset_sent")).content
            pages.append(re.sub(rb'name="csrfmiddlewaretoken" value="[^"]+"', b"", content))

        self.assertEqual(pages[0], pages[1])

    def test_the_email_change_page_says_the_same(self):
        user = create_active_user()
        client = VerifiedClient()
        client.force_login(user)

        response = client.get(reverse("accounts:email_change_sent"))

        self.assertContains(response, "Spam-Ordner")
        self.assertContains(response, "Der Link ist 24 Stunden gültig.")
        self.assertContains(response, "Ihre aktuelle E-Mail-Adresse bleibt unverändert, bis Sie die neue bestätigen.")
        self.assertContains(response, "E-Mail-Adresse erneut ändern")

    def test_invalid_link_pages_have_two_buttons(self):
        registration = self.client.get(reverse("accounts:registration_verify", args=["x"]))
        reset = self.client.get(reverse("accounts:password_reset_verify", args=["x"]))

        for response in (registration, reset):
            self.assertContains(response, "Dieser Link ist ungültig, abgelaufen oder wurde schon verwendet.")
            self.assertContains(response, f'<a class="btn btn-primary" href="{LOGIN_URL}">Anmelden</a>')
        self.assertContains(registration, "Wenn Sie die Registrierung schon abgeschlossen haben, melden Sie sich bitte an.")
        self.assertContains(registration, f'href="{reverse("accounts:registration")}">Erneut registrieren')
        self.assertContains(reset, f'href="{reverse("accounts:password_reset")}">Neuen Link anfordern')

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
        self.assertContains(response, f"Bitte warten Sie etwa {minutes} Minuten", status_code=429)
        self.assertContains(
            response, "Wenn Sie Ihr Passwort vergessen haben, können Sie ein neues wählen.", status_code=429
        )
        self.assertContains(response, f'href="{reverse("accounts:password_reset")}"', status_code=429)
        self.assertNotContains(response, "person@example.org", status_code=429)


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

        self.assertRegex(html, r'<div role="status">\s*<div class="alert alert-success">')

    def test_a_form_error_alert_keeps_role_alert(self):
        response = self.client.post(LOGIN_URL, {"username": "a@example.org", "password": "x"}, REMOTE_ADDR="10.6.2.1")

        self.assertContains(response, '<div class="alert alert-danger" role="alert">')


@tag("mariadb")
class EmailWebsiteLineAndWordingTests(TestCase):
    WEBSITE = "https://www.example.org/"

    def send_all_link_mails(self):
        register(Client(), "new.person@example.org")
        user = create_active_user("active.person@example.org")
        Client().post(reverse("accounts:password_reset"), {"email": user.email}, REMOTE_ADDR="10.5.1.1")
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

    def test_the_emails_about_the_second_factor_say_emergency_code(self):
        emails.send_recovery_code_used("a@example.org")
        emails.send_recovery_codes_renewed("a@example.org")
        emails.send_two_factor_enabled("a@example.org")
        emails.send_two_factor_disabled("a@example.org")

        text = "\n".join(m.subject + "\n" + m.body for m in mail.outbox)

        self.assertIn("Notfallcode", text)
        self.assertIn("ausgeschaltet", text)
        self.assertNotIn("Wiederherstellungscode", text)
        self.assertNotIn(SHARP_S, text)

    def test_the_labels_of_the_editable_texts_and_the_security_log_say_emergency_code(self):
        with translation.override("de-ch"):
            labels = [str(label) for _value, label in EmailText._meta.get_field("key").flatchoices]
            labels += [str(label) for _value, label in SecurityEvent._meta.get_field("event").flatchoices]

        self.assertIn("Hinweis: Notfallcode verwendet", labels)
        self.assertIn("Notfallcodes erneuert", labels)
        self.assertFalse([label for label in labels if "Wiederherstellung" in label])
