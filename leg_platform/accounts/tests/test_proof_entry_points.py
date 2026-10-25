"""Negative authorization, replay and limits of the proof entry points of the flow "Change sign-in" and of the passkey
wizard (security review and tester gap list, round 4; D-062, D-106, D-112).

Every case asserts what did NOT happen: no session ended, no mail, no security event, no device or passkey changed.
"""

import json
import secrets
import time
from unittest.mock import patch

from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse

from leg_platform.accounts import login_methods, passkey_methods, second_factor, sign_in_change
from leg_platform.accounts.models import AuthenticatorDevice, MailJob, Passkey, RecoveryCode, SecurityEvent, User
from leg_platform.accounts.tests import test_passkey_wizard as wizard
from leg_platform.accounts.tests import test_sign_in_change as flow
from leg_platform.accounts.tests.helpers import PASSWORD, code_for, create_active_user
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp

LOGIN = reverse("accounts:login")
PROFILE = reverse("accounts:profile")
SETUP = reverse("accounts:passkey_setup")
MANAGE = reverse("accounts:passkeys")
LIFETIME = sign_in_change.LIFETIME.total_seconds()
WRONG_CODE = "000001"
# A well-formed assertion that no ceremony belongs to: a wrong proof that the counter of the session counts.
BAD_PASSKEY_PROOF = json.dumps({
    "ceremony": "00000000-0000-0000-0000-000000000000",
    "credential": {"type": "public-key", "id": "AAAA", "rawId": "AAAA", "response": {}},
})


def state(user):
    """What a refused request must leave as it was: the session version, the mails and the security events."""
    user.refresh_from_db()
    return user.session_version, MailJob.objects.count(), SecurityEvent.objects.count()


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


@tag("mariadb")
class FlowRefusalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("password.person@example.org")
        cls.code_user, cls.device = create_user_with_totp("code.person@example.org")

    # Anonymous visitors
    def test_anonymous_requests_reach_no_step_and_change_nothing(self):
        anonymous = Client()
        before = state(self.code_user)
        data = {"method": login_methods.PASSKEY, "current_password": PASSWORD, "otp_token": WRONG_CODE}
        for step in ("proof", "choose", "same", "disable", "scan", "number", "cancel"):
            for verb in (anonymous.get, anonymous.post):
                with self.subTest(step=step, verb=verb.__name__):
                    response = verb(f"{flow.FLOW}?step={step}", data)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith(LOGIN))
        self.assertEqual(state(self.code_user), before)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.code_user, confirmed=True).exists())
        self.assertNotIn(passkey_methods.SETUP_KEY, anonymous.session)

    # The grant belongs to one session
    def test_the_grant_of_one_session_is_no_grant_of_another_session_of_the_same_user(self):
        proving = logged_in(self.code_user)
        flow.prove(proving, device=self.device)
        other = logged_in(self.code_user)
        before = state(self.code_user)

        for url in (flow.CHOOSE, flow.SAME, flow.DISABLE):
            self.assertRedirects(other.get(url), flow.FLOW, fetch_redirect_response=False)
        for url, data in ((flow.DISABLE, {}), (flow.CHOOSE, {"method": login_methods.PASSKEY})):
            self.assertRedirects(other.post(url, data), flow.FLOW, fetch_redirect_response=False)

        self.assertEqual(state(self.code_user), before)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.code_user, confirmed=True).exists())
        self.assertNotIn(passkey_methods.SETUP_KEY, other.session)
        self.assertEqual(proving.get(flow.CHOOSE).status_code, 200)

    # S1: turning off a number code that is not there
    def test_turning_the_number_code_off_without_one_is_refused_without_a_side_effect(self):
        client = logged_in(self.user)
        flow.prove(client)
        other = logged_in(self.user)
        before = state(self.user)

        response = client.post(flow.DISABLE)

        self.assertRedirects(response, flow.CARD, fetch_redirect_response=False)
        self.assertEqual(state(self.user), before)
        self.assertEqual(other.get(PROFILE).status_code, 200)
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_DISABLED).exists())
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.TWO_FACTOR_DISABLED).exists())

    def test_the_rule_is_checked_again_under_the_lock(self):
        from leg_platform.accounts import totp

        with self.assertRaises(totp.SecondFactorError):
            totp.disable(self.user)
        self.assertEqual(MailJob.objects.count(), 0)

    # Expired, future and broken grants
    def expire(self, client, how):
        session = client.session
        key = sign_in_change.SESSION_KEY
        if how == "future":
            session[key]["at"] = time.time() + 600
        elif how == "old":
            session[key]["at"] = time.time() - LIFETIME - 1
        else:
            session[key] = how
        session.save()

    def test_an_expired_future_or_broken_grant_refuses_the_steps_that_act(self):
        for how in ("old", "future", "not a dict", ["a"], None, 5):
            with self.subTest(grant=how):
                client = logged_in(self.code_user)
                flow.prove(client, device=self.device)
                self.expire(client, how)
                before = state(self.code_user)
                self.assertRedirects(client.post(flow.DISABLE), flow.FLOW, fetch_redirect_response=False)
                self.assertRedirects(client.get(flow.CHOOSE), flow.FLOW, fetch_redirect_response=False)
                self.assertEqual(state(self.code_user), before)
                self.assertTrue(AuthenticatorDevice.objects.filter(user=self.code_user, confirmed=True).exists())
                password_client = logged_in(self.user)
                flow.prove(password_client)
                self.expire(password_client, how)
                response = password_client.post(flow.CHOOSE, {"method": login_methods.PASSKEY})
                self.assertRedirects(response, flow.FLOW, fetch_redirect_response=False)
                self.assertNotIn(passkey_methods.SETUP_KEY, password_client.session)

    def test_the_grant_ends_exactly_at_the_lifetime(self):
        client = logged_in(self.user)
        flow.prove(client)
        at = client.session[sign_in_change.SESSION_KEY]["at"]

        with patch("leg_platform.accounts.sign_in_change.time.time", return_value=at + LIFETIME - 0.001):
            self.assertEqual(client.get(flow.CHOOSE).status_code, 200)
        with patch("leg_platform.accounts.sign_in_change.time.time", return_value=at + LIFETIME):
            self.assertRedirects(client.get(flow.CHOOSE), flow.FLOW, fetch_redirect_response=False)
            self.assertRedirects(
                client.post(flow.CHOOSE, {"method": login_methods.PASSKEY}), flow.FLOW, fetch_redirect_response=False
            )
        self.assertNotIn(passkey_methods.SETUP_KEY, client.session)

    # S4: the window of five minutes is not doubled by the hand-over
    def test_the_passkey_setup_grant_keeps_the_time_of_the_original_proof(self):
        client = logged_in(self.user)
        flow.prove(client)
        at = client.session[sign_in_change.SESSION_KEY]["at"]

        response = flow.choose(client, login_methods.PASSKEY)

        self.assertRedirects(response, SETUP, fetch_redirect_response=False)
        self.assertEqual(client.session[passkey_methods.SETUP_KEY]["started"], at)
        with patch("leg_platform.accounts.passkey_methods.time.time", return_value=at + LIFETIME - 0.001):
            self.assertTrue(client.get(SETUP).context["allowed"])
        with patch("leg_platform.accounts.passkey_methods.time.time", return_value=at + LIFETIME + 1):
            self.assertFalse(client.get(SETUP).context["allowed"])

    def test_a_handed_over_time_in_the_future_or_of_a_wrong_type_counts_as_now(self):
        from types import SimpleNamespace

        from django.contrib.sessions.backends.db import SessionStore

        for started in (time.time() + 3600, "yesterday", None, True, [1]):
            with self.subTest(started=started):
                request = SimpleNamespace(session=SessionStore(), user=self.user)
                before = time.time()
                passkey_methods.allow_setup(request, started=started)
                value = request.session[passkey_methods.SETUP_KEY]["started"]
                self.assertTrue(before <= value <= time.time())

    def test_a_broken_setup_grant_is_no_grant(self):
        from types import SimpleNamespace

        from django.contrib.sessions.backends.db import SessionStore

        for broken in ("x", ["a"], 5, None):
            request = SimpleNamespace(session=SessionStore(), user=self.user)
            request.session[passkey_methods.SETUP_KEY] = broken
            self.assertFalse(passkey_methods.setup_allowed(request))

    # Replay
    def test_a_second_turn_off_needs_a_new_proof_and_acts_once(self):
        client = logged_in(self.code_user)
        flow.prove(client, device=self.device)
        self.assertRedirects(client.post(flow.DISABLE), flow.CARD, fetch_redirect_response=False)
        after_first = state(self.code_user)

        self.assertRedirects(client.post(flow.DISABLE), flow.FLOW, fetch_redirect_response=False)

        self.assertEqual(state(self.code_user), after_first)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_DISABLED).count(), 1)

    def test_the_same_number_cannot_confirm_twice(self):
        client = logged_in(self.user)
        flow.prove(client)
        flow.choose(client, login_methods.PASSWORD_AND_APP)
        device = AuthenticatorDevice.objects.get(user=self.user)
        code = code_for(device)
        self.assertEqual(client.post(flow.NUMBER, {"otp_token": code}).status_code, 200)
        after_first = state(self.user)

        second = client.post(flow.NUMBER, {"otp_token": code})

        self.assertRedirects(second, flow.CHOOSE, fetch_redirect_response=False)
        self.assertEqual(state(self.user), after_first)
        self.assertEqual(RecoveryCode.objects.filter(device__user=self.user).count(), 10)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_ENABLED).count(), 1)

    # The setup screens need a started setup
    def test_scan_and_number_without_an_enrolment_change_and_show_nothing(self):
        for granted in (False, True):
            client = logged_in(self.user)
            if granted:
                flow.prove(client)
            before = state(self.user)
            for url in (flow.SCAN, flow.NUMBER):
                self.assertEqual(client.get(url).status_code, 302)
                self.assertTrue(client.get(url)["Location"].startswith(flow.FLOW))
            for url in (flow.SCAN, flow.NUMBER):
                response = client.post(url, {"otp_token": WRONG_CODE})
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(flow.FLOW))
            self.assertEqual(state(self.user), before)
            self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())

    # S5: cancelling the setup of the number code
    def test_cancelling_the_number_code_setup_deletes_the_unconfirmed_secret_only(self):
        client = logged_in(self.user)
        flow.prove(client)
        flow.choose(client, login_methods.PASSWORD_AND_APP)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.user, confirmed=False).exists())

        self.assertRedirects(client.post(flow.CANCEL), flow.CARD, fetch_redirect_response=False)

        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())
        # A confirmed number code of the account is never touched by a cancel.
        other = logged_in(self.code_user)
        other.post(flow.CANCEL)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=self.code_user, confirmed=True).exists())

    # Odd parameters
    def test_repeated_empty_and_upper_case_parameters_do_nothing_surprising(self):
        client = logged_in(self.user)
        flow.prove(client)
        before = state(self.user)

        # The last value counts, as everywhere in Django: here "proof", which needs a password.
        response = client.post(f"{flow.FLOW}?step=choose&step=proof", {"method": login_methods.PASSKEY})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bitte bestätigen Sie sich")
        for step in ("", "CHOOSE", "Choose", "choose ", "codes", "passkey"):
            with self.subTest(step=step):
                flow.prove(client)
                response = client.get(f"{flow.FLOW}?step={step}")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Bitte bestätigen Sie sich")
                self.assertNotContains(response, "Diese Codes brauchen Sie")
        flow.prove(client)
        response = client.post(f"{flow.FLOW}?step=codes", {"otp_token": WRONG_CODE})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Diese Codes brauchen Sie")
        self.assertEqual(state(self.user), before)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())

    def test_a_repeated_method_parameter_acts_on_the_last_value_only(self):
        client = logged_in(self.user)
        flow.prove(client)

        response = client.post(flow.CHOOSE, {"method": [login_methods.PASSWORD_AND_APP, login_methods.PASSKEY]})

        self.assertRedirects(response, SETUP, fetch_redirect_response=False)
        self.assertFalse(AuthenticatorDevice.objects.filter(user=self.user).exists())


@tag("mariadb")
class WrongProofLimitTests(TestCase):
    """Five wrong proofs end the session on the entry points of the new screens (D-062, ``SESSION_CODE_LIMIT``)."""

    def five_wrong(self, client, url, data, address):
        limit = second_factor.SESSION_CODE_LIMIT
        for attempt in range(limit - 1):
            response = client.post(url, data, REMOTE_ADDR=address)
            self.assertEqual(response.status_code, 200, f"attempt {attempt + 1}")
        return client.post(url, data, REMOTE_ADDR=address)

    def assert_ended(self, client, last):
        self.assertRedirects(last, LOGIN, fetch_redirect_response=False)
        self.assertRedirects(client.get(PROFILE), f"{LOGIN}?next={PROFILE}", fetch_redirect_response=False)

    def test_the_flow_ends_the_session_after_five_wrong_passwords(self):
        user = create_active_user("limit.password@example.org")
        client = logged_in(user)

        last = self.five_wrong(client, flow.FLOW, {"current_password": "not the password at all"}, "10.94.0.1")

        self.assert_ended(client, last)
        self.assertNotIn(sign_in_change.SESSION_KEY, client.session)
        self.assertTrue(User.objects.get(pk=user.pk).check_password(PASSWORD))

    def test_the_flow_ends_the_session_after_five_wrong_number_codes(self):
        user, _device = create_user_with_totp("limit.code@example.org")
        client = logged_in(user)

        last = self.five_wrong(client, flow.FLOW, {"otp_token": WRONG_CODE}, "10.94.0.2")

        self.assert_ended(client, last)
        self.assertTrue(AuthenticatorDevice.objects.filter(user=user, confirmed=True).exists())


@override_settings(INSTANCE_BASE_URL="https://portal.example.org")
@tag("mariadb")
class PasskeyEntryPointTests(wizard.WizardFixture, TestCase):
    # Anonymous visitors
    def test_anonymous_requests_reach_neither_the_setup_nor_a_victims_keys(self):
        keys = self.passkey_user(count=2)
        anonymous = Client()
        before = state(self.user)
        options = self.json_post("/passkeys/options/", {"purpose": "register", "mode": "another_device"}, client=anonymous)
        self.assertIn(options.status_code, (400, 403))

        for response in (
            self.json_post(SETUP, {"ceremony": "x", "credential": {}, "name": "Laptop"}, client=anonymous),
            anonymous.post(SETUP, {"activate": "yes"}, secure=True),
            anonymous.post(MANAGE, {"credential": keys[0].pk, "passkey_assertion": BAD_PASSKEY_PROOF}, secure=True),
        ):
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response["Location"].startswith(LOGIN))

        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        self.assertEqual(state(self.user), before)

    # S2: five wrong proofs
    def test_five_wrong_passkey_proofs_end_the_session_at_the_setup(self):
        self.passkey_user(count=1)
        self.login(passkey=True)
        client = self.client
        for attempt in range(second_factor.SESSION_CODE_LIMIT - 1):
            response = client.post(SETUP, {"passkey_assertion": BAD_PASSKEY_PROOF}, secure=True, REMOTE_ADDR="10.94.1.1")
            self.assertEqual(response.status_code, 200, f"attempt {attempt + 1}")
        last = client.post(SETUP, {"passkey_assertion": BAD_PASSKEY_PROOF}, secure=True, REMOTE_ADDR="10.94.1.1")
        self.assertRedirects(last, LOGIN, fetch_redirect_response=False)
        self.assertEqual(client.get(PROFILE, secure=True).status_code, 302)
        self.assertNotIn(passkey_methods.SETUP_KEY, client.session)

    def test_five_wrong_passkey_proofs_end_the_session_at_the_removal(self):
        keys = self.passkey_user(count=2)
        self.login(passkey=True)
        client = self.client
        data = {"credential": keys[0].pk, "passkey_assertion": BAD_PASSKEY_PROOF}
        for attempt in range(second_factor.SESSION_CODE_LIMIT - 1):
            response = client.post(MANAGE, data, secure=True, REMOTE_ADDR="10.94.1.2")
            self.assertEqual(response.status_code, 200, f"attempt {attempt + 1}")
        last = client.post(MANAGE, data, secure=True, REMOTE_ADDR="10.94.1.2")
        self.assertRedirects(last, LOGIN, fetch_redirect_response=False)
        self.assertEqual(client.get(PROFILE, secure=True).status_code, 302)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)

    # Removal: keys of others and odd identifiers
    def test_the_removal_refuses_every_key_that_is_not_an_active_key_of_the_account(self):
        victim_keys = self.passkey_user(count=2)
        victim = self.user
        staged_victim = Passkey.objects.create(
            user=victim, credential_digest="a" * 64, credential_id=b"s", public_key=b"s", name="Staged", active=False,
        )
        attacker = create_active_user("attacker@example.org")
        self.user, self.request.user = attacker, attacker
        self.request.session = type(self.request.session)()
        own = self.passkey_user(count=2)
        own_staged = Passkey.objects.create(
            user=attacker, credential_digest="b" * 64, credential_id=b"t", public_key=b"t", name="Own staged",
            active=False,
        )
        self.login(passkey=True)
        before_attacker = state(attacker)
        before_victim = state(victim)
        cases = {
            "a key of the victim": victim_keys[0].pk,
            "a staged key of the victim": staged_victim.pk,
            "an own staged key": own_staged.pk,
            "no such key": 10 ** 9,
            "negative": -1,
            "not a number": "abc",
            "empty": "",
            "sql": f"{own[0].pk} OR 1=1",
            "huge": "9" * 40,
            "list ending in the victim's key": [own[0].pk, victim_keys[1].pk],
        }
        for number, (name, credential) in enumerate(cases.items(), start=1):
            with self.subTest(case=name):
                options = self.options("proof", action=MANAGE)
                # The signature counter of the passkey must rise with every proof (a replayed counter is refused).
                response = self.post(MANAGE, {"credential": credential, "passkey_assertion": json.dumps({
                    "ceremony": options["ceremony"], "credential": self.assertion(options, count=number)})})
                self.assertEqual(response.status_code, 200, f"{name}: {response.get('Location')}")
                self.assertEqual(self.client.get(PROFILE, secure=True).status_code, 200)

        self.assertEqual(victim.passkeys.filter(active=True).count(), 2)
        self.assertEqual(attacker.passkeys.filter(active=True).count(), 2)
        self.assertTrue(Passkey.objects.filter(pk__in=[staged_victim.pk, own_staged.pk]).count() == 2)
        self.assertEqual(state(attacker), before_attacker)
        self.assertEqual(state(victim), before_victim)

    def test_a_proof_cannot_be_used_twice(self):
        keys = self.passkey_user(count=3)
        self.login(passkey=True)
        options = self.options("proof", action=MANAGE)
        proof = json.dumps({"ceremony": options["ceremony"], "credential": self.assertion(options)})
        first = self.post(MANAGE, {"credential": keys[0].pk, "passkey_assertion": proof})
        self.assertRedirects(first, LOGIN, fetch_redirect_response=False)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        self.user.refresh_from_db()
        self.login(passkey=True)
        after_first = state(self.user)

        replay = self.post(MANAGE, {"credential": keys[1].pk, "passkey_assertion": proof})

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 2)
        self.assertEqual(state(self.user), after_first)

    # The setup grant
    def stage_first_key(self):
        """A password account with a staged key and its setup grant: the state before the confirmation."""
        self.login()
        self.grant()
        self.post(f"{wizard.SETUP}?step=name", {"name": "Mein Handy"})
        self.assertEqual(self.create_key().status_code, 200)
        self.assertEqual(self.user.passkeys.filter(active=False).count(), 1)

    def expire_setup_grant(self, how):
        session = self.client.session
        grant = session[passkey_methods.SETUP_KEY]
        if how == "old":
            grant["started"] = time.time() - LIFETIME - 1
        elif how == "future":
            grant["started"] = time.time() + 600
        else:
            session[passkey_methods.SETUP_KEY] = how
        session.save()

    def test_an_expired_or_broken_setup_grant_activates_nothing_and_keeps_the_password(self):
        for how in ("old", "future", "not a dict", None):
            with self.subTest(grant=how):
                self.user = create_active_user(f"expired.{abs(hash(str(how)))}@example.org")
                self.request.user = self.user
                self.request.session = type(self.request.session)()
                self.client = Client()
                self.stage_first_key()
                self.expire_setup_grant(how)
                before = state(self.user)

                response = self.post(wizard.SETUP, {"activate": "yes"})
                named = self.post(f"{wizard.SETUP}?step=name", {"name": "Other"})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(named.status_code, 200)
                self.assertEqual(self.client.session[wizard.passkey_views.NAME_KEY], "Mein Handy")
                self.user.refresh_from_db()
                self.assertEqual(self.user.authentication_method, "password")
                self.assertTrue(self.user.has_usable_password())
                self.assertEqual(self.user.passkeys.filter(active=True).count(), 0)
                self.assertEqual(state(self.user), before)

    def test_the_grant_of_one_session_is_no_grant_of_another_session(self):
        self.stage_first_key()
        other = logged_in(self.user)
        before = state(self.user)

        page = other.get(f"{SETUP}?step=device", secure=True)
        activated = other.post(SETUP, {"activate": "yes"}, secure=True)
        registered = self.json_post(SETUP, {"ceremony": "x", "credential": {}, "name": "Laptop"}, client=other)

        self.assertContains(page, "Bitte bestätigen Sie sich")
        self.assertEqual(activated.status_code, 200)
        self.assertEqual(registered.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.authentication_method, "password")
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 0)
        self.assertEqual(state(self.user), before)

    # Replay
    def test_a_second_activation_changes_nothing_and_notifies_nobody_again(self):
        self.stage_first_key()
        self.assertEqual(self.post(wizard.SETUP, {"activate": "yes"}).status_code, 302)
        after_first = state(self.user)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.SIGN_IN_CHANGED).count(), 1)

        second = self.post(wizard.SETUP, {"activate": "yes"})

        self.assertEqual(second.status_code, 200)
        self.assertEqual(state(self.user), after_first)
        self.assertEqual(self.user.passkeys.filter(active=True).count(), 1)

    def test_a_registration_cannot_be_replayed(self):
        self.login()
        self.grant()
        self.credential_id = secrets.token_bytes(32)
        options = self.options("register", mode="this_device")
        body = {"ceremony": options["ceremony"], "credential": self.registration(options), "name": "Mein Handy"}
        self.assertEqual(self.json_post(wizard.SETUP, body).status_code, 200)

        replay = self.json_post(wizard.SETUP, body)

        self.assertEqual(replay.status_code, 400)
        self.assertEqual(self.user.passkeys.count(), 1)
