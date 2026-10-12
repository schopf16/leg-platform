"""Axes integration for independent account and client-IP lockouts (D-017)."""

from axes.handlers.database import AxesDatabaseHandler

from leg_platform.accounts.login_protection import (
    reserve_client_ip_attempt,
    settle_client_ip_attempt,
)


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
            return super().user_login_failed(sender, credentials, request, **kwargs)
        finally:
            if request is not None:
                request.axes_ip_address = client_ip
                settle_client_ip_attempt(request, succeeded=False)

    def user_logged_in(self, sender, request, user, **kwargs):
        try:
            return super().user_logged_in(sender, request, user, **kwargs)
        finally:
            settle_client_ip_attempt(request, succeeded=True)