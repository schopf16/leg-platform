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
# The last activity is written at most once per interval, so that a request does not always
# write the session; the idle timeout is therefore exact to one interval.
ACTIVITY_WRITE_INTERVAL = 60


@receiver(user_logged_in)
def start_session(sender, request, user, **kwargs):
    """A session starts at login, after Django renewed its ID."""
    now = time.time()
    request.session[STARTED_KEY] = now
    request.session[LAST_ACTIVITY_KEY] = now


def timeouts_for(user):
    """Return (idle, absolute) timeouts in seconds; shorter for staff users and administrators."""
    if requires_second_factor(user):
        return (
            settings.SESSION_PRIVILEGED_IDLE_TIMEOUT.total_seconds(),
            settings.SESSION_PRIVILEGED_ABSOLUTE_TIMEOUT.total_seconds(),
        )
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
        idle, absolute = timeouts_for(request.user)
        now = time.time()
        if now - last > idle or now - started > absolute or now < started:
            return True
        if now - last >= ACTIVITY_WRITE_INTERVAL:
            session[LAST_ACTIVITY_KEY] = now
        return False
