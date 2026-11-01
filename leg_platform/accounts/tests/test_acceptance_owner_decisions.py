"""End-to-end acceptance tests of the owner decisions of 2026-10-15 (D-062), through the test client.

Written by the tester from the decision text (``docs/DECISIONS.md``, D-062, blocks 8 to 10) and not
from the implementation; they use the public URLs only. Each test creates its own accounts and
groups. Test addresses use reserved domains (RFC 2606).

(a) Without a second factor every confirmation of the profile asks for the password.
(b) With a second factor only the 6-digit app code counts: no password field, a recovery code is
    refused and not used up, a posted password alone grants nothing.
(c) "Phone lost": only the password AND a recovery code together remove the second factor.
(d) A holder of a session cannot block the login of the owner by wrong codes or passwords.
(e) Attributes of the code field and of every password field.
(f) No sharp s (the letter of the Swiss spelling rule) in any page that these flows render.
"""

import re
from html.parser import HTMLParser

from axes.models import AccessAttempt
from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts import second_factor, totp
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    LoginIpAttempt,
    MailJob,
    PendingEmailChange,
    RecoveryCode,
    RecoveryCodeDevice,
    SecurityEvent,
    SessionCodeFailure,
    User,
)
from leg_platform.accounts.tests.helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    VerifiedClient,
    age_authentication,
    code_for,
    confirm_code_of,
    create_active_user,
    password_data,
    registration_data,
    run_mail_queue,
)
from leg_platform.accounts.tests.test_second_factor import (
    CODE_URL,
    LOGIN_URL,
    codes_in,
    create_user_with_totp,
    log_in,
    recovery_codes_of,
    send_code,
)

PROFILE = reverse("accounts:profile")
EMAIL_URL = reverse("accounts:email_change_request")
PASSWORD_URL = reverse("accounts:password_change")
DELETE_URL = reverse("accounts:account_delete")
CODES_URL = reverse("accounts:recovery_codes")
DISABLE_URL = reverse("accounts:two_factor_disable")
LOST_URL = reverse("accounts:two_factor_lost_phone")
SETUP_URL = reverse("accounts:two_factor_setup")
LIMIT = second_factor.SESSION_CODE_LIMIT
CAP = second_factor.ACCOUNT_FAILURE_LIMIT
NEW_ADDRESS = "new.owner.decision@example.org"
SECOND_ADDRESS = "second.owner.decision@example.org"
WRONG_PASSWORD = "this is certainly not the password"
WRONG_APP_CODE = "000001"
WRONG_RECOVERY_CODE = "abcd2346"
SHARP_S = "\u00df"
CAPITAL_SHARP_S = "\u1e9e"
MISSING_CODE = "Geben Sie den 2FA-Authentifizierungscode ein."
NOT_A_CODE = "Das ist kein 2FA-Authentifizierungscode."


class _Tags(HTMLParser):
    """Collects the attributes of all ``input`` and ``button`` elements of a page."""

    def __init__(self):
        super().__init__()
        self.inputs = []
        self.buttons = []

    def handle_starttag(self, tag_name, attrs):
        if tag_name == "input":
            self.inputs.append(dict(attrs))
        elif tag_name == "button":
            self.buttons.append(dict(attrs))


def tags_of(html):
    parser = _Tags()
    parser.feed(html)
    return parser


def input_named(html, name):
    found = [item for item in tags_of(html).inputs if item.get("name") == name]
    assert len(found) == 1, f"Expected exactly one input {name}, found {len(found)}."
    return found[0]


def has_input(html, name):
    return any(item.get("name") == name for item in tags_of(html).inputs)


def make_a(email):
    """An account with a second factor (state A) and a logged-in client."""
    user, device = create_user_with_totp(email)
    client = Client()
    client.force_login(user)
    return user, device, client


def make_b(email):
    """An account without a second factor (state B) and a logged-in client."""
    user = create_active_user(email)
    client = Client()
    client.force_login(user)
    return user, client


def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


def owner_logs_in_with_code(user, device, address):
    """The owner logs in with the password and the current app code, from the given address."""
    stepper = Client()
    first = log_in(stepper, user.email, address=address)
    assert first.status_code == 302, "The password step of the owner was refused."
    second = send_code(stepper, code_for(device), address)
    assert second.status_code == 302, "The code step of the owner was refused."
    assert stepper.get(PROFILE).status_code == 200, "The owner has no session after the login."


def owner_logs_in_with_password(user, address):
    stepper = Client()
    first = log_in(stepper, user.email, address=address)
    assert first.status_code == 302, "The login of the owner was refused."
    assert stepper.get(PROFILE).status_code == 200, "The owner has no session after the login."


def nothing_blocks_the_owner(test, user, device=None):
    """The budget of the codes, Axes and the IP counter are untouched and nothing is paused."""
    test.assertEqual(AccessAttempt.objects.count(), 0)
    test.assertEqual(LoginIpAttempt.objects.count(), 0)
    test.assertFalse(totp.code_attempts_paused(user))
    if device is not None:
        test.assertEqual(AuthenticatorDevice.objects.get(pk=device.pk).failed_codes, 0)
    test.assertFalse(
        SecurityEvent.objects.filter(
            event__in=(
                SecurityEvent.Event.CODE_ATTEMPTS_WARNING,
                SecurityEvent.Event.CODE_ATTEMPTS_PAUSED,
                SecurityEvent.Event.LOGIN_LOCKOUT,
            )
        ).exists()
    )


# The five confirmations that exist with a second factor: the address, the password, deleting the
# account, new recovery codes and turning off. Each has the other fields it needs to be form-valid.
FORMS_A = {
    "email": (EMAIL_URL, {"email": NEW_ADDRESS, "secondary_email": ""}),
    "password": (PASSWORD_URL, {"new_password1": OTHER_PASSWORD, "new_password2": OTHER_PASSWORD}),
    "delete": (DELETE_URL, {"confirm": "on"}),
    "codes": (CODES_URL, {}),
    "disable": (DISABLE_URL, {}),
}
# The confirmations without a second factor (new codes and turning off need one). The setup
# asks for the password after more than five minutes (D-055).
FORMS_B = {
    "email": FORMS_A["email"],
    "password": FORMS_A["password"],
    "delete": FORMS_A["delete"],
    "setup": (SETUP_URL, {}),
}


def took_effect(name, user, before=None):
    """Whether the confirmation changed the account."""
    if name == "email":
        return PendingEmailChange.objects.filter(user=user).exists()
    if name == "password":
        return User.objects.get(pk=user.pk).check_password(OTHER_PASSWORD)
    if name == "delete":
        return not User.objects.filter(pk=user.pk).exists()
    if name == "codes":
        return {
            digest
            for digest in RecoveryCode.objects.filter(device__user=user).values_list("digest", flat=True)
        } != before
    if name == "disable":
        return not second_factor.has_second_factor(user)
    if name == "setup":
        return AuthenticatorDevice.objects.filter(user=user).exists()
    raise AssertionError(name)


def digests_of(user):
    return set(RecoveryCode.objects.filter(device__user=user).values_list("digest", flat=True))


def post_form(client, forms, name, address, **fields):
    path, base = forms[name]
    return client.post(path, {**base, **fields}, REMOTE_ADDR=address)


# (a) ---------------------------------------------------------------------------------------------


@tag("mariadb")
class WithoutASecondFactorTheConfirmationsAskForThePasswordTests(TestCase):
    """Owner decision, state B: every confirmation asks for the current password and nothing else."""

    def prepare(self, name):
        user, client = make_b(f"state.b.{name}@example.org")
        if name == "setup":
            age_authentication(client)
        return user, client

    def test_every_confirmation_shows_the_password_field_and_no_code_field(self):
        for name in FORMS_B:
            with self.subTest(form=name):
                _user, client = self.prepare(name)
                if name == "setup":
                    url = f"{PROFILE}?edit=two_factor&step=setup"
                elif name == "delete":
                    url = DELETE_URL  # its own page since 2026-10-24
                else:
                    url = f"{PROFILE}?edit={name}"
                html = client.get(url).content.decode()
                self.assertTrue(has_input(html, "current_password"))
                self.assertFalse(has_input(html, "otp_token"))

    def test_no_password_a_wrong_password_and_a_posted_code_grant_nothing(self):
        for name in FORMS_B:
            with self.subTest(form=name):
                user, client = self.prepare(name)
                address = "10.97.1.1"
                missing = post_form(client, FORMS_B, name, address)
                wrong = post_form(client, FORMS_B, name, address, current_password=WRONG_PASSWORD)
                code_only = post_form(client, FORMS_B, name, address, otp_token="123456")
                for response in (missing, wrong, code_only):
                    self.assertEqual(response.status_code, 200)
                self.assertFalse(took_effect(name, user))

    def test_the_right_password_is_enough(self):
        for name in FORMS_B:
            with self.subTest(form=name):
                user, client = self.prepare(name)
                response = post_form(client, FORMS_B, name, "10.97.1.2", current_password=PASSWORD)
                self.assertIn(response.status_code, (200, 302))
                self.assertTrue(took_effect(name, user))

    def test_a_wrong_password_is_not_counted_against_the_login_of_the_account(self):
        user, client = make_b("state.b.login@example.org")
        for _attempt in range(LIMIT - 1):
            post_form(client, FORMS_B, "delete", "10.97.1.3", current_password=WRONG_PASSWORD)

        nothing_blocks_the_owner(self, user)
        owner_logs_in_with_password(user, "10.97.1.3")


# (b) ---------------------------------------------------------------------------------------------


@tag("mariadb")
class WithASecondFactorOnlyTheAppCodeCountsTests(TestCase):
    def test_every_confirmation_shows_the_code_field_and_no_password_field(self):
        _user, _device, client = make_a("state.a.fields@example.org")
        pages = {
            "email": f"{PROFILE}?edit=email",
            "password": f"{PROFILE}?edit=password",
            "delete": reverse("accounts:account_delete"),
            "codes": f"{PROFILE}?edit=two_factor&step=codes_new",
            "disable": f"{PROFILE}?edit=two_factor&step=disable",
        }
        for name, url in pages.items():
            with self.subTest(form=name):
                html = client.get(url).content.decode()
                self.assertTrue(has_input(html, "otp_token"))
                self.assertFalse(has_input(html, "current_password"))

    def test_a_posted_password_without_a_code_grants_nothing(self):
        for name in FORMS_A:
            with self.subTest(form=name):
                user, device, client = make_a(f"password.only.{name}@example.org")
                before = digests_of(user)

                alone = post_form(client, FORMS_A, name, "10.97.2.1", current_password=PASSWORD)
                with_empty_code = post_form(
                    client, FORMS_A, name, "10.97.2.1", current_password=PASSWORD, otp_token=""
                )

                self.assertContains(alone, MISSING_CODE)
                self.assertContains(with_empty_code, MISSING_CODE)
                self.assertFalse(took_effect(name, user, before))

    def test_a_recovery_code_is_refused_and_not_used_up(self):
        user, device, client = make_a("recovery.refused@example.org")
        codes = recovery_codes_of(user.email)
        before = digests_of(user)
        remaining_before = totp.remaining_recovery_codes(user)
        spelled = [codes[0], f"{codes[0][:4].upper()}-{codes[0][4:]}", codes[1]]

        for name in FORMS_A:
            for value in spelled:
                with self.subTest(form=name, value=value):
                    response = post_form(
                        client, FORMS_A, name, "10.97.2.2", otp_token=value, current_password=PASSWORD
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, NOT_A_CODE)
                    self.assertFalse(took_effect(name, user, before))

        self.assertEqual(totp.remaining_recovery_codes(user), remaining_before)
        self.assertEqual(digests_of(user), before)
        # A shape error counts nowhere: neither for the session nor for the account.
        self.assertFalse(SessionCodeFailure.objects.exists())
        nothing_blocks_the_owner(self, user, device)
        # The proof that the codes are still unused: the login takes the first one.
        stepper = Client()
        log_in(stepper, user.email, address="10.97.2.3")
        response = send_code(stepper, codes[0], "10.97.2.3")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(stepper.get(PROFILE).status_code, 200)
        self.assertEqual(totp.remaining_recovery_codes(user), remaining_before - 1)

    def test_the_app_code_alone_is_enough_and_a_wrong_posted_password_does_not_matter(self):
        for name in FORMS_A:
            with self.subTest(form=name):
                user, device, client = make_a(f"code.alone.{name}@example.org")
                before = digests_of(user)

                response = post_form(
                    client,
                    FORMS_A,
                    name,
                    "10.97.2.4",
                    otp_token=code_for(device),
                    current_password=WRONG_PASSWORD,
                )

                self.assertIn(response.status_code, (200, 302))
                self.assertTrue(took_effect(name, user, before))

    def test_a_wrong_code_grants_nothing_even_with_the_right_password(self):
        for name in FORMS_A:
            with self.subTest(form=name):
                user, _device, client = make_a(f"wrong.code.{name}@example.org")
                before = digests_of(user)

                response = post_form(
                    client, FORMS_A, name, "10.97.2.5", otp_token=WRONG_APP_CODE, current_password=PASSWORD
                )

                self.assertEqual(response.status_code, 200)
                self.assertFalse(took_effect(name, user, before))
                self.assertNotContains(response, PASSWORD)
                self.assertNotContains(response, f'value="{WRONG_APP_CODE}"')

    def test_the_new_recovery_codes_are_shown_after_the_code(self):
        user, device, client = make_a("new.codes.shown@example.org")

        response = post_form(client, FORMS_A, "codes", "10.97.2.6", otp_token=code_for(device))

        self.assertEqual(len(codes_in(response)), totp.RECOVERY_CODE_COUNT)


# (c) ---------------------------------------------------------------------------------------------


@tag("mariadb")
class PhoneLostTests(TestCase):
    def prepare(self, email):
        user, device, client = make_a(email)
        user.set_secondary_email(SECOND_ADDRESS)
        return user, device, client

    def lost(self, client, address="10.97.3.1", **fields):
        return client.post(LOST_URL, fields, REMOTE_ADDR=address)

    def assert_nothing_removed(self, user, remaining, other_session=None):
        self.assertTrue(AuthenticatorDevice.objects.filter(user=user).exists())
        self.assertTrue(RecoveryCodeDevice.objects.filter(user=user).exists())
        self.assertEqual(totp.remaining_recovery_codes(user), remaining)
        self.assertTrue(second_factor.has_second_factor(user))
        if other_session is not None:
            self.assertEqual(other_session.get(PROFILE).status_code, 200)

    def test_the_way_is_reachable_by_address_and_asks_for_both(self):
        _user, _device, client = self.prepare("lost.page@example.org")

        html = client.get(f"{PROFILE}?edit=two_factor&step=lost_phone").content.decode()

        self.assertTrue(has_input(html, "current_password"))
        self.assertTrue(has_input(html, "otp_token"))

    def test_every_combination_except_both_right_removes_nothing(self):
        for position, case in enumerate(
            (
                "wrong password, right recovery code",
                "right password, wrong recovery code",
                "wrong password, wrong recovery code",
                "right password, no code",
                "no password, right recovery code",
                "nothing at all",
                "right password, current app code",
                "right password, recovery code of another account",
                "right password, malformed recovery code",
            )
        ):
            with self.subTest(case=case):
                user, device, client = self.prepare(f"lost.refused.{position}@example.org")
                other = Client()
                other.force_login(user)
                right_code = recovery_codes_of(user.email)[0]
                foreign_user, _foreign_device = create_user_with_totp(f"foreign.{position}@example.org")
                foreign_code = recovery_codes_of(foreign_user.email)[0]
                fields = {
                    "wrong password, right recovery code": {
                        "current_password": WRONG_PASSWORD,
                        "otp_token": right_code,
                    },
                    "right password, wrong recovery code": {
                        "current_password": PASSWORD,
                        "otp_token": WRONG_RECOVERY_CODE,
                    },
                    "wrong password, wrong recovery code": {
                        "current_password": WRONG_PASSWORD,
                        "otp_token": WRONG_RECOVERY_CODE,
                    },
                    "right password, no code": {"current_password": PASSWORD},
                    "no password, right recovery code": {"otp_token": right_code},
                    "nothing at all": {},
                    "right password, current app code": {
                        "current_password": PASSWORD,
                        "otp_token": code_for(device),
                    },
                    "right password, recovery code of another account": {
                        "current_password": PASSWORD,
                        "otp_token": foreign_code,
                    },
                    "right password, malformed recovery code": {
                        "current_password": PASSWORD,
                        "otp_token": "abc",
                    },
                }[case]
                remaining = totp.remaining_recovery_codes(user)
                foreign_remaining = totp.remaining_recovery_codes(foreign_user)
                jobs = MailJob.objects.count()

                response = self.lost(client, **fields)

                self.assertEqual(response.status_code, 200)
                self.assert_nothing_removed(user, remaining, other)
                # A foreign code is not used up either, and no mail went out.
                self.assertEqual(totp.remaining_recovery_codes(foreign_user), foreign_remaining)
                self.assertEqual(MailJob.objects.count(), jobs)
                self.assertEqual(client.get(PROFILE).status_code, 200)

    def test_the_answer_does_not_tell_which_of_the_two_was_wrong(self):
        user, _device, client = self.prepare("lost.same.message@example.org")
        other_user, _other_device, other_client = self.prepare("lost.same.message2@example.org")

        wrong_password = self.lost(
            client, current_password=WRONG_PASSWORD, otp_token=recovery_codes_of(user.email)[0]
        )
        wrong_code = self.lost(
            other_client, current_password=PASSWORD, otp_token=WRONG_RECOVERY_CODE, address="10.97.3.9"
        )

        message = "Das Passwort oder der Wiederherstellungscode ist falsch."
        self.assertContains(wrong_password, message)
        self.assertContains(wrong_code, message)

    def test_both_right_remove_the_second_factor_tell_every_address_and_end_other_sessions(self):
        user, _device, client = self.prepare("lost.success@example.org")
        other_session = logged_in_client(user)
        third_session = logged_in_client(user)
        code = recovery_codes_of(user.email)[0]

        with self.captureOnCommitCallbacks(execute=True):
            response = self.lost(client, current_password=PASSWORD, otp_token=code)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=user).exists())
        self.assertFalse(RecoveryCodeDevice.objects.filter(user=user).exists())
        self.assertFalse(second_factor.has_second_factor(user))
        self.assertEqual(digests_of(user), set())
        # This session stays, every session of another device ends.
        self.assertEqual(client.get(PROFILE).status_code, 200)
        self.assertEqual(other_session.get(PROFILE).status_code, 302)
        self.assertEqual(third_session.get(PROFILE).status_code, 302)
        # Every address is told: the use of the recovery code and the removal.
        run_mail_queue()
        for address in (user.email, SECOND_ADDRESS):
            with self.subTest(address=address):
                self.assertEqual(len([message for message in mail.outbox if address in message.to]), 2)
        events = set(SecurityEvent.objects.filter(account_id=user.pk).values_list("event", flat=True))
        self.assertIn(SecurityEvent.Event.RECOVERY_CODE_USED, events)
        self.assertIn(SecurityEvent.Event.TWO_FACTOR_REMOVED_PHONE_LOST, events)
        # The login needs the password only now.
        owner_logs_in_with_password(user, "10.97.3.2")

    def test_a_second_run_of_the_way_changes_nothing_and_sends_no_more_mail(self):
        user, _device, client = self.prepare("lost.twice@example.org")
        code = recovery_codes_of(user.email)[0]
        self.lost(client, current_password=PASSWORD, otp_token=code)
        run_mail_queue()
        sent = len(mail.outbox)

        again = self.lost(client, current_password=PASSWORD, otp_token=code)

        self.assertIn(again.status_code, (200, 302))
        self.assertFalse(second_factor.has_second_factor(user))
        run_mail_queue()
        self.assertEqual(len(mail.outbox), sent)

    def test_a_recovery_code_that_was_used_at_the_login_cannot_remove_the_second_factor(self):
        user, device, client = self.prepare("lost.used.code@example.org")
        code = recovery_codes_of(user.email)[0]
        stepper = Client()
        log_in(stepper, user.email, address="10.97.3.3")
        self.assertEqual(send_code(stepper, code, "10.97.3.3").status_code, 302)
        remaining = totp.remaining_recovery_codes(user)

        response = self.lost(client, current_password=PASSWORD, otp_token=code)

        self.assertEqual(response.status_code, 200)
        self.assert_nothing_removed(user, remaining)

    def test_after_the_way_the_code_is_gone_and_the_login_needs_the_password_only(self):
        # The code is used once: the recovery codes are deleted with the second factor, so the
        # code exists nowhere any more, and the login no longer asks for a code step.
        user, _device, client = self.prepare("lost.reuse@example.org")
        code = recovery_codes_of(user.email)[0]
        self.lost(client, current_password=PASSWORD, otp_token=code)

        stepper = Client()
        first = log_in(stepper, user.email, address="10.97.3.4")

        self.assertEqual(digests_of(user), set())
        self.assertEqual(RecoveryCodeDevice.objects.filter(user=user).count(), 0)
        self.assertEqual(first.status_code, 302)
        self.assertNotEqual(first.url, CODE_URL)
        self.assertEqual(stepper.get(PROFILE).status_code, 200)

    def test_an_account_without_a_second_factor_cannot_use_the_way(self):
        user, client = make_b("lost.state.b@example.org")

        response = self.lost(client, current_password=PASSWORD, otp_token="abcd2345")

        self.assertNotEqual(response.status_code, 500)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.RECOVERY_CODE_USED).exists())
        self.assertFalse(SecurityEvent.objects.filter(
            event=SecurityEvent.Event.TWO_FACTOR_REMOVED_PHONE_LOST
        ).exists())

    def test_staff_users_cannot_use_the_way(self):
        user, _device = create_user_with_totp("lost.staff@example.org")
        user.set_privileges(is_staff=True, is_superuser=False)
        client = VerifiedClient()
        client.force_login(user)
        remaining = totp.remaining_recovery_codes(user)

        response = self.lost(
            client, current_password=PASSWORD, otp_token=recovery_codes_of(user.email)[0]
        )

        self.assertNotEqual(response.status_code, 500)
        self.assert_nothing_removed(user, remaining)


# (d) ---------------------------------------------------------------------------------------------


def wrong_code(client, name="disable", address="10.97.4.1", value=WRONG_APP_CODE):
    path, base = FORMS_A[name]
    return client.post(path, {**base, "otp_token": value}, REMOTE_ADDR=address)


def wrong_password(client, address="10.97.4.2"):
    return client.post(
        DELETE_URL, {"confirm": "on", "current_password": WRONG_PASSWORD}, REMOTE_ADDR=address
    )


@tag("mariadb")
class AHolderOfASessionCannotBlockTheOwnerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.device = create_user_with_totp("owner.blocked@example.org")
        cls.user.set_secondary_email(SECOND_ADDRESS)

    def setUp(self):
        self.holder = logged_in_client(self.user)
        self.owner_session = logged_in_client(self.user)

    def test_the_control_real_wrong_logins_do_leave_traces(self):
        # Without this, "no Axes entry" below would prove nothing.
        stepper = Client()
        log_in(stepper, self.user.email, password=WRONG_PASSWORD, address="10.97.4.9")
        self.assertGreater(AccessAttempt.objects.count(), 0)

        stepper = Client()
        log_in(stepper, self.user.email, address="10.97.4.8")
        send_code(stepper, WRONG_APP_CODE, "10.97.4.8")
        self.assertEqual(AuthenticatorDevice.objects.get(pk=self.device.pk).failed_codes, 1)

    def test_four_wrong_codes_in_every_confirmation_block_nothing(self):
        for name in FORMS_A:
            if name == "delete" or name == "password":
                continue  # one run of each is enough below; the loop stays below the limit
            for _attempt in range(LIMIT - 1):
                self.assertEqual(wrong_code(self.holder, name).status_code, 200)
            # A right entry starts the count again (D-062).
            second_factor_ok = self.holder.post(
                CODES_URL, {"otp_token": code_for(self.device)}, REMOTE_ADDR="10.97.4.1"
            )
            self.assertEqual(second_factor_ok.status_code, 200)

        nothing_blocks_the_owner(self, self.user, self.device)
        self.assertEqual(self.holder.get(PROFILE).status_code, 200)
        # The owner logs in from the same address as the holder.
        owner_logs_in_with_code(self.user, self.device, "10.97.4.1")

    def test_the_fifth_wrong_code_ends_only_the_session_of_the_holder(self):
        for _attempt in range(LIMIT - 1):
            self.assertEqual(wrong_code(self.holder).status_code, 200)

        last = wrong_code(self.holder)

        self.assertRedirects(last, LOGIN_URL, fetch_redirect_response=False)
        self.assertEqual(self.holder.get(PROFILE).status_code, 302)
        self.assertEqual(self.owner_session.get(PROFILE).status_code, 200)
        nothing_blocks_the_owner(self, self.user, self.device)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.SESSION_ENDED_WRONG_CODES).count(), 1
        )
        run_mail_queue()
        self.assertEqual(
            sorted(address for message in mail.outbox for address in message.to),
            sorted([self.user.email, SECOND_ADDRESS]),
        )
        # The owner logs in with the password and the code, from the address of the holder.
        owner_logs_in_with_code(self.user, self.device, "10.97.4.1")

    def test_fifteen_wrong_entries_in_all_sessions_end_every_session_and_block_nobody(self):
        sessions = [self.holder, self.owner_session, logged_in_client(self.user), logged_in_client(self.user)]
        last = None
        for number in range(CAP):
            last = wrong_code(sessions[number // (LIMIT - 1)], address=f"10.97.5.{number}")

        self.assertRedirects(last, LOGIN_URL, fetch_redirect_response=False)
        for client in sessions:
            self.assertEqual(client.get(PROFILE).status_code, 302)
        self.assertEqual(
            SecurityEvent.objects.filter(event=SecurityEvent.Event.ALL_SESSIONS_ENDED).count(), 1
        )
        run_mail_queue()
        self.assertEqual(
            sorted(address for message in mail.outbox for address in message.to),
            sorted([self.user.email, SECOND_ADDRESS]),
        )
        nothing_blocks_the_owner(self, self.user, self.device)
        owner_logs_in_with_code(self.user, self.device, "10.97.5.1")

    def test_the_wrong_codes_of_the_phone_lost_way_block_nobody_either(self):
        for value in ("abcd2346", "abcd2347", "abcd2342", "abcd2343", "abcd2344"):
            response = self.holder.post(
                LOST_URL, {"current_password": PASSWORD, "otp_token": value}, REMOTE_ADDR="10.97.4.5"
            )

        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        self.assertEqual(totp.remaining_recovery_codes(self.user), len(recovery_codes_of(self.user.email)))
        nothing_blocks_the_owner(self, self.user, self.device)
        owner_logs_in_with_code(self.user, self.device, "10.97.4.5")

    def test_wrong_passwords_in_the_phone_lost_way_block_nobody_either(self):
        for _attempt in range(LIMIT):
            response = self.holder.post(
                LOST_URL,
                {"current_password": WRONG_PASSWORD, "otp_token": recovery_codes_of(self.user.email)[0]},
                REMOTE_ADDR="10.97.4.6",
            )

        self.assertRedirects(response, LOGIN_URL, fetch_redirect_response=False)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user).exists())
        nothing_blocks_the_owner(self, self.user, self.device)
        owner_logs_in_with_code(self.user, self.device, "10.97.4.6")


@tag("mariadb")
class AHolderOfASessionWithoutASecondFactorCannotBlockTheOwnerTests(TestCase):
    def test_five_wrong_passwords_end_only_the_session_of_the_holder(self):
        user = create_active_user("owner.b.blocked@example.org")
        holder = logged_in_client(user)
        owner_session = logged_in_client(user)
        for _attempt in range(LIMIT - 1):
            self.assertEqual(wrong_password(holder).status_code, 200)

        last = wrong_password(holder)

        self.assertRedirects(last, LOGIN_URL, fetch_redirect_response=False)
        self.assertEqual(holder.get(PROFILE).status_code, 302)
        self.assertEqual(owner_session.get(PROFILE).status_code, 200)
        nothing_blocks_the_owner(self, user)
        self.assertTrue(User.objects.get(pk=user.pk).check_password(PASSWORD))
        owner_logs_in_with_password(user, "10.97.4.2")

    def test_fifteen_wrong_passwords_in_all_sessions_end_every_session(self):
        user = create_active_user("owner.b.cap@example.org")
        sessions = [logged_in_client(user) for _number in range(4)]
        last = None
        for number in range(CAP):
            last = wrong_password(sessions[number // (LIMIT - 1)], address=f"10.97.6.{number}")

        self.assertRedirects(last, LOGIN_URL, fetch_redirect_response=False)
        for client in sessions:
            self.assertEqual(client.get(PROFILE).status_code, 302)
        nothing_blocks_the_owner(self, user)
        owner_logs_in_with_password(user, "10.97.6.1")


# (e) ---------------------------------------------------------------------------------------------


@tag("mariadb")
class FieldAttributesTests(TestCase):
    FORBIDDEN = ("inputmode", "maxlength", "pattern")

    def assert_plain_code_field(self, html):
        attrs = input_named(html, "otp_token")
        self.assertEqual(attrs.get("type"), "text")
        for attribute in self.FORBIDDEN:
            self.assertNotIn(attribute, attrs)

    def assert_every_password_field_has_the_eye(self, html):
        fields = [item for item in tags_of(html).inputs if item.get("type") == "password"]
        self.assertTrue(fields, "The page has no password field.")
        for item in fields:
            with self.subTest(field=item.get("name")):
                self.assertTrue(item.get("id"))
                button = re.search(
                    r'<button[^>]*data-password-toggle[^>]*aria-controls="%s"[^>]*>(.*?)</button>'
                    % re.escape(item["id"]),
                    html,
                    re.S,
                )
                self.assertIsNotNone(button, "No eye button controls this password field.")
                self.assertIn('class="eye"', button.group(1))

    def test_the_code_field_of_every_confirmation_is_a_plain_text_field(self):
        _user, _device, client = make_a("attrs.code@example.org")
        for url in (
            f"{PROFILE}?edit=email",
            f"{PROFILE}?edit=password",
            reverse("accounts:account_delete"),
            f"{PROFILE}?edit=two_factor&step=codes_new",
            f"{PROFILE}?edit=two_factor&step=disable",
            f"{PROFILE}?edit=two_factor&step=lost_phone",
        ):
            with self.subTest(page=url):
                self.assert_plain_code_field(client.get(url).content.decode())

    def test_the_code_field_stays_a_plain_text_field_in_an_error_answer(self):
        _user, _device, client = make_a("attrs.error@example.org")
        for name in FORMS_A:
            with self.subTest(form=name):
                self.assert_plain_code_field(wrong_code(client, name, value="123abc").content.decode())

    def test_every_password_field_is_hidden_and_has_the_eye_element(self):
        _user_b, client_b = make_b("attrs.password.b@example.org")
        age_authentication(client_b)
        _user_a, _device, client_a = make_a("attrs.password.a@example.org")
        pages = (
            (client_b, f"{PROFILE}?edit=email"),
            (client_b, f"{PROFILE}?edit=password"),
            (client_b, reverse("accounts:account_delete")),
            (client_b, f"{PROFILE}?edit=two_factor&step=setup"),
            (client_a, f"{PROFILE}?edit=password"),
            (client_a, f"{PROFILE}?edit=two_factor&step=lost_phone"),
            (Client(), LOGIN_URL),
        )
        for client, url in pages:
            with self.subTest(page=url):
                self.assert_every_password_field_has_the_eye(client.get(url).content.decode())

    def test_the_password_field_of_the_error_answers_keeps_the_eye_and_no_value(self):
        _user, client = make_b("attrs.password.error@example.org")

        html = client.post(
            DELETE_URL, {"confirm": "on", "current_password": WRONG_PASSWORD}, REMOTE_ADDR="10.97.7.1"
        ).content.decode()

        self.assert_every_password_field_has_the_eye(html)
        self.assertNotIn(WRONG_PASSWORD, html)

    def test_the_code_step_of_the_login_has_a_plain_text_field_too(self):
        user, _device = create_user_with_totp("attrs.login.code@example.org")
        stepper = Client()
        log_in(stepper, user.email, address="10.97.7.2")

        html = stepper.get(CODE_URL).content.decode()

        attrs = input_named(html, "otp_token")
        self.assertEqual(attrs.get("type"), "text")


# (f) ---------------------------------------------------------------------------------------------


@tag("mariadb")
class NoSharpSInRenderedPagesTests(TestCase):
    # The details step of the registration reads the address directory (the alias "addresses").
    databases = {"default", "addresses"}

    def assert_clean(self, html, label):
        self.assertNotIn(SHARP_S, html, f"{label} contains the sharp s.")
        self.assertNotIn(CAPITAL_SHARP_S, html, f"{label} contains the capital sharp s.")

    def test_login_and_registration(self):
        client = Client()
        for name in ("accounts:login", "accounts:registration", "accounts:password_reset"):
            with self.subTest(page=name):
                self.assert_clean(client.get(reverse(name)).content.decode(), name)

    def test_the_profile_in_every_card_state_without_a_second_factor(self):
        _user, client = make_b("sharp.s.b@example.org")
        states = ["", "?edit=personal", "?edit=email", "?edit=password", "?edit=delete"]
        states.append("?edit=two_factor&step=setup")
        for state in states:
            with self.subTest(state=state):
                self.assert_clean(client.get(PROFILE + state).content.decode(), state)
        # Answers with errors: the confirmation texts of state B.
        for name in FORMS_B:
            with self.subTest(error=name):
                self.assert_clean(
                    post_form(client, FORMS_B, name, "10.97.8.1", current_password=WRONG_PASSWORD)
                    .content.decode(),
                    name,
                )

    def test_the_profile_in_every_card_state_with_a_second_factor(self):
        user, device, client = make_a("sharp.s.a@example.org")
        states = [
            "",
            "?edit=personal",
            "?edit=email",
            "?edit=password",
            "?edit=delete",
            "?edit=two_factor",
            "?edit=two_factor&step=setup",
            "?edit=two_factor&step=codes_new",
            "?edit=two_factor&step=disable",
            "?edit=two_factor&step=lost_phone",
        ]
        for state in states:
            with self.subTest(state=state):
                self.assert_clean(client.get(PROFILE + state).content.decode(), state)
        # The answers with errors: wrong code, no code, shape error, phone lost with wrong data.
        for name in FORMS_A:
            for value in (WRONG_APP_CODE, "", "abcd2345"):
                with self.subTest(error=name, value=value):
                    self.assert_clean(wrong_code(client, name, value=value).content.decode(), name)
        lost = client.post(
            LOST_URL, {"current_password": WRONG_PASSWORD, "otp_token": WRONG_RECOVERY_CODE}
        )
        self.assert_clean(lost.content.decode(), "phone lost, wrong")
        shape = client.post(LOST_URL, {"current_password": PASSWORD, "otp_token": "abc"})
        self.assert_clean(shape.content.decode(), "phone lost, shape")
        codes = post_form(client, FORMS_A, "codes", "10.97.8.2", otp_token=code_for(device))
        self.assert_clean(codes.content.decode(), "new recovery codes")

    def test_the_pages_after_the_end_of_a_session_and_the_login_code_step(self):
        user, device, client = make_a("sharp.s.ended@example.org")
        for _attempt in range(LIMIT):
            wrong_code(client)
        self.assert_clean(client.get(LOGIN_URL).content.decode(), "login after the end of the session")
        stepper = Client()
        log_in(stepper, user.email, address="10.97.8.3")
        self.assert_clean(stepper.get(CODE_URL).content.decode(), "login code step")
        self.assert_clean(
            send_code(stepper, WRONG_APP_CODE, "10.97.8.3").content.decode(), "login code step, wrong"
        )

    def test_the_registration_steps(self):
        client = Client()
        client.post(reverse("accounts:registration"), registration_data("sharp.s.new@example.org"))
        run_mail_queue()
        self.assert_clean(
            client.get(reverse("accounts:registration_sent")).content.decode(), "registration, code step"
        )
        refused = client.post(reverse("accounts:registration_sent"), {"otp_token": "000000"})
        self.assert_clean(refused.content.decode(), "registration, code step with an error")
        confirm_code_of(client, mail.outbox[-1])
        method = client.get(reverse("accounts:registration_method"))
        self.assert_clean(method.content.decode(), "registration, method step")
        reference = re.search(r'name="registration" value="(\d+)"', method.content.decode()).group(1)
        client.post(reverse("accounts:registration_method"), {"method": "password", "registration": reference})
        page = client.get(reverse("accounts:registration_password"))
        self.assert_clean(page.content.decode(), "registration, password step")
        wrong = client.post(
            reverse("accounts:registration_password"),
            password_data("short", registration=reference),
        )
        self.assert_clean(wrong.content.decode(), "registration, password step with an error")
        client.post(reverse("accounts:registration_password"), password_data(registration=reference))
        details = client.get(reverse("accounts:registration_complete"))
        self.assertEqual(details.status_code, 200)
        self.assert_clean(details.content.decode(), "registration, details step")

    def test_the_admin_dashboard(self):
        administrator = create_active_user("sharp.s.admin@example.org")
        administrator.set_privileges(is_staff=True, is_superuser=True)
        client = VerifiedClient()
        client.force_login(administrator)

        response = client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assert_clean(response.content.decode(), "admin dashboard")
