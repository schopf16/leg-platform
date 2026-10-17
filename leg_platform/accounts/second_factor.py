"""The second factor: the central check, the login step and re-authentication (D-040).

Whether a session completed the second factor is decided here, independent of the method.
TOTP and recovery codes are in ``totp.py``. The login of an account with a second factor is two
steps (D-017): the password is remembered as a pending login; ``login()`` runs only after the
second factor was verified.
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

# The password step and the code step must follow closely; a longer pause starts again (D-040).
PENDING_LIFETIME = timedelta(minutes=5)
PENDING_SESSION_KEY = "second_factor_pending"
# Request attribute that carries "stay signed in" to ``session_timeout.start_session``; never a session value (D-056).
STAY_WISH_ATTRIBUTE = "_stay_signed_in_wish"
LOGIN_BACKEND = "leg_platform.accounts.backends.EmailModelBackend"

# Setting up 2FA skips the password if this session authenticated at most this long ago (D-055); the
# time is written by the server. Removing 2FA, new recovery codes, email change and deletion always ask.
RECENT_AUTHENTICATION_WINDOW = timedelta(minutes=5)
AUTHENTICATED_AT_KEY = "authenticated_at"

# Wrong entries in profile confirmations (D-062) count per session and per account, never as a failed
# login: the session ends after ``SESSION_CODE_LIMIT``, all sessions after ``ACCOUNT_FAILURE_LIMIT`` per window.
SESSION_CODE_LIMIT = 5
ACCOUNT_FAILURE_LIMIT = 15
ACCOUNT_FAILURE_WINDOW = timedelta(hours=24)
SESSION_CODE_FAILURE_PURPOSE = b"LEG-Platform session code failures v1"
# The per-session count hangs on a random id that survives ``cycle_key`` (so renewing the key keeps the count).
SESSION_COUNTER_KEY = "confirmation_counter_id"
# Counts of sessions that ended without success or logout are removed after this time.
SESSION_CODE_FAILURE_RETENTION = timedelta(days=30)
# Request attribute that says "end this session": set by the check, read by the view.
SESSION_ENDED_ATTRIBUTE = "_session_ended_wrong_codes"
SESSION_ENDED_MESSAGE = gettext_lazy(
    "For your safety you were logged out: wrong codes or passwords were entered several times. "
    "Please log in again."
)

# Views an account without a completed second factor may use although it must have one.
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
# The same for an account that must replace its password first (D-005, D-041).
PASSWORD_CHANGE_VIEWS = frozenset(
    {"accounts:logout", "accounts:password_change", "instance_stylesheet", "instance_logo"}
)
# Public presentation files: the middlewares do not read ``request.user`` for them, so no
# ``Set-Cookie``/``Vary: Cookie`` and nothing from the session reaches a cache (D-073).
SESSION_FREE_VIEWS = frozenset({"instance_logo"})


def is_session_free(request):
    """Whether the request is for a ``SESSION_FREE_VIEWS`` view (resolved here: the timeout middleware runs before URL resolution)."""
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
    """Whether this session completed the second factor (django-otp ``is_verified()``); fails closed if the middleware is missing."""
    is_verified = getattr(user, "is_verified", None)
    return bool(user.is_authenticated and is_verified is not None and is_verified())


@receiver(user_logged_in)
def remember_authentication(sender, request, user, **kwargs):
    """Remember when this session logged in (``login()`` also runs after registration); other session changes do not rewrite it."""
    request.session[AUTHENTICATED_AT_KEY] = time.time()
    # Kept if the session already has one.
    request.session.setdefault(SESSION_COUNTER_KEY, secrets.token_urlsafe(32))
    forget_confirmation_failures(request, user)


@receiver(user_logged_out)
def forget_confirmation_failures_at_logout(sender, request, user, **kwargs):
    """A normal logout ends this session's count; the end by wrong entries keeps it."""
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

    Must come after ``AuthenticationMiddleware``, ``OTPMiddleware`` and ``MessageMiddleware``. Such a
    session can only log out and set the factor up; if the account has one, the session is ended
    and the person logs in again. An account that must replace its password can only log out and
    change it, before anything else (D-005).
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
    """Remember that the password was checked; the code is asked next.

    The session ID is renewed so a planted ID cannot complete the second step. ``stay`` lives and dies with the pending login (D-056).
    """
    request.session.cycle_key()
    request.session[PENDING_SESSION_KEY] = {
        "user": user.pk,
        "started": time.time(),
        "next": next_url,
        "auth_hash": user.get_session_auth_hash(),
        "stay": bool(stay),
    }
    # The password was right: release this attempt's IP slot; the code step reserves its own (D-017).
    settle_client_ip_attempt(request, succeeded=True)


def verify_second_factor(request, user, code, *, app=True, recovery=True, per_session=False):
    """Check the code under the abuse protection; return the device that accepted it, or None.

    Every attempt counts like a login attempt: refused while the account or IP is locked out, and a
    wrong code is a failed attempt (D-017). Axes is used for the lockout check only. ``app`` and
    ``recovery`` say which kinds of code are accepted (``totp.verify_code``).

    ``per_session`` (profile confirmations, D-062): a wrong code is counted for this session and the
    account's cap (``record_confirmation_failure``), not as a failed login, so a session holder
    cannot pause or lock the login. The limit ends the session (the request is marked, the view
    logs out). An existing pause or lockout still refuses the code.
    """
    credentials = {"username": user.email}
    # One attempt at a time per account: lockout check, verification and failure record are one step (D-040).
    with transaction.atomic():
        locked = User._base_manager.select_for_update().filter(pk=user.pk).first()
        try:
            AxesStandaloneBackend().authenticate(request, username=user.email)
        except PermissionDenied:
            user_login_failed.send(sender=__name__, credentials=credentials, request=request)
            return None
        if totp.code_attempts_paused(user):
            # Budget used up: even a correct code is refused, as with an Axes lockout (nothing recorded).
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
    """Check the password of the logged-in account in a profile confirmation.

    A wrong password is counted for the session and the account's cap (``record_confirmation_failure``),
    **not** as a failed login (no Axes, no IP counter), so a session holder cannot lock the login.
    A lockout caused by failed logins still refuses. The row is locked while verifying and recording
    a failure (D-040). ``reset_on_success=False``: the password is not the last proof (a second follows).
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
    """The identifier that this session's count hangs on, or None.

    Chosen by the server, written into the session once (at login, or at first use) and kept when
    Django renews the session key. The browser cannot choose it.
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
    """Keyed digests of the identifier under the current key and ``SECRET_KEY_FALLBACKS`` (current first), or []."""
    identifier = _counter_identifier(request)
    if identifier is None:
        return []
    return keyed_digests_with_fallbacks(identifier, SESSION_CODE_FAILURE_PURPOSE)


def _counter_row(request, user):
    """The row of this session and its current digest (None, None without an identifier).

    A row found under a fallback key's digest is moved to the current key. Call under the account lock.
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
    """Whether a confirmation is refused unseen (and the session ends): out of tries, or no longer valid.

    No longer valid = the account's sessions ended or the password changed after this request started
    (stops a flood of already-running requests). Call under the account lock (``locked``).
    """
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
    """Count a wrong code or password of a confirmation, for the session and the account.

    At ``SESSION_CODE_LIMIT`` this session ends (one event, one notice); at ``ACCOUNT_FAILURE_LIMIT``
    in ``ACCOUNT_FAILURE_WINDOW`` all sessions of the account end and the count restarts. The login
    is not paused. The request is marked so the view logs out. Call under the account lock, in the check's transaction.
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
        # Also sessions that are running a request (they find the session hash changed).
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
    """Delete the count of this session (all digests it can have).

    Lock order: account row, then counter rows (D-072). A short transaction of its own at login and
    logout; inside a check's transaction the lock is already held. A deleted account needs no lock (cascade).
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
    the request (``session_timeout.STAY_WISH_ATTRIBUTE``); it is valid for every account (D-088).
    """
    request.session.pop(PENDING_SESSION_KEY, None)
    setattr(request, STAY_WISH_ATTRIBUTE, stay is True)
    # django-otp stores the device in the new session after Django's login() renewed its ID.
    user.otp_device = device
    login(request, user, backend=LOGIN_BACKEND)
