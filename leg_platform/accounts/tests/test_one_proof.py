"""One proof per confirmation (D-062, owner decision of 2026-10-15, "2FA wins").

State A (a second factor is active): the forms of the profile ask for the code only; there is no
password field, and a posted password grants nothing. State B (no second factor): the password
only. The negative tests come first in each class.
"""

import re
from pathlib import Path

from django.test import Client, RequestFactory, SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation
from unittest import mock

from leg_platform.accounts import second_factor, totp
from leg_platform.accounts.forms import ChangePasswordForm, OneProofMixin, ReauthenticationForm
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    PendingEmailChange,
    SessionCodeFailure,
    User,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    age_authentication,
    code_for,
    create_active_user,
)
from leg_platform.accounts.tests.test_second_factor import (
    RECOVERY_CODES,
    codes_in,
    create_user_with_totp,
)

PROFILE = reverse("accounts:profile")
EMAIL_URL = reverse("accounts:email_change_request")
PASSWORD_URL = reverse("accounts:password_change")
DELETE_URL = reverse("accounts:account_delete")
CODES_URL = reverse("accounts:recovery_codes")
DISABLE_URL = reverse("accounts:two_factor_disable")
NEW = "new.address.proof@example.org"
LEAD = "Geben Sie den 6-stelligen Code aus Ihrer Authenticator-App ein. Ihr Passwort wird hier nicht benötigt."
MISSING = "Geben Sie den 2FA-Authentifizierungscode ein."
SHAPE = "Das ist kein 2FA-Authentifizierungscode. Er besteht aus 6 Ziffern."
WRONG = "Der 2FA-Authentifizierungscode ist falsch oder abgelaufen."
PAUSED = "pausiert"

PAGES = {
    "email": f"{PROFILE}?edit=email",
    "password": f"{PROFILE}?edit=password",
    "delete": f"{PROFILE}?edit=delete",
    "codes": f"{PROFILE}?edit=two_factor&step=codes_new",
    "disable": f"{PROFILE}?edit=two_factor&step=disable",
}


def input_tag(html, name):
    match = re.search(rf"<input[^>]*name=\"{name}\"[^>]*>", html)
    assert match, f"No input {name}."
    return match.group(0)


def account_a(email):
    user, device = create_user_with_totp(email)
    client = Client()
    client.force_login(user)
    return user, device, client


@tag("mariadb")
class FieldsOfTheStatesTests(TestCase):
    def test_state_b_asks_for_the_password_only(self):
        user = create_active_user("state.b@example.org")
        client = Client()
        client.force_login(user)

        for name in ("email", "password", "delete"):
            with self.subTest(form=name):
                html = client.get(PAGES[name]).content.decode()
                self.assertIn('name="current_password"', html)
                self.assertNotIn('name="otp_token"', html)
                self.assertNotIn(LEAD, html)
        # Without a second factor there is nothing to confirm with a code.
        for name, url in (("codes", CODES_URL), ("disable", DISABLE_URL)):
            with self.subTest(form=name):
                self.assertRedirects(
                    client.get(url), reverse("accounts:two_factor"), fetch_redirect_response=False
                )

    def test_state_a_asks_for_the_code_only_and_says_so(self):
        _user, _device, client = account_a("state.a@example.org")

        for name, url in PAGES.items():
            with self.subTest(form=name):
                html = client.get(url).content.decode()
                self.assertIn('name="otp_token"', html)
                self.assertNotIn('name="current_password"', html)
                self.assertNotIn("Aktuelles Passwort", html)
                self.assertIn("2FA-Authentifizierungscode", html)
                self.assertNotIn(">Code aus der App<", html)
                self.assertIn(LEAD, html)
                # No word about recovery codes in the normal forms (block 8).
                self.assertNotIn("Wiederherstellungscode ein", html)
                self.assertNotIn("abcd-efgh", html)
                # The lead stands between the label and the field.
                label = html.index('for="id_otp_token"')
                lead = html.index(LEAD)
                field = html.index('name="otp_token"')
                self.assertLess(label, lead)
                self.assertLess(lead, field)

    def test_the_code_field_is_a_plain_text_field(self):
        _user, _device, client = account_a("attrs.a@example.org")

        for name, url in PAGES.items():
            tag_ = input_tag(client.get(url).content.decode(), "otp_token")
            with self.subTest(form=name):
                self.assertIn('autocomplete="one-time-code"', tag_)
                self.assertIn('type="text"', tag_)
                # Like every other text field (owner, block 8): no special keyboard, length or
                # pattern in the browser, no special style class.
                for attribute in ("inputmode", "maxlength", "pattern", "autocapitalize", "spellcheck"):
                    self.assertNotIn(attribute, tag_)
                self.assertIn('class="form-control', tag_)
                self.assertNotIn("value=", tag_)
                # Focus where it is the first field only; in the e-mail form it is the third.
                self.assertEqual("autofocus" in tag_, name != "email")

    def test_the_forced_password_change_follows_the_same_rule(self):
        for email, with_code in (("forced.a@example.org", True), ("forced.b@example.org", False)):
            user, _device, client = (
                account_a(email) if with_code else (create_active_user(email), None, Client())
            )
            client.force_login(user)
            user.password_change_required = True
            user.save()
            with self.subTest(second_factor=with_code):
                html = client.get(PASSWORD_URL).content.decode()
                self.assertEqual('name="otp_token"' in html, with_code)
                self.assertEqual('name="current_password"' in html, not with_code)
                self.assertEqual(LEAD in html, with_code)

    def test_the_setup_after_five_minutes_still_asks_for_the_password(self):
        user = create_active_user("setup.b@example.org")
        client = Client()
        client.force_login(user)
        age_authentication(client)

        html = client.get(f"{PROFILE}?edit=two_factor&step=setup").content.decode()

        self.assertIn('name="current_password"', html)
        self.assertNotIn('name="otp_token"', html)
        self.assertNotIn(LEAD, html)

    def test_a_staff_member_without_a_second_factor_gives_the_password_and_with_one_the_code(self):
        factory = RequestFactory()
        staff = create_active_user("staff.proof@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        request = factory.get("/")
        request.user = staff

        without = ReauthenticationForm(request=request)
        self.assertIn("current_password", without.fields)
        self.assertNotIn("otp_token", without.fields)
        self.assertEqual(without.proof, "password")

        AuthenticatorDevice.objects.create_device(staff, confirmed=True)
        with_factor = ReauthenticationForm(request=request)
        self.assertIn("otp_token", with_factor.fields)
        self.assertNotIn("current_password", with_factor.fields)
        self.assertEqual(with_factor.proof, "code")


@tag("mariadb")
class CodeAsTheOnlyProofTests(TestCase):
    """Negative tests first: the password alone, a wrong code and a posted password grant nothing."""

    def setUp(self):
        self.user, self.device, self.client = account_a("only.code@example.org")

    def email(self, **data):
        payload = {"email": NEW, "secondary_email": "", **data}
        return self.client.post(EMAIL_URL, payload, REMOTE_ADDR="10.90.1.1")

    def test_the_password_alone_changes_nothing(self):
        response = self.email(current_password=PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, MISSING)
        self.assertFalse(PendingEmailChange.objects.filter(user=self.user).exists())

    def test_a_posted_password_is_ignored_and_neither_helps_nor_hurts(self):
        response = self.email(current_password="not the password at all", otp_token=code_for(self.device))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(PendingEmailChange.objects.filter(user=self.user).exists())

    def test_a_wrong_code_changes_nothing_and_is_not_written_back(self):
        response = self.email(otp_token="123456", current_password=PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, WRONG)
        self.assertNotContains(response, 'value="123456"')
        self.assertNotContains(response, PASSWORD)
        self.assertFalse(PendingEmailChange.objects.filter(user=self.user).exists())

    def test_the_code_alone_is_enough(self):
        response = self.email(otp_token=code_for(self.device))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(PendingEmailChange.objects.filter(user=self.user).exists())

    def test_the_description_order_is_lead_error_note(self):
        html = self.email(otp_token="123456").content.decode()

        self.assertIn(
            'aria-describedby="id_otp_token_lead id_otp_token_error id_otp_token_note"',
            input_tag(html, "otp_token"),
        )
        for suffix in ("lead", "error", "note"):
            self.assertIn(f'id="id_otp_token_{suffix}"', html)
        self.assertNotIn("id_otp_token_helptext", html)
        self.assertIn("Neue Adressen bestätigen Sie mit einem Link.", html)

    def test_the_error_summary_names_the_field(self):
        html = self.email(otp_token="123456").content.decode()

        self.assertRegex(html, r"2FA-Authentifizierungscode: Der 2FA-Authentifizierungscode ist falsch")

    def test_the_delete_form_has_the_same_field_and_no_note_about_a_link(self):
        html = self.client.post(DELETE_URL, {"otp_token": "123456", "confirm": "on"}).content.decode()

        self.assertIn(
            'aria-describedby="id_otp_token_lead id_otp_token_error"',
            input_tag(html, "otp_token"),
        )
        self.assertNotIn("Neue Adressen bestätigen Sie mit einem Link.", html)


@tag("mariadb")
class ShapeAndBudgetTests(TestCase):
    def setUp(self):
        self.user, self.device, self.client = account_a("budget.a@example.org")

    def post(self, value):
        return self.client.post(
            DISABLE_URL, {"otp_token": value}, REMOTE_ADDR="10.90.2.1"
        )

    def test_a_password_in_the_code_field_is_refused_and_is_not_counted(self):
        for attempt in range(20):
            response = self.post(f"{PASSWORD} {attempt}")
            self.assertContains(response, SHAPE)

        device = AuthenticatorDevice.objects.get(pk=self.device.pk)
        self.assertEqual(device.failed_codes, 0)
        self.assertFalse(totp.code_attempts_paused(self.user))
        # The account is not locked out: the right code still works.
        self.assertEqual(self.post(code_for(self.device)).status_code, 302)

    def test_what_has_no_shape_of_a_code_is_refused_before_the_check(self):
        # Six digits only (block 8): a recovery code, six letters or anything else is no app code.
        for value in (
            "12345", "1234567", "abcdefghi", "abcd-efg", "12 34 5", "!#%&/(", "é" * 6, "a" * 31,
            "abcd2345", "abcdef", "12345a", "١٢٣٤٥٦",
        ):
            with self.subTest(value=value):
                self.assertContains(self.post(value), SHAPE)
        self.assertEqual(AuthenticatorDevice.objects.get(pk=self.device.pk).failed_codes, 0)

    def test_the_shapes_of_a_code_reach_the_check_and_a_wrong_one_counts_for_the_session(self):
        for value in ("000001", "12 34 56", "123-456", "000 002"):
            with self.subTest(value=value):
                self.assertContains(self.post(value), WRONG)
        # Not against the budget of the account, which would also pause the login (D-062, block 9).
        self.assertEqual(AuthenticatorDevice.objects.get(pk=self.device.pk).failed_codes, 0)
        self.assertEqual(SessionCodeFailure.objects.get(account=self.user).failures, 4)

    def test_a_used_up_budget_pauses_with_the_lockout_page_and_changes_nothing(self):
        AuthenticatorDevice.objects.filter(pk=self.device.pk).update(
            failed_codes=totp.CODE_PAUSE_AT, failed_codes_since=timezone.now()
        )

        response = self.post(code_for(self.device))

        # The abuse protection answers with its own page and text about the pause; even the right
        # code is refused (D-040).
        self.assertContains(response, "pausiert", status_code=429)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())

    def test_the_limit_of_the_session_stops_guessing(self):
        for _attempt in range(second_factor.SESSION_CODE_LIMIT):
            last = self.post("000002")
        # The fifth wrong code ends the session (D-062, block 9); the account is not paused.
        self.assertRedirects(last, reverse("accounts:login"), fetch_redirect_response=False)
        device = AuthenticatorDevice.objects.get(pk=self.device.pk)
        self.assertEqual(device.failed_codes, 0)
        self.assertFalse(totp.code_attempts_paused(self.user))


@tag("mariadb")
class RecoveryCodeTests(TestCase):
    """A recovery code is no proof in the confirmations of the profile (block 8): it is refused
    and never used up. It counts at the login and in the way "phone lost" only."""

    def setUp(self):
        self.user, self.device, self.client = account_a("active.person@example.org")
        self.code = RECOVERY_CODES[0]

    def test_no_confirmation_accepts_a_recovery_code_and_none_uses_it_up(self):
        before = totp.remaining_recovery_codes(self.user)
        posts = {
            "email": (EMAIL_URL, {"email": NEW, "secondary_email": ""}),
            "password": (PASSWORD_URL, {"new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD}),
            "delete": (DELETE_URL, {"confirm": "on"}),
            "new codes": (CODES_URL, {}),
            "turn off": (DISABLE_URL, {}),
        }

        for name, (url, data) in posts.items():
            with self.subTest(form=name):
                response = self.client.post(url, {**data, "otp_token": self.code}, REMOTE_ADDR="10.90.3.1")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, SHAPE)

        self.assertEqual(totp.remaining_recovery_codes(self.user), before)
        self.assertFalse(PendingEmailChange.objects.filter(user=self.user).exists())
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(PASSWORD))
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        # Nothing reached the check, so nothing was counted either.
        self.assertEqual(AuthenticatorDevice.objects.get(pk=self.device.pk).failed_codes, 0)

    def test_the_verification_itself_refuses_a_recovery_code_when_it_is_told_to(self):
        # Also if a recovery code got past the shape check, the check accepts the app only.
        before = totp.remaining_recovery_codes(self.user)

        self.assertIsNone(totp.verify_code(self.user, self.code, recovery=False))

        self.assertEqual(totp.remaining_recovery_codes(self.user), before)
        self.assertIsNotNone(totp.verify_code(self.user, self.code))

    def test_a_failing_form_does_not_use_up_the_app_code(self):
        code = code_for(self.device)
        failing = self.client.post(
            EMAIL_URL, {"email": "not an address", "secondary_email": "", "otp_token": code}, REMOTE_ADDR="10.90.3.2"
        )

        right = self.client.post(
            EMAIL_URL, {"email": NEW, "secondary_email": "", "otp_token": code}, REMOTE_ADDR="10.90.3.2"
        )

        self.assertEqual(failing.status_code, 200)
        self.assertEqual(right.status_code, 302)


@tag("mariadb")
class PasswordCardTests(TestCase):
    def post(self, client, **data):
        return client.post(
            PASSWORD_URL,
            {"new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD, **data},
            REMOTE_ADDR="10.90.4.1",
        )

    def test_state_a_changes_the_password_with_the_code_and_the_new_password_only(self):
        user, device, client = account_a("pw.a@example.org")
        other = Client()
        other.force_login(user)

        response = self.post(client, otp_token=code_for(device))

        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password(OTHER_PASSWORD))
        # Every other session of the account ended.
        self.assertEqual(other.get(PROFILE).status_code, 302)
        self.assertEqual(client.get(PROFILE).status_code, 200)

    def test_state_a_without_the_code_changes_nothing_also_with_the_password(self):
        user, _device, client = account_a("pw.a2@example.org")

        for data in ({}, {"current_password": PASSWORD}, {"otp_token": PASSWORD}):
            with self.subTest(data=data):
                self.assertEqual(self.post(client, **data).status_code, 200)
                user.refresh_from_db()
                self.assertTrue(user.check_password(PASSWORD))

    def test_the_new_password_may_not_be_the_stored_one_after_a_valid_proof(self):
        user, device, client = account_a("same.password.a@example.org")
        code = code_for(device)

        same = client.post(
            PASSWORD_URL,
            {"otp_token": code, "new_password1": PASSWORD, "new_password2": PASSWORD},
            REMOTE_ADDR="10.90.4.2",
        )
        again = self.post(client, otp_token=code)

        self.assertEqual(same.status_code, 200)
        self.assertContains(same, "Wählen Sie ein Passwort, das sich vom aktuellen unterscheidet.")
        # The code of the first request was a valid proof and is used up (D-062, security review H-1).
        self.assertEqual(again.status_code, 200)
        self.assertContains(again, WRONG)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))

    def test_a_change_of_the_account_in_between_stops_the_change_with_the_code(self):
        user, device, client = account_a("pw.race.a@example.org")
        original = ChangePasswordForm.is_valid

        def valid_then_changed(form):
            result = original(form)
            User.objects.get(pk=user.pk).end_all_sessions()
            return result

        with mock.patch.object(ChangePasswordForm, "is_valid", valid_then_changed):
            response = self.post(client, otp_token=code_for(device))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ihr Konto wurde inzwischen geändert. Bitte versuchen Sie es erneut.")
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))

    def test_state_b_changes_the_password_with_the_current_one(self):
        user = create_active_user("pw.b@example.org")
        client = Client()
        client.force_login(user)

        wrong = self.post(client, current_password="wrong password here")
        right = self.post(client, current_password=PASSWORD)

        self.assertEqual(wrong.status_code, 200)
        self.assertEqual(right.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password(OTHER_PASSWORD))

    def test_state_b_still_checks_the_password_again_under_the_lock(self):
        user = create_active_user("pw.race.b@example.org")
        client = Client()
        client.force_login(user)
        original = ChangePasswordForm.is_valid

        def valid_then_changed(form):
            result = original(form)
            other = User.objects.get(pk=user.pk)
            other.set_password("another unrelated passphrase 99")
            other.save()
            return result

        with mock.patch.object(ChangePasswordForm, "is_valid", valid_then_changed):
            response = self.post(client, current_password=PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Das Passwort stimmt nicht.")
        user.refresh_from_db()
        self.assertTrue(user.check_password("another unrelated passphrase 99"))

    def test_the_forced_change_works_with_the_code(self):
        user, device, client = account_a("forced.change@example.org")
        user.password_change_required = True
        user.save()

        response = self.post(client, otp_token=code_for(device))

        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertFalse(user.password_change_required)
        self.assertTrue(user.check_password(OTHER_PASSWORD))


@tag("mariadb")
class OtherConfirmationsTests(TestCase):
    def test_deleting_the_account_with_the_code_and_not_with_the_password(self):
        user, device, client = account_a("delete.a@example.org")

        refused = client.post(DELETE_URL, {"current_password": PASSWORD, "confirm": "on"}, REMOTE_ADDR="10.90.5.1")
        self.assertEqual(refused.status_code, 200)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

        done = client.post(DELETE_URL, {"otp_token": code_for(device), "confirm": "on"}, REMOTE_ADDR="10.90.5.1")
        self.assertEqual(done.status_code, 200)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())

    def test_deleting_the_account_without_a_second_factor_with_the_password(self):
        user = create_active_user("delete.b@example.org")
        client = Client()
        client.force_login(user)

        client.post(DELETE_URL, {"current_password": PASSWORD, "confirm": "on"}, REMOTE_ADDR="10.90.5.2")

        self.assertFalse(User.objects.filter(pk=user.pk).exists())

    def test_new_codes_with_the_code(self):
        user, device, client = account_a("actions.a@example.org")

        codes = client.post(CODES_URL, {"otp_token": code_for(device)}, REMOTE_ADDR="10.90.5.3")

        self.assertEqual(len(codes_in(codes)), totp.RECOVERY_CODE_COUNT)

    def test_turning_off_the_second_factor_with_the_code(self):
        user, device, client = account_a("actions.a2@example.org")

        off = client.post(DISABLE_URL, {"otp_token": code_for(device)}, REMOTE_ADDR="10.90.5.4")

        self.assertEqual(off.status_code, 302)
        self.assertFalse(second_factor.has_second_factor(user))


class TextsAndStyleTests(SimpleTestCase):
    def test_the_texts_are_german_without_the_sharp_s(self):
        expected = {
            "code_label": "2FA-Authentifizierungscode",
            "code_lead": LEAD,
            "code_required_message": MISSING,
            "code_shape_message": SHAPE,
            "code_wrong_message": "Der 2FA-Authentifizierungscode ist falsch oder abgelaufen. Warten Sie auf den nächsten Code in Ihrer App und versuchen Sie es erneut.",
        }
        with translation.override("de-ch"):
            for name, text in expected.items():
                with self.subTest(name=name):
                    self.assertEqual(str(getattr(OneProofMixin, name)), text)
                    self.assertNotIn("\u00df", text)

    def test_the_field_is_48_px_high_on_a_phone(self):
        css = re.sub(
            r"/\*.*?\*/", "", (Path(__file__).resolve().parents[2] / "static" / "css" / "frame.css").read_text(encoding="utf-8"), flags=re.S
        )
        phone = css[css.index("@media (max-width: 760px)") :]
        rules = [
            match.group(2)
            for match in re.finditer(r"([^{}]+)\{([^}]*)\}", phone)
            if ".profile-form .form-control" in [part.strip() for part in match.group(1).split(",")]
        ]

        self.assertTrue(any("min-height: 48px" in rule for rule in rules))
