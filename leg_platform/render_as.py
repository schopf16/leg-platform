"""The read-only support view "View as <person>" (D-131).

A staff member (active ``is_staff``, second factor completed) sees the customer pages as one customer sees them. The
state is only in the staff member's own session: ``{"subject": <account id>, "started_at": <unix time>}``. ``request.user``
is never replaced, nobody logs in as the customer, no cookie or token of the customer exists. ``request.subject`` is the
person viewed (None outside the mode); ``subject(request)`` is the one accessor the customer view layer uses: the person
viewed in the mode, else ``request.user``. Only read paths use it; write paths keep ``request.user`` and are unreachable
in the mode.

Deny by default, in this middleware: only GET/HEAD without a query string on ``ALLOWED_VIEWS`` are answered; POST only
for "end view" and "log out"; everything else is a 403 page. Nothing is written for the person (no ``mark_seen``, no
mail, no history, no counter); every response is no-store. Events (ids only, no names, no values): started, ended (with
the reason), page views and refused requests (method, route name and integer route ids; no query string, no body; same
page and person within 300 s: no new event).
"""

import logging
import time

from django.contrib.auth.signals import user_logged_out
from django.dispatch import receiver
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers, patch_cache_control
from django.views import View

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent, User
from leg_platform.accounts.second_factor import SESSION_FREE_VIEWS, is_second_factor_complete

logger = logging.getLogger(__name__)

SESSION_KEY = "render_as"
SEEN_KEY = "render_as_seen"
# The same page of the same person within this many seconds is one event (like ``log_viewed``).
VIEW_EVENT_INTERVAL = 300
END_REASONS = ("explicit", "timeout", "logout", "subject_changed", "staff_changed")
# The pages the support view renders (GET/HEAD, no query string). Everything else is refused.
ALLOWED_VIEWS = frozenset({"home", "customer:buildings", "customer:contract", "accounts:profile"})
# POST allowed besides: the end of the view and the logout of the staff member.
POST_VIEWS = frozenset({"accounts:render_as_end", "accounts:logout"})
# Public presentation files and the language switch (no account data, no side effect on an account): GET/HEAD, and
# POST only for the language switch. Other methods are refused like any other request.
PASSTHROUGH_VIEWS = SESSION_FREE_VIEWS | {"instance_stylesheet"}
PASSTHROUGH_POST_VIEWS = frozenset({"set_language"})
SAFE_METHODS = ("GET", "HEAD")


def subject(request):
    """The person whose pages are rendered: the person viewed in the support view, else ``request.user``."""
    return getattr(request, "subject", None) or getattr(request, "user", None)


def is_active(request):
    """Whether this request is in the support view (decided by the middleware)."""
    return getattr(request, "subject", None) is not None


def allowed_paths():
    """The paths of the allowed pages, space separated (for ``static/js/render-as.js``, which only disables what the
    server would refuse)."""
    return " ".join(reverse(name) for name in sorted(ALLOWED_VIEWS))


def start(request, person):
    """Start the view of ``person`` (checks are the caller's: the management view)."""
    request.session[SESSION_KEY] = {"subject": person.pk, "started_at": int(time.time())}
    request.session.pop(SEEN_KEY, None)
    security_log.record(SecurityEvent.Event.RENDER_AS_STARTED, account=person.pk, actor=request.user.pk)


def end(request, reason):
    """End the view and log it (ids and the fixed reason only). Returns the id of the person that was viewed or None."""
    state = request.session.pop(SESSION_KEY, None)
    request.session.pop(SEEN_KEY, None)
    request.subject = None
    if not isinstance(state, dict):
        return None
    subject_id = state.get("subject")
    security_log.record(
        SecurityEvent.Event.RENDER_AS_ENDED,
        account=subject_id if isinstance(subject_id, int) else None,
        actor=getattr(request.user, "pk", None),
        detail=f"reason {reason}",
    )
    return subject_id


@receiver(user_logged_out)
def end_at_logout(sender, request, user, **kwargs):
    """A logout, or the end of a session by a timeout, ends the view (the signal comes before the session is flushed)."""
    if request is None or user is None:
        return
    session = getattr(request, "session", None)
    if session is not None and isinstance(session.get(SESSION_KEY), dict):
        end(request, getattr(request, "render_as_end_reason", "logout"))


def _valid_subject(request, state):
    """The person of the state if the view may go on, else (None, reason)."""
    from leg_platform.customer.access import is_customer
    from leg_platform.management.access import is_management_user

    user = request.user
    if not is_management_user(user) or not is_second_factor_complete(user):
        return None, "staff_changed"
    subject_id = state.get("subject")
    if not isinstance(subject_id, int) or isinstance(subject_id, bool) or subject_id == user.pk:
        return None, "subject_changed"
    person = User.objects.filter(pk=subject_id).first()
    if person is None or not is_customer(person):
        return None, "subject_changed"
    return person, ""


def _seen_state(request):
    now = int(time.time())
    seen = request.session.get(SEEN_KEY)
    seen = (
        {key: value for key, value in seen.items() if isinstance(value, int) and 0 <= now - value < VIEW_EVENT_INTERVAL}
        if isinstance(seen, dict) else {}
    )
    return seen, now


def _first_in_interval(request, key):
    """True (and remembered) if ``key`` was not seen in this session within ``VIEW_EVENT_INTERVAL`` seconds."""
    seen, now = _seen_state(request)
    if key in seen:
        return False
    seen[key] = now
    request.session[SEEN_KEY] = seen
    return True


def _record_page_view(request, name, person):
    if _first_in_interval(request, f"{name}:{person.pk}"):
        security_log.record(
            SecurityEvent.Event.RENDER_AS_PAGE_VIEWED, account=person.pk, actor=request.user.pk, detail=f"page {name}"
        )


def _record_refused(request, name, person):
    """Log a refused request: method, route name and the integer ids of the route only (never the query string, the
    body or other route values). One event per method and route in the interval, so a loop cannot flood the log."""
    if not _first_in_interval(request, f"refused:{request.method}:{name}"):
        return
    ids = sorted(value for value in request.resolver_match.kwargs.values() if isinstance(value, int))
    detail = f"refused {request.method} {name}" + (f" ids {','.join(map(str, ids))}" if ids else "")
    security_log.record(
        SecurityEvent.Event.RENDER_AS_REQUEST_REFUSED, account=person.pk, actor=request.user.pk, detail=detail
    )


def refused(request):
    return render(request, "render_as_refused.html", status=403)


def passthrough_allowed(name, method):
    """Whether the public file or the language switch ``name`` is answered in the mode for this HTTP method."""
    return name in PASSTHROUGH_VIEWS and (method in SAFE_METHODS or (method == "POST" and name in PASSTHROUGH_POST_VIEWS))


class RenderAsMiddleware:
    """Decides, for every request of a session in the support view, whether it may be answered (see the module).

    Must come after ``SecondFactorMiddleware``. Without the state in the session it does nothing at all.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if is_active(request):
            add_never_cache_headers(response)
            patch_cache_control(response, no_store=True, private=True)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        name = request.resolver_match.view_name
        if name in SESSION_FREE_VIEWS and passthrough_allowed(name, request.method):
            return None  # before the session is read
        if not request.user.is_authenticated:
            return None
        state = request.session.get(SESSION_KEY)
        if state is None:
            return None
        person, reason = _valid_subject(request, state) if isinstance(state, dict) else (None, "subject_changed")
        if person is None:
            end(request, reason)
            return None
        request.subject = person
        if passthrough_allowed(name, request.method):
            return None
        if request.method == "POST" and name in POST_VIEWS:
            return None
        if request.method in ("GET", "HEAD") and name in ALLOWED_VIEWS and not request.GET:
            if request.method == "GET":
                _record_page_view(request, name, person)
            return None
        _record_refused(request, name, person)
        return refused(request)


class EndView(View):
    """"End view" in the bar: POST with CSRF; back to the person's page in the management area."""

    http_method_names = ["post"]

    def post(self, request):
        from leg_platform.management.access import is_management_user

        if not request.user.is_authenticated or not is_management_user(request.user):
            raise Http404
        subject_id = end(request, "explicit")
        if isinstance(subject_id, int):
            return redirect("management:persons_detail", pk=subject_id)
        return redirect("management:persons")
