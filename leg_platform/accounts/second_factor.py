"""The second factor: the central check, the login step and re-authentication (D-040).

Whether the second factor of a session is complete is decided in one place, independent of the
method (docs/PROJECT.md, "AGOV and Passkeys"). Protected functions, the mandatory second factor
of staff users and system administrators and re-authentication rely only on the functions of this
module. The parts that are specific to TOTP and recovery codes are in ``totp.py``.

The login of an account with a second factor is two steps (D-017): the password is checked first
and remembered in the session as a pending login; Django's ``login()`` is called only when the
second factor has been verified as well.
"""

import secrets
import time
from datetime import timedelta
from typing import NamedTuple

from axes.backends import AxesStandaloneBackend
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.dispatch import receiver
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext, gettext_lazy

from leg_platform.accounts import security_log, totp
from leg_platform.accounts.backends import EmailModelBackend
from leg_platform.accounts.digests import keyed_digests_with_fallbacks
from leg_platform.accounts.login_protection import settle_client_ip_attempt
from leg_platform.accounts.mail_queue import enqueue_for_account
from leg_platform.accounts.models import (
    ConfirmationFailure,
    MailJob,
    SecurityEvent,
    SessionCodeFailure,
    User,
)

# The password step and the code step must follow each other closely; a longer pause starts
# again at the login page. A proposal awaiting the project owner's confirmation (D-040).
PENDING_LIFETIME = timedelta(minutes=5)
PENDING_SESSION_KEY = "second_factor_pending"
# The attribute of the request object that carries the wish "stay signed in" to the receiver of
# the session start (``session_timeout.start_session``); never a session value (D-056).
STAY_WISH_ATTRIBUTE = "_stay_signed_in_wish"
LOGIN_BACKEND = "leg_platform.accounts.backends.EmailModelBackend"

# Setting up two-factor authentication does not ask for the password again if this session
# authenticated, by the password or by completing the registration, at most this long ago
# (owner decision of 2026-10-14, D-055). The time is written by the server when the session logs in
# and is never taken from the client; removing the second factor, new recovery codes, an email
# address change and deleting the account always ask for the password.
RECENT_AUTHENTICATION_WINDOW = timedelta(minutes=5)
AUTHENTICATED_AT_KEY = "authenticated_at"

# The confirmations in the profile ask for the code of the app, or for the password where there is
# no second factor (D-062). A wrong entry there is counted for the session and for the account, and
# never as a failed login (so it can neither burn the budget of the codes nor lock the login):
# the session ends with this many wrong entries, all sessions of the account with
# ``ACCOUNT_FAILURE_LIMIT`` in ``ACCOUNT_FAILURE_WINDOW``.
SESSION_CODE_LIMIT = 5
ACCOUNT_FAILURE_LIMIT = 15
ACCOUNT_FAILURE_WINDOW = timedelta(hours=24)
SESSION_CODE_FAILURE_PURPOSE = b"LEG-Platform session code failures v1"
# The count per session hangs on a random identifier that the server writes into the session once
# and that stays when Django renews the key of the session (``cycle_key``): a session that renews
# its key, for example by posting the login form, keeps its count (security review, block 10, M-1).
SESSION_COUNTER_KEY = "confirmation_counter_id"
# Counts of sessions that ended without a success or a logout are removed by the clean-up after
# this time (``cleanup_confirmation_failures``).
SESSION_CODE_FAILURE_RETENTION = timedelta(days=30)
# The attribute of the request that says "end this session": set by the check, read by the view.
SESSION_ENDED_ATTRIBUTE = "_session_ended_wrong_codes"
SESSION_ENDED_MESSAGE = gettext_lazy(
    "For your safety you were logged out: wrong codes or passwords were entered several times. "
    "Please log in again."
)

# Views that an account without a completed second factor may use although it must have one:
# logging out and setting the second factor up.
UNRESTRICTED_VIEWS = frozenset(
    {
        "accounts:logout",
        "accounts:two_factor_setup",
        "accounts:two_factor_confirm",
        "accounts:two_factor_qr",
        # Public endpoints used before authentication; they do not expose account data.
        "instance_stylesheet",
        "instance_logo",
        "addresses:lookup",
    }
)
# The same for an account that must replace its password first (D-005, D-041). The branding
# stylesheet and the logos are presentation only, so that the page is rendered correctly.
PASSWORD_CHANGE_VIEWS = frozenset(
    {"accounts:logout", "accounts:password_change", "instance_stylesheet", "instance_logo"}
)
# Views whose response must not depend on the session: neither the second-factor nor the timeout
# middleware reads ``request.user`` for them, so the response has no ``Set-Cookie`` and no
# ``Vary: Cookie`` and nothing from the session reaches a cache (security review of D-073, L-2).
# Only for public presentation files that need no account.
SESSION_FREE_VIEWS = frozenset({"instance_logo"})


def is_session_free(request):
    """Whether the request is for a view of ``SESSION_FREE_VIEWS``. The path is resolved here
    because the timeout middleware runs before the URL is resolved; an unknown path is not free."""
    try:
        return resolve(request.path_info).view_name in SESSION_FREE_VIEWS
    except Resolver404:
        return False


def requires_second_factor(user):
    """Staff users and system administrators must use the second factor (D-004)."""
    return bool(user.is_authenticated and (user.is_staff or user.is_superuser))


def has_second_factor(user):
    """Whether the account has set up a second factor (not whether the session completed it)."""
    return totp.has_authenticator(user)


def is_second_factor_complete(user):
    """Whether this session completed the second factor (django-otp's ``is_verified()``).

    False if the middleware is missing: the check fails closed.
    """
    is_verified = getattr(user, "is_verified", None)
    return bool(user.is_authenticated and is_verified is not None and is_verified())


@receiver(user_logged_in)
def remember_authentication(sender, request, user, **kwargs):
    """Remember when this session logged in (after Django renewed its ID).

    ``user_logged_in`` is sent by ``login()``: after the password (and the second factor, if
    there is one) and when the registration is completed. A password change or reset, activity
    and the session cycling of Django do not write this value again.
    """
    request.session[AUTHENTICATED_AT_KEY] = time.time()
    # The identifier of the count of wrong entries in the confirmations of this session; kept if
    # the session already has one. A completed login proved the password and the code.
    request.session.setdefault(SESSION_COUNTER_KEY, secrets.token_urlsafe(32))
    forget_confirmation_failures(request, user)


@receiver(user_logged_out)
def forget_confirmation_failures_at_logout(sender, request, user, **kwargs):
    """A normal logout ends the count of this session; the end by wrong entries keeps it (it
    refuses a request of the session that was already running)."""
    if request is None or user is None or getattr(request, SESSION_ENDED_ATTRIBUTE, False):
        return
    forget_confirmation_failures(request, user)


def recently_authenticated(request, max_age=RECENT_AUTHENTICATION_WINDOW):
    """Whether this session authenticated at most ``max_age`` ago (a server-side timestamp)."""
    authenticated_at = request.session.get(AUTHENTICATED_AT_KEY)
    if isinstance(authenticated_at, bool) or not isinstance(authenticated_at, (int, float)):
        return False
    age = time.time() - authenticated_at
    return 0 <= age <= max_age.total_seconds()


class SecondFactorMiddleware:
    """Gives a session of an account that must use a second factor no privileges without it.

    Must come after ``AuthenticationMiddleware``, ``OTPMiddleware`` and ``MessageMiddleware``.
    A session without a completed second factor can only log out and set the factor up. If the
    account has a factor, the session is ended and the person logs in again, which asks for it.
    An account that must replace its password (the initial system administrator) can only log
    out and change the password, before anything else, also before the second factor is set up
    (D-005).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.resolver_match.view_name in SESSION_FREE_VIEWS:
            # Before ``request.user`` is read: reading it touches the session.
            return None
        user = request.user
        if user.is_authenticated and user.password_change_required:
            if request.resolver_match.view_name in PASSWORD_CHANGE_VIEWS:
                return None
            # The password change page itself explains why it is shown.
            return redirect("accounts:password_change")
        if not requires_second_factor(user) or is_second_factor_complete(user):
            return None
        if request.resolver_match.view_name in UNRESTRICTED_VIEWS:
            return None
        if has_second_factor(user):
            logout(request)
            messages.info(request, gettext("Please log in again."))
            return redirect("accounts:login")
        messages.warning(
            request,
            gettext("Your account needs two-factor authentication. Set it up to continue."),
        )
        return redirect("accounts:two_factor_setup")


class PendingLogin(NamedTuple):
    user: User
    next_url: str
    stay: bool = False


def pending_login(request):
    """Return the account whose password was checked and whose code is awaited, or None."""
    pending = request.session.get(PENDING_SESSION_KEY)
    if not pending:
        return None
    if time.time() - pending["started"] > PENDING_LIFETIME.total_seconds():
        request.session.pop(PENDING_SESSION_KEY, None)
        return None
    user = User.objects.filter(pk=pending["user"], status=User.Status.ACTIVE).first()
    # A password change or reset between the two steps ends the pending login.
    if (
        user is None
        or not has_second_factor(user)
        or not pending.get("auth_hash")
        or not constant_time_compare(user.get_session_auth_hash(), pending["auth_hash"])
    ):
        request.session.pop(PENDING_SESSION_KEY, None)
        return None
    return PendingLogin(user, pending["next"], pending.get("stay") is True)


def begin_pending_login(request, user, next_url, stay=False):
    """Remember that the password of the account was checked; the code is asked next.

    The session ID is renewed, so that a session ID that someone else planted in the browser
    before the password was entered cannot be used to complete the second step. ``stay`` is the
    wish "stay signed in" of the login form; it lives and dies with the pending login (D-056).
    """
    request.session.cycle_key()
    request.session[PENDING_SESSION_KEY] = {
        "user": user.pk,
        "started": time.time(),
        "next": next_url,
        "auth_hash": user.get_session_auth_hash(),
        "stay": bool(stay),
    }
    # The password was right: the IP slot of this attempt is released. The code step reserves
    # its own slot and keeps it if the code is wrong (D-017).
    settle_client_ip_attempt(request, succeeded=True)


def verify_second_factor(request, user, code, *, app=True, recovery=True, per_session=False):
    """Check the code under the abuse protection; return the device that accepted it, or None.

    Every code attempt counts like a login attempt: it is refused while the account or the IP
    address is locked out, and a wrong code is a failed attempt of the account and the IP
    address (D-017). The Axes backend is used for the lockout check only, as
    ``django.contrib.auth.authenticate()`` does with every backend. ``app`` and ``recovery`` say
    which kinds of code are accepted (``totp.verify_code``).

    ``per_session`` is for the confirmations in the profile (D-062): a wrong code is counted for
    this session and for the account's cap (``record_confirmation_failure``), not against the
    budget of the account and not as a failed login, so that whoever only holds a session cannot
    pause or lock the login of the account. The limit ends the session; the request is marked and
    the view answers with the logout. An existing pause or lockout still refuses the code.
    """
    credentials = {"username": user.email}
    # One attempt at a time per account: the lockout check, the verification and the record of
    # a failure form one step, so that parallel requests cannot exceed the limit (D-040).
    with transaction.atomic():
        locked = User._base_manager.select_for_update().filter(pk=user.pk).first()
        try:
            AxesStandaloneBackend().authenticate(request, username=user.email)
        except PermissionDenied:
            user_login_failed.send(sender=__name__, credentials=credentials, request=request)
            return None
        if totp.code_attempts_paused(user):
            # The budget of wrong codes is used up: even a correct code is refused. Like a
            # lockout of Axes, so that nothing is recorded and no lockout is extended.
            request.axes_locked_out = True
            request.second_factor_paused = True
            user_login_failed.send(sender=__name__, credentials=credentials, request=request)
            return None
        if per_session and confirmation_refused(request, user, locked):
            settle_client_ip_attempt(request, succeeded=True)
            return None
        device = totp.verify_code(user, code, app=app, recovery=recovery)
        if device is None:
            if not per_session:
                totp.record_failed_code(user)
                user_login_failed.send(sender=__name__, credentials=credentials, request=request)
                return None
            # Not a failed login and not a failure of the budget: the session and the cap count.
            record_confirmation_failure(request, user)
            settle_client_ip_attempt(request, succeeded=True)
            return None
        if per_session:
            reset_confirmation_failures(request, user)
    settle_client_ip_attempt(request, succeeded=True)
    return device


def verify_password(request, user, password, *, reset_on_success=True):
    """Check the password of the logged-in account in a confirmation of the profile.

    A wrong password is counted for the session and for the account's cap
    (``record_confirmation_failure``), **not** as a failed login: it does not reach Axes or the IP
    counter, so whoever only holds a session cannot lock the login of the account (security
    review, block 10, M-2). A lockout that the failed logins caused still refuses the check. The
    login itself does not use this function and stays under Axes (D-017). The account row is
    locked while the password is verified and a failure is recorded, so that parallel requests
    cannot exceed the limit (D-040). ``reset_on_success=False`` is for a form where the password is
    not the last proof (a second proof follows and decides the reset).
    """
    credentials = {"username": user.email}
    with transaction.atomic():
        locked = User._base_manager.select_for_update().filter(pk=user.pk).first()
        try:
            AxesStandaloneBackend().authenticate(request, username=user.email)
        except PermissionDenied:
            user_login_failed.send(sender=__name__, credentials=credentials, request=request)
            return False
        if confirmation_refused(request, user, locked):
            settle_client_ip_attempt(request, succeeded=True)
            return False
        checked = EmailModelBackend().authenticate(
            request, username=user.email, password=password
        )
        succeeded = checked is not None and checked.pk == user.pk
        if not succeeded:
            record_confirmation_failure(request, user)
        elif reset_on_success:
            reset_confirmation_failures(request, user)
    settle_client_ip_attempt(request, succeeded=True)
    return succeeded


def _counter_identifier(request):
    """The identifier that the count of this session hangs on, or None.

    Chosen by the server, written into the session once (at the login, or here at the first use
    with the current key of the session, which is the same for parallel requests) and kept when
    Django renews the key of the session. The browser cannot choose it: the session data is on
    the server.
    """
    session = getattr(request, "session", None)
    if session is None:
        # No session: nothing to count on, the confirmation is refused (fail closed).
        return None
    identifier = session.get(SESSION_COUNTER_KEY)
    if isinstance(identifier, str) and identifier:
        return identifier
    key = session.session_key
    if key:
        session[SESSION_COUNTER_KEY] = key
    return key or None


def _session_digests(request):
    """The keyed digests of the identifier under the current key and the fallback keys of
    ``SECRET_KEY_FALLBACKS`` (current first), or an empty list."""
    identifier = _counter_identifier(request)
    if identifier is None:
        return []
    return keyed_digests_with_fallbacks(identifier, SESSION_CODE_FAILURE_PURPOSE)


def _counter_row(request, user):
    """The row of this session and its current digest (None, None without an identifier).

    A row found under the digest of a fallback key is moved to the current key. Call it under the
    lock of the account.
    """
    digests = _session_digests(request)
    if not digests:
        return None, None
    rows = list(SessionCodeFailure.objects.filter(account=user, session_digest__in=digests))
    if not rows:
        return None, digests[0]
    row = max(rows, key=lambda candidate: candidate.failures)
    for other in rows:
        if other.pk != row.pk:
            other.delete()
    if row.session_digest != digests[0]:
        row.session_digest = digests[0]
        row.save(update_fields=["session_digest"])
    return row, digests[0]


def confirmation_refused(request, user, locked):
    """Whether a confirmation is refused unseen, and the session ends: it is out of tries (a
    parallel request used the last one) or it is no longer valid (the sessions of the account
    ended or the password changed after this request started, so a flood of requests that were
    already running cannot go on). Call it under the lock of the account (``locked``)."""
    refused = (
        locked is None
        or not constant_time_compare(locked.get_session_auth_hash(), user.get_session_auth_hash())
    )
    if not refused:
        row, digest = _counter_row(request, user)
        refused = digest is None or (row is not None and row.failures >= SESSION_CODE_LIMIT)
    if refused:
        setattr(request, SESSION_ENDED_ATTRIBUTE, True)
    return refused


def record_confirmation_failure(request, user):
    """Count a wrong code or password of a confirmation, for the session and for the account.

    At ``SESSION_CODE_LIMIT`` wrong entries this session ends (one event and one notice); at
    ``ACCOUNT_FAILURE_LIMIT`` in ``ACCOUNT_FAILURE_WINDOW`` over all sessions, all sessions of the
    account end (one event and one notice) and the count starts again. Neither pauses the login.
    The request is marked so that the view logs this session out. Call it under the lock of the
    account, in the transaction of the check.
    """
    row, digest = _counter_row(request, user)
    if digest is None:
        setattr(request, SESSION_ENDED_ATTRIBUTE, True)
        return
    now = timezone.now()
    if row is None:
        row = SessionCodeFailure(account=user, session_digest=digest)
    row.failures += 1
    row.updated_at = now
    row.save()
    ConfirmationFailure.objects.filter(
        account=user, occurred_at__lt=now - ACCOUNT_FAILURE_WINDOW
    ).delete()
    ConfirmationFailure.objects.create(account=user, occurred_at=now)
    if ConfirmationFailure.objects.filter(account=user).count() >= ACCOUNT_FAILURE_LIMIT:
        ConfirmationFailure.objects.filter(account=user).delete()
        # Every session ends, also the ones that are running a request (they find the session
        # hash changed, ``confirmation_refused``). The login is not paused.
        user.end_all_sessions()
        security_log.record(SecurityEvent.Event.ALL_SESSIONS_ENDED, account=user)
        enqueue_for_account(MailJob.Kind.ALL_SESSIONS_ENDED, user)
        setattr(request, SESSION_ENDED_ATTRIBUTE, True)
    elif row.failures >= SESSION_CODE_LIMIT:
        setattr(request, SESSION_ENDED_ATTRIBUTE, True)
        # Exactly once: later requests of this session are refused before they are counted.
        if row.failures == SESSION_CODE_LIMIT:
            security_log.record(SecurityEvent.Event.SESSION_ENDED_WRONG_CODES, account=user)
            enqueue_for_account(MailJob.Kind.SESSION_ENDED_WRONG_CODES, user)


def reset_confirmation_failures(request, user):
    """A correct proof: the count of this session starts again (the cap of the account does not)."""
    forget_confirmation_failures(request, user)


def forget_confirmation_failures(request, user):
    """Delete the count of this session (all digests that it can have).

    Every statement on the counter rows (``SessionCodeFailure``, ``ConfirmationFailure``) runs
    under the lock of the account row, so the order is always account row, then counter rows
    (D-072). At the login and the logout this is a short transaction of its own; inside the
    transaction of a check (``reset_confirmation_failures``) the account row is already locked by
    the same transaction and the lock is no wait. The account may be gone already (deleted
    account): then there is nothing to lock and the rows go with the cascade.
    """
    digests = _session_digests(request)
    if digests:
        with transaction.atomic():
            User._base_manager.select_for_update().filter(pk=user.pk).first()
            SessionCodeFailure.objects.filter(account=user, session_digest__in=digests).delete()


def end_session_for_wrong_codes(request):
    """End this session only (the others stay) and tell the person at the next page of the login."""
    logout(request)
    messages.warning(request, SESSION_ENDED_MESSAGE)


def complete_login(request, user, device, stay=False):
    """Log the account in after the password and the second factor were verified.

    ``stay`` is the wish of the pending login; the receiver of the session start reads it from
    the request (``session_timeout.STAY_WISH_ATTRIBUTE``) and ignores it for staff users.
    """
    request.session.pop(PENDING_SESSION_KEY, None)
    setattr(request, STAY_WISH_ATTRIBUTE, stay is True)
    # django-otp stores the device in the new session after Django's login() renewed its ID.
    user.otp_device = device
    login(request, user, backend=LOGIN_BACKEND)
