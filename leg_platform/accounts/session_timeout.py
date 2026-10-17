"""Session timeouts: idle for every session, absolute only for "stay signed in" (D-041, D-088).

The session stores when it started and was last used; an idle session is ended at its next
request, and a "stay signed in" session also at its fixed end. Activity extends the idle timeout;
there is no other forced end (owner rule D-088: whoever logged in stays logged in until the idle
timeout or the logout). Staff users and system administrators have the shorter idle timeout.
"""

import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.signals import user_logged_in
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.dispatch import receiver
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.utils.translation import gettext

from leg_platform.accounts import security_log
from leg_platform.accounts.mail_queue import enqueue_for_account
from leg_platform.accounts.models import MailJob, SecurityEvent
from leg_platform.accounts.second_factor import (
    STAY_WISH_ATTRIBUTE,
    has_second_factor,
    is_session_free,
    requires_second_factor,
)
from leg_platform.log_filters import TOKEN_URL_PATTERN

STARTED_KEY = "session_started"
LAST_ACTIVITY_KEY = "session_last_activity"
# "Stay signed in" (D-056, D-088): the login view puts the wish on the request, never into the
# session; the signal below makes a persistent session of it, for every account. A privileged
# account gets it only if it already has its second factor (the code was accepted at this login);
# without one the session stays an ordinary restricted session. ``STAYING_KEY`` marks a
# persistent session.
STAYING_KEY = "stay_signed_in"
# The last activity is written at most once per interval; the idle timeout is exact to one interval.
ACTIVITY_WRITE_INTERVAL = 60
# Views that poll in the background (status of a running import, live log): they are answered
# only for a session that is still open but never count as activity, so that an open tab does not
# keep an abandoned session alive for ever (D-088).
PASSIVE_VIEWS = frozenset({"admin:addresses_importrequest_status", "admin:logs_tail"})


def is_passive(request):
    """Whether the request is for a ``PASSIVE_VIEWS`` view (resolved here: the middleware runs before URL resolution)."""
    try:
        return resolve(request.path_info).view_name in PASSIVE_VIEWS
    except Resolver404:
        return False


def announce_persistent_session(user):
    """A long session of a staff or administrator account was started (H-2 of the review of 2026-10-17): the security
    log and an email to every address of the account say so, so that a stolen long session does not stay unnoticed.
    The second factor is checked once per login and a long session needs none again (D-100)."""
    with transaction.atomic():
        security_log.record(SecurityEvent.Event.PERSISTENT_SESSION_STARTED, account=user)
        enqueue_for_account(MailJob.Kind.PERSISTENT_SESSION_STARTED, user)


@receiver(user_logged_in)
def start_session(sender, request, user, **kwargs):
    """A session starts at login, after Django renewed its ID."""
    now = time.time()
    request.session[STARTED_KEY] = now
    request.session[LAST_ACTIVITY_KEY] = now
    wish = getattr(request, STAY_WISH_ATTRIBUTE, False) is True
    if wish and (not requires_second_factor(user) or has_second_factor(user)):
        # A timedelta makes Django store an absolute end time (an integer would mean seconds of
        # inactivity and every save would move the end); ``timeouts_for`` uses the same value.
        request.session.set_expiry(settings.STAY_SIGNED_IN_DURATION)
        request.session[STAYING_KEY] = True
        if requires_second_factor(user):
            announce_persistent_session(user)
    else:
        # Also when Django kept an earlier login's data: a persistent expiry must not carry over.
        request.session.set_expiry(None)
        request.session.pop(STAYING_KEY, None)


def timeouts_for(user, session=None):
    """Return (idle, absolute) timeouts in seconds; the absolute one is None for an ordinary session.

    A "stay signed in" session has the configured duration for both (it keeps its own limit, also for
    staff users and administrators). Any other session has only the idle timeout, shorter for staff
    users and administrators; activity extends it (D-088).
    """
    if session is not None and session.get(STAYING_KEY) is True:
        duration = settings.STAY_SIGNED_IN_DURATION.total_seconds()
        return duration, duration
    if requires_second_factor(user):
        return settings.SESSION_PRIVILEGED_IDLE_TIMEOUT.total_seconds(), None
    return settings.SESSION_IDLE_TIMEOUT.total_seconds(), None


class SessionTimeoutMiddleware:
    """Ends a logged-in session that is idle for too long (or a staying session that is too old).

    Must come after ``AuthenticationMiddleware`` and ``MessageMiddleware``. A logged-in session
    without the stored times (not created by a login) is ended as well: it fails closed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # A session-free view (the public logo) neither ends nor extends the session, and does not read it.
        if not is_session_free(request) and request.user.is_authenticated:
            expired = self._check(request, passive=is_passive(request))
            if expired:
                logout(request)
                if is_passive(request):
                    # A polling script: no redirect to the login page, no message left for the next page.
                    return HttpResponse(status=401, headers={"Cache-Control": "no-store"})
                messages.info(request, gettext("You were logged out for security reasons. Please log in again."))
                # Never put the token of a link into the login URL.
                if request.method in ("GET", "HEAD") and not TOKEN_URL_PATTERN.search(request.path):
                    return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
                return redirect(settings.LOGIN_URL)
        return self.get_response(request)

    @staticmethod
    def _check(request, passive=False):
        session = request.session
        started = session.get(STARTED_KEY)
        last = session.get(LAST_ACTIVITY_KEY)
        if not isinstance(started, (int, float)) or not isinstance(last, (int, float)):
            return True
        idle, absolute = timeouts_for(request.user, session)
        now = time.time()
        if now - last > idle or (absolute is not None and now - started > absolute) or now < started:
            return True
        if not passive and now - last >= ACTIVITY_WRITE_INTERVAL:
            session[LAST_ACTIVITY_KEY] = now
        return False
