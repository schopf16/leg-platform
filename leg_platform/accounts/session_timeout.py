"""Session timeouts: idle and absolute, enforced on the server (D-041).

The session stores when it started and was last used; an idle or too old session is ended at its
next request. Staff users and system administrators have shorter timeouts.
"""

import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.signals import user_logged_in
from django.contrib.auth.views import redirect_to_login
from django.dispatch import receiver
from django.shortcuts import redirect
from django.utils.translation import gettext

from leg_platform.accounts.second_factor import (
    STAY_WISH_ATTRIBUTE,
    is_session_free,
    requires_second_factor,
)
from leg_platform.log_filters import TOKEN_URL_PATTERN

STARTED_KEY = "session_started"
LAST_ACTIVITY_KEY = "session_last_activity"
# "Stay signed in" (D-056): the login view puts the wish on the request, never into the session;
# the signal below makes a persistent session of it, but not for staff users or administrators. With a
# second factor the wish waits in the pending login. ``STAYING_KEY`` marks a persistent session.
STAYING_KEY = "stay_signed_in"
# The last activity is written at most once per interval; the idle timeout is exact to one interval.
ACTIVITY_WRITE_INTERVAL = 60


@receiver(user_logged_in)
def start_session(sender, request, user, **kwargs):
    """A session starts at login, after Django renewed its ID."""
    now = time.time()
    request.session[STARTED_KEY] = now
    request.session[LAST_ACTIVITY_KEY] = now
    if getattr(request, STAY_WISH_ATTRIBUTE, False) is True and not requires_second_factor(user):
        # A timedelta makes Django store an absolute end time (an integer would mean seconds of
        # inactivity and every save would move the end); ``timeouts_for`` uses the same value.
        request.session.set_expiry(settings.STAY_SIGNED_IN_DURATION)
        request.session[STAYING_KEY] = True
    else:
        # Also when Django kept an earlier login's data: a persistent expiry must not carry over.
        request.session.set_expiry(None)
        request.session.pop(STAYING_KEY, None)


def timeouts_for(user, session=None):
    """Return (idle, absolute) timeouts in seconds; shorter for staff users and administrators.

    A "stay signed in" session has the configured duration for both, unless the account is privileged.
    """
    if requires_second_factor(user):
        return (
            settings.SESSION_PRIVILEGED_IDLE_TIMEOUT.total_seconds(),
            settings.SESSION_PRIVILEGED_ABSOLUTE_TIMEOUT.total_seconds(),
        )
    if session is not None and session.get(STAYING_KEY) is True:
        duration = settings.STAY_SIGNED_IN_DURATION.total_seconds()
        return duration, duration
    return (
        settings.SESSION_IDLE_TIMEOUT.total_seconds(),
        settings.SESSION_ABSOLUTE_TIMEOUT.total_seconds(),
    )


class SessionTimeoutMiddleware:
    """Ends a logged-in session that is idle for too long or too old.

    Must come after ``AuthenticationMiddleware`` and ``MessageMiddleware``. A logged-in session
    without the stored times (not created by a login) is ended as well: it fails closed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # A session-free view (the public logo) neither ends nor extends the session, and does not read it.
        if not is_session_free(request) and request.user.is_authenticated:
            expired = self._check(request)
            if expired:
                logout(request)
                messages.info(request, gettext("You were logged out for security reasons. Please log in again."))
                # Never put the token of a link into the login URL.
                if request.method in ("GET", "HEAD") and not TOKEN_URL_PATTERN.search(request.path):
                    return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
                return redirect(settings.LOGIN_URL)
        return self.get_response(request)

    @staticmethod
    def _check(request):
        session = request.session
        started = session.get(STARTED_KEY)
        last = session.get(LAST_ACTIVITY_KEY)
        if not isinstance(started, (int, float)) or not isinstance(last, (int, float)):
            return True
        idle, absolute = timeouts_for(request.user, session)
        now = time.time()
        if now - last > idle or now - started > absolute or now < started:
            return True
        if now - last >= ACTIVITY_WRITE_INTERVAL:
            session[LAST_ACTIVITY_KEY] = now
        return False
