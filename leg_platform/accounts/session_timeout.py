"""Idle and absolute session timeouts (docs/SECURITY.md, section 3; D-041).

The timeouts are enforced on the server: the session stores when it started and when it was
last used, and a session that is idle for too long or older than the absolute timeout is ended
at its next request. Staff users and system administrators have shorter timeouts than users.
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

from leg_platform.accounts.second_factor import requires_second_factor
from leg_platform.log_filters import TOKEN_URL_PATTERN

STARTED_KEY = "session_started"
LAST_ACTIVITY_KEY = "session_last_activity"
# "Stay signed in" (D-056). The login view writes the wish of the person into the session before
# login(); the signal below turns it into a persistent session, but only for an account that is
# not a staff user or system administrator. ``STAYING_KEY`` marks such a session.
STAY_REQUESTED_KEY = "stay_signed_in_requested"
STAYING_KEY = "stay_signed_in"
# The last activity is written at most once per interval, so that a request does not always
# write the session; the idle timeout is therefore exact to one interval.
ACTIVITY_WRITE_INTERVAL = 60


@receiver(user_logged_in)
def start_session(sender, request, user, **kwargs):
    """A session starts at login, after Django renewed its ID."""
    now = time.time()
    request.session[STARTED_KEY] = now
    request.session[LAST_ACTIVITY_KEY] = now
    requested = request.session.pop(STAY_REQUESTED_KEY, False) is True
    if requested and not requires_second_factor(user):
        # A persistent cookie and a server-side session of the same length. The checks of
        # ``timeouts_for`` use the same value for the idle and the absolute timeout.
        request.session.set_expiry(settings.STAY_SIGNED_IN_DURATION.total_seconds())
        request.session[STAYING_KEY] = True
    else:
        request.session.pop(STAYING_KEY, None)


def timeouts_for(user, session=None):
    """Return (idle, absolute) timeouts in seconds; shorter for staff users and administrators.

    A session that was started with "stay signed in" has the configured duration for both, unless
    the account is a staff user or administrator (their shorter timeouts always apply).
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
        if request.user.is_authenticated:
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
