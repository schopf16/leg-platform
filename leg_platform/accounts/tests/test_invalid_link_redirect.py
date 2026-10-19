"""Every link of an email that is invalid redirects to one fixed page, whatever the cause (D-067).

An invalid, expired, used or replaced link must not render a page under the token URL, and the
answer must not tell a person (or a script) which of the causes applies: the status, the Location,
the headers and the body are the same for every cause (no oracle for tokens, docs/SECURITY.md,
section 6). The confirmation views that follow the link (the state is in the session) redirect to
the same page when no valid state exists. The fixed pages are plain pages: GET only, 200, not
stored, the same for everybody.
"""

import re
from datetime import timedelta

from django.db import transaction
from django.test import Client, TestCase, tag
from django.urls import resolve, reverse
from django.utils import timezone

from leg_platform.accounts import views
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    PendingEmailChange,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import PASSWORD, create_active_user
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp

Purpose = VerificationToken.Purpose
LIFETIME = timedelta(hours=1)
FORGED = "A" * 43
NEW_ADDRESS = "new.address@example.org"


class Flow:
    """One flow of a link: how to make its user and a valid token, and how to break its state."""

    def __init__(self, name, verify, fixed, purpose, other_purpose):
        self.name = name
        self.verify = f"accounts:{verify}"
        self.fixed = f"accounts:{fixed}"
        self.purpose = purpose
        self.other_purpose = other_purpose
        self.user = None
        # Further causes that are a state of the account, with the method that makes the token.
        self.status_causes = {}

    def causes(self):
        return CAUSES + tuple(self.status_causes)

    def prepare(self):
        raise NotImplementedError

    def issue(self, purpose=None):
        purpose = purpose or self.purpose
        target = NEW_ADDRESS if purpose in (Purpose.EMAIL_CHANGE, Purpose.SECONDARY_EMAIL) else ""
        return VerificationToken.objects.issue(self.user, purpose, LIFETIME, target_email=target)

    def break_state(self, change=None):
        """Change the account so that a token that was valid no longer is; the token stays.

        The account logic removes the tokens of a blocked account; they are put back, so that the
        state of the account is the only reason why the link is refused.
        """
        kept = list(
            VerificationToken.objects.values("user_id", "purpose", "digest", "expires_at", "target_email")
        )
        (change or self.break_account)()
        for row in kept:
            VerificationToken.objects.get_or_create(digest=row["digest"], defaults=row)

    def break_account(self):
        raise NotImplementedError


def create_unverified_user(email):
    return User.objects.create_user(
        email,
        first_name="Anna",
        last_name="Example",
        street="Examplestrasse",
        house_number="12a",
        town="Bern",
        swiss_postal_code=3000,
    )


class RegistrationFlow(Flow):
    def __init__(self):
        super().__init__("registration", "registration_verify", "registration_invalid", Purpose.REGISTRATION, Purpose.PASSWORD_RESET)

    def prepare(self):
        self.user = create_unverified_user("registering.person@example.org")

    def break_account(self):
        self.user.complete_registration(PASSWORD)


class PasswordResetFlow(Flow):
    def __init__(self):
        super().__init__("password reset", "password_reset_verify", "password_reset_invalid", Purpose.PASSWORD_RESET, Purpose.REGISTRATION)

    def prepare(self):
        self.user = create_active_user("resetting.person@example.org")

    def break_account(self):
        self.user.set_status(User.Status.BLOCKED)


class EmailChangeFlow(Flow):
    def __init__(self):
        super().__init__("email change", "email_change_verify", "email_change_invalid", Purpose.EMAIL_CHANGE, Purpose.PASSWORD_RESET)
        # The link is refused for every account that is not active (D-016): blocked (the
        # ``account state`` cause), deactivated and unverified.
        self.status_causes = {
            "account deactivated": self.token_of_deactivated_account,
            "account unverified": self.token_of_unverified_account,
        }

    def prepare(self):
        self.user = create_active_user("changing.person@example.org")

    def break_account(self):
        self.user.set_status(User.Status.BLOCKED)

    def token_of_deactivated_account(self):
        raw = self.issue()
        self.break_state(lambda: self.user.set_status(User.Status.DEACTIVATED))
        return raw

    def token_of_unverified_account(self):
        # An unverified account has no customer number, so an active one cannot become
        # unverified; the account is created so (as by a registration that is not complete).
        self.user = create_unverified_user("unverified.person@example.org")
        return self.issue()


class EmailChangeStopFlow(Flow):
    def __init__(self):
        super().__init__("email change stop", "email_change_stop_verify", "email_change_stop_invalid", Purpose.EMAIL_CHANGE_STOP, Purpose.EMAIL_CHANGE)

    def prepare(self):
        self.user = create_active_user("stopping.person@example.org")
        PendingEmailChange.objects.create(
            user=self.user,
            kind=PendingEmailChange.Kind.LOGIN,
            address=NEW_ADDRESS,
            expires_at=timezone.now() + LIFETIME,
        )

    def break_account(self):
        # The link has a meaning only while a request is open (security review of phase 4, H1).
        PendingEmailChange.objects.filter(user=self.user).delete()


class TwoFactorResetFlow(Flow):
    def __init__(self):
        super().__init__("second factor removal", "two_factor_reset_verify", "two_factor_reset_invalid", Purpose.TWO_FACTOR_RESET, Purpose.PASSWORD_RESET)

    def prepare(self):
        self.user, _device = create_user_with_totp("holder.person@example.org")

    def break_account(self):
        AuthenticatorDevice.objects.filter(user=self.user).delete()


def all_flows():
    return [
        RegistrationFlow(),
        PasswordResetFlow(),
        EmailChangeFlow(),
        EmailChangeStopFlow(),
        TwoFactorResetFlow(),
    ]


def token_of_cause(flow, cause):
    """Return a raw token that is invalid for the given cause."""
    if cause == "forged":
        return FORGED
    if cause == "wrong length":
        return "q" * 10
    if cause == "expired":
        raw = flow.issue()
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        return raw
    if cause == "used":
        raw = flow.issue()
        VerificationToken.objects.all().delete()
        return raw
    if cause == "replaced":
        old = flow.issue()
        VerificationToken.objects.all().delete()
        flow.issue()
        return old
    if cause == "other purpose":
        return flow.issue(flow.other_purpose)
    if cause == "account state":
        raw = flow.issue()
        flow.break_state()
        return raw
    if cause in flow.status_causes:
        return flow.status_causes[cause]()
    raise AssertionError(cause)


CAUSES = ("forged", "wrong length", "expired", "used", "replaced", "other purpose", "account state")


class InTransaction:
    """Run a block that changes the database and undo it, so that every cause starts clean."""

    def __enter__(self):
        self.atomic = transaction.atomic()
        self.atomic.__enter__()
        return self

    def __exit__(self, *exc):
        transaction.set_rollback(True)
        return self.atomic.__exit__(*exc)


def answer(response, with_cookies=True):
    """Everything of a response that a person or a script could use to tell two causes apart.

    Every header is compared, including ``Vary`` and ``Content-Language``, except the two that
    are the current time (``Date`` and ``Expires``, which ``never_cache`` sets from the clock).
    The cookies are compared by name and attributes: the value of a session cookie is random
    and its ``expires`` is the clock, but the question is whether the cause decides if a cookie
    is set at all (security review of unit A, M-1).
    """
    headers = tuple(
        sorted(
            (name.lower(), value)
            for name, value in response.headers.items()
            if name.lower() not in ("date", "expires")
        )
    )
    cookies = tuple(
        sorted(
            (
                name,
                tuple(
                    sorted(
                        (attribute, value)
                        for attribute, value in morsel.items()
                        if attribute != "expires"
                    )
                ),
            )
            for name, morsel in response.cookies.items()
        )
    )
    return (response.status_code, headers, cookies if with_cookies else None, response.content)


@tag("mariadb")
class EveryCauseGivesTheSameAnswerTests(TestCase):
    def test_every_cause_redirects_to_the_fixed_page_with_the_same_answer(self):
        for flow in all_flows():
            answers = {}
            for cause in flow.causes():
                with self.subTest(flow=flow.name, cause=cause), InTransaction():
                    flow.prepare()
                    raw = token_of_cause(flow, cause)

                    response = Client().get(reverse(flow.verify, args=[raw]))

                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response["Location"], reverse(flow.fixed))
                    # The token is in no part of the answer.
                    self.assertNotIn(raw, response["Location"])
                    self.assertNotIn(raw.encode(), response.content)
                    answers[cause] = answer(response)
                    # No cookie is set for a visitor, whatever the cause (M-1).
                    self.assertEqual(answers[cause][2], ())
            with self.subTest(flow=flow.name, check="all causes alike"):
                self.assertEqual(len(answers), len(flow.causes()))
                self.assertEqual(len(set(answers.values())), 1, answers)

    def test_the_redirect_keeps_the_referrer_policy_and_is_not_stored(self):
        for flow in all_flows():
            with self.subTest(flow=flow.name), InTransaction():
                flow.prepare()

                response = Client().get(reverse(flow.verify, args=[FORGED]))

                self.assertEqual(response["Referrer-Policy"], "no-referrer")
                self.assertIn("no-store", response["Cache-Control"])

    def test_a_valid_link_does_not_go_to_the_fixed_page(self):
        # Without this check the tests above would pass for a link that always fails.
        for flow in all_flows():
            with self.subTest(flow=flow.name), InTransaction():
                flow.prepare()
                raw = flow.issue()

                response = Client().get(reverse(flow.verify, args=[raw]))

                self.assertEqual(response.status_code, 302)
                self.assertNotEqual(response["Location"], reverse(flow.fixed))
                self.assertNotIn(raw, response["Location"])

    def test_the_causes_do_not_differ_for_a_person_who_is_logged_in(self):
        for flow in all_flows():
            answers = set()
            for cause in flow.causes():
                with self.subTest(flow=flow.name, cause=cause), InTransaction():
                    flow.prepare()
                    raw = token_of_cause(flow, cause)
                    client = Client()
                    client.force_login(create_active_user("logged.in.person@example.org"))

                    answers.add(answer(client.get(reverse(flow.verify, args=[raw]))))
            with self.subTest(flow=flow.name, check="all causes alike"):
                self.assertEqual(len(answers), 1)


CONFIRMATIONS = (
    # (confirmation view, session key of its state, the flow that opens it)
    ("accounts:email_change_confirm", views.EMAIL_CHANGE_SESSION_KEY, EmailChangeFlow),
    ("accounts:email_change_stop_confirm", views.EMAIL_CHANGE_STOP_SESSION_KEY, EmailChangeStopFlow),
    ("accounts:two_factor_reset_confirm", views.TWO_FACTOR_RESET_SESSION_KEY, TwoFactorResetFlow),
)
STATE_CAUSES = ("no state", "garbage digest", "expired", "used", "account state")


def client_with_state(flow, cause):
    """Return a client whose session holds the state that a link left, broken in the given way."""
    client = Client()
    if cause == "no state":
        return client
    if cause == "garbage digest":
        session = client.session
        session[CONFIRMATION_KEY[flow.name]] = "f" * 64
        session.save()
        return client
    raw = flow.issue()
    response = client.get(reverse(flow.verify, args=[raw]))
    assert response.status_code == 302 and response["Location"] != reverse(flow.fixed)
    if cause == "expired":
        VerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    elif cause == "used":
        VerificationToken.objects.all().delete()
    else:
        flow.break_state()
    return client


CONFIRMATION_KEY = {
    flow_class().name: key for _name, key, flow_class in CONFIRMATIONS
}


@tag("mariadb")
class ConfirmationViewsRedirectTests(TestCase):
    def test_a_confirmation_without_a_valid_state_redirects_to_the_same_fixed_page(self):
        for name, key, flow_class in CONFIRMATIONS:
            flow = flow_class()
            answers = {}
            for cause in STATE_CAUSES:
                for method in ("get", "post"):
                    with self.subTest(view=name, cause=cause, method=method), InTransaction():
                        flow.prepare()
                        client = client_with_state(flow, cause)

                        response = getattr(client, method)(reverse(name))

                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(response["Location"], reverse(flow.fixed))
                        self.assertIn("no-store", response["Cache-Control"])
                        self.assertEqual(response.content, b"")
                        answers[(cause, method)] = response
                        # L-5: a state that is no longer valid is not kept in the session.
                        self.assertNotIn(key, client.session)
            with self.subTest(view=name, check="all causes alike"):
                # Status, every header and the body are the same for every cause.
                self.assertEqual(
                    len({answer(response, with_cookies=False) for response in answers.values()}),
                    1,
                )
            with self.subTest(view=name, check="cookie"):
                # The one difference: a browser whose session held a state gets its session
                # cookie back, because the state was removed (L-5). That depends on the session
                # that the browser sent, never on the cause: every cause with a state is alike,
                # and a browser without a state gets no cookie (no session is created for it).
                with_state = {
                    answer(response)
                    for (cause, _method), response in answers.items()
                    if cause != "no state"
                }
                without_state = {
                    answer(response)
                    for (cause, _method), response in answers.items()
                    if cause == "no state"
                }
                self.assertEqual(len(with_state), 1)
                self.assertEqual(len(without_state), 1)
                self.assertEqual([name for name, _attrs in with_state.pop()[2]], ["sessionid"])
                self.assertEqual(without_state.pop()[2], ())

    def test_a_confirmation_with_a_valid_state_still_shows_its_page(self):
        for name, _key, flow_class in CONFIRMATIONS:
            flow = flow_class()
            with self.subTest(view=name), InTransaction():
                flow.prepare()
                client = Client()
                client.get(reverse(flow.verify, args=[flow.issue()]))

                response = client.get(reverse(name))

                self.assertEqual(response.status_code, 200)

    def test_a_confirmation_that_fails_late_redirects_to_the_fixed_page(self):
        # The state was valid when the page was shown and is not any more when the button is pressed.
        for name, key, flow_class in CONFIRMATIONS:
            flow = flow_class()
            with self.subTest(view=name), InTransaction():
                flow.prepare()
                client = Client()
                client.get(reverse(flow.verify, args=[flow.issue()]))
                self.assertEqual(client.get(reverse(name)).status_code, 200)
                self.assertIn(key, client.session)
                flow.break_state()

                response = client.post(reverse(name), {})

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse(flow.fixed))
                # L-5: the state is removed whenever the view redirects to the fixed page.
                self.assertNotIn(key, client.session)


FIXED_PAGES = (
    ("accounts:email_change_invalid", "/email-change/expired/", "accounts/email_change_invalid.html"),
    ("accounts:email_change_stop_invalid", "/email-change/stop-expired/", "accounts/email_change_stop_invalid.html"),
    ("accounts:two_factor_reset_invalid", "/two-factor-reset/expired/", "accounts/two_factor_reset_invalid.html"),
)


@tag("mariadb")
class FixedPagesTests(TestCase):
    def test_the_paths_are_fixed_and_cannot_be_taken_for_a_token_route(self):
        for name, path, _template in FIXED_PAGES:
            with self.subTest(name=name):
                self.assertEqual(reverse(name), path)
                match = resolve(path)
                self.assertEqual(match.func.view_class, views.LinkNoLongerValidView)
                self.assertNotIn("token", match.kwargs)

    def test_the_path_that_the_designer_avoided_is_a_token_route(self):
        # "/email-change/stop/expired/" would be taken for the token "expired" of the stop link.
        self.assertEqual(resolve("/email-change/stop/expired/").url_name, "email_change_stop_verify")
        self.assertEqual(resolve("/email-change/stop-expired/").url_name, "email_change_stop_invalid")
        self.assertEqual(resolve("/email-change/stop/").url_name, "email_change_stop_confirm")

    def test_a_fixed_page_is_a_plain_page_that_is_not_stored(self):
        for name, _path, template in FIXED_PAGES:
            with self.subTest(name=name):
                response = Client().get(reverse(name))

                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, template)
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response["Referrer-Policy"], "same-origin")
                self.assertEqual(Client().head(reverse(name)).status_code, 200)

    def test_a_fixed_page_accepts_nothing_else_than_a_read(self):
        for name, _path, _template in FIXED_PAGES:
            for method in ("post", "put", "patch", "delete"):
                with self.subTest(name=name, method=method):
                    response = getattr(Client(), method)(reverse(name), {})

                    self.assertEqual(response.status_code, 405)

    def test_a_fixed_page_is_the_same_for_everybody_and_shows_nothing_of_an_account(self):
        user, _device = create_user_with_totp("holder.person@example.org", last_name="Uniquefamilyname")
        PendingEmailChange.objects.create(
            user=user,
            kind=PendingEmailChange.Kind.LOGIN,
            address=NEW_ADDRESS,
            expires_at=timezone.now() + LIFETIME,
        )
        # A browser that holds the state of every link, and one that is logged in.
        holder = Client()
        for flow_class in (EmailChangeFlow, EmailChangeStopFlow, TwoFactorResetFlow):
            flow = flow_class()
            flow.user = user
            holder.get(reverse(flow.verify, args=[flow.issue()]))
        member = Client()
        member.force_login(user)
        for name, _path, _template in FIXED_PAGES:
            with self.subTest(name=name):
                fresh = Client().get(reverse(name))
                beside_state = holder.get(reverse(name))
                logged_in = member.get(reverse(name))

                # The CSRF token of the language form differs in every response (it is masked).
                token = re.compile(rb'name="csrfmiddlewaretoken" value="[^"]+"')
                self.assertEqual(token.sub(b"", fresh.content), token.sub(b"", beside_state.content))
                for page in (fresh, beside_state, logged_in):
                    html = page.content.decode()
                    for marker in (user.email, "Uniquefamilyname", NEW_ADDRESS):
                        self.assertNotIn(marker, html)
