"""The address of a pending email request and the time of the next resend (D-058).

The next page names the address the email goes to and offers "send again" after a pause. Address
and time live in this browser's session only. The 60-second pause is enforced on the server (the
page countdown only shows it); the per-address, network and instance limits (D-038) apply to every resend.
"""

import time
from typing import NamedTuple

RESEND_COOLDOWN_SECONDS = 60
REGISTRATION = "registration"
PASSWORD_RESET = "password_reset"
_PURPOSES = (REGISTRATION, PASSWORD_RESET)
_KEY = "mail_request_%s"


class PendingRequest(NamedTuple):
    address: str
    wait: int  # seconds until a resend is possible, 0 if it is possible now


def remember(request, purpose, address):
    """Remember the address of the request that was just made and start the pause."""
    if purpose not in _PURPOSES:
        raise ValueError(purpose)
    request.session[_KEY % purpose] = {
        "address": address,
        "available_at": time.time() + RESEND_COOLDOWN_SECONDS,
    }


def forget(request, purpose):
    """Remove the pending request of this browser from the session (after completion, and when a link was refused)."""
    if purpose not in _PURPOSES:
        raise ValueError(purpose)
    request.session.pop(_KEY % purpose, None)


def pending(request, purpose):
    """Return the pending request of this browser, or None. Values of the session are checked."""
    if purpose not in _PURPOSES:
        raise ValueError(purpose)
    stored = request.session.get(_KEY % purpose)
    if not isinstance(stored, dict):
        return None
    address = stored.get("address")
    available_at = stored.get("available_at")
    if (
        not isinstance(address, str)
        or not address
        or isinstance(available_at, bool)
        or not isinstance(available_at, (int, float))
    ):
        return None
    wait = max(0, int(available_at - time.time() + 0.999))
    # A time far in the future (a clock jump) never blocks for longer than the pause.
    return PendingRequest(address, min(wait, RESEND_COOLDOWN_SECONDS))
