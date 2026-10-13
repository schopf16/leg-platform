"""The second factor: the central check, the login step and re-authentication (D-040).

Whether the second factor of a session is complete is decided in one place, independent of the
method (docs/PROJECT.md, "AGOV and Passkeys"). Protected functions, the mandatory second factor
of staff users and system administrators and re-authentication rely only on the functions of this
module. The parts that are specific to TOTP and recovery codes are in ``totp.py``.

The login of an account with a second factor is two steps (D-017): the password is checked first
and remembered in the session as a pending login; Django's ``login()`` is called only when the
second factor has been verified as well.
"""

import time
from datetime import timedelta
from typing import NamedTuple

from axes.backends import AxesStandaloneBackend
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.signals import user_login_failed
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect
from django.utils.translation import gettext

from leg_platform.accounts import totp
from leg_platform.accounts.login_protection import settle_client_ip_attempt
from leg_platform.accounts.models import User

# The password step and the code step must follow each other closely; a longer pause starts
# again at the login page. A proposal awaiting the project owner's confirmation (D-040).
PENDING_LIFETIME = timedelta(minutes=5)
PENDING_SESSION_KEY = "second_factor_pending"
LOGIN_BACKEND = "leg_platform.accounts.backends.EmailModelBackend"

# Views that an account without a completed second factor may use although it must have one:
# logging out and setting the second factor up.
UNRESTRICTED_VIEWS = frozenset(
    {
        "accounts:logout",
        "accounts:two_factor_setup",
        "accounts:two_factor_confirm",
        "accounts:two_factor_qr",
    }
)


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


class SecondFactorMiddleware:
    """Gives a session of an account that must use a second factor no privileges without it.

    Must come after ``AuthenticationMiddleware``, ``OTPMiddleware`` and ``MessageMiddleware``.
    A session without a completed second factor can only log out and set the factor up. If the
    account has a factor, the session is ended and the person logs in again, which asks for it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = request.user
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


def pending_login(request):
    """Return the account whose password was checked and whose code is awaited, or None."""
    pending = request.session.get(PENDING_SESSION_KEY)
    if not pending:
        return None
    if time.time() - pending["started"] > PENDING_LIFETIME.total_seconds():
        request.session.pop(PENDING_SESSION_KEY, None)
        return None
    user = User.objects.filter(pk=pending["user"], status=User.Status.ACTIVE).first()
    if user is None or not has_second_factor(user):
        request.session.pop(PENDING_SESSION_KEY, None)
        return None
    return PendingLogin(user, pending["next"])


def begin_pending_login(request, user, next_url):
    """Remember that the password of the account was checked; the code is asked next.

    The session ID is renewed, so that a session ID that someone else planted in the browser
    before the password was entered cannot be used to complete the second step.
    """
    request.session.cycle_key()
    request.session[PENDING_SESSION_KEY] = {
        "user": user.pk,
        "started": time.time(),
        "next": next_url,
    }
    # The password was right: the IP slot of this attempt is released. The code step reserves
    # its own slot and keeps it if the code is wrong (D-017).
    settle_client_ip_attempt(request, succeeded=True)


def verify_second_factor(request, user, code):
    """Check the code under the abuse protection; return the device that accepted it, or None.

    Every code attempt counts like a login attempt: it is refused while the account or the IP
    address is locked out, and a wrong code is a failed attempt of the account and the IP
    address (D-017). The Axes backend is used for the lockout check only, as
    ``django.contrib.auth.authenticate()`` does with every backend.
    """
    credentials = {"username": user.email}
    # One attempt at a time per account: the lockout check, the verification and the record of
    # a failure form one step, so that parallel requests cannot exceed the limit (D-040).
    with transaction.atomic():
        User._base_manager.select_for_update().filter(pk=user.pk).first()
        try:
            AxesStandaloneBackend().authenticate(request, username=user.email)
        except PermissionDenied:
            user_login_failed.send(sender=__name__, credentials=credentials, request=request)
            return None
        device = totp.verify_code(user, code)
        if device is None:
            user_login_failed.send(sender=__name__, credentials=credentials, request=request)
            return None
    settle_client_ip_attempt(request, succeeded=True)
    return device


def verify_password(request, user, password):
    """Check the password of the logged-in account under the abuse protection (D-017).

    The account row is locked while the lockout is checked, the password is verified and a
    failure is recorded, so that parallel requests cannot exceed the limit (D-040).
    """
    with transaction.atomic():
        User._base_manager.select_for_update().filter(pk=user.pk).first()
        checked = authenticate(request, username=user.email, password=password)
    succeeded = checked is not None and checked.pk == user.pk
    # A failure was already settled by the failed-login signal.
    settle_client_ip_attempt(request, succeeded=succeeded)
    return succeeded


def complete_login(request, user, device):
    """Log the account in after the password and the second factor were verified."""
    request.session.pop(PENDING_SESSION_KEY, None)
    # django-otp stores the device in the new session after Django's login() renewed its ID.
    user.otp_device = device
    login(request, user, backend=LOGIN_BACKEND)
