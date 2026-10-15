"""Security review of D-062 (block 7): no guessing of the password through the password card,
the state of the proof for odd accounts, a change of the second factor in the middle of a request,
the shape error without a trace in the abuse protection, and the real budget of 15 wrong codes."""

import re
from unittest import mock

from axes.models import AccessAttempt
from django.test import Client, RequestFactory, TestCase, tag
from django.urls import reverse

from leg_platform.accounts import second_factor, totp
from leg_platform.accounts.forms import ChangePasswordForm, ReauthenticationForm
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    LoginIpAttempt,
    MailJob,
    RecoveryCodeDevice,
    User,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    code_for,
    create_active_user,
)
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp

PASSWORD_URL = reverse("accounts:password_change")
DISABLE_URL = reverse("accounts:two_factor_disable")
SAME = "Wählen Sie ein Passwort, das sich vom aktuellen unterscheidet."


def errors_of(html):
    """The texts of the error messages of a page, without the markup around them."""
    return [
        re.sub(r"\s+", " ", text).strip()
        for text in re.findall(r'<div class="invalid-feedback d-block"[^>]*>(.*?)</div>', html, re.S)
    ]


def request_of(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


@tag("mariadb")
class NoGuessingThroughTheNewPasswordTests(TestCase):
    """H-1: "the new password is the current one" is told only after a valid proof, so the form
    cannot be used to guess the password without the abuse protection."""

    def post(self, client, new, **data):
        return client.post(
            PASSWORD_URL,
            {"new_password1": new, "new_password2": new, **data},
            REMOTE_ADDR="10.91.1.1",
        )

    def test_a_wrong_code_gives_the_same_answer_whatever_the_new_password_is(self):
        user = create_active_user("oracle.a@example.org")
        AuthenticatorDevice.objects.create_device(user, confirmed=True)
        client = Client()
        client.force_login(user)

        with mock.patch("leg_platform.accounts.forms.check_password") as compared:
            same = self.post(client, PASSWORD, otp_token="000001")
            other = self.post(client, OTHER_PASSWORD, otp_token="000002")

        compared.assert_not_called()
        for response in (same, other):
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, SAME)
        self.assertEqual(errors_of(same.content.decode()), errors_of(other.content.decode()))
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))

    def test_no_code_and_a_shape_error_give_no_hint_either(self):
        user = create_active_user("oracle.a2@example.org")
        AuthenticatorDevice.objects.create_device(user, confirmed=True)
        client = Client()
        client.force_login(user)

        with mock.patch("leg_platform.accounts.forms.check_password") as compared:
            for data in ({}, {"otp_token": "not a code"}, {"current_password": PASSWORD}):
                with self.subTest(data=data):
                    response = self.post(client, PASSWORD, **data)
                    self.assertNotContains(response, SAME)
        compared.assert_not_called()

    def test_a_wrong_current_password_gives_the_same_answer_whatever_the_new_password_is(self):
        user = create_active_user("oracle.b@example.org")
        client = Client()
        client.force_login(user)

        with mock.patch("leg_platform.accounts.forms.check_password") as compared:
            same = self.post(client, PASSWORD, current_password="a wrong password for the test")
            other = self.post(client, OTHER_PASSWORD, current_password="a wrong password for the test")

        compared.assert_not_called()
        for response in (same, other):
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, SAME)
        self.assertEqual(errors_of(same.content.decode()), errors_of(other.content.decode()))

    def test_state_b_with_the_right_password_and_the_same_new_one_says_so(self):
        user = create_active_user("oracle.b2@example.org")
        client = Client()
        client.force_login(user)
        stored = User.objects.get(pk=user.pk).password

        with mock.patch.object(User, "set_password") as setter:
            response = self.post(client, PASSWORD, current_password=PASSWORD)

        self.assertContains(response, SAME)
        setter.assert_not_called()
        self.assertEqual(User.objects.get(pk=user.pk).password, stored)

    def test_state_a_with_a_valid_code_and_the_same_new_one_says_so_and_changes_nothing(self):
        user, device = create_user_with_totp("oracle.a3@example.org")
        client = Client()
        client.force_login(user)
        stored = User.objects.get(pk=user.pk).password

        with mock.patch.object(User, "set_password") as setter:
            response = self.post(client, PASSWORD, otp_token=code_for(device))

        self.assertContains(response, SAME)
        setter.assert_not_called()
        self.assertEqual(User.objects.get(pk=user.pk).password, stored)


@tag("mariadb")
class StateOfTheProofTests(TestCase):
    def test_a_recovery_code_alone_is_no_second_factor_and_the_password_is_asked(self):
        user = create_active_user("only.recovery@example.org")
        recovery = RecoveryCodeDevice.objects.create(user=user, name="recovery codes", confirmed=True)
        recovery.add_codes(["abcd2345"])

        form = ReauthenticationForm(request=request_of(user))

        self.assertEqual(form.proof, "password")
        self.assertIn("current_password", form.fields)
        self.assertNotIn("otp_token", form.fields)

    def test_an_unconfirmed_device_is_no_second_factor_and_the_password_is_asked(self):
        user = create_active_user("unconfirmed.device@example.org")
        AuthenticatorDevice.objects.create_device(user, confirmed=False)

        for form in (
            ReauthenticationForm(request=request_of(user)),
            ChangePasswordForm(user, request=request_of(user)),
        ):
            self.assertEqual(form.proof, "password")
            self.assertIn("current_password", form.fields)
            self.assertNotIn("otp_token", form.fields)

    def test_the_second_factor_removed_while_the_form_is_checked_fails_closed(self):
        user, device = create_user_with_totp("removed.between@example.org")
        form = ReauthenticationForm(data={"otp_token": code_for(device)}, request=request_of(user))
        self.assertEqual(form.proof, "code")

        AuthenticatorDevice.objects.filter(user=user).delete()

        self.assertFalse(form.is_valid())
        self.assertIn(
            "Ihr Konto wurde inzwischen geändert. Bitte versuchen Sie es erneut.",
            [str(error) for error in form.non_field_errors()],
        )

    def test_the_second_factor_set_up_while_the_form_is_checked_fails_closed(self):
        user = create_active_user("added.between@example.org")
        form = ReauthenticationForm(data={"current_password": PASSWORD}, request=request_of(user))
        self.assertEqual(form.proof, "password")

        AuthenticatorDevice.objects.create_device(user, confirmed=True)

        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())

    def test_the_password_card_fails_closed_the_same_way(self):
        user = create_active_user("added.between.pw@example.org")
        form = ChangePasswordForm(
            user,
            data={"current_password": PASSWORD, "new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD},
            request=request_of(user),
        )
        AuthenticatorDevice.objects.create_device(user, confirmed=True)

        self.assertFalse(form.is_valid())


@tag("mariadb")
class DescriptionOrderAndForcedPageTests(TestCase):
    def test_state_b_describes_the_error_before_the_note(self):
        user = create_active_user("order.b@example.org")
        client = Client()
        client.force_login(user)

        html = client.post(
            reverse("accounts:email_change_request"),
            {"email": "order.new@example.org", "secondary_email": "", "current_password": "wrong password here"},
            REMOTE_ADDR="10.91.3.1",
        ).content.decode()

        tag_ = re.search(r'<input[^>]*name="current_password"[^>]*>', html).group(0)
        self.assertIn('aria-describedby="id_current_password_error id_current_password_note"', tag_)

    def test_the_forced_page_has_the_frame_class_that_sets_hints_and_errors_to_14_px(self):
        # The rules `.register-clear .form-hint` and `.register-clear .invalid-feedback` are 14 px
        # (`test_sizes.py`); the page of the forced change must carry the class.
        for with_code in (True, False):
            user = create_active_user(f"forced.page.{with_code}@example.org")
            if with_code:
                AuthenticatorDevice.objects.create_device(user, confirmed=True)
            user.password_change_required = True
            user.save()
            client = Client()
            client.force_login(user)
            with self.subTest(second_factor=with_code):
                html = client.get(PASSWORD_URL).content.decode()
                self.assertRegex(html, r'<body[^>]*class="[^"]*register-clear')
                self.assertIn('class="form-hint"', html)


@tag("mariadb")
class AbuseProtectionTests(TestCase):
    def setUp(self):
        self.user, self.device = create_user_with_totp("abuse.a@example.org")
        self.client = Client()
        self.client.force_login(self.user)

    def post(self, value, address="10.91.2.1"):
        return self.client.post(DISABLE_URL, {"otp_token": value}, REMOTE_ADDR=address)

    def test_a_shape_error_takes_no_place_of_the_address_and_leaves_no_entry(self):
        for value in ("a password typed by habit", "12", "x" * 40):
            self.post(value)

        self.assertEqual(LoginIpAttempt.objects.count(), 0)
        self.assertEqual(AccessAttempt.objects.count(), 0)
        self.assertEqual(AuthenticatorDevice.objects.get(pk=self.device.pk).failed_codes, 0)

    def test_wrong_codes_never_pause_the_code_entry_of_the_account(self):
        # Fifteen real wrong codes, in sessions that end after five (D-062, block 9): the budget of
        # the account, which also pauses the login, is untouched and no notice about it is sent.
        for _session in range(3):
            client = Client()
            client.force_login(self.user)
            for number in range(second_factor.SESSION_CODE_LIMIT):
                client.post(DISABLE_URL, {"otp_token": f"{number + 100:06d}"}, REMOTE_ADDR="10.91.2.1")

        device = AuthenticatorDevice.objects.get(pk=self.device.pk)
        self.assertEqual(device.failed_codes, 0)
        self.assertFalse(totp.code_attempts_paused(self.user))
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.CODE_ATTEMPTS_WARNING).exists())
        # No failed login of the abuse protection either: the owner can log in right away.
        self.assertEqual(AccessAttempt.objects.count(), 0)
        self.assertEqual(LoginIpAttempt.objects.count(), 0)
        self.assertEqual(self.post(code_for(self.device)).status_code, 302)
