"""Axes integration for independent account and client-IP lockouts (D-017)."""

from axes.handlers.database import AxesDatabaseHandler
from django.conf import settings
from django.shortcuts import render
from django.utils.cache import add_never_cache_headers

from leg_platform.accounts import security_log
from leg_platform.accounts.login_protection import (
    LOGIN_FAILURE_LIMIT,
    reserve_client_ip_attempt,
    settle_client_ip_attempt,
)
from leg_platform.accounts.models import SecurityEvent, User
from leg_platform.accounts.normalization import normalize_email_address

LOCKOUT_TEMPLATE = "accounts/lockout.html"


def lockout_response(request, response=None, credentials=None):
    """The answer of every lockout (``AXES_LOCKOUT_CALLABLE``): the lockout page, never stored.

    Axes' own response sends no ``Cache-Control`` header, so a proxy could keep a "blocked" page (D-067).
    The page never shows the submitted address; the portal has no script that posts a login, so no JSON answer.
    """
    response = render(request, LOCKOUT_TEMPLATE, status=settings.AXES_HTTP_RESPONSE_CODE)
    add_never_cache_headers(response)
    return response


class LoginProtectionHandler(AxesDatabaseHandler):
    """Use Axes for per-email lockouts and isolate the IP lockout state."""

    def is_allowed(self, request, credentials=None):
        if not reserve_client_ip_attempt(request):
            request.axes_ip_locked_out = True
            return False
        return super().is_allowed(request, credentials)

    def user_login_failed(self, sender, credentials, request=None, **kwargs):
        client_ip = request.axes_ip_address if request is not None else None
        if request is not None:
            request.axes_ip_address = None
        try:
            result = super().user_login_failed(sender, credentials, request, **kwargs)
            if request is not None:
                self._record_lockout(credentials, request)
            return result
        finally:
            if request is not None:
                request.axes_ip_address = client_ip
                settle_client_ip_attempt(request, succeeded=False)

    @staticmethod
    def _record_lockout(credentials, request):
        # Only the failure that reaches the limit, once per lockout (D-018).
        if (
            not getattr(request, "axes_locked_out", False)
            or getattr(request, "axes_failures_since_start", None) != LOGIN_FAILURE_LIMIT
        ):
            return
        address = normalize_email_address((credentials or {}).get("username"))
        account = (
            User._base_manager.filter(normalized_email=address).values_list("pk", flat=True).first()
            if address
            else None
        )
        security_log.record(SecurityEvent.Event.LOGIN_LOCKOUT, account=account)

    def user_logged_in(self, sender, request, user, **kwargs):
        try:
            return super().user_logged_in(sender, request, user, **kwargs)
        finally:
            settle_client_ip_attempt(request, succeeded=True)